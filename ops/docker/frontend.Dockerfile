# syntax=docker/dockerfile:1.7
#
# Frontend image: builds the React/Vite operator UI and serves the static assets with
# nginx. The UI is a static bundle; it holds no secrets and no server-side state.
#
# Built from the repository root context. `frontend/public/docs` is a symlink to the
# repository `docs/` directory; the builder resolves it so the documentation links that
# connectors advertise resolve to real files in the served bundle.

FROM node:22.19-bookworm-slim AS builder

WORKDIR /app

# Deterministic install from the lockfile.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Source last, so a source-only change does not re-run the dependency install.
COPY frontend/ ./
COPY docs/ /docs-source/

# Replace the public/docs symlink with a real copy of the repository docs. A build that
# cannot copy the documentation fails here rather than shipping blank links.
RUN rm -f public/docs && cp -r /docs-source public/docs

# `npm run build` runs `tsc -b && vite build`, so a type error fails the image build.
RUN npm run build


FROM nginxinc/nginx-unprivileged:1.27.3-alpine AS runtime

# The base image already runs as uid 101 (`nginx`). Restate it so a future change to the
# base image cannot silently reintroduce root.
USER 101

COPY --chmod=0644 ops/docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD ["wget", "--quiet", "--tries=1", "--spider", "http://127.0.0.1:8080/healthz"]

CMD ["nginx", "-g", "daemon off;"]
