# Sandboxed code execution service.
#
# Deliberately NOT built on the backend image: it must hold no application
# code, no database driver and no credentials, only the language toolchains and
# this small service.

# A current Node binary (the Debian package lags badly, and the permission
# model used by the JavaScript runner needs Node 22). Only the binary is
# copied - no npm, so nothing here can install packages at run time.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Toolchains. Fixed at build time: code never installs anything when it runs.
#   g++      C++ compiler            default-jdk-headless   javac + JVM
#   iptables network isolation (see app/firewall.py)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        g++ \
        default-jdk-headless \
        iptables \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node

# The unprivileged identity executed programs run as. No login shell, no home.
RUN groupadd --gid 10001 runner \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin runner

WORKDIR /srv

COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY app /srv/app
COPY tests /srv/tests
COPY pytest.ini /srv/pytest.ini

EXPOSE 9000

# Runs as root *only* to drop each execution to the runner uid and to install
# the firewall rule at start-up; compose removes every other capability.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
