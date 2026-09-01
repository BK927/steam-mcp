ARG PYTHON_IMAGE="python:3.13.11-slim-bookworm@sha256:20080e807bfc404f8450b185cf0fc95d553462673598549613735f70a5b4d5d0"
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    HOST=0.0.0.0 \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml requirements.lock README.md LICENSE PRIVACY.md ./
COPY steam_mcp ./steam_mcp
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && python -m pip install --no-cache-dir --no-build-isolation --no-deps .

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080
CMD ["python", "-m", "steam_mcp.server"]
