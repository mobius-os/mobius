# Single-container Möbius image.
#
# Builds the frontend, installs the backend + CLI tools, and serves
# everything from one FastAPI process.  Works on VPS, Railway, PikaPods.

# -- Stage 1: build the frontend --------------------------------------
FROM node:20-slim AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --ignore-scripts
COPY frontend/ .
RUN npm run build

# -- Stage 2: backend + everything ------------------------------------
FROM python:3.12-slim

# Copy Node.js binary from the frontend stage instead of installing via
# apt.  The debian nodejs/npm packages pull in ~200MB of system node
# packages we don't need — only the claude CLI and npm globals need Node.
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# System deps and global npm packages in a single layer.
# agent-browser downloads its own Chromium during `install`; we move it
# to /opt/agent-browser so both root and the mobius user share a single
# Chromium copy via the symlinks below (~/.agent-browser is where
# agent-browser looks by default).
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron curl ca-certificates git \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 \
    fonts-liberation fonts-noto-color-emoji \
    && npm install -g esbuild@0.20.2 \
    && npm install -g @anthropic-ai/claude-code@2.1.152 \
    && npm install -g @openai/codex@0.134.0 \
    && npm install -g agent-browser \
    && agent-browser install \
    && mv /root/.agent-browser /opt/agent-browser \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Share the agent-browser install between root and mobius via symlinks.
# The mobius user is created further down; we chown the shared dir to
# mobius:mobius after that, so mobius can write session sockets/locks
# as the owner without needing world-write on the Chromium binaries.
# (root still has access because root always does.)
RUN ln -s /opt/agent-browser /root/.agent-browser

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# openai-codex Python SDK: installed in a separate step because its
# upstream pyproject pins openai-codex-cli-bin==0.131.0a4 (a version
# not published on PyPI). Install --no-deps to skip that broken pin,
# then pin the cli-bin to a version that actually exists.
# Pinned to commit SHA (not tag) for full reproducibility — tags are
# mutable on GitHub. SHA corresponds to refs/tags/rust-v0.134.0 as of
# 2026-05-27. 0.134 replaced the private `_client._sync._approval_handler`
# monkey-patch with a public `approval_handler` constructor argument
# on `openai_codex.client.AppServerClient` — codex_sdk_runner.py now
# uses that public API instead of monkey-patching.
RUN pip install --no-cache-dir --no-deps \
      'openai-codex @ git+https://github.com/openai/codex.git@a75c443fdb64db48c3cf4bdb247c7ee52c0144c9#subdirectory=sdk/python' \
    && pip install --no-cache-dir 'openai-codex-cli-bin==0.134.0'

COPY backend/app ./app/
COPY backend/scripts ./scripts/
COPY skill/ ./skill/
COPY protected-files.txt ./protected-files.txt

# Recovery sources — immutable baked copies of the backend code and
# scripts. The entrypoint chowns the LIVE /app/app/ and /app/scripts/
# to mobius so the agent can edit them; if the agent breaks something,
# recovery_restore.sh restores from these baked copies. Stay root-owned
# and chmod a-w so even root running the recovery script can't
# accidentally modify them in-place. They are the floor of the
# recovery story: if the agent corrupts the live copy, this is what
# we copy back from.
COPY backend/app ./app-baked/
COPY backend/scripts ./scripts-baked/
RUN chmod -R a-w /app/app-baked /app/scripts-baked

# Frontend static files + app-frame served by FastAPI.
COPY --from=frontend /build/dist ./static/
COPY frontend/public/app-frame.html ./app-frame.html

# Self-hosted vendor libs for mini-app import maps. Pinned via npm
# install at image build time, served same-origin under /vendor/ with
# a long cache. Eliminates the cold-load esm.sh waterfall for three.js
# (cold-load saves 1-3s on any 3D app). Pinned to match the version
# we previously served via CDN.
RUN mkdir -p /tmp/vendor-install && cd /tmp/vendor-install \
    && npm init -y >/dev/null \
    && npm install --no-audit --no-fund --silent three@0.162.0 \
    && mkdir -p /app/static/vendor/three/addons \
    && cp node_modules/three/build/three.module.js /app/static/vendor/three/three.module.js \
    && cp -r node_modules/three/examples/jsm/. /app/static/vendor/three/addons/ \
    && cd / && rm -rf /tmp/vendor-install

# Full frontend source so the agent can edit and rebuild the shell.
# /app/shell-src/ is the read-only reference (originals for recovery).
# On first boot, entrypoint copies to /data/shell/ if it doesn't exist.
COPY frontend/ ./shell-src/
RUN cd ./shell-src && npm ci --ignore-scripts 2>/dev/null && rm -rf .vite

# Create a non-root user so the agent can use --dangerously-skip-permissions.
RUN useradd -m -s /bin/bash mobius \
    && mkdir -p /data/db /data/apps /data/compiled /data/shared \
    && chown -R mobius:mobius /data \
    && ln -s /opt/agent-browser /home/mobius/.agent-browser \
    && chown -R mobius:mobius /opt/agent-browser

COPY backend/scripts/entrypoint.sh ./scripts/entrypoint.sh
RUN chmod +x ./scripts/entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["./scripts/entrypoint.sh"]
