# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS build

ARG VITE_BASE_PATH=/
ENV VITE_BASE_PATH="${VITE_BASE_PATH}"

WORKDIR /build

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.28-alpine@sha256:7377697a821c131a924a7105fafbe7414db4e9fcc77a6f08f776f33f141ec3f8

ARG APP_UID=10001
ARG APP_GID=10001

USER 0
RUN sed -i "s/^nginx:x:101:101:/nginx:x:${APP_UID}:${APP_GID}:/" /etc/passwd \
    && sed -i "s/^nginx:x:101:/nginx:x:${APP_GID}:/" /etc/group

COPY docker/frontend/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build --chown=${APP_UID}:${APP_GID} /build/dist/ /usr/share/nginx/html/

USER ${APP_UID}:${APP_GID}

EXPOSE 8080
STOPSIGNAL SIGQUIT

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["wget", "--quiet", "--output-document=/dev/null", "http://127.0.0.1:8080/"]
