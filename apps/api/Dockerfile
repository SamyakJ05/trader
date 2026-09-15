# Production image. Two stages so the toolchain used to install dependencies
# does not ship with them.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir uv

# Dependencies resolve from pyproject alone, so this layer is cached until the
# dependency list itself changes — not on every source edit.
COPY pyproject.toml ./
# No [dev]: pytest, ruff and their trees have no business in an image that
# holds broker credentials and faces the internet.
RUN uv pip install --system --no-cache "."

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Run as a non-root user. A container that is compromised should not also be
# root inside it.
RUN useradd --create-home --uid 10001 trader
WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY --chown=trader:trader alembic.ini ./
COPY --chown=trader:trader alembic ./alembic
COPY --chown=trader:trader app ./app

USER trader
EXPOSE 8000

# No --reload: it watches the filesystem, restarts on any change, and has no
# place in front of real orders. Workers are modest because the work is I/O
# bound and Postgres connections are limited.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
