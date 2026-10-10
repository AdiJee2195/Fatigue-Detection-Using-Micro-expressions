# Multi-stage build: the final image contains only static files and nginx, so no
# Node runtime, no node_modules and no build toolchain ship to production.
#
# Build context is ./frontend (see docker-compose.yml).

# ---------------------------------------------------------------------------
# 1. build
# ---------------------------------------------------------------------------
FROM node:22-alpine AS build
WORKDIR /app

# Copy manifests first so the dependency layer is cached across source changes.
COPY package.json package-lock.json ./
# `npm ci` (not install) so the lockfile is authoritative and the build is reproducible.
RUN npm ci

COPY tsconfig.json vite.config.ts vitest.config.ts postcss.config.js tailwind.config.js index.html ./
COPY src ./src

# The gateway URL is baked in at build time; override per environment with
# `--build-arg VITE_GATEWAY_WS=wss://gateway.example.com`.
ARG VITE_GATEWAY_WS=ws://localhost:8080
ARG VITE_SESSION_ID=
ENV VITE_GATEWAY_WS=$VITE_GATEWAY_WS \
    VITE_SESSION_ID=$VITE_SESSION_ID
# `npm run build` runs `tsc --noEmit` first, so a type error fails the image build
# rather than shipping broken JavaScript.
RUN npm run build

# ---------------------------------------------------------------------------
# 2. serve
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine AS serve

# The stock config ships a default server block; ours replaces it.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

# Run as a non-root user. nginx.conf is written so the process needs no elevated
# privileges: it listens on 8080, keeps its temp files under /tmp, and does not
# try to write to /var/cache/nginx.
RUN chown -R nginx:nginx /usr/share/nginx/html /etc/nginx/conf.d \
    && sed -i 's|^user .*|user nginx;|' /etc/nginx/nginx.conf \
    && sed -i 's|^pid .*|pid /tmp/nginx.pid;|' /etc/nginx/nginx.conf
USER nginx

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
