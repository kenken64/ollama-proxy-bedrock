# Ollama Proxy → AWS API Gateway (Bedrock)

An Ollama-compatible HTTP proxy that lets any Ollama-speaking client
(**openclaw**, **hermes**, **nanoclaw**, the `ollama` CLI, LangChain, etc.)
use the AWS API Gateway Bedrock endpoint — with no code changes on the client
side.

```
openclaw / hermes / nanoclaw ──Ollama API──▶ localhost:11434 ──Bedrock schema + X-API-Key──▶ AWS API Gateway ──▶ Bedrock
```

Pure Python 3 standard library — **no dependencies to install**.

---

## Quick start

```bash
# 1. Start the proxy (defaults: 127.0.0.1:11434, built-in UAT gateway + key)
python3 ollama_proxy.py

# 2. Point your tools at it
export OLLAMA_HOST=http://127.0.0.1:11434

# 3. Use it like Ollama
curl http://127.0.0.1:11434/api/tags
curl -X POST http://127.0.0.1:11434/api/chat -H "Content-Type: application/json" -d '{
  "model": "sonnet4.5",
  "messages": [{"role": "user", "content": "Hello!"}],
  "stream": false
}'
```

Run in the background:

```bash
nohup python3 ollama_proxy.py > proxy.log 2>&1 &
```

### Docker

```bash
# Build & run with compose (port 11434, restart=unless-stopped, healthcheck)
docker compose up -d --build

# Logs / status / stop
docker compose logs -f
docker compose ps
docker compose down
```

Or plain Docker:

```bash
docker build -t ollama-proxy-bedrock .
docker run -d --name ollama-proxy -p 11434:11434 ollama-proxy-bedrock
```

Override config at runtime (no rebuild):

```bash
docker compose up -d -e GATEWAY_API_KEY=<key>   # or edit environment in compose
docker run -e GATEWAY_URL=... -e GATEWAY_API_KEY=... -p 11434:11434 ollama-proxy-bedrock
```

---

## Files

| File | Purpose |
|---|---|
| `ollama_proxy.py` | The Ollama-compatible proxy server (main deliverable) |
| `test_client.py` | Standalone CLI test client that calls the AWS gateway **directly** (no proxy) |
| `Dockerfile` | Container image for the proxy (python:3.12-slim, non-root, healthcheck) |
| `docker-compose.yml` | One-service compose setup with env overrides |

---

## Proxy endpoints

| Method & path | Ollama equivalent | Status |
|---|---|---|
| `GET /` | Health check — returns `Ollama is running` | ✅ |
| `HEAD /` | Client probe support | ✅ |
| `GET /api/version` | `{"version": "0.5.0-proxy"}` | ✅ |
| `GET /api/tags` | List available models | ✅ |
| `POST /api/show` | Model details | ✅ |
| `POST /api/chat` | Chat completion, `stream: true/false` | ✅ |
| `POST /api/generate` | Text completion, `stream: true/false` | ✅ |

All other paths return `404 {"error": "not found"}`.

### Models exposed via `/api/tags`

Clients reference these short names (with or without `:latest`):

| Ollama name | Gateway / Bedrock model ID | Notes |
|---|---|---|
| `sonnet4.5` (default) | `global.anthropic.claude-sonnet-4-5-20250929-v1:0` | ✅ Verified working, most reliable |
| `sonnet` | `apac.anthropic.claude-3-sonnet-20240229-v1:0` | ✅ Verified (occasional Marketplace errors) |
| `haiku` | `anthropic.claude-3-haiku-20240307-v1:0` | ⚠️ Flaky — intermittent IAM/Marketplace 403s |

Unknown model names are passed through verbatim as Bedrock model IDs, so full
model IDs / inference profile ARNs can also be used directly as the `model`
value.

### Request translation

| Ollama request field | Translated to gateway field |
|---|---|
| `model` | `modelId` (via the name map above) |
| `messages[].role/content` | `messages[].role` / `messages[].content = [{"text": ...}]` |
| `role: "system"` messages | `system = [{"text": ...}]` (Bedrock Converse style) |
| `options.num_predict` | `inferenceConfig.maxTokens` (**also sets the quota reservation**) |
| `stream: true/false` | Controls NDJSON streaming vs single JSON response |

### Response format

**Non-streaming `/api/chat`:**

