# Möbius development

Read this before changing Möbius's own source (backend Python, shell internals,
the runtime, the database schema, the constitution, or the skills Möbius ships)
or before preparing that work for the upstream Möbius repositories. Building
mini-apps, theming, and ordinary restarts do not need it: `platform-maintenance`
owns activation and restarts, and `contributing` owns public GitHub actions.

---

## Platform design

- Keep the platform small, general, and composable. Put domain-specific
  complexity in apps; reserve platform complexity for shared primitives and
  hard invariants.
- Every owner runs their own copy of Möbius and may pay for its compute,
  memory, storage, network, and agent usage. Pursue material, evidenced
  efficiency gains as user-facing improvements, but never at the cost of
  behavior, correctness, maintainability, or future flexibility.
- Fix forward inside the platform. Preserve compatibility for owner data and the
  published app contract; otherwise update every caller in the same change
  rather than adding shims, fallbacks, or parallel systems.

## Code invariants

- **Chat persistence.** Every write to `Chat.messages` or `Chat.pending_messages`
  goes through `chat_writer.py` domain commands; never assign either JSON column
  directly. Read that module's docstring before changing chat persistence.
- **Owner-input cards.** Card access is deliberately uniform: any authenticated
  participant that can read a Q&A, Restart, or sealed-input card may answer it
  through that card's ordinary endpoint. The chat access check and exact card
  identity are the authority boundary; do not add a second card-answer role or
  token hierarchy.
- **Restart cards.** `request_restart` binds its card to the current run and
  ready boot and owns at-most-once admission for that exact action; the worker
  admits one drain and restart handoff. A lost response, duplicate click, or
  ambiguous process death must never cause an agent to replay the restart. Any
  later ready boot resumes every linked Restart-card chat independently, and
  each resumed agent verifies whether its changes loaded.
- **Schema migrations.** SQLAlchemy `create_all` creates missing tables but never
  adds a column to an existing one. A new model field needs a new numbered,
  idempotent function at the append-only end of
  `backend/app/schema_migrations.py`; never edit a migration already in the
  ledger. Test both a fresh database and the frozen previous-release upgrade
  fixture.
- **Bootstrap and privileged runtime.** `platform_activation.py` is the source of
  truth for the image-owned bootstrap allowlist. `backend/runtime/identity_broker.py`
  is served privileged source: the frozen `/app/runtime/served_runtime_launcher.py`
  validates it before the served platform starts, and a validation failure
  selects the complete baked platform for that boot.

## Instructions Möbius ships

- `skill/core.md` is always-on for every owner's agent. Keep it to identity,
  activation-independent invariants, safety, privacy, and state boundaries; put
  procedures in skills. Tests pin many of its phrases: move a pinned rule
  together with its test so its protection moves too.
- Seed skills live in `backend/scripts/seed-skills/`; the server reconciles the
  served templates into installed skills at its next restart, so a template edit
  reaches installations on that restart rather than on an image replacement. To
  use an edit here now, write identical bytes to `/data/shared/skills/<name>.md`;
  boot then records the copy as current instead of locally modified.
- Write general guidance generally. A public owner may be non-technical and
  never touch this repository, so Möbius-development specifics belong here, not
  in general skills. State the rule an incident taught, not the incident.
- App-dependent instructions never go in `core.md`: an installed app's
  `system_prompt` fragment is appended by `backend/app/system_prompts.py`
  (`compose_system_prompt`) only while that app is installed, so `core.md`
  describes only what is true with no apps installed.
- `workflows-app.md` is read on every background-work turn, so it describes the
  top effort tier without naming its literal keyword: that word anywhere in the
  turn's context can arm the orchestration tool by itself.

## Validation

The app container has no Docker (see `platform-reference`), which is not a
reason to hide or skip validation. Inside Möbius:

- run `scripts/test.sh --fast` for the cheap hermetic contracts and
  `scripts/wt-pytest.sh <focused tests>` for the changed behavior;
- if the worktree runner says the checkout lock differs from the image runtime,
  treat those results as useful but not dependency-authoritative and use a
  lock-matched environment or hosted checks for that contract; and
