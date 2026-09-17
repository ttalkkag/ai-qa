FROM ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 AS uv
FROM mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27

COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    CI=true
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --extra dev --python 3.11 --no-install-project
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund

COPY apps ./apps
COPY config ./config
COPY devauto ./devauto
COPY integrations ./integrations
COPY static ./static
COPY templates ./templates
COPY manage.py playwright.config.cjs ./
COPY tests/e2e ./tests/e2e
COPY tests/fixtures/board ./tests/fixtures/board
RUN uv sync --locked --extra dev --python 3.11

CMD ["npm", "run", "test:e2e"]
