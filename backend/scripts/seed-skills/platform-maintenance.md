# Platform maintenance

How backend edits load, how to make them durable, where platform files live,
and how to repair a broken shell. Read this before editing backend Python,
changing the image userspace, or asking for a server restart.

---

## Authority and the external repair boundary

The normal Möbius agent has passwordless full root inside its own container by
default. Check the operator-controlled capability before root work:

```bash
sudo -n true
```

Use `sudo` deliberately when the task needs it. If that check fails, stop: the
operator disabled root with `MOBIUS_AGENT_SUDO=0`, and bypassing that choice is
not part of the agent's authority. Package changes made only in a running
container are ephemeral; declare them in the Dockerfile or lockfile and ship a
new image when they must survive recreation.

### Container root stops at the container boundary

The normal Möbius app container intentionally has no Docker daemon or CLI and
does not mount the host's Docker socket. `sudo` grants root inside that
container only. Treat Docker's absence there as an expected trust boundary,
not a broken dependency. Do not install a Docker CLI, start Docker-in-Docker,
or request a host-socket mount for agent tests: the CLI alone has no daemon,
while a socket or privileged daemon would cross into operator-owned host
authority and would not work consistently on managed deployments.

This boundary is not a reason to hide or skip validation. Inside Möbius:

- run `scripts/test.sh --fast` for the cheap hermetic contracts and
  `scripts/wt-pytest.sh <focused tests>` for the changed behavior;
- if the worktree runner says the checkout lock differs from the image
  runtime, treat those results as useful but not dependency-authoritative and
  use a lock-matched environment or hosted checks for that contract; and
- for concurrency or ordering, persistence, auth or security, migrations,
  provider protocols, dependency/runtime changes, or broad cross-cutting work,
  explicitly recommend opening or updating a **Draft PR** through Contribute,
  letting its hosted checks run, and using **Request review** once they pass.

`scripts/test.sh --backend` remains for a Docker-capable contributor host. The
hosted pull-request checks give full-suite evidence for the exact reviewed
commit without merging it, and the merge queue remains the unconditional
authoritative gate.

Recovery is not a daemon, listener, alternate boot mode, or second process
inside Möbius. If the interface is unavailable, ask the partner to open
**Recovery** from the deployment card in Möbius Launch. The launcher creates a
separate temporary worker on demand and pins Railway SSH to the exact live
Möbius service instance. Commands reach that container as root, while the
worker itself remains outside the container and is deleted when the session
finishes or expires. Never try to start or repair an in-container Recovery
service; none should exist.

Self-hosted operators use the authority they already own:

```bash
docker compose exec -u 0 app bash
```

That also attaches to the normal live container; it does not select a Recovery
boot profile.

### Host-owned container replacement

A container recreation is not an ordinary server restart. On a self-hosted
Host, use one of the two owning paths:

- `scripts/deploy-prod.sh` for a checkout/image deployment; or
- the installed Settings replacement controller documented in
  `scripts/CONTAINER-REBUILD.md` for an official-image refresh.

Both paths open a root-owned cutover challenge, ask the still-running worker to
park and nonce-bind exact active chat runs, then let Docker perform the only
stop. A failed replacement explicitly re-arms the same receipt for one rollback
boot. This is why a raw `docker compose up --force-recreate`, `docker restart`,
or direct container replacement is not an equivalent shortcut: it bypasses the
handoff and intentionally falls back to conservative manual Resume after boot.
Unexpected crashes remain manual by design; never make arbitrary boots look
planned merely to hide recovery prompts.

The running image must already contain the frozen `external-cutover-v1` helper.
The first upgrade from an older image cannot manufacture that root capability;
`deploy-prod.sh` says when it is using the legacy owner-presence gate, and that
one upgrade installs the helper for later replacements.

---

## Choose the smallest activation action

Do not infer activation from the fact that a task touched platform source.
Review the exact changed paths and use the smallest matching action:

