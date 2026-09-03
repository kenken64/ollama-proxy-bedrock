# Ollama-compatible proxy -> AWS API Gateway (Bedrock)
# Pure Python stdlib app; no dependency install step needed.
FROM python:3.12-slim

# Run as non-root
RUN useradd --create-home --uid 10001 proxyuser

WORKDIR /app
COPY ollama_proxy.py /app/ollama_proxy.py
COPY test_client.py /app/test_client.py

# Runtime configuration (all overridable via environment).
# GATEWAY_URL and GATEWAY_API_KEY fall back to the built-in UAT defaults
# in the code; override at runtime: docker run -e GATEWAY_API_KEY=... ...
ENV PROXY_HOST=0.0.0.0 \
    PROXY_PORT=11434 \
    DEFAULT_MODEL=sonnet4.5 \
    MAX_TOKENS=256

EXPOSE 11434

USER proxyuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PROXY_PORT','11434')+'/',timeout=4)"

CMD ["python3", "ollama_proxy.py"]