```json
{
  "model": "sonnet4.5",
  "created_at": "2026-09-03T19:53:00.569713Z",
  "message": {"role": "assistant", "content": "proxy chat OK"},
  "done": true,
  "done_reason": "stop",
  "prompt_eval_count": 14,
  "eval_count": 6
}
```

**Streaming (`"stream": true`)** — newline-delimited JSON (`application/x-ndjson`),
one content chunk followed by the final `done` chunk with token counts:

```json
{"model": "sonnet4.5", "created_at": "...", "message": {"role": "assistant", "content": "..."}, "done": false}
{"model": "sonnet4.5", "created_at": "...", "message": {"role": "assistant", "content": ""}, "done": true, "done_reason": "stop", "prompt_eval_count": 12, "eval_count": 10}
```

> Note: the gateway returns complete (non-streamed) responses, so the proxy
> emits the whole reply as a single chunk. The NDJSON *format* is fully
> Ollama-compatible, but clients won't see token-by-token incremental output.

Errors are returned Ollama-style with matching HTTP status:

```json
{"error": "gateway error (model=...): Token quota exceeded"}
```

---

## Per-request API keys (multi-tenant)

The standard Ollama API has no auth, but the proxy lets **each client pass its
own gateway API key** — the built-in `GATEWAY_API_KEY` is only used as a
fallback when the client sends none. Three ways to pass a key, checked in
this order:

| Priority | Method | Example |
|---|---|---|
| 1 | `X-API-Key` header | `curl -H "X-API-Key: team150-..." ...` |
| 2 | `Authorization: Bearer <key>` header | OpenAI-style; most clients with an "API key" field send this automatically |
| 3 | Key embedded in model name | `"model": "key-<team-key>-sonnet4.5"` (for clients with only a model field) |

Method 3 syntax: `key-<API_KEY>-<model>` — the proxy strips the prefix and
uses `<API_KEY>` as the gateway key. The header methods win over the
model-name method.

Verified behavior:

```
X-API-Key: team1-...        -> 403 Forbidden (bad key reaches gateway)
Authorization: Bearer <good> -> 200 OK
model: "key-team1-...-sonnet4.5" -> 403 Forbidden (bad key reaches gateway)
(no key)                    -> 200 OK (built-in fallback key)
```

### Client configuration

**openclaw** — set the provider's API key (sent as `Authorization: Bearer`):

```json5
{
  "provider": "ollama",
  "base_url": "http://localhost:11434",
  "api_key": "<team-gateway-key>",   // forwarded to AWS as X-API-Key
  "model": "sonnet4.5"
}
```

**hermes** — via LiteLLM (`ollama_chat/` prefix targets `/api/chat`):

```python
from litellm import completion

completion(
    model="ollama_chat/sonnet4.5",
    messages=[{"role": "user", "content": "Hello"}],
    api_base="http://localhost:11434",
    api_key="<team-gateway-key>",     # forwarded to AWS as X-API-Key
)
```

**nanoclaw** — if it only accepts a model name (no API key field), embed the
key in the model:

```
model = "key-<team-gateway-key>-sonnet4.5"
```

---

## Auto-continue (long generations)

The gateway clamps every call to **1024 output tokens** (~750 words). When a
client asks for more (e.g. `"options": {"num_predict": 4500}` for a 3000-word
essay), the proxy automatically **chains multiple gateway calls** and stitches
the result into one seamless Ollama response — transparently to the client.

Two gateway constraints shaped the design:

1. **Lambda timeout / API Gateway 29s limit** — each call must complete in
   ~25s, so 1024 tokens per round is the practical ceiling.
2. **API Gateway rejects request bodies > ~6 KB with `403 Forbidden`** — so
   the conversation can't grow with the full accumulated text each round.

Solution — **sliding-window continuation**: each round sends the *original
prompt* + only the **last 1200 characters** of generated text + a "continue"
instruction. Every request stays ~1.6 KB regardless of total essay length:

```
Round 1: [original prompt]                                  → ~217 B  ✅
Round 2: [original prompt] + [last 1200 chars] + [continue] → ~1.6 KB ✅
Round 3..N: same fixed size                                 → ~1.6 KB ✅
```

Verified live:

| Request | Rounds | Output tokens | Words | Duration | Result |
|---|---|---|---|---|---|
| 1000-word essay (`num_predict: 1500`) | 1–2 | ~1024–2048 | 723–1437 | 22–47s | ✅ |
| 3000-word essay (`num_predict: 4500`) | 5 | 5120 | 3609 | 1m44s | ✅ |
| 10000-word essay (`num_predict: 14000`) | 14 | 14336 | 9666 | 5m41s | ✅ |

