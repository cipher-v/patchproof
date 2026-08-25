FROM python:3.12-slim-bookworm

ARG UV_VERSION=0.12.3

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/patchproof/.venv

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /opt/patchproof
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev \
    && useradd --create-home --uid 10001 patchproof \
    && chown -R patchproof:patchproof /opt/patchproof

USER 10001
ENV PATH="/opt/patchproof/.venv/bin:${PATH}"
CMD ["python", "-m", "patchproof.cloud_entrypoint"]
