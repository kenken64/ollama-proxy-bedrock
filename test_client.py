#!/usr/bin/env python3
"""Test client for the AWS API Gateway (UAT) Bedrock chat proxy.

Sends a chat request directly to the base URL (no /api/chat suffix).

Request schema (gateway contract):
    POST <BASE_URL>
    Headers: X-API-Key, Content-Type: application/json
    Body: {
        "modelId": "<bedrock model id or inference profile>",
        "system": [{"text": "You are a helpful assistant."}],      # optional
        "messages": [{"role": "user", "content": [{"text": "..."}]}],
        "inferenceConfig": {"temperature": 0.5, "maxTokens": 200}
    }

Notes (from live probing):
    - The gateway reserves 1024 tokens per request by default; pass
      inferenceConfig.maxTokens to lower the reservation to fit quota.
    - Base Claude model IDs reject on-demand invocation; use inference
      profile IDs (e.g. global.anthropic.claude-sonnet-4-5-20250929-v1:0).

Usage:
    python3 test_client.py "Your message here"
    python3 test_client.py --system "You are a helpful assistant." \
        --temperature 0.5 --max-tokens 200 "Explain AWS Lambda in one sentence."
    python3 test_client.py --payload request.json   # send a raw JSON file as-is
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

BASE_URL = "https://gnfyayl5ib.execute-api.ap-southeast-1.amazonaws.com/UAT/"
API_KEY = "ydmoI59i8c4NyRnY8IHDe1JEdjoAscD28RzUmJYr"
DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"


def build_payload(message: str, model_id: str, max_tokens: int,
                  temperature: float | None, system: str | None) -> dict:
    """Build the exact gateway request payload."""
    payload = {
        "modelId": model_id,
        "messages": [
            {"role": "user", "content": [{"text": message}]}
        ],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if system:
        payload["system"] = [{"text": system}]
    if temperature is not None:
        payload["inferenceConfig"]["temperature"] = temperature
    return payload


def send_payload(payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        BASE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": resp.status, "body": json.loads(resp.read())}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
        return {"status": e.code, "body": body}
    except urllib.error.URLError as e:
        return {"status": None, "body": {"error": str(e.reason)}}


def send_chat(message: str, model_id: str = DEFAULT_MODEL,
              max_tokens: int = 256, temperature: float | None = None,
              system: str | None = None, timeout: int = 120) -> dict:
    return send_payload(
        build_payload(message, model_id, max_tokens, temperature, system),
        timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Test client for the UAT Bedrock chat proxy")
    parser.add_argument("message", nargs="?", default="Say hello in one word.",
                        help="User message to send")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Bedrock model ID")
    parser.add_argument("--system", default=None,
                        help="System prompt (sent as system: [{text: ...}])")
    parser.add_argument("--temperature", type=float, default=None,
                        help="inferenceConfig.temperature (0.0 - 1.0)")
    parser.add_argument("--max-tokens", type=int, default=256,
                        help="inferenceConfig.maxTokens (also sets the quota reservation)")
    parser.add_argument("--payload", default=None,
                        help="Path to a JSON file to send verbatim as the request body")
    parser.add_argument("--timeout", type=int, default=120, help="Request timeout (seconds)")
    args = parser.parse_args()

    if args.payload:
        with open(args.payload, "r", encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = build_payload(args.message, args.model, args.max_tokens,
                                args.temperature, args.system)

    print(f"POST {BASE_URL}")
    print("Request payload:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("-" * 60)

    result = send_payload(payload, args.timeout)

    print(f"HTTP status: {result['status']}")
    print(json.dumps(result["body"], indent=2, ensure_ascii=False))

    if result["status"] == 200:
        # Print just the assistant's reply for convenience
        try:
            reply = result["body"]["output"]["message"]["content"][0]["text"]
            print("-" * 60)
            print(f"Assistant: {reply}")
        except (KeyError, IndexError, TypeError):
            pass
        return 0
    if result["status"] == 429:
        print("\nNote: quota exhausted on the server side "
              "(reservation of 1024 tokens > available tokens). Try again later.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