Notes:
- Early wrap-up guard: if the model returns `end_turn` before ~80% of the
  target is produced, the proxy keeps asking it to continue.
- Continuation context is only ~1200 chars — fine for linear long-form
  writing; the model can't see earlier sections.
- **Client timeouts matter:** a 10k-word essay takes ~6 minutes. Clients
  with their own HTTP timeouts must allow for it.
- Each round is charged separately against team quota (input tokens include
  the prompt + window each round).

---

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_URL` | `https://gnfyayl5ib.execute-api.ap-southeast-1.amazonaws.com/UAT/` | AWS API Gateway endpoint |
| `GATEWAY_API_KEY` | `ydmoI59i8c4NyRnY8IHDe1JEdjoAscD28RzUmJYr` | Fallback gateway key when the client sends none (see [Per-request API keys](#per-request-api-keys-multi-tenant)) |
| `PROXY_HOST` | `127.0.0.1` | Bind address |
| `PROXY_PORT` | `11434` | Listen port (Ollama default) |
| `DEFAULT_MODEL` | `sonnet4.5` | Model used when the client omits `model` |
| `MAX_TOKENS` | `256` | Default `maxTokens` / quota reservation |
| `GATEWAY_MAX_TOKENS` | `1024` | Gateway's per-call `maxTokens` clamp (must match the Lambda's cap) |
| `MAX_CONTINUE_ROUNDS` | `16` | Max chained calls for auto-continue (16 × 1024 ≈ 16k tokens ≈ 11k words) |
| `CONTINUE_CONTEXT_CHARS` | `1200` | Sliding-window size for continuation context (keeps requests under the gateway's ~6 KB body limit) |

Example — run on a different port with a different default model:

```bash
PROXY_PORT=8080 DEFAULT_MODEL=haiku python3 ollama_proxy.py
```

---

## AWS API Gateway contract (discovered by live probing)

The gateway implements a **custom Bedrock Converse-style schema** — it is
*not* the Ollama API and *not* the OpenAI API:

```
POST https://gnfyayl5ib.execute-api.ap-southeast-1.amazonaws.com/UAT/
Headers:
  X-API-Key: <api key>
  Content-Type: application/json
Body:
{
  "modelId": "<bedrock model id or inference profile id>",
  "messages": [{"role": "user", "content": [{"text": "..."}]}],
  "system": [{"text": "..."}],            # optional
  "inferenceConfig": {"maxTokens": 100}   # optional; default reservation 1024
}
```

**Success (200):**

```json
{
  "output": {"message": {"role": "assistant", "content": [{"text": "Hello."}]}},
  "stopReason": "end_turn",
  "usage": {"inputTokens": 13, "outputTokens": 5, "totalTokens": 18},
  "metrics": {"latencyMs": 261},
  "gateway": {
    "teamId": "TEAM-001",
    "reservedByThisRequest": 100,
    "chargedTokens": 18,
    "quotaSettlement": "SETTLED",
    "tokenLimit": 10000,
    "usedTokens": 3561,
    "reservedTokens": 6144,
    "availableTokens": 295
  }
}
```

### Gateway behavior notes (important)

1. **Only `POST /UAT/` exists.** Sub-paths (`/api/chat`, `/v1/chat/completions`,
   …) return `403 MissingAuthenticationTokenException` — that 403 means
   "route not found", not "bad key".
2. **API key auth:** a valid key passes through to the backend; an invalid key
   returns `403 ForbiddenException` from the usage plan. Keys tested:
   - `ydmoI59i8c4NyRnY8IHDe1JEdjoAscD28RzUmJYr` — ✅ valid (`TEAM-001`)
   - `team150-03720966706131533298820428113687` — ❌ Forbidden
   - `team1-19075726590408042981079012627181` — ❌ Forbidden
3. **Token quota (per team):** `TEAM-001` limit (10k initially; raised to 50k,
   then 100k during testing). Each request **reserves 1024 tokens by default**;
   `inferenceConfig.maxTokens` changes the reservation. If
   `availableTokens < reservation` → `429 Token quota exceeded`. Actual usage
   is charged and settled after completion (`chargedTokens`, unused
   reservation is released).
4. **Model IDs:** many base models (e.g. `anthropic.claude-3-sonnet-…`,
   `anthropic.claude-sonnet-4-5-…`) reject on-demand invocation — an
   **inference profile ID must be used** (`global.…`, `apac.…` prefixes).
   Some Claude 3 models intermittently fail with `AccessDeniedException`
   (AWS Marketplace subscription settling); `global.anthropic.claude-sonnet-4-5`
   was the most reliable in testing.
5. **`maxTokens` is clamped to 1024 server-side**
   (`min(requested, DEFAULT_MAX_OUTPUT_TOKENS)` in the Lambda). Asking for
   more silently caps the response — use the proxy's
   [auto-continue](#auto-continue-long-generations) for longer outputs.
6. **Timeouts:** API Gateway REST APIs have a hard **29-second** integration
   limit, and the Lambda timeout was originally too short — generations
   beyond ~200 output tokens failed with a bare
   `{"message": "Internal server error"}` (Lambda killed mid-`converse()`).
   Fixed by raising the Lambda timeout to ~28s. Longer generations require
   the proxy's auto-continue or an async pattern.
7. **Request body size limit ~6 KB:** API Gateway (or a WAF
   `SizeRestrictions_BODY` rule) rejects larger bodies with
   `403 Forbidden` *before* the Lambda runs. This blocks long multi-turn
   conversations; the proxy's sliding-window continuation works around it.

---

## `test_client.py` — direct gateway tester

Calls the gateway directly (bypasses the proxy) — useful for diagnosing
gateway-side issues:

```bash
python3 test_client.py "What is the capital of France?"
python3 test_client.py --model global.anthropic.claude-sonnet-4-5-20250929-v1:0 \
    --max-tokens 100 "Hello"
python3 test_client.py --system "You are a helpful assistant." \
    --temperature 0.5 --max-tokens 200 "Explain AWS Lambda in one sentence."
python3 test_client.py --payload request.json   # send a raw JSON file as-is
```

Flags: `--model` (default: Sonnet 4.5 global profile), `--system`,
`--temperature`, `--max-tokens` (default 256), `--payload` (raw JSON file),
`--timeout` (default 120s). The full request payload is echoed before sending.

---

## Tested & verified

| Test | Result |
|---|---|
| `GET /` / `/api/version` / `/api/tags` / `/api/show` | ✅ |
| `/api/chat` non-streaming | ✅ "proxy chat OK" |
| `/api/chat` streaming (NDJSON) | ✅ content + done chunks |
| `/api/generate` both modes | ✅ |
| System message → Bedrock `system` field | ✅ ("sky color?" → "Blue") |
| End-to-end via gateway with Sonnet 4.5 | ✅ Multiple 200 responses |
| Quota handling (`num_predict` → reservation) | ✅ Requests fit remaining quota |
| Per-request key via `X-API-Key` header | ✅ bad key → gateway 403 |
| Per-request key via `Authorization: Bearer` | ✅ good key → 200 |
| Per-request key embedded in model name | ✅ `key-<key>-sonnet4.5` → gateway 403 |
| No client key → built-in fallback | ✅ 200 |
| Auto-continue: 3000-word essay | ✅ 5 rounds, 5120 tokens, 1m44s |
| Auto-continue: 10000-word essay | ✅ 14 rounds, 14336 tokens, 5m41s |
| Sliding window vs ~6 KB body limit | ✅ requests stay ~1.6 KB |
| Early wrap-up guard (80% threshold) | ✅ prevents premature end_turn |

## Known limitations

- **No true token streaming** — the gateway buffers, so the proxy emits the
  full reply as one chunk (format is still correct NDJSON).
- **No tool calling / images / embeddings** — the gateway schema only supports
  text messages; clients sending `tools` or image content will have those
  fields ignored.
- **Long generations are slow** — auto-continue chains sequential 1024-token
  calls (~25s each); a 10k-word essay takes ~6 minutes. Client HTTP timeouts
  must accommodate this.
- **Continuation context is shallow** — the sliding window gives the model
  only the last ~1200 chars; it can't reference earlier sections.
- **Gateway ~6 KB request body limit** — long multi-turn chat histories sent
  directly by clients will hit `403 Forbidden` (same wall auto-continue works
  around). A WAF rule change on the gateway side is the real fix.
- `/api/pull`, `/api/create`, `/api/embeddings` etc. are not implemented
  (clients get 404).
