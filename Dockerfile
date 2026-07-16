# Single-container Möbius image.
#
# Builds the frontend, installs the backend + CLI tools, and serves
# everything from one FastAPI process.  Works on VPS, Railway, PikaPods.

# -- Stage 1: build the frontend --------------------------------------
FROM node:22-slim AS frontend

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
#
# Background-agent jobs run as the unprivileged mobius user, but bwrap must
# create mount/PID namespaces inside the outer Docker container. Debian's
# audited setuid mode retains only bwrap's small setup capability set and drops
# it before execing the job. docker-compose.yml supplies the three required
# capabilities absent from Docker's default bounding set.
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron curl ca-certificates git sudo procps bubblewrap \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 \
    fonts-liberation fonts-noto-color-emoji \
    && npm install -g esbuild@0.28.1 \
    && npm install -g @anthropic-ai/claude-code@2.1.207 \
    && npm install -g @openai/codex@0.144.4 \
    && npm install -g agent-browser@0.31.1 \
    && agent-browser install \
    && mv /root/.agent-browser /opt/agent-browser \
    && chmod 4755 /usr/bin/bwrap \
    && test "$(stat -c '%a' /usr/bin/bwrap)" = 4755 \
    && git_version="$(git --version | awk '{print $3}')" \
    && [ "$(printf '%s\n' "2.38" "$git_version" | sort -V | head -n1)" = "2.38" ] \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# tectonic is a server-side subprocess; CSP connect-src 'self' applies only to
# browser fetches from the mini-app iframe, not OS-level subprocesses — tectonic's
# package fetches (from Tectonic's bundle server) are unrestricted at the OS level.
# Placed after the apt-get layer so a tectonic version bump doesn't bust the apt cache.
ARG TECTONIC_VERSION=0.16.9
RUN curl -fsSL "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
    | tar xz -C /usr/local/bin/ tectonic && chmod +x /usr/local/bin/tectonic && tectonic --version

# GitHub CLI: the agent's Contribute flow opens PRs/issues upstream through
# `gh` (a server-side subprocess, so CSP connect-src 'self' — which governs
# only mini-app iframe fetches — does not apply). Pinned and sha256-verified
# against the release's own checksums file, fetched at build time; a mismatch
# fails the build. Built for the image arch (amd64|arm64); only the single
# `gh` binary is installed, docs/man pages are dropped. Placed after the apt
# layer so a gh bump doesn't bust the apt cache.
ARG GH_CLI_VERSION=2.96.0
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

# Share the agent-browser install between root and mobius via symlinks.
# The mobius user is created further down; we chown the shared dir to
# mobius:mobius after that, so mobius can write session sockets/locks
# as the owner without needing world-write on the Chromium binaries.
# (root still has access because root always does.)
RUN ln -s /opt/agent-browser /root/.agent-browser

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

# Keep the dependency-defining Dockerfile in the image so the test wrapper can
# prove that a prebuilt test image matches the checkout before starting a long
# suite.  Application source is bind-mounted for tests; this manifest covers
# the inputs whose effects are baked into the image and cannot be overridden by
# that mount.
COPY Dockerfile ./test-image-inputs/Dockerfile

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# openai-codex Python SDK: installed in a separate step because its
# upstream pyproject pins openai-codex-cli-bin==0.137.0a4. Install
# --no-deps so Docker keeps the exact runtime pin below explicit.
# Pinned to commit SHA (not tag) for full reproducibility — tags are
# mutable on GitHub. SHA corresponds to refs/tags/rust-v0.144.4 as of
# 2026-07-14. The SDK exposes the request bridge as a public
# `approval_handler` constructor argument on
# `openai_codex.client.CodexClient`; `AsyncCodex` still does not forward
# it, so codex_sdk_runner.py installs the handler on the wrapped sync
# client's `_approval_handler`.
RUN pip install --no-cache-dir --no-deps \
      'openai-codex @ git+https://github.com/openai/codex.git@8c68d4c87dc54d38861f5114e920c3de2efa5876#subdirectory=sdk/python' \
    && pip install --no-cache-dir 'openai-codex-cli-bin==0.137.0a4'