- for concurrency or ordering, persistence, auth or security, migrations,
  provider protocols, dependency/runtime changes, or broad cross-cutting work,
  explicitly recommend opening or updating a **Draft PR** through Contribute,
  letting its hosted checks run, and using **Request review** once they pass.

`scripts/test.sh --backend` remains for a Docker-capable contributor host. The
hosted pull-request checks give full-suite evidence for the exact reviewed
commit without merging it, and the merge queue remains the unconditional
authoritative gate. To diagnose failed checks, run
`scripts/ci-failures.sh <owner/repo> <pr-number|run-id>`.

## Platform internals behind the app skills

The app-facing skills state the rule; these are the platform reasons and
maintainer steps behind them.

- **Service origins.** A separate service is a platform integration, never part
  of an ordinary mini-app. The shared service gateway is one origin, configured
  once, with one path per explicitly enabled owner-trusted service plus a
  shell-owned direct adapter. It isolates that group from the shell but not
  services from one another (paths do not partition localStorage or same-origin
  fetch), so cookies stay host-only and path-scoped and the gateway exposes only
  enabled prefixes. A mutually untrusted service or an independent PWA needs a
  dedicated distinct origin. Standalone `/apps/<slug>/` launches use a trusted
  signed outer host that owns authentication, manifest/offline identity,
  installation, and error chrome around the same opaque `AppCanvas` frame.
- **`/app-embeds/` lane.** It answers `Access-Control-Allow-Origin: null`
  without credentials so null-origin subresource loaders work; never broaden
  owner APIs or add credentials to it. Wrappers must not prefetch the entry
  document: a null-origin wrapper fetch duplicates the download and has enabled
  cache-poisoning probes.
- **Static-app packaging.** Legacy CRA chunk hashes can rotate across build
  directories because license/source-map references embed their own emitted
  filename; normalize only that name when checking semantic equality rather
  than rewriting the bundler.
- **Bundled app libraries.** Adding a supported import means adding it to
  `frontend/package.json` and `BUNDLED_RUNTIME_LIBS`, then validating the
  resulting app bundle size against the parent broker's 8 MiB transfer cap.
- **App signals.** Signals are `app_signal` activity records; during the
  migration Reflection also reads legacy `signals.jsonl` files written by older
  cached runtimes.
- **Scheduled jobs.** FastAPI lifespan parses each app's effective cadence and
  job, validates the live app/source tree, and rewrites the crontab entry
  through `app-job-runner.py` before cron starts; boot never executes app-owned
  `init-cron.sh`.
- **Shell zoom.** The paired `shellViewportZoom` tests enforce both the shell
  scale lock and the app frame's freedom to zoom locally, so a policy change
  must be explicit.
- **Component catalog.** `app-component-shapes.md` blocks are copied, not
  imported. When roughly three apps carry the same `mobius-ui:` fenced block
  (same role and structure), it has earned extraction into a real shared
  library; `grep -rl 'mobius-ui:'` finds the copies.
- **Authenticated capture helper.** Do not add `screenshot --if-changed` to it:
  unchanged captures omit their output, while the helper promises a freshly
  verified, atomic image.

## Deployment and container replacement

- **Managed Recovery.** On Möbius Launch, **Recovery** on the deployment card
  creates a separate temporary worker on demand and pins Railway SSH to the
  exact live Möbius service instance. Commands reach that container as root;
  the worker stays outside the container and is deleted when the session
  finishes or expires.
- **Replacement paths.** On a self-hosted host, use `scripts/deploy-prod.sh`
  for a checkout/image deployment or the Settings replacement controller
  documented in `scripts/CONTAINER-REBUILD.md` for an official-image refresh.
  Both open a root-owned cutover challenge, ask the running worker to park and
  nonce-bind exact active chat runs, then let Docker perform the only stop. A
  failed replacement re-arms the same receipt for one rollback boot. A raw
  `docker compose up --force-recreate`, `docker restart`, or direct replacement
  bypasses that handoff and falls back to manual Resume after boot. Unexpected
  crashes stay manual by design; never make arbitrary boots look planned.
- **Cutover helper.** The running image must already contain the frozen
  `external-cutover-v1` helper. The first upgrade from an older image cannot
  manufacture that root capability; `deploy-prod.sh` reports when it uses the
  legacy owner-presence gate, and that one upgrade installs the helper.
