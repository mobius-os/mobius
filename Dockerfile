# Single-container Möbius image.
#
# Builds the frontend, installs the backend + CLI tools, and serves
# everything from one FastAPI process.  Works on VPS, Railway, PikaPods.

# -- Stage 1: build the frontend --------------------------------------
FROM node:20-slim AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# -- Stage 2: backend + everything ------------------------------------
FROM python:3.12-slim

# System deps: curl, nodejs/npm (for claude CLI), esbuild binary.
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron curl ca-certificates nodejs npm git \
    && npm install -g esbuild@0.20.2 \
    && npm install -g @anthropic-ai/claude-code@2.1.92 \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app/
COPY backend/scripts ./scripts/
COPY skill/ ./skill/
COPY protected-files.txt ./protected-files.txt

# Frontend static files + app-frame served by FastAPI.
COPY --from=frontend /build/dist ./static/
COPY frontend/public/app-frame.html ./app-frame.html

# Full frontend source so the agent can edit and rebuild the shell.
# /app/shell-src/ is the read-only reference (originals for recovery).
# On first boot, entrypoint copies to /data/shell/ if it doesn't exist.
COPY frontend/ ./shell-src/
RUN cd ./shell-src && npm ci --ignore-scripts 2>/dev/null && rm -rf .vite

# Create a non-root user so the agent can use --dangerously-skip-permissions.
RUN useradd -m -s /bin/bash mobius \
    && mkdir -p /data/db /data/apps /data/compiled /data/shared \
    && chown -R mobius:mobius /data

COPY backend/scripts/entrypoint.sh ./scripts/entrypoint.sh
RUN chmod +x ./scripts/entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["./scripts/entrypoint.sh"]