# Capture each installed agent CLI's npm publish date into a small JSON the
# Settings row reads (routes/settings._cli_release_dates), keyed by the
# version actually installed above. Done at build time so a CLI pin bump
# refreshes the date automatically — no hand-maintained map, no test to
# satisfy. Best effort: if the npm registry is unreachable the file is left
# empty and the Settings row simply shows the bare version, never an error.
RUN node -e "const cp=require('child_process'),fs=require('fs');\
const want=['@anthropic-ai/claude-code','@openai/codex'];\
let installed={};\
try{installed=(JSON.parse(cp.execSync('npm ls -g --depth=0 --json',{stdio:['ignore','pipe','ignore']}).toString()).dependencies)||{};}catch(e){}\
const out={};\
for(const name of want){const v=installed[name]&&installed[name].version;if(!v)continue;\
try{const t=JSON.parse(cp.execSync('npm view '+name+'@'+v+' time --json',{stdio:['ignore','pipe','ignore']}).toString());if(t&&t[v])out[v]=String(t[v]).slice(0,10);}catch(e){}}\
fs.writeFileSync('/app/cli-release-dates.json',JSON.stringify(out));\
console.log('cli-release-dates.json:',JSON.stringify(out));" \
    || echo '{}' > /app/cli-release-dates.json

COPY backend/app ./app/
COPY backend/scripts ./scripts/
COPY skill/ ./skill/
COPY core-apps/ ./core-apps/
COPY protected-files.txt ./protected-files.txt

# Frozen recovery floor (recoveryd) — the Tier-1 recovery system that runs
# in its OWN container (same image, command `python3 -P /app/recovery/
# recoveryd.py`). It imports ZERO app.* code and survives a fully-broken
# platform. Baked root-owned + chmod a-w so even root can't modify it in
# place and the agent (mobius) cannot touch it; recoveryd self-checks this
# at startup and refuses to run if any file is writable. This is the floor
# of the recovery story, distinct from the platform-baked backend floor.
COPY backend/recovery ./recovery/
RUN chmod -R a-w /app/recovery

# Frontend static files + app-frame served by FastAPI.
COPY --from=frontend /build/dist ./static/
COPY frontend/public/app-frame.html ./app-frame.html

# Self-hosted vendor libs for mini-app import maps. Pinned via npm
# install at image build time, served same-origin under /vendor/ with
# a long cache. Eliminates the cold-load esm.sh waterfall for three.js
# (cold-load saves 1-3s on any 3D app). Pinned to match the version
# we previously served via CDN.
# Copy the WHOLE build/ dir, not just three.module.js: since 0.163 the
# library is split (three.module.js does `export * from './three.core.js'`),
# so a single-file copy leaves three.core.js missing. Requests for it then
# fall through to the SPA HTML fallback (200 text/html), and strict module
# MIME checking rejects it as "failed to load dynamic module".
# The bare `/vendor/three/` is a maintained compat alias (relative
# symlink) — the seed documents that path, and PWAs that cached an older
# app-frame whose importmap pointed at the unversioned URL still request
# it. Without the alias those requests 404 → SPA HTML → spinner-forever.
RUN mkdir -p /tmp/vendor-install && cd /tmp/vendor-install \
    && npm init -y >/dev/null \
    && npm install --no-audit --no-fund --silent three@0.185.1 \
    && mkdir -p /app/static/vendor/three@0.185.1/addons \
    && cp -r node_modules/three/build/. /app/static/vendor/three@0.185.1/ \
    && cp -r node_modules/three/examples/jsm/. /app/static/vendor/three@0.185.1/addons/ \
    && ln -s three@0.185.1 /app/static/vendor/three \
    && cd / && rm -rf /tmp/vendor-install

