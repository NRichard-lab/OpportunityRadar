# syntax=docker/dockerfile:1.7

# Playwright 1.62.0 image for linux/amd64, pinned to the immutable platform
# manifest containing Chromium 151.0.7922.34 (revision 1234) and its Linux
# runtime dependencies.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble@sha256:51d31fdfacb0cff99a1a724152e34ae408d2bd4e7da310ff157450f49261cc59

ARG APP_UID=10001
ARG APP_GID=10001
ARG DEPLOYMENT_VERSION=development

LABEL org.opencontainers.image.title="Opportunity Radar backend" \
      org.opencontainers.image.revision="${DEPLOYMENT_VERSION}" \
      org.opencontainers.image.playwright.version="1.62.0" \
      org.opencontainers.image.chromium.version="151.0.7922.34" \
      org.opencontainers.image.chromium.revision="1234" \
      tech.blueash.browser-egress="network_namespace_dns_pinned_proxy_v1"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    OPPORTUNITY_RADAR_PLAYWRIGHT_VERSION=1.62.0 \
    OPPORTUNITY_RADAR_CHROMIUM_VERSION=151.0.7922.34 \
    OPPORTUNITY_RADAR_CHROMIUM_REVISION=1234 \
    OPPORTUNITY_RADAR_CHROMIUM_EXECUTABLE=/ms-playwright/chromium-1234/chrome-linux64/chrome \
    OPPORTUNITY_RADAR_CHROMIUM_WRAPPER=/usr/local/bin/opportunity-radar-chromium-netns \
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
COPY docker/backend/chromium-netns-wrapper /usr/local/bin/opportunity-radar-chromium-netns

RUN chmod 0555 /usr/local/bin/opportunity-radar-chromium-netns \
    "${OPPORTUNITY_RADAR_CHROMIUM_EXECUTABLE}" \
    && test -x "${OPPORTUNITY_RADAR_CHROMIUM_EXECUTABLE}" \
    && python -c "import importlib.metadata as m; assert m.version('playwright') == '1.62.0'"

USER ${APP_UID}:${APP_GID}

EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()"]

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
