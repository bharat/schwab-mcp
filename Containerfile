# syntax=docker/dockerfile:1.23

FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim@sha256:f5b1b14b0100eb85b07650b33d702a65c2a826bc301ddbdc89f43da6d23b3ab1 AS uv-tools

FROM python:3.14-slim@sha256:5b3879b6f3cb77e712644d50262d05a7c146b7312d784a18eff7ff5462e77033 AS builder

ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv-tools /usr/local/bin/uv /usr/bin/

WORKDIR /app

COPY pyproject.toml README.md uv.lock ./
COPY src ./src

RUN uv build --wheel --out-dir /dist && \
    uv export \
        --format requirements-txt \
        --group ta \
        --no-emit-project \
        --output-file /dist/requirements.txt

FROM python:3.14-slim@sha256:5b3879b6f3cb77e712644d50262d05a7c146b7312d784a18eff7ff5462e77033

ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv-tools /usr/local/bin/uv /usr/bin/

WORKDIR /workspace

COPY --from=builder /dist/ /tmp/dist/

RUN uv pip install --system --no-cache -r /tmp/dist/requirements.txt \
    && uv pip install --system --no-cache /tmp/dist/*.whl \
    && rm -rf /tmp/dist

LABEL org.opencontainers.image.title="Schwab MCP Server" \
      org.opencontainers.image.description="Model Context Protocol server for Schwab built on schwab-mcp." \
      org.opencontainers.image.source="https://github.com/jkoelker/schwab-mcp"

ENTRYPOINT ["schwab-mcp"]
CMD ["server"]