# Self-hosted React for the mini-app import map — same rationale as
# three.js above, but load-bearing for OFFLINE rather than just cold-load
# speed. Mini-apps import react/react-dom via app-frame.html's (and
# standalone.py's) import map. Serving these from esm.sh meant offline-
# capable apps depended on a third-party CDN whose React entry is a
# multi-hop re-export chain (react@19.2.7 -> /react@19.2.7/es2022/
# react.bundle.mjs → …; react-dom pulls scheduler + sub-chunks as separate
# URLs). The service worker cache-firsts esm.sh, but only opportunistically
# per URL, so a single uncached hop (or a version bump invalidating the
# prior cache) left an offline app blank on its top-level
# `import 'react-dom/client'`. Serving React same-origin under /vendor
# removes the third-party dependency entirely and makes offline
# deterministic.
#
# The build (backend/scripts/build-react-vendor.mjs) bundles all four
# import-map entries into ONE core.mjs (so React is included exactly once)
# and emits tiny facades that re-export it — every specifier resolves to a
# single shared React instance. Bundling each entry separately with
# `--external:react` does NOT work: react/react-dom are CommonJS, so
# esbuild emits a throwing `__require("react")` shim that breaks every
# mini-app in the browser. See the script header for the full rationale.
COPY backend/scripts/build-react-vendor.mjs /tmp/build-react-vendor.mjs
RUN mkdir -p /tmp/react-install && cd /tmp/react-install \
    && npm init -y >/dev/null \
    && npm install --no-audit --no-fund --silent react@19.2.7 react-dom@19.2.7 \
    && mkdir -p /app/static/vendor/react@19.2.7 \
    && node /tmp/build-react-vendor.mjs /tmp/react-install \
         /app/static/vendor/react@19.2.7 "$(command -v esbuild)" \
    && cd / && rm -rf /tmp/react-install /tmp/build-react-vendor.mjs

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
    && npm install --no-audit --no-fund --silent pdfjs-dist@4.10.38 \
    && mkdir -p /app/static/vendor/pdfjs@4.10.38 \
    && cp node_modules/pdfjs-dist/build/pdf.mjs /app/static/vendor/pdfjs@4.10.38/pdf.mjs \
    && cp node_modules/pdfjs-dist/build/pdf.worker.mjs /app/static/vendor/pdfjs@4.10.38/pdf.worker.mjs \
    && ln -s pdfjs@4.10.38 /app/static/vendor/pdfjs \
    && cd / && rm -rf /tmp/pdfjs-install

# Self-hosted CodeMirror 6 for the mini-app import map — same OFFLINE
# rationale as React above. The Notes / LaTeX / Editor / Web Studio apps
# import @codemirror/* + @lezer/highlight + the `codemirror` meta-package
# via the import map. Served from esm.sh, those were static top-level
# imports an offline (or flaky-network) app had to fetch from a third-party
# CDN before any app code ran — a single uncached hop took the WHOLE app
# down (this is the "LaTeX PDF won't load / struggling" report: CodeMirror's
# failed fetch rejected the app's dynamic import and the PDF viewer never
# mounted). The build (build-codemirror-vendor.mjs) bundles every import-map
# specifier into ONE core.mjs so the shared cores (@codemirror/state,
# @lezer/common) exist exactly once — CodeMirror requires a single instance —
# then emits facades that re-export it. See the script header for rationale.
COPY backend/scripts/build-codemirror-vendor.mjs /tmp/build-codemirror-vendor.mjs
RUN mkdir -p /tmp/cm-install && cd /tmp/cm-install \
    && npm init -y >/dev/null \
    && npm install --no-audit --no-fund --silent \
         codemirror@6.0.2 @codemirror/state@6.7.0 @codemirror/view@6.43.4 \
         @codemirror/commands@6.10.4 @codemirror/language@6.12.4 \
         @codemirror/lang-markdown@6.5.0 @lezer/highlight@1.2.3 \
    && mkdir -p /app/static/vendor/codemirror@6 \
    && node /tmp/build-codemirror-vendor.mjs /tmp/cm-install \
         /app/static/vendor/codemirror@6 "$(command -v esbuild)" \
    && cd / && rm -rf /tmp/cm-install /tmp/build-codemirror-vendor.mjs