| Changed surface | Smallest activation |
|---|---|
| `/data/shared/theme.css` | Hot-reloads after the theme notification. No build or restart. |
| Mini-app source under `/data/apps/<slug>/` | Run `apply_app.py`; the compiled app live-swaps. No shell rebuild or server restart. |
| `frontend/src/` and other frontend build inputs | The watcher rebuilds the served shell, then `shell_apply_now` applies it. A normal save triggers this automatically; source arriving through Git needs a changed frontend file touched. No server restart. |
| `backend/app/*.py` | After compile checks, tests, and commit, one server restart loads the settled backend revision. |
| `skill/core.md` | A server restart refreshes the cached constitution for new agent sessions only; existing sessions keep their immutable prompt snapshot. Unless new sessions need the rule immediately, leave it pending for the next Restart-card choice. |
| `backend/scripts/entrypoint.sh`, the exact `/app/scripts/*` bootstrap files it invokes, `backend/scripts/seed-skills/`, or `backend/runtime/` | Image-owned. Batch and test the change, then leave one image replacement pending; never rebuild between iterations. `platform_activation.py` is the source of truth for the exact bootstrap allowlist. |
| `backend/runtime/identity_broker.py` | Served privileged source. The frozen `/app/runtime/served_runtime_launcher.py` validates it before the served platform starts and launches it from `/data/platform`; validation failure selects the complete baked platform for that boot. One server restart activates a valid broker edit. The other `backend/runtime` files above stay image-owned. |
| `backend/scripts/pm-commit` | One server restart refreshes the installed launcher from the served checkout; no image rebuild. |
| Other `backend/scripts/`, tests, docs, and shared skill content | Takes effect on its next invocation or read. No server restart or image rebuild. An agent that already read old instructions cannot be rewritten in place. |
| A package needed by the current task | Install it into the running container first when safe. A new process can use it immediately; restart only when the already-running backend must load it. |
| `backend/requirements.txt`, lockfiles, `frontend/package.json`, or `Dockerfile` | These declarations make a live install reproducible after container replacement; they do not activate it and do not require an immediate rebuild. |

### Dependencies — live first, durable second

1. Run `sudo -n true` once. Use `sudo` for apt or other system/global install
   locations, not for ordinary writes under `/data`, which must remain
   partner-owned.
2. Use the owning package manager to install only the named dependency, pinned
   to the intended version when possible. Do not run blanket upgrades or
   ad-hoc remote installers. Put Python packages in the active interpreter and
   Node packages in the active runtime dependency tree; a global Node install
   does not satisfy a project's imports. Verify the import, executable, or
   version. New processes can use the install immediately. A long-running
   backend needs a Restart-card-selected server restart when it must load the
   new package itself.
3. If shipped behavior depends on the package, record the same resolution in
   the owning manifest and lockfile, plus the Dockerfile only when image wiring
   is needed. These declarations are durability metadata, not an activation
   action: they let a future image/container replacement restore the live
   install.
4. Treat a container rebuild as a last resort, not an ordinary closeout step.
   Require it now only when the change genuinely cannot activate live, or when
   the partner explicitly asks to validate the image.

### Activation preflight — before any restart question

1. List the exact paths changed for the current task and whether a later
   activation action has already loaded them.
2. Map every path through the table above. For routine activation, do not
   propose a restart when nothing needs one, or substitute it for hot reload,
   app apply, shell rebuild, live dependency install, or container rebuild.
   This is advice against unnecessary restarts, not a limit on the Restart
   card: when the partner explicitly requests a restart or a card-flow test,
   the card can be created even if there are no pending changed paths. Do not
   invent a source change to make the card available.
3. Batch every restart-requiring edit, test it, and commit it before asking.
   Do not restart between iterations or request a speculative restart.
4. For activation, explain the exact change that remains inactive and why a
   server restart loads it. For an explicit restart without pending changes,
   say that no source activation is pending. The platform supplies the card's
   question text. For a constitution-only change, ordinarily leave it pending
   unless the partner needs the rule in new sessions now.

---

## Calling the Möbius API — use `mapi`

`mapi` is the standard way for an agent to call this instance's backend. It is
`curl` with `$API_BASE_URL` and the owner `Authorization: Bearer $AGENT_TOKEN`
already filled in, and it only accepts `/api/...` targets so owner auth can
never be forwarded to an external URL. `mapi /api/apps/` is exactly:

```bash
curl -s "$API_BASE_URL/api/apps/" -H "Authorization: Bearer $AGENT_TOKEN"
```

Supported safe curl options pass through, so ordinary recipes translate by
dropping the base URL and the auth header. Options that can retarget the
authenticated request—such as redirects, proxies, curl config files, alternate
destinations, or replacement Host headers—are refused:

```bash
mapi /api/apps/ | python3 -m json.tool
mapi -X PATCH /api/apps/<app-id> -H 'Content-Type: application/json' -d '{...}'
mapi -X PUT /api/storage/shared/theme.css \
  -H 'Content-Type: text/css' --data-binary @/data/shared/theme.css
```

Notes:
- `mapi` reflects the AGENT's owner token. A background app job only has
  `$APP_TOKEN`, so app-job scripts keep plain `curl -H "Authorization: Bearer $APP_TOKEN" ...`.
- A successful write often returns **204 No Content**: `mapi` then prints
  nothing. That silence is success, not failure — verify with a follow-up
  `GET`, or show the status with
  `mapi -o /dev/null -w '%{http_code}' -X PUT /api/... -d '...'`.
- Use the exact documented path **including its trailing slash** (for example
  `/api/apps/`). Möbius routes do not redirect slash-less variants: the
  slash-less form is a plain 404, not a redirect curl could follow.
- Raw `curl` remains correct for anything that is not this instance's `/api`.
- Prefer `mapi` everywhere else, including new skills and examples.

---

### Debugging the platform runtime

Use the existing authenticated diagnostics instead of adding temporary routes:

```bash
mapi /api/debug/status | python3 -m json.tool
mapi "/api/debug/memory?process_limit=20&allocation_limit=25" | python3 -m json.tool
mapi "/api/debug/logs?lines=50&chat_id=$CHAT_ID" | python3 -m json.tool
```

(`mapi` fills in auth + base URL.)

`status` is the cheap health view and deliberately omits variable-sized runtime
payload totals; its `runtime_memory.payload_sizing` field points to the detailed
`memory` report. Query flags on `status` do not enable payload sizing. Use
`memory` for processes, maps, runtime-owner payloads, GC diagnostics, and
optional allocation tracing. Add `deep=true` only when a GC object-type walk is
actually needed.

---

## Backend edits — restart to load, hand off persistence

The live backend is `/data/platform/backend/app/*.py`. Edits there take effect
on the **next uvicorn restart** and survive container restarts and image rebuilds
because `/data/platform` is the persistent served clone. The baked
`/app/platform-baked` tree is a read-only fallback, not the normal edit surface.

- A bad import keeps the edited tree from serving. Boot import-probes the
  persistent clone and falls back to the baked backend, leaving the local tree
  intact for repair. Always run `python3 -m py_compile <file>` before a restart.
- A local fix is persistent but not upstream. Startup preserves installed source and local changes without fetching newer
  code. Explicit updates reconcile local changes onto the reviewed release;
  they can still conflict, and another installation will not receive a local fix. Ask
  whether it is a local overlay or needs a separate upstream handoff. Do not
  push or manage external repository workflow from inside Möbius.

All chat-persistence writes must route through the `chat_writer` actor. Never
assign `Chat.messages` or `Chat.pending_messages` directly; see
`backend/app/chat_writer.py` for the write-surface contract.

### Backend-fix loop

1. Record the starting revision, edit `/data/platform/backend/app/...py`,
   compile every changed Python file, and run focused tests.
2. Commit only the exact paths you own with `PM_COMMIT_ROOT=/data/platform
   pm-commit --from <starting-sha> '<what and why>' -- <paths>`.
