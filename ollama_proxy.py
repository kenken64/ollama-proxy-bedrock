#!/usr/bin/env python3
"""Ollama-compatible proxy in front of the AWS API Gateway Bedrock endpoint.

Tools that speak the Ollama API (openclaw, hermes, nanoclaw, ...) point at this
proxy as if it were a local Ollama server. The proxy translates requests into
the gateway's Bedrock Converse-style schema and forwards them with the API key.

    client ──Ollama API──▶  http://localhost:11434  ──Bedrock──▶  API Gateway

Endpoints implemented:
    GET  /                  health check ("Ollama is running")
    GET  /api/version       version info
    GET  /api/tags          model list (names as clients should reference them)
    POST /api/show          model info (minimal)
    POST /api/chat          chat completion (stream + non-stream)
    POST /api/generate      text completion (stream + non-stream)

Gateway contract (discovered by live probing):
    POST <GATEWAY_URL>
    Headers: X-API-Key, Content-Type: application/json
    Body: {
        "modelId": "<bedrock model id or inference profile>",
        "messages": [{"role": "...", "content": [{"text": "..."}]}],
        "inferenceConfig": {"maxTokens": N}   # also sets the quota reservation
    }

Config via environment variables:
    GATEWAY_URL      default: https://gnfyayl5ib.execute-api.ap-southeast-1.amazonaws.com/UAT/
    GATEWAY_API_KEY  default: built-in UAT key
    PROXY_HOST       default: 127.0.0.1
    PROXY_PORT       default: 11434
    DEFAULT_MODEL    default: sonnet  (short name from MODELS below)
    MAX_TOKENS       default maxTokens / quota reservation (256)
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GATEWAY_URL = os.environ.get(
    "GATEWAY_URL",
    "https://gnfyayl5ib.execute-api.ap-southeast-1.amazonaws.com/UAT/",
)
GATEWAY_API_KEY = os.environ.get(
    "GATEWAY_API_KEY", "ydmoI59i8c4NyRnY8IHDe1JEdjoAscD28RzUmJYr"
)
PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "11434"))
DEFAULT_MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "256"))

# Auto-continue: when the client's requested num_predict exceeds the gateway's
# per-call maxTokens clamp (1024), chain multiple calls ("continue") until the
# target is reached or the model stops on its own.
GATEWAY_MAX_TOKENS = int(os.environ.get("GATEWAY_MAX_TOKENS", "1024"))
MAX_CONTINUE_ROUNDS = int(os.environ.get("MAX_CONTINUE_ROUNDS", "16"))

# Short names (what Ollama clients ask for) -> gateway/Bedrock model IDs.
# NOTE: only inference-profile IDs that the gateway account can actually
# invoke belong here; verified working ones are marked.
MODELS = {
    "sonnet4.5": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",  # verified
    "sonnet": "apac.anthropic.claude-3-sonnet-20240229-v1:0",         # verified
    "haiku": "anthropic.claude-3-haiku-20240307-v1:0",                # verified (flaky acct)
}
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "sonnet4.5")

OLLAMA_VERSION = "0.5.0-proxy"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


KEY_PREFIX = "key-"  # model-name prefix for embedding the gateway API key


def split_model_and_key(name: str):
    """Allow embedding the gateway API key in the model name for clients that
    cannot set HTTP headers:  "key-<API_KEY>-<model>" -> ("<model>", "<API_KEY>").
    Returns (model_name, api_key_or_None)."""
    if name and name.startswith(KEY_PREFIX):
        rest = name[len(KEY_PREFIX):]
        for short in MODELS:
            sep = "-" + short
            if rest.endswith(sep):
                candidate = rest[: -len(sep)]
                if candidate:
                    return short, candidate
    return name, None


def resolve_model(name: str) -> str:
    """Map an Ollama-style model name to a gateway modelId."""
    if not name:
        return MODELS[DEFAULT_MODEL]
    name = name.strip()
    if ":" in name and name.split(":", 1)[0] in MODELS:
        name = name.split(":", 1)[0]  # strip ollama-style tag, e.g. "sonnet:latest"
    return MODELS.get(name, name)  # pass through unknown names verbatim


def ollama_tools_to_bedrock(tools):
    """Convert Ollama/OpenAI-style tools -> Bedrock Converse toolConfig.

    Ollama:  {"type": "function", "function": {"name", "description", "parameters"}}
    Bedrock: {"toolSpec": {"name", "description", "inputSchema": {"json": ...}}}
    Non-function entries are dropped."""
    specs = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") == "function" else t
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        spec = {"name": name}
        if fn.get("description"):
            spec["description"] = fn["description"]
        spec["inputSchema"] = {"json": fn.get("parameters") or {"type": "object"}}
        specs.append({"toolSpec": spec})
    return {"tools": specs} if specs else None


def ollama_messages_to_bedrock(messages):
    """Convert Ollama chat messages -> gateway message schema.

    Handles tool calling:
      - assistant messages with tool_calls -> assistant content with toolUse blocks
      - role:"tool" messages -> user content with toolResult blocks

    Bedrock requires toolResult.toolUseId to match the toolUse.toolUseId from
    the preceding assistant message. Ollama tool messages carry no ID, so IDs
    are synthesized deterministically as "<name>_<k>" (k-th call of that tool
    in the conversation) and consumed FIFO — the assistant toolUse and the
    client tool result therefore pair up consistently within this request.
    """
    out = []
    system_parts = []
    pending_tool_ids = []  # synthesized toolUseIds awaiting results, FIFO
    name_counters = {}     # per-tool-name call index
    tool_result_buffer = []

    def flush_buffer():
        nonlocal tool_result_buffer
        if tool_result_buffer:
            content = []
            for result_text in tool_result_buffer:
                tool_use_id = (
                    pending_tool_ids.pop(0) if pending_tool_ids else "call_unknown"
                )
                content.append({
                    "toolResult": {
                        "toolUseId": tool_use_id,
                        "content": [{"text": str(result_text)}],
                    }
                })
            out.append({"role": "user", "content": content})
            tool_result_buffer = []

    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            flush_buffer()
            system_parts.append(content)
            continue
        if role == "tool":
            # Ollama tool result message; buffer so consecutive tool results
            # merge into ONE user message (Bedrock requirement)
            tool_result_buffer.append(content)
            continue

        flush_buffer()

        if role == "assistant":
            blocks = []
            if content:
                blocks.append({"text": content})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                name_counters[name] = name_counters.get(name, 0) + 1
                tool_use_id = f"{name}_{name_counters[name]}"
                pending_tool_ids.append(tool_use_id)
                blocks.append({
                    "toolUse": {
                        "toolUseId": tool_use_id,
                        "name": name,
                        "input": fn.get("arguments") or {},
                    }
                })
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            continue

        out.append({"role": "user", "content": [{"text": content}]})

    flush_buffer()

    if not out:
        out.append({"role": "user", "content": [{"text": ""}]})
    return out, system_parts


def call_gateway(model_id: str, messages, max_tokens: int, system_parts=None,
                 api_key: str | None = None, tool_config=None) -> dict:
    """Forward a chat request to the AWS API Gateway. Returns dict with
    keys: ok, status, gateway_body, error (str|None), text, tool_calls,
    stop_reason, usage, latency_ms.

    api_key: per-request gateway key supplied by the client; falls back to
    GATEWAY_API_KEY when not provided.
    tool_config: Bedrock Converse toolConfig dict (from ollama_tools_to_bedrock)."""
    payload = {
        "modelId": model_id,
        "messages": messages,
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if system_parts:
        payload["system"] = [{"text": "\n".join(system_parts)}]
    if tool_config:
        payload["toolConfig"] = tool_config

    req = urllib.request.Request(
        GATEWAY_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key or GATEWAY_API_KEY,
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read())
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"error": raw}
        status = e.code
    except Exception as e:  # network errors etc.
        return {
            "ok": False,
            "status": None,
            "error": f"gateway request failed: {e}",
            "gateway_body": None,
        }
    latency_ms = int((time.monotonic() - started) * 1000)

    if status != 200:
        err = body.get("error") or body.get("message") or f"HTTP {status}"
        detail = body.get("bedrockMessage")
        if detail:
            err = f"{err}: {detail}"
        return {
            "ok": False,
            "status": status,
            "error": err,
            "gateway_body": body,
            "latency_ms": latency_ms,
        }

    # Parse ALL content blocks: text + toolUse (order preserved)
    text_parts = []
    tool_calls = []
    output = body.get("output") if isinstance(body, dict) else None
    message = output.get("message") if isinstance(output, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block and isinstance(block["toolUse"], dict):
                tu = block["toolUse"]
                tool_calls.append({
                    "function": {
                        "name": tu.get("name", ""),
                        "arguments": tu.get("input") or {},
                    }
                })
    return {
        "ok": True,
        "status": status,
        "error": None,
        "gateway_body": body,
        "text": "".join(text_parts),
        "tool_calls": tool_calls,
        "stop_reason": body.get("stopReason") if isinstance(body, dict) else None,
        "usage": body.get("usage", {}),
        "latency_ms": latency_ms,
    }


CONTINUE_PROMPT = (
    "Continue exactly from where you stopped. Do not repeat, summarize, "
    "or conclude yet — keep writing the next section in the same style."
)


# Sliding-window size (characters) of already-generated text sent back to the
# model as continuation context. The API Gateway rejects bodies > ~6 KB with
# 403 Forbidden, so we must NOT grow the conversation with full text each round.
CONTINUE_CONTEXT_CHARS = int(os.environ.get("CONTINUE_CONTEXT_CHARS", "1200"))


def call_gateway_long(model_id: str, messages, max_tokens: int,
                      system_parts=None, api_key: str | None = None) -> dict:
    """Call the gateway, automatically chaining 'continue' rounds when the
    requested max_tokens exceeds the gateway's per-call clamp
    (GATEWAY_MAX_TOKENS) and the model stopped because it hit that ceiling.

    Sliding window: each continuation round sends only the ORIGINAL messages
    plus the last CONTINUE_CONTEXT_CHARS characters of generated text (not the
    full transcript), keeping every request small enough to pass the gateway's
    body-size limit regardless of how many rounds we chain.

    Returns the same shape as call_gateway, with:
      - text: concatenation of all rounds
      - usage: summed input/output tokens across rounds
      - rounds: number of gateway calls made
      - truncated: True if we stopped because of MAX_CONTINUE_ROUNDS
    """
    per_call = min(max_tokens, GATEWAY_MAX_TOKENS)
    original_messages = list(messages)
    all_text = []
    total_in = 0
    total_out = 0
    rounds = 0
    truncated = False

    for _ in range(MAX_CONTINUE_ROUNDS):
        if rounds == 0:
            convo = original_messages
            sys_parts = system_parts
        else:
            # Sliding window: original prompt + tail of what we have so far
            generated = "".join(all_text)
            tail = generated[-CONTINUE_CONTEXT_CHARS:]
            convo = original_messages + [
                {"role": "assistant", "content": [{"text": tail}]},
                {"role": "user", "content": [{"text": CONTINUE_PROMPT}]},
            ]
            sys_parts = None

        result = call_gateway(model_id, convo, per_call,
                              sys_parts, api_key=api_key)
        if not result["ok"]:
            # If we already have partial output, return it instead of failing
            if all_text:
                truncated = True
                break
            return result

        rounds += 1
        chunk = result["text"] or ""
        all_text.append(chunk)
        usage = result["usage"]
        total_in += usage.get("inputTokens", 0)
        total_out += usage.get("outputTokens", 0)

        stop_reason = (result["gateway_body"] or {}).get("stopReason")

        # Reached the requested target -> done
        if total_out >= max_tokens:
            break

        # Model says it finished naturally. Trust it only if we have produced
        # at least ~80% of the target — otherwise treat it as an early wrap-up
        # and keep going (models often conclude a "continue" turn early).
        if stop_reason != "max_tokens" and total_out >= int(max_tokens * 0.8):
            break
    else:
        truncated = True

    return {
        "ok": True,
        "status": 200,
        "error": None,
        "gateway_body": None,
        "text": "".join(all_text),
        "usage": {"inputTokens": total_in, "outputTokens": total_out},
        "rounds": rounds,
        "truncated": truncated,
    }


def chat_response(model_name: str, text: str, usage: dict, done: bool = True,
                  tool_calls=None, done_reason: str = "stop") -> dict:
    """Build an Ollama /api/chat response object."""
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    resp = {
        "model": model_name,
        "created_at": now_iso(),
        "message": message,
        "done": done,
    }
    if done:
        resp.update(
            {
                "done_reason": done_reason,
                "total_duration": 0,
                "prompt_eval_count": usage.get("inputTokens", 0),
                "eval_count": usage.get("outputTokens", 0),
            }
        )
    return resp


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "ollama-proxy/1.0"
    protocol_version = "HTTP/1.1"

    # -- low-level response helpers ----------------------------------------

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text, status=200):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_ollama_error(self, message, status=500):
        # Ollama returns errors as JSON {"error": "..."} with a matching status
        self._send_json({"error": message}, status=status)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _client_api_key(self) -> str | None:
        """Extract a per-request gateway API key from the incoming request.
        Accepted forms (checked in order):
          1. X-API-Key: <key>                    (same header the gateway uses)
          2. Authorization: Bearer <key>         (OpenAI-style; most Ollama
                                                clients with an 'api key'
                                                setting send this)
        Returns None when absent -> proxy falls back to GATEWAY_API_KEY."""
        key = self.headers.get("X-API-Key")
        if key:
            return key.strip()
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            # Some clients require a non-empty key but send a placeholder.
            if token and token.lower() not in ("ollama", "none", "null", "unused"):
                return token
        return None

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_text("Ollama is running")
        elif self.path == "/api/version":
            self._send_json({"version": OLLAMA_VERSION})
        elif self.path == "/api/tags":
            self._handle_tags()
        else:
            self._send_ollama_error("not found", status=404)

    def do_POST(self):
        if self.path == "/api/chat":
            self._handle_chat()
        elif self.path == "/api/generate":
            self._handle_generate()
        elif self.path == "/api/show":
            self._handle_show()
        else:
            self._send_ollama_error("not found", status=404)

    def do_HEAD(self):
        # Some clients probe with HEAD /
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- endpoint implementations ------------------------------------------

    def _handle_tags(self):
        models = []
        for name in MODELS:
            models.append(
                {
                    "name": f"{name}:latest",
                    "model": f"{name}:latest",
                    "modified_at": now_iso(),
                    "size": 0,
                    "digest": "",
                    "details": {
                        "format": "bedrock",
                        "family": "claude",
                        "parameter_size": "",
                        "quantization_level": "",
                    },
                }
            )
        self._send_json({"models": models})

    def _handle_show(self):
        body = self._read_json_body()
        name = body.get("name") or body.get("model") or DEFAULT_MODEL
        short = name.split(":", 1)[0]
        self._send_json(
            {
                "modelfile": f"FROM {resolve_model(name)}",
                "parameters": "",
                "template": "",
                "details": {
                    "format": "bedrock",
                    "family": "claude",
                    "parameter_size": "",
                    "quantization_level": "",
                },
                "model_info": {
                    "general.name": short,
                    "bedrock.model_id": resolve_model(name),
                },
            }
        )

    def _handle_chat(self):
        body = self._read_json_body()
        raw_model = body.get("model") or DEFAULT_MODEL
        model_name, key_from_model = split_model_and_key(raw_model)
        model_id = resolve_model(model_name)
        api_key = self._client_api_key() or key_from_model  # header wins
        stream = body.get("stream", True)

        options = body.get("options") or {}
        max_tokens = int(
            options.get("num_predict") or body.get("max_tokens") or DEFAULT_MAX_TOKENS
        )

        messages, system_parts = ollama_messages_to_bedrock(body.get("messages"))
        tool_config = ollama_tools_to_bedrock(body.get("tools"))

        if max_tokens > GATEWAY_MAX_TOKENS and not tool_config:
            # Auto-continue: chain multiple gateway calls to reach the target
            # (text-only; tool calls are returned immediately instead)
            result = call_gateway_long(model_id, messages, max_tokens,
                                       system_parts, api_key=api_key)
        else:
            result = call_gateway(model_id, messages, max_tokens, system_parts,
                                  api_key=api_key, tool_config=tool_config)

        if not result["ok"]:
            status = result["status"] or 502
            # Map gateway errors to sensible HTTP statuses for the client
            if status == 429:
                out_status = 429
            elif status in (400, 401, 403, 404):
                out_status = status
            else:
                out_status = 502
            self._send_ollama_error(
                f"gateway error (model={model_id}): {result['error']}",
                status=out_status,
            )
            return

        usage = result["usage"]
        text = result["text"]
        tool_calls = result.get("tool_calls") or None
        done_reason = "stop"
        if tool_calls or result.get("stop_reason") == "tool_use":
            done_reason = "tool_calls"

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def send_chunk(obj):
                data = (json.dumps(obj) + "\n").encode("utf-8")
                self.wfile.write(b"%x\r\n" % len(data))
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            # Ollama convention: text in content chunk(s); tool_calls arrive in
            # the final done chunk.
            if text:
                send_chunk(chat_response(model_name, text, {}, done=False))
            send_chunk(chat_response(model_name, "", usage, done=True,
                                     tool_calls=tool_calls,
                                     done_reason=done_reason))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            resp = chat_response(model_name, text, usage, done=True,
                                 tool_calls=tool_calls, done_reason=done_reason)
            self._send_json(resp)

    def _handle_generate(self):
        body = self._read_json_body()
        raw_model = body.get("model") or DEFAULT_MODEL
        model_name, key_from_model = split_model_and_key(raw_model)
        api_key = self._client_api_key() or key_from_model  # header wins
        prompt = body.get("prompt", "")
        stream = body.get("stream", True)
        options = body.get("options") or {}
        max_tokens = int(
            options.get("num_predict") or body.get("max_tokens") or DEFAULT_MAX_TOKENS
        )

        messages = [{"role": "user", "content": [{"text": prompt}]}]
        system_parts = [body["system"]] if body.get("system") else []
        if max_tokens > GATEWAY_MAX_TOKENS:
            result = call_gateway_long(resolve_model(model_name), messages,
                                       max_tokens, system_parts, api_key=api_key)
        else:
            result = call_gateway(resolve_model(model_name), messages,
                                  max_tokens, system_parts, api_key=api_key)

        if not result["ok"]:
            status = result["status"] or 502
            self._send_ollama_error(
                f"gateway error: {result['error']}",
                status=status if status in (400, 401, 403, 404, 429) else 502,
            )
            return

        usage = result["usage"]
        text = result["text"]

        def gen_obj(chunk_text, done):
            obj = {
                "model": model_name,
                "created_at": now_iso(),
                "response": chunk_text,
                "done": done,
            }
            if done:
                obj.update(
                    {
                        "prompt_eval_count": usage.get("inputTokens", 0),
                        "eval_count": usage.get("outputTokens", 0),
                    }
                )
            return obj

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def send_chunk(obj):
                data = (json.dumps(obj) + "\n").encode("utf-8")
                self.wfile.write(b"%x\r\n" % len(data))
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            if text:
                send_chunk(gen_obj(text, False))
            send_chunk(gen_obj("", True))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            self._send_json(gen_obj(text, True))

    # -- logging ------------------------------------------------------------

    def log_message(self, format, *args):
        sys.stderr.write(
            "[%s] %s %s\n"
            % (datetime.now().strftime("%H:%M:%S"), self.address_string(), format % args)
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    server = ThreadingHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
    print(f"Ollama-compatible proxy listening on http://{PROXY_HOST}:{PROXY_PORT}")
    print(f"Forwarding to: {GATEWAY_URL}")
    print(f"Models exposed: {', '.join(MODELS)} (default: {DEFAULT_MODEL})")
    print(f"Default maxTokens/reservation: {DEFAULT_MAX_TOKENS}")
    print()
    print("Point clients at this proxy, e.g.:")
    print(f"  export OLLAMA_HOST=http://{PROXY_HOST}:{PROXY_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
