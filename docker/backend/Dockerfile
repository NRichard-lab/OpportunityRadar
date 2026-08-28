# syntax=docker/dockerfile:1.7

# The digest pins the Debian-based Python 3.12 runtime used for this release.
FROM python:3.12-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579

ARG APP_UID=10001
ARG APP_GID=10001
ARG DEPLOYMENT_VERSION=development

LABEL org.opencontainers.image.title="Opportunity Radar backend" \
      org.opencontainers.image.revision="${DEPLOYMENT_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    DEPLOYMENT_VERSION="${DEPLOYMENT_VERSION}"

WORKDIR /app

RUN groupadd --gid "${APP_GID}" opportunity-radar \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin opportunity-radar

COPY requirements-production.txt /tmp/requirements-production.txt
RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: \
        --requirement /tmp/requirements-production.txt \
    && rm /tmp/requirements-production.txt

# Copy only executable application source. Runtime data, credentials, developer
# environments, browser binaries, tests, and local exports never enter the image.
COPY --chown=${APP_UID}:${APP_GID} *.py ./
COPY --chown=${APP_UID}:${APP_GID} backend/ ./backend/
COPY --chown=${APP_UID}:${APP_GID} collectors/ ./collectors/

USER ${APP_UID}:${APP_GID}

EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()"]

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