# KaTeX — self-hosted for both the shell (window.katex via <script> in
# index.html) and mini-apps (ES module import via the app-frame.html
# importmap). Eliminates the last two third-party CDN dependencies
# (cdn.jsdelivr.net for the shell, esm.sh for mini-apps).
#
# JS: katex.min.js (UMD global, loaded as window.katex by the shell) +
#     katex.mjs (ESM, imported by mini-apps via importmap).
# CSS: katex.min.css with @font-face rules that reference ./fonts/*.
# Fonts: woff2 only (all modern browsers support woff2; skipping ttf/woff
#        shrinks the layer by ~1.5 MB).
# A bare /vendor/katex/ symlink acts as a stable unversioned alias so
# any cached standalone PWA app-frame that referenced the old
# esm.sh-backed katex still resolves after the upgrade.
RUN mkdir -p /tmp/katex-install && cd /tmp/katex-install \
    && npm init -y >/dev/null \
    && npm install --no-audit --no-fund --silent katex@0.17.0 \
    && mkdir -p /app/static/vendor/katex@0.17.0/fonts \
    && cp node_modules/katex/dist/katex.min.js /app/static/vendor/katex@0.17.0/ \
    && cp node_modules/katex/dist/katex.mjs    /app/static/vendor/katex@0.17.0/ \
    && cp node_modules/katex/dist/katex.min.css /app/static/vendor/katex@0.17.0/ \
    && cp node_modules/katex/dist/fonts/*.woff2 /app/static/vendor/katex@0.17.0/fonts/ \
    && ln -s katex@0.17.0 /app/static/vendor/katex \
    && cd / && rm -rf /tmp/katex-install

# recharts — self-hosted for the mini-app import map (P1-C). Mini-apps that
# render charts import recharts; serving from esm.sh meant an offline-capable
# chart app depended on a third-party CDN fetch. We self-host same-origin so
# offline is deterministic. recharts externalises react/react-dom and maps them
# to the already-vendored /vendor/react entries in the importmap.
# The build (build-recharts-vendor.mjs) bundles only the exported components
# listed in the old esm.sh ?exports= filter so the bundle is not inflated.
COPY backend/scripts/build-recharts-vendor.mjs /tmp/build-recharts-vendor.mjs
RUN mkdir -p /tmp/recharts-install && cd /tmp/recharts-install \
    && npm init -y >/dev/null \
    && npm install --no-audit --no-fund --silent recharts@2.15.4 react@19.2.7 react-dom@19.2.7 \
    && mkdir -p /app/static/vendor/recharts@2.15.4 \
    && node /tmp/build-recharts-vendor.mjs /tmp/recharts-install \
         /app/static/vendor/recharts@2.15.4 "$(command -v esbuild)" \
    && cd / && rm -rf /tmp/recharts-install /tmp/build-recharts-vendor.mjs

# date-fns — self-hosted for the mini-app import map (P1-C). date-fns is a
# pure-JS date utility library with no peer deps; a simple bundle suffices.
COPY backend/scripts/build-date-fns-vendor.mjs /tmp/build-date-fns-vendor.mjs
RUN mkdir -p /tmp/datefns-install && cd /tmp/datefns-install \
    && npm init -y >/dev/null \
    && npm install --no-audit --no-fund --silent date-fns@4.3.0 \
    && mkdir -p /app/static/vendor/date-fns@4.3.0 \
    && node /tmp/build-date-fns-vendor.mjs /tmp/datefns-install \
         /app/static/vendor/date-fns@4.3.0 "$(command -v esbuild)" \
    && cd / && rm -rf /tmp/datefns-install /tmp/build-date-fns-vendor.mjs

# d3-geo — self-hosted for the mini-app import map. The Atlas globe imports
# d3-geo; serving from esm.sh meant an offline-capable globe app depended on a
# third-party CDN fetch before the projection could render. Pure-JS, no peer
# deps; a simple bundle suffices (same shape as date-fns).
COPY backend/scripts/build-d3-geo-vendor.mjs /tmp/build-d3-geo-vendor.mjs
RUN mkdir -p /tmp/d3geo-install && cd /tmp/d3geo-install \
    && npm init -y >/dev/null \
    && npm install --no-audit --no-fund --silent d3-geo@3.1.1 \
    && mkdir -p /app/static/vendor/d3-geo@3.1.1 \
    && node /tmp/build-d3-geo-vendor.mjs /tmp/d3geo-install \
         /app/static/vendor/d3-geo@3.1.1 "$(command -v esbuild)" \
    && cd / && rm -rf /tmp/d3geo-install /tmp/build-d3-geo-vendor.mjs

# marked — self-hosted for the mini-app import map. The Notes app imports marked
# to render markdown note-card previews; serving from esm.sh meant offline
# previews depended on a third-party CDN. Pure-JS, no peer deps.
COPY backend/scripts/build-marked-vendor.mjs /tmp/build-marked-vendor.mjs
RUN mkdir -p /tmp/marked-install && cd /tmp/marked-install \
    && npm init -y >/dev/null \
    && npm install --no-audit --no-fund --silent marked@17.0.6 \
    && mkdir -p /app/static/vendor/marked@17.0.6 \
    && node /tmp/build-marked-vendor.mjs /tmp/marked-install \
         /app/static/vendor/marked@17.0.6 "$(command -v esbuild)" \
    && cd / && rm -rf /tmp/marked-install /tmp/build-marked-vendor.mjs

# DOMPurify — self-hosted for the mini-app import map. The Notes preview
# sanitizes markdown-derived HTML with DOMPurify before injecting it; serving
# from esm.sh meant sanitization (on the render path) depended on a third-party
# CDN. Pure-JS, no peer deps; binds to the DOM at runtime in the browser frame.
COPY backend/scripts/build-dompurify-vendor.mjs /tmp/build-dompurify-vendor.mjs
RUN mkdir -p /tmp/dompurify-install && cd /tmp/dompurify-install \
    && npm init -y >/dev/null \
    && npm install --no-audit --no-fund --silent dompurify@3.4.11 \
    && mkdir -p /app/static/vendor/dompurify@3.4.11 \
    && node /tmp/build-dompurify-vendor.mjs /tmp/dompurify-install \
         /app/static/vendor/dompurify@3.4.11 "$(command -v esbuild)" \
    && cd / && rm -rf /tmp/dompurify-install /tmp/build-dompurify-vendor.mjs

# Full frontend source with installed node_modules. /app/shell-src is kept so
# /data/platform/frontend/node_modules can symlink to it at runtime.
COPY frontend/ ./shell-src/
RUN cd ./shell-src && npm ci --ignore-scripts 2>/dev/null && rm -rf .vite

# Content fingerprint for scripts/test.sh.  Hash file contents (not paths) in
# the same stable order as scripts/test-image-fingerprint.sh so host and image
# layouts may differ without changing the result.
RUN { \
      sha256sum /app/test-image-inputs/Dockerfile | cut -d' ' -f1; \
      sha256sum /app/requirements.txt | cut -d' ' -f1; \
      sha256sum /app/shell-src/package.json | cut -d' ' -f1; \
      sha256sum /app/shell-src/package-lock.json | cut -d' ' -f1; \
      for f in \
        build-react-vendor.mjs build-codemirror-vendor.mjs \
        build-recharts-vendor.mjs build-date-fns-vendor.mjs \
        build-d3-geo-vendor.mjs build-marked-vendor.mjs \
        build-dompurify-vendor.mjs; do \
          sha256sum "/app/scripts/$f" | cut -d' ' -f1; \
      done; \
    } | sha256sum | cut -d' ' -f1 > /app/test-image-fingerprint

# Whole-repo platform seed. /data is a runtime volume, so bake the real clone
# under /app and let entrypoint copy it into /data/platform on first boot. The
# checkout is pinned when BUILD_SHA is a real commit; local builds with
# BUILD_SHA unset/unknown keep the default branch tip.
ARG MOBIUS_PLATFORM_ORIGIN=https://github.com/mobius-os/mobius.git
ARG BUILD_SHA=unknown
ARG BUILD_DATE=unknown
ARG RAILWAY_GIT_COMMIT_SHA=unknown
ARG RAILWAY_DEPLOYMENT_ID=unknown
RUN set -eux; \
    git clone --depth 1 "$MOBIUS_PLATFORM_ORIGIN" /app/platform-baked; \
    _build_sha="${BUILD_SHA:-unknown}"; \
    _railway_sha="${RAILWAY_GIT_COMMIT_SHA:-unknown}"; \
    if [ "$_build_sha" = "unknown" ] && [ "$_railway_sha" != "unknown" ] && [ -n "$_railway_sha" ]; then \
      _build_sha="$_railway_sha"; \
    fi; \
    _build_date="${BUILD_DATE:-unknown}"; \
    if [ "$_build_date" = "unknown" ] || [ -z "$_build_date" ]; then \
      _build_date="$(date -u +%Y-%m-%d)"; \
    fi; \
    if printf '%s' "$_build_sha" | grep -Eq '^[0-9a-fA-F]{40}$'; then \
      if git -C /app/platform-baked fetch --depth 1 origin "$_build_sha" \
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

# Create a non-root user so the agent can use --dangerously-skip-permissions.
RUN useradd -m -s /bin/bash mobius \
    && mkdir -p /data/db /data/apps /data/compiled /data/shared \
    && chown -R mobius:mobius /data \
    && ln -s /opt/agent-browser /home/mobius/.agent-browser \
    && chown -R mobius:mobius /opt/agent-browser

# apt scoped-sudo (owner spec): mobius (the in-product agent) may install/remove
# OS packages but NOT have full root, so it can't break the recovery floor or
# core system files. Scoped to apt/dpkg only; validated by visudo. NOT a hard
# sandbox (apt maintainer scripts run as root) — the real safety is that the
# recovery runtime depends on ZERO apt-installed packages, so a bad package can
# never compromise it.
RUN printf 'mobius ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/apt, /usr/bin/dpkg\n' \
      > /etc/sudoers.d/mobius-apt \
    && chmod 440 /etc/sudoers.d/mobius-apt \
    && visudo -cf /etc/sudoers.d/mobius-apt

COPY backend/scripts/entrypoint.sh ./scripts/entrypoint.sh
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

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["./scripts/entrypoint.sh"]
