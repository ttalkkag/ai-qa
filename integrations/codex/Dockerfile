FROM ghcr.io/astral-sh/uv:0.12.12 AS uv
FROM mcr.microsoft.com/playwright:v1.63.0-noble
COPY --from=uv /uv /uvx /usr/local/bin/
RUN npm install --global @openai/codex@0.154.0
ENV PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy
WORKDIR /workspace
