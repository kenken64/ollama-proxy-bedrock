#!/usr/bin/env python3
"""Test client for the AWS API Gateway (UAT) Bedrock chat proxy.

Sends a chat request directly to the base URL (no /api/chat suffix).

Request schema (discovered by probing the endpoint):
    POST <BASE_URL>
    Headers: X-API-Key, Content-Type: application/json
    Body: {
        "modelId": "<bedrock model id>",
        "messages": [{"role": "user", "content": [{"text": "..."}]}],
        "inferenceConfig": {"maxTokens": 100}   # caps the quota reservation
    }

Notes (from live probing):
    - The gateway reserves 1024 tokens per request by default; pass
      inferenceConfig.maxTokens to lower the reservation to fit quota.
    - anthropic.claude-3-sonnet-20240229-v1:0 requires an inference profile;
      use anthropic.claude-3-haiku-20240307-v1:0 or the apac.* profile IDs.

Usage:
    python3 test_client.py "Your message here"
    python3 test_client.py --model anthropic.claude-3-haiku-20240307-v1:0 --max-tokens 100 "Hello"
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

BASE_URL = "https://gnfyayl5ib.execute-api.ap-southeast-1.amazonaws.com/UAT/"
API_KEY = "ydmoI59i8c4NyRnY8IHDe1JEdjoAscD28RzUmJYr"
DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"


def send_chat(message: str, model_id: str = DEFAULT_MODEL,
              max_tokens: int = 256, timeout: int = 120) -> dict:
    payload = {
        "modelId": model_id,
        "messages": [
            {"role": "user", "content": [{"text": message}]}
        ],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Test client for the UAT Bedrock chat proxy")
    parser.add_argument("message", nargs="?", default="Say hello in one word.",
                        help="User message to send")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Bedrock model ID")
    parser.add_argument("--max-tokens", type=int, default=256,
                        help="Max output tokens (also sets the quota reservation)")
    parser.add_argument("--timeout", type=int, default=120, help="Request timeout (seconds)")
    args = parser.parse_args()

    print(f"POST {BASE_URL}")
    print(f"modelId: {args.model}")
    print(f"message: {args.message}")
    print("-" * 60)

    result = send_chat(args.message, args.model, args.max_tokens, args.timeout)

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