3. Run the activation preflight. For routine backend activation, ask for a
   restart only if the settled backend change is not live. An explicit partner
   request for a restart or Restart-card test may proceed without that change.
   Explain that the restart interrupts active agent turns, name the current
   number of running turns when known, warn that service may be unavailable
   for tens of seconds, then call Möbius's `request_restart` tool as the final
   action. Approval of a task, a broad “go ahead” or “fix it,” or delegation
   of the backend-fix loop is not itself a Restart-card selection.

   `request_restart` takes no action arguments. The platform binds the current
   restartable boot and displays pending changed paths when any exist; it does
   not require changed paths. It saves its own card with one exact
   **Restart now** action plus a written-response path. Its receipt confirms
   only that the card was saved, not a restart choice: end the turn with no
   further text or tools. The owner or a non-delegated top-level agent run may
   answer an existing Restart card, including from another chat. Routing a
   card to an agent does not preapprove Restart: the answerer inspects the
   exact card and selects **Restart now** only when its Goal authorizes the
   disruption. It may leave the card for the owner. Delegated
   children and app-scoped tokens cannot. A **Restart now** selection triggers
   one platform-owned dispatch without an agent issuing or replaying a shell
   command. A written response continues the conversation without triggering
   a restart. Do not use `request_approval` or Codex's
   `request_user_input` for platform restart permission.

   If the tool is absent, the same saved-card operation is available through:

   ```bash
   python3 /data/platform/backend/scripts/owner_approval.py --restart
   ```

   A failed save is not a waiting card and not consent. Retry only the
   identical request to recover its receipt. If the running backend predates
   this operation, ask plainly and leave activation pending; never fabricate
   a card or park a process waiting for an answer.

   The card owns at-most-once admission for its exact action. A lost response,
   duplicate click, or ambiguous process death must never cause an agent to
   replay the restart. Any later ready Möbius boot resumes every linked
   Restart-card chat independently; each resumed agent verifies whether its
   changes loaded. Unrelated waits and queued work keep their existing
   barriers. An uncertain outcome needs fresh,
   specific approval rather than an automatic retry. A scheduled/background
   agent cannot open a live Restart card, so it leaves activation pending.
   An eligible agent may answer an existing card only when its instructions
   authorize that choice; merely being able to answer is not a restart request.
4. If the edited tree fails to import, the baked shell stays available. Refresh
   and repair `/data/platform` there, or use external Recovery if the interface
   itself is unavailable.
5. Restart time varies with active work and boot time; the page reloads when
   healthy. Verify the fix in the original chat.

## SQLite migrations

SQLAlchemy `create_all` creates missing tables; it never adds a column to an
existing table. A new model field therefore needs a new numbered, idempotent
function at the append-only end of `backend/app/schema_migrations.py`; never
edit a migration already present in the ledger. Test both a fresh database and
the frozen previous-release upgrade fixture rather than assuming model metadata
altered the latter.

If `/api/ready` reports `reason: schema_mismatch` or
`database_initialization_failed`, the process has deliberately skipped its
writer, reconciliation, cron, and database supervisors. Recovery repairs the
database externally, then a normal Restart-card choice lets boot
verify the database and start those owners coherently. Do not hand-edit the
in-memory readiness verdict or try to start skipped owners piecemeal.

---

## File locations

- Uploaded files: `/data/chats/{chat_id}/uploads/`
- Chat media: `/data/chats/{chat_id}/media/`
- Encrypted app credentials: `/data/app-secrets/{app_id}/` — use the app-secret
  API, never edit ciphertext files.
- Per-app storage (numeric id): `/data/apps/{app_id}/<path>`
- Per-app source (slug): `/data/apps/{slug}/`
- Shared storage: `/data/shared/<path>`
- Compiled bundles: read the exact `compiled_path` from `GET /api/apps/{id}`
- Cron logs: `/data/cron-logs/`
- Owner service token: `/data/service-token.txt` (mode 0600)

Chat files are purged when their chat is permanently deleted after the
retention window. Put data that must outlive a chat in per-app or shared
storage.

## Viewing apps directly

Capture an app through the authenticated shell, which supplies the frame-init
message a standalone frame does not receive:

```bash
bash "$SCRIPTS_DIR/agent-screenshot.sh" --content-only /app/<id>
```

The frame URL is stable and cache-revalidated, but opening it alone normally
ends at “Loading timeout.” Use the authenticated capture helper or the live
shell.
