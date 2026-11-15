# The image every task sandbox runs. Nothing from the control plane is baked in: no API code,
# no credentials, no Docker client. Generated code runs here as an unprivileged user.
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep nodejs npm ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# Test tooling that most Python tasks need, installed once in the image so a run with no
# network access can still execute tests.
RUN pip install --no-cache-dir pytest==9.2.0 pytest-asyncio==1.5.0 httpx==0.28.1

RUN groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/sandbox --shell /bin/sh sandbox \
    && mkdir -p /workspace /opt/codepilot \
    && chown sandbox:sandbox /workspace /home/sandbox

# The file agent is owned by root and read-only to the sandbox user, so generated code can't
# rewrite the program that enforces the workspace boundary.
COPY agentd.py /opt/codepilot/agentd.py
RUN chmod 0555 /opt/codepilot/agentd.py

ENV HOME=/home/sandbox \
    PATH=/home/sandbox/.local/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUSERBASE=/home/sandbox/.local \
    PIP_USER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    GIT_CONFIG_GLOBAL=/home/sandbox/.gitconfig

USER sandbox
WORKDIR /workspace
CMD ["sleep", "infinity"]
