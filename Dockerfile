FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

WORKDIR /build

RUN python -m venv "${VIRTUAL_ENV}" \
    && python -m pip install --upgrade pip setuptools wheel

# Copy dependency metadata before the source so Docker can reuse the dependency
# layer while application code changes.
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install .


FROM python:3.12-slim-bookworm AS runtime

ARG SLOTLEASE_VERSION=dev

LABEL org.opencontainers.image.title="SlotLease" \
      org.opencontainers.image.description="Safety-first lifecycle control for PostgreSQL logical replication slots" \
      org.opencontainers.image.version="${SLOTLEASE_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN addgroup --system --gid 10001 slotlease \
    && adduser --system --uid 10001 --ingroup slotlease \
        --home /nonexistent --no-create-home slotlease

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER 10001:10001

# The policy is deliberately not baked into the image. Mount it read-only and
# inject SLOTLEASE_DSN at runtime so the same image can inspect many clusters.
ENTRYPOINT ["slotlease"]
CMD ["--help"]
