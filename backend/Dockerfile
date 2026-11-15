# The control plane: API and worker. It carries a Docker client to start sandboxes, and is
# the only component that ever talks to the daemon.
FROM python:3.12-slim-bookworm

COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY alembic ./alembic

ENV ENV=production PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
