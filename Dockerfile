# Single-container Möbius image.
#
# Builds the frontend, installs the backend + CLI tools, and serves
# everything from one FastAPI process.  Works on VPS, Railway, PikaPods.

# Keep the Node runtime source independent of the frontend build. Copying Node
# from the completed frontend stage made every UI edit invalidate the backend's
# expensive apt/agent-CLI/browser layers during local E2E builds. The pinned
# agent-browser requires Node >=24; preship-gate.sh uses this same major so local
# frontend verification cannot silently pass on an older runtime.
FROM node:24-trixie-slim AS node-runtime

# -- Stage 1: build the frontend --------------------------------------
FROM node-runtime AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --ignore-scripts && rm -rf /root/.npm
COPY frontend/ .
RUN npm run build && rm -rf /root/.npm

# -- Stage 2: backend + everything ------------------------------------
FROM python:3.12-slim-trixie

# Copy Node.js binary from the frontend stage instead of installing via
# apt.  The debian nodejs/npm packages pull in ~200MB of system node
# packages we don't need — only npm globals need Node.
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Create the runtime user before installing the shared browser payload. Setting
# its ownership in the payload's own layer avoids copying Chromium into a second
# image layer solely to change metadata later.
RUN useradd -m -s /bin/bash mobius

# System deps and global npm packages in a single layer.
# agent-browser downloads its own Chromium during `install`; we move it
# to /opt/agent-browser so both root and the mobius user share a single
# Chromium copy via the symlinks below (~/.agent-browser is where
# agent-browser looks by default).
# Discard npm's download cache in each layer: installed packages are the
# runtime artifact; registry tarballs only make the production image larger.
ARG CODEX_VERSION=0.152.1
ARG AGENT_BROWSER_VERSION=0.35.1
RUN apt-get update && apt-get install -y --no-install-recommends \
    age ca-certificates cron curl git jq procps ripgrep sqlite3 sudo unzip util-linux \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 \
    fonts-liberation fonts-noto-color-emoji \
    && npm install -g --engine-strict --strict-allow-scripts \
      --allow-scripts="@openai/codex@${CODEX_VERSION},agent-browser@${AGENT_BROWSER_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
      "agent-browser@${AGENT_BROWSER_VERSION}" \
    && agent-browser install \
    && mv /root/.agent-browser /opt/agent-browser \
    && chown -R mobius:mobius /opt/agent-browser \
    && git_version="$(git --version | awk '{print $3}')" \
    && [ "$(printf '%s\n' "2.38" "$git_version" | sort -V | head -n1)" = "2.38" ] \
    && rm -rf /root/.npm /var/lib/apt/lists/*

# tectonic is a server-side subprocess; CSP connect-src 'self' applies only to
# browser fetches from the mini-app iframe, not OS-level subprocesses — tectonic's
# package fetches (from Tectonic's bundle server) are unrestricted at the OS level.
# Placed after the apt-get layer so a tectonic version bump doesn't bust the apt cache.
ARG TECTONIC_VERSION=0.17.0
ARG TECTONIC_SHA256_AMD64=8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7
ARG TECTONIC_SHA256_ARM64=b10954a95404f3ab2328d2fa59a5ebab8e657f893fab096f98be8db7c0c979b8
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) target=x86_64; sha256="$TECTONIC_SHA256_AMD64" ;; \
      arm64) target=aarch64; sha256="$TECTONIC_SHA256_ARM64" ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    tarball="tectonic-${TECTONIC_VERSION}-${target}-unknown-linux-musl.tar.gz"; \
    base="https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}"; \
    curl -fsSL "${base}/${tarball}" -o "/tmp/${tarball}"; \
    echo "${sha256}  /tmp/${tarball}" | sha256sum -c -; \
    tar xzf "/tmp/${tarball}" -C /usr/local/bin/ tectonic; \
    rm "/tmp/${tarball}"; \
    chmod +x /usr/local/bin/tectonic; \
    tectonic --version

# GitHub CLI: the agent's Contribute flow opens PRs/issues upstream through
# `gh` (a server-side subprocess, so CSP connect-src 'self' — which governs
# only mini-app iframe fetches — does not apply). Pinned and sha256-verified
# against the release's own checksums file, fetched at build time; a mismatch
# fails the build. Built for the image arch (amd64|arm64); only the single
# `gh` binary is installed, docs/man pages are dropped. Placed after the apt
# layer so a gh bump doesn't bust the apt cache.
ARG GH_CLI_VERSION=2.97.0
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64|arm64) ;; *) echo "unsupported arch: $arch" >&2; exit 1 ;; esac; \
    tarball="gh_${GH_CLI_VERSION}_linux_${arch}.tar.gz"; \
    base="https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}"; \
    curl -fsSL "${base}/${tarball}" -o "/tmp/${tarball}"; \
    curl -fsSL "${base}/gh_${GH_CLI_VERSION}_checksums.txt" -o /tmp/gh_checksums.txt; \
    grep " ${tarball}\$" /tmp/gh_checksums.txt | (cd /tmp && sha256sum -c -); \
    tar xzf "/tmp/${tarball}" -C /tmp; \
    install -m 0755 "/tmp/gh_${GH_CLI_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf "/tmp/${tarball}" /tmp/gh_checksums.txt \
      "/tmp/gh_${GH_CLI_VERSION}_linux_${arch}"; \
    gh --version

# Share the mobius-owned agent-browser install without a second Chromium copy.
RUN ln -s /opt/agent-browser /root/.agent-browser \
    && ln -s /opt/agent-browser /home/mobius/.agent-browser

# Agent turns point AGENT_BROWSER_CONFIG here so untrusted workspace config
# cannot register executable plugins. Runtime settings still travel through
# explicit AGENT_BROWSER_* environment variables owned by chat.py.
RUN install -d -m 0755 /app \
    && printf '{}\n' > /app/agent-browser-config.json \
    && chmod 0644 /app/agent-browser-config.json

# openai/codex-plugin-cc — Claude Code plugin that exposes Codex as a
# delegation/review subagent inside the agent's session. Cloned at
# image-build time so the source is reproducible and pinned to a
# release tag; the actual `claude plugin install` happens at first
# boot in entrypoint.sh (it has to write into the agent's runtime
# CLAUDE_CONFIG_DIR=/data/cli-auth/claude/, which is a volume and
# can't be baked into the image). Stays root-owned + world-readable
# (git clone's default 755/644) — install only reads from here.
RUN git clone --depth 1 --branch v1.0.6 \
      https://github.com/openai/codex-plugin-cc.git /opt/codex-plugin-cc

WORKDIR /app

COPY backend/requirements.txt backend/requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && python -c \
      'from pathlib import Path; import claude_agent_sdk; p = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"; assert p.is_file() and p.stat().st_mode & 0o111' \
    && ln -s "$(python -c 'from pathlib import Path; import claude_agent_sdk; print(Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")')" /usr/local/bin/claude \
    && python -c \
      'from pathlib import Path; import shutil, claude_agent_sdk; assert Path(shutil.which("claude")).samefile(Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")' \
    && claude --version | grep -Fx '2.1.259 (Claude Code)'

# openai-codex Python SDK: its upstream pyproject pins a second, older
# openai-codex-cli-bin payload. Keep that declared package so `pip check` and
# owner-authored Python SDK code retain the documented default constructor, but
# replace its private payload in the SAME image layer with a link to Möbius's
# lockstep npm CLI. This preserves the external SDK contract without storing a
# second ~350 MB runtime or running a second protocol version.
# Pinned to commit SHA (not tag) for full reproducibility — tags are
# mutable on GitHub. SHA corresponds to refs/tags/rust-v0.152.1
# as of 2026-09-01, and is kept in lockstep with the npm @openai/codex
# binary above (the SDK spawns it via codex_bin=shutil.which("codex")).
# We moved from rust-v0.144.5 to this tag because the 0.144.x generated
# ReasoningEffort enum was strict (none/minimal/low/medium/high/xhigh)
# and rejected efforts the running CLI advertises for newer models, so
# codex.models() and ThreadResumeResponse validation failed and broke a
# real chat resume. alpha.13 turned ReasoningEffort into a forgiving
# `str, Enum` with a `_missing_` hook that accepts any effort string;
# 0.152.1 is the latest stable tag published to BOTH the git repo and npm, so
# binary and schema stay matched. The SDK exposes the request bridge as a
# public `approval_handler` constructor argument on
# `openai_codex.client.CodexClient`; `AsyncCodex` still does not forward
# it, so codex_sdk_runner.py installs the handler on the wrapped sync
# client's `_approval_handler`.
RUN pip install --no-cache-dir --no-deps \
      'openai-codex @ git+https://github.com/openai/codex.git@5adb68a49933ae446bf11935662c83dba55a0804#subdirectory=sdk/python' \
    && pip install --no-cache-dir 'openai-codex-cli-bin==0.147.0' \
    && _codex_cli_bin="$(python -c \
      'from pathlib import Path; import codex_cli_bin; print(Path(codex_cli_bin.__file__).parent)')" \
    && rm -rf "${_codex_cli_bin}/bin" \
      "${_codex_cli_bin}/codex-path" \
      "${_codex_cli_bin}/codex-resources" \
    && mkdir -p "${_codex_cli_bin}/bin" \
    && ln -s /usr/local/bin/codex "${_codex_cli_bin}/bin/codex" \
    && python -c \
      'from pathlib import Path; import openai_codex; from codex_cli_bin import bundled_codex_path; assert bundled_codex_path().samefile(Path("/usr/local/bin/codex"))' \
    && pip check

# Capture each installed agent CLI's publish date into a small JSON the
# Settings row reads (routes/settings._cli_release_dates), keyed by the
# version reported by its actual executable. Done at build time so a CLI bump
# refreshes the date automatically — no hand-maintained map, no test to
# satisfy. Best effort: if the npm registry is unreachable the file is left
# empty and the Settings row simply shows the bare version, never an error.
RUN if ! node -e "const cp=require('child_process'),fs=require('fs');\
let installed={};\
try{installed=(JSON.parse(cp.execSync('npm ls -g --depth=0 --json',{stdio:['ignore','pipe','ignore']}).toString()).dependencies)||{};}catch(e){}\
const claude=(cp.execSync('claude --version').toString().match(/^(\\S+)/)||[])[1];\
const want={'@anthropic-ai/claude-code':claude,'@openai/codex':installed['@openai/codex']&&installed['@openai/codex'].version};\
const out={};\
for(const [name,v] of Object.entries(want)){if(!v)continue;\
try{const t=JSON.parse(cp.execSync('npm view '+name+'@'+v+' time --json',{stdio:['ignore','pipe','ignore']}).toString());if(t&&t[v])out[v]=String(t[v]).slice(0,10);}catch(e){}}\
fs.writeFileSync('/app/cli-release-dates.json',JSON.stringify(out));\
console.log('cli-release-dates.json:',JSON.stringify(out));"; then \
      echo '{}' > /app/cli-release-dates.json; \
    fi; \
    rm -rf /root/.npm

# Install the shell and mini-app compiler dependency tree from manifests alone.
# Application source is copied later, so ordinary frontend edits reuse this
# pinned npm layer. The production compiler resolves app bare imports only from
# this directory and embeds the complete graph into each app module.
COPY frontend/package.json frontend/package-lock.json* ./shell-src/
RUN cd ./shell-src \
    && npm ci --ignore-scripts 2>/dev/null \
    && rm -rf .vite /root/.npm

# pdf.js (Mozilla's engine — what Firefox's built-in PDF viewer uses),
# vendored same-origin so the LaTeX app renders a compiled PDF as a real
# scroll/zoom viewer rather than the "open externally" button mobile
# browsers show for an <iframe> blob PDF. It MUST be same-origin: a
# cross-origin worker (from esm.sh) is blocked by the same-origin policy
# regardless of CSP, and same-origin also makes the viewer work offline.
# pdfjs-dist ships prebuilt ESM — copy the lib + its matching worker; the
# app sets GlobalWorkerOptions.workerSrc to the /vendor worker URL.
RUN mkdir -p /tmp/pdfjs-install && cd /tmp/pdfjs-install \
    && npm init -y >/dev/null \
    && npm install --no-audit --no-fund --silent \
      --engine-strict --strict-allow-scripts pdfjs-dist@6.2.108 \
    && mkdir -p /app/static/vendor/pdfjs@6.2.108 \
    && cp node_modules/pdfjs-dist/build/pdf.mjs /app/static/vendor/pdfjs@6.2.108/pdf.mjs \
    && cp node_modules/pdfjs-dist/build/pdf.worker.mjs /app/static/vendor/pdfjs@6.2.108/pdf.worker.mjs \
    && ln -s pdfjs@6.2.108 /app/static/vendor/pdfjs \
    && cd / && rm -rf /tmp/pdfjs-install /root/.npm

# KaTeX browser assets — the package's JavaScript is bundled when an app imports
# it, while the shell and app-authored stylesheets still use these public files.
#
# JS: katex.min.js (UMD global) plus a public ESM file for explicit app asset
#     consumers. The shell uses its own on-demand bundle instead.
# CSS: katex.min.css with @font-face rules that reference ./fonts/*.
# Fonts: woff2 only (all modern browsers support woff2; skipping ttf/woff
#        shrinks the layer by ~1.5 MB).
# The stable /vendor/katex/ alias is used by installed app stylesheets.
RUN mkdir -p /tmp/katex-install && cd /tmp/katex-install \
    && npm init -y >/dev/null \
    && npm install --no-audit --no-fund --silent \
      --engine-strict --strict-allow-scripts katex@0.18.4 \
    && mkdir -p /app/static/vendor/katex@0.18.4/fonts \
    && cp node_modules/katex/dist/katex.min.js /app/static/vendor/katex@0.18.4/ \
    && cp node_modules/katex/dist/katex.mjs    /app/static/vendor/katex@0.18.4/ \
    && cp node_modules/katex/dist/katex.min.css /app/static/vendor/katex@0.18.4/ \
    && cp node_modules/katex/dist/fonts/*.woff2 /app/static/vendor/katex@0.18.4/fonts/ \
    && ln -s katex@0.18.4 /app/static/vendor/katex \
    && cd / && rm -rf /tmp/katex-install /root/.npm

# Frontend static files + app-frame served by FastAPI, plus the full source
# tree retained for /data/platform/frontend/node_modules to link at runtime.
COPY --from=frontend /build/dist ./static/
COPY frontend/public/app-frame.html ./app-frame.html
COPY frontend/ ./shell-src/

# Content fingerprint for scripts/test.sh. Stage the exact source layout and
# invoke the host-side helper itself, keeping one authoritative input list and
# failing the build if any declared input was not copied into the image.
COPY scripts/test-image-fingerprint.sh /tmp/test-image-inputs/scripts/test-image-fingerprint.sh
COPY Dockerfile /tmp/test-image-inputs/Dockerfile
COPY backend/app/platform_activation.py /tmp/test-image-inputs/backend/app/platform_activation.py
COPY backend/requirements.txt backend/requirements.lock /tmp/test-image-inputs/backend/
COPY backend/legacy_runtime/ /tmp/test-image-inputs/backend/legacy_runtime/
COPY frontend/package.json frontend/package-lock.json /tmp/test-image-inputs/frontend/
RUN MOBIUS_TEST_IMAGE_INPUT_ROOT=/tmp/test-image-inputs \
      /tmp/test-image-inputs/scripts/test-image-fingerprint.sh \
      > /app/test-image-fingerprint \
    && rm -rf /tmp/test-image-inputs

# /data/platform survives image replacement and can predate the PyJWT migration.
# Install the narrow historical import surface on the standard interpreter path;
# entrypoint intentionally clears PYTHONPATH before starting the platform.
COPY backend/legacy_runtime/jose/ /usr/local/lib/python3.12/site-packages/jose/
COPY backend/legacy_runtime/verify_jose.py /tmp/verify-legacy-jose.py
RUN python /tmp/verify-legacy-jose.py && rm /tmp/verify-legacy-jose.py

# Whole-repo platform seed. /data is a runtime volume, so bake the real clone
# under /app and let entrypoint copy it into /data/platform on first boot. The
# checkout is pinned to BUILD_SHA. Production/self-host builds fail closed when
# that identity is absent: otherwise the baked seed could silently drift from
# the checkout being built. The disposable test compose is the sole explicit
# exception because it mounts and verifies its checkout at runtime.
ARG MOBIUS_PLATFORM_ORIGIN=https://github.com/mobius-os/mobius.git
# A local deployment may fetch an unpushed reviewed commit from a temporary,
# trusted Host-side Git service. Keep that transport separate from the durable
# origin written into the baked checkout so a container never retains an
# ephemeral or host-private URL.
ARG MOBIUS_PLATFORM_FETCH_ORIGIN=
ARG BUILD_SHA=unknown
ARG BUILD_DATE=unknown
ARG RAILWAY_GIT_COMMIT_SHA=unknown
ARG RAILWAY_DEPLOYMENT_ID=unknown
ARG MOBIUS_ALLOW_UNKNOWN_BUILD_SHA=0
ARG MOBIUS_USE_LOCAL_PLATFORM_SOURCE=0
ARG MOBIUS_LOCAL_PLATFORM_SHA=unknown
ARG MOBIUS_LOCAL_PLATFORM_DATE=unknown
RUN set -eux; \
    _build_sha="${BUILD_SHA:-unknown}"; \
    _railway_sha="${RAILWAY_GIT_COMMIT_SHA:-unknown}"; \
    _platform_fetch_origin="${MOBIUS_PLATFORM_FETCH_ORIGIN:-$MOBIUS_PLATFORM_ORIGIN}"; \
    case "${MOBIUS_USE_LOCAL_PLATFORM_SOURCE:-0}" in 0|1) ;; *) \
      echo "FATAL: MOBIUS_USE_LOCAL_PLATFORM_SOURCE must be 0 or 1" >&2; exit 1;; \
    esac; \
    if [ "$_build_sha" = "unknown" ] && [ "$_railway_sha" != "unknown" ] && [ -n "$_railway_sha" ]; then \
      _build_sha="$_railway_sha"; \
    fi; \
    case "${MOBIUS_ALLOW_UNKNOWN_BUILD_SHA:-0}" in 0|1) ;; *) \
      echo "FATAL: MOBIUS_ALLOW_UNKNOWN_BUILD_SHA must be 0 or 1" >&2; exit 1;; \
    esac; \
    if ! printf '%s' "$_build_sha" | grep -Eq '^[0-9a-fA-F]{40}$' \
       && [ "${MOBIUS_ALLOW_UNKNOWN_BUILD_SHA:-0}" != "1" ]; then \
      echo "FATAL: an exact 40-character BUILD_SHA is required; set it to the checkout commit before building" >&2; \
      exit 1; \
    fi; \
    git clone --depth 1 "$_platform_fetch_origin" /app/platform-baked; \
    _build_date="${BUILD_DATE:-unknown}"; \
    if [ "$_build_date" = "unknown" ] || [ -z "$_build_date" ]; then \
      _build_date="$(date -u +%Y-%m-%d)"; \
    fi; \
    if [ "${MOBIUS_USE_LOCAL_PLATFORM_SOURCE:-0}" != "1" ] \
       && printf '%s' "$_build_sha" | grep -Eq '^[0-9a-fA-F]{40}$'; then \
      if git -C /app/platform-baked fetch --depth 1 "$_platform_fetch_origin" "$_build_sha" \
         && git -C /app/platform-baked checkout "$_build_sha"; then \
        :; \
      else \
        echo "FATAL: could not check out BUILD_SHA=$_build_sha" >&2; \
        exit 1; \
      fi; \
    fi; \
    git -C /app/platform-baked remote set-url origin "$MOBIUS_PLATFORM_ORIGIN"; \
    git -C /app/platform-baked config user.name "Mobius Agent"; \
    git -C /app/platform-baked config user.email "agent@mobius"; \
    git -C /app/platform-baked checkout -B main HEAD; \
    git -C /app/platform-baked branch -f upstream HEAD; \
    git -C /app/platform-baked update-ref refs/remotes/origin/main HEAD; \
    git -C /app/platform-baked symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main 2>/dev/null || true; \
    if [ -d /app/platform-baked/frontend ]; then \
      cd /app/platform-baked/frontend; \
      [ -e node_modules ] || [ -L node_modules ] || ln -s /app/shell-src/node_modules node_modules || true; \
      mkdir -p dist; \
      cp -a /app/static/. dist/; \
    fi; \
    if [ "$_build_sha" != "unknown" ]; then \
      git -C /app/platform-baked tag "baked-${_build_sha}" HEAD 2>/dev/null || true; \
    fi; \
    git -C /app/platform-baked rev-parse HEAD > /app/platform-baked/.baked-sha; \
    printf '{"sha":"%s","build_date":"%s","railway_deployment_id":"%s"}\n' \
      "$_build_sha" "$_build_date" "${RAILWAY_DEPLOYMENT_ID:-unknown}" \
      > /app/build-info.json; \
    chown -R root:root /app/platform-baked; \
    chmod -R a+rX,go-w /app/platform-baked

# `railway up` uploads a working tree rather than a Git source deployment, so
# Railway cannot provide RAILWAY_GIT_COMMIT_SHA. Review deployments may still
# need the exact unpushed checkout to seed /data/platform. Keep that exception
# explicit and provenance-bound: normal builds never copy this overlay, while
# the opt-in path requires the caller to name the exact local commit and records
# a synthetic Git commit whose tree is the uploaded source.
COPY . /tmp/mobius-local-platform-source/
RUN set -eux; \
    case "${MOBIUS_USE_LOCAL_PLATFORM_SOURCE:-0}" in 0|1) ;; *) \
      echo "FATAL: MOBIUS_USE_LOCAL_PLATFORM_SOURCE must be 0 or 1" >&2; exit 1;; \
    esac; \
    if [ "${MOBIUS_USE_LOCAL_PLATFORM_SOURCE:-0}" = "1" ]; then \
      printf '%s' "${MOBIUS_LOCAL_PLATFORM_SHA:-unknown}" \
        | grep -Eq '^[0-9a-fA-F]{40}$' \
        || { echo "FATAL: local platform source requires its exact Git SHA" >&2; exit 1; }; \
      cp -a /tmp/mobius-local-platform-source/. /app/platform-baked/; \
      git -C /app/platform-baked add -A; \
      git -C /app/platform-baked commit --allow-empty \
        -m "Seed local review source ${MOBIUS_LOCAL_PLATFORM_SHA}"; \
      git -C /app/platform-baked checkout -B main HEAD; \
      git -C /app/platform-baked branch -f upstream HEAD; \
      git -C /app/platform-baked update-ref refs/remotes/origin/main HEAD; \
      if [ -d /app/platform-baked/frontend ]; then \
        cd /app/platform-baked/frontend; \
        [ -e node_modules ] || [ -L node_modules ] \
          || ln -s /app/shell-src/node_modules node_modules; \
        mkdir -p dist; \
        cp -a /app/static/. dist/; \
      fi; \
      git -C /app/platform-baked rev-parse HEAD > /app/platform-baked/.baked-sha; \
      printf '{"sha":"%s","build_date":"%s","railway_deployment_id":"%s","source":"local-overlay"}\n' \
        "${MOBIUS_LOCAL_PLATFORM_SHA}" \
        "${MOBIUS_LOCAL_PLATFORM_DATE:-unknown}" \
        "${RAILWAY_DEPLOYMENT_ID:-unknown}" > /app/build-info.json; \
      chown -R root:root /app/platform-baked; \
      chmod -R a+rX,go-w /app/platform-baked; \
    fi; \
    rm -rf /tmp/mobius-local-platform-source

# What this image actually contains, so a running container can compare itself
# with the source it serves instead of remembering which update touched what:
# the hash of every image input at the baked checkout (build-info.json
# `image_inputs`), and the package inventories the layers above installed. A
# later `pip`/`apt` install made live in a container shows up as a difference
# from these lists, which is exactly what a replacement would drop.
RUN set -eux; \
    python3 -c 'import json, subprocess, pathlib; \
info = pathlib.Path("/app/build-info.json"); data = json.loads(info.read_text()); \
data["image_inputs"] = json.loads(subprocess.run(["python3", "/app/platform-baked/backend/app/platform_activation.py", "--hashes", "/app/platform-baked"], check=True, capture_output=True, text=True).stdout); \
info.write_text(json.dumps(data, sort_keys=True) + "\n")'; \
    mkdir -p /app/image-inventory; \
    pip freeze --disable-pip-version-check 2>/dev/null | sort > /app/image-inventory/pip.txt; \
    dpkg-query -W -f '${Package}=${Version}\n' | sort > /app/image-inventory/apt.txt; \
    chmod -R a+rX /app/image-inventory /app/build-info.json

# Initialize the runtime volume paths for the non-root agent user.
RUN mkdir -p /data/db /data/apps /data/compiled /data/shared \
    && chown -R mobius:mobius /data

# Runtime source belongs at the tail of the image so normal code changes reuse
# the browser, CLI, Python, vendor, and platform-seed layers above.
COPY backend/app ./app/
COPY backend/scripts ./scripts/
COPY backend/runtime ./runtime/
COPY skill/ ./skill/
COPY protected-files.txt ./protected-files.txt

# The restart supervisor imports no mutable platform code.
RUN chmod -R a-w /app/runtime
RUN chmod +x ./scripts/entrypoint.sh

# Build identity — passed at `docker compose build` time (deploy-prod.sh
# exports BUILD_SHA=$(git rev-parse HEAD)). Declared late above for the
# platform-baked seed layer, after the heavy apt/pip/npm layers, so a per-build
# SHA change invalidates only the trailing layers. Surfaced at GET /api/version
# so a deploy can verify the served backend matches the commit.
ENV BUILD_SHA=${BUILD_SHA}
# BUILD_DATE is the commit date (YYYY-MM-DD) of BUILD_SHA, stamped by
# deploy-prod.sh. Managed Docker builders that do not pass BUILD_DATE use
# /app/build-info.json, written above, so Settings can still show a date.
ENV BUILD_DATE=${BUILD_DATE}
LABEL org.opencontainers.image.source="https://github.com/mobius-os/mobius" \
      org.opencontainers.image.revision="${BUILD_SHA}"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -f http://localhost:8000/api/ready || exit 1

CMD ["./scripts/entrypoint.sh"]
