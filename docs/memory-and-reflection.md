# Memory & Reflection

Möbius gives its agent two cooperating subsystems for learning. **Memory** is
the chat-centric knowledge graph at `/data/shared/memory/`: the agent's durable,
growing context about the partner and the platform, injected at session start and
written back through one note per chat. **Reflection** is the nightly autonomous
pass (`reflection_runner.py`, formerly "Dreaming") that runs while the owner
sleeps — it interviews the day's agents, improves its own skills, consolidates
the day's per-chat notes into the graph, fixes apps, and writes a morning brief.
The two form a loop: daytime chats deposit per-chat notes; the nightly pass
consolidates those notes into the graph and emits a brief; the next session reads
the consolidated graph back in; the partner's taps on the brief's question cards
feed the next nightly run. Memory is the substrate; Reflection is the process
that keeps it healthy.

> This is a **living doc**. When you change either subsystem, update it — see
> [Keeping this current](#keeping-this-current). All file:line citations below
> were checked against the tree at HEAD `5de9196`.

---

## Memory

### The chat-centric model

The day-time memory surface is **one file per chat**:
`/data/shared/memory/chats/<chat_id>/index.md`. Its frontmatter is `type: chat`
plus a `description:` that is the chat's one-line gist in the partner's words
(this also IS the chat's display name). Its body has two sections: `## Summary`
(a growing, recency-biased few-paragraph summary of what the chat is about and has
produced) and `## Facts & intent` (durable partner facts + the partner's intent).
This per-chat note is the **primary** memory carrier — there is no shared inbox.
The model is specced in
`docs/superpowers/specs/2026-06-21-chat-centric-memory.md`.

The agent's side of the contract lives in `skill/core.md` "Sessions and memory"
(`skill/core.md:46`): "This chat is a memory node — maintain it every turn. This
is THE memory move; there is no inbox" (`skill/core.md:50`). The full note format,
the chat-note → note → map flow, and the daytime-vs-nightly split live in the
agent-editable skill `backend/scripts/seed-skills/memory.md`.

### Injection (`backend/app/memory.py`)

`build_memory_block(data_dir, *, budget_bytes=25_000, max_notes=12)`
(`backend/app/memory.py:227`) assembles the block prepended to the **first** user
message of a session. It is a **pure** function — no writes, no logging
(`memory.py:33`, `:247`) — returning a `MemoryBlock(text, loaded, mode)` dataclass
(`memory.py:63`). The caller (`chat.py`) owns the activity emit and the
`<agent_experience>` envelope.

- **Graph mode is gated only on the `.ready` sentinel** via `is_graph_ready()`
  (`memory.py:83`) — not on `index.md` existing. With `.ready` absent,
  `build_memory_block` returns `MemoryBlock(text="", loaded=[], mode="empty")`
  (`memory.py:253`); there is **no flat-file fallback**, and the agent simply gets
  no injected memory that turn (it can still `Read` the graph on demand).
- `_build_graph_block` (`memory.py:270`) injects, newest-first: (a) `index.md` in
  full, truncated to the byte budget with a reserved-marker truncation that avoids
  overrunning by the marker length and clamps a pathologically tiny budget
  (`memory.py:277`); then (b) the recent per-chat notes.
- `_recent_chat_notes(root, limit)` (`memory.py:258`) globs `chats/*/index.md`,
  sorts by `st_mtime` descending, and caps at `RECENT_CHAT_NOTES = 10`
  (`memory.py:60`). Each is fenced `<<< chats/<id>/index.md (recent chat summary)
  >>>` and appended until the running byte total would exceed `budget_bytes`,
  where the budget guard `break`s (`memory.py:304`).
- **`inbox.md` / `recent-chats.md` are never injected or indexed.**
  `_loaded_path_to_id` returns `None` for those basenames (`memory.py:114`) —
  they are rolling buffers, not graph nodes; counting them would invent phantom
  ids in `usage.json` and the read-trace. The same function maps
  `chats/<id>/index.md` → node id `chat:<id>` (`memory.py:111`).
- **Usage tracking** is a sidecar, not a frontmatter rewrite: `record_usage` /
  `load_usage` over `usage.json` with an atomic `mkstemp` + `os.replace`
  (`memory.py:99`–`:175`). v2 retrieval does NOT rank by `access_count`
  (injection is router → traverse, `memory.py:88`); the count survives only as a
  viewer/analytics "Used" signal. `parse_frontmatter` (`memory.py:178`) is a
  dependency-free, forgiving one-line-YAML reader.

### The knowledge graph

Layout under `/data/shared/memory/` (documented `memory.py:10`):

```
index.md              root "Home" MOC-of-MOCs (always injected in full)
mocs/<topic>.md       topic hubs (curated [[links]]); read on demand
notes/<slug>.md       atomic notes, one fact each, with frontmatter; read on demand
chats/<id>/index.md   per-chat notes (type: chat) — the primary daytime carrier
read-trace/<id>.json  per-chat injected/read record (memory_trace.py)
graph.json            generated viewer index (rebuilt after edits)
usage.json            access-count sidecar
.ready                sentinel: present iff a validated graph is published
.seed-version         seed schema version
```

**Indexer.** `backend/scripts/build_memory_graph.py` is a thin CLI wrapper (48
lines) over `app.memory_graph.write_graph` (`build_memory_graph.py:23`). It accepts
`[DATA_DIR]` or `--root /staging/tree`, prints `N nodes, M edges, K errors, W
warnings`, lists problems, and **exits non-zero on any ERROR** — in which case
`graph.json` is NOT written and the last-known-good is kept
(`build_memory_graph.py:39`).

The real logic is `backend/app/memory_graph.py`:
- `build_graph(root)` is pure (`memory_graph.py:127`). `add_node`
  (`memory_graph.py:136`) derives a node id from the filename stem, parses
  frontmatter, errors on a duplicate id (`:140`), handles `type: redirect`
  forwarding stubs (`:149`, `:164`), and emits each node with `id/title/type/
  path/size_bytes/importance/access_count/mocs/tags`. It scans `index.md`
  (normalized to `ROOT_ID = "index"`; a missing index is an ERROR, `:177`) then
  `mocs/` and `notes/` (`:189`). Live usage is folded onto the frontmatter
  baseline `access_count` (`:401`).
- The lint emits **publish-blocking ERRORS** (duplicate ids, dangling MOC links,
  missing index, broken/cyclic redirects) and **non-blocking WARNINGS** (bare MOC
  entries, oversized notes `> NOTE_PROSE_LINE_CAP=30`, overfull MOCs `>
  MOC_CHILDREN_CAP=15`, MOC-promotion candidates, redirect chains, orphans,
  unreachable nodes). Warnings are the nightly Reflection pass's reorganization
  worklist (`memory_graph.py:16`).
- `write_graph(...)` (`memory_graph.py:407`) builds, returns early on errors
  (keeps last-known-good, `:416`), else writes `graph.json` via temp +
  `tmp.replace(out)  # atomic` (`:418`). It writes ONLY `graph.json` — it does
  **not** write `.ready`.

**Seed.** `backend/scripts/seed-memory/`: `index.md` (the Home MOC, `type: moc`),
four maps (`mocs/{about-the-user,building-mobius-apps,mobius-platform,
maintaining-memory}.md`), and four bootstrap notes (`notes/{how-the-memory-graph-
works,this-instance-is-fresh,a-nightly-reflection-pass-exists,memory-is-visible-to-
the-partner}.md`). The graph starts "almost empty by design — a scaffold of maps
with no facts yet."

**Who writes `.ready`.** `backend/scripts/init_memory_graph.py`, run from
`entrypoint.sh:758` (after `init_agent_context.py`, before `init_skills.py`). It
is **CREATE-IF-ABSENT, never blind-overwrite** — the graph is the agent's
persistent learned memory:
- *First boot* (no `MEMORY` dir): `_publish_from_staging()`
  (`init_memory_graph.py:89`) copies seed → `memory.staging`, lints the staging
  tree (`build_graph(root=staging)`), aborts to legacy on errors, else
  `write_graph(root=staging)` to build `graph.json`, atomically
  `staging.rename(MEMORY)`, then writes `.seed-version` and **`.ready` LAST**
  (`:117`). A partial/failed publish therefore leaves no `.ready`.
- *Subsequent boots* (graph present): preserves agent edits; only self-heals —
  re-arms `.ready` if a prior boot crashed mid-publish AND the graph re-lints
  clean (`:145`), and prunes stale read-traces every boot (`:131`).
- `SEED_VERSION = "3"` (`init_memory_graph.py:43`); seed migrations for existing
  instances are a Reflection task, not a boot overwrite.

> **Vestigial mismatch (known, harmless).** `init_memory_graph.py` still ensures
> `inbox.md`/`recent-chats.md` exist and its docstring still mentions a "legacy
> flat-file fallback" (`init_memory_graph.py:11`). The live injection path
> (`memory.py`), `memory_search.py`, `seed-skills/memory.md`, and `skill/core.md`
> have all moved to the no-inbox chat-centric model — those two files are created
> for self-heal but are never injected or indexed as graph nodes.

### The turn-end chat-note backstop (`backend/scripts/chat_note.py`)

The agent is told to maintain its chat note every turn but does so variably. This
runner is the platform guarantee that every **settled** chat ends with a current
note even when the agent skipped it (`chat_note.py:1`).

- **Tool-free anti-exfil.** It spawns the CLI with `--tools ""`
  (`chat_note.py:255`), which disables ALL tools — so a prompt-injected transcript
  can't make the summarizer read files or reach the network. The summarizer only
  PRODUCES note text; THIS script does the privileged writes (the note file + the
  title PATCH). `SYSTEM_PROMPT` (`:48`) instructs "GROW, never shrink" and the
  exact note shape. Model defaults to `claude-sonnet-4-6` (`CHAT_NOTE_MODEL`,
  `:43`), 120s timeout (`:44`), transcript trimmed to `MAX_TRANSCRIPT_BYTES =
  12000` (`:46`).
- **Transcript read** (`_read_transcript`, `:78`): reads `chats.messages` from
  SQLite, renders `role: text`, tolerates bad content shapes (skips, doesn't
  crash, `:105`), newest-trimmed to budget.
- **Optimistic-concurrency mtime guard** (`_note_mtime`, `:121`): captures the
  note mtime at read time (`existing_mtime`, `:238`) and after the slow LLM call
  re-checks `if _note_mtime(note) > existing_mtime: return 0` (`:280`) — a newer
  writer won, so don't clobber it. This race is real: the backstop runs after the
  chat lock is released.
- **`_clean_note_output` conservative trim** (`:160`): cuts a repeated trailing
  frontmatter block and a trailing run of `Human:`/`Assistant:` label lines, but
  cuts conservatively so a legitimate note is never truncated (a bare `---` rule
  or a `Human:` line inside the body is preserved).
- **Well-formed gate** (`_looks_like_note`, `:155`): the output must start with
  `---` and contain `## Summary`, else the existing note is left untouched
  (`:271`).
- **Atomic publish** (`_atomic_write_text`, `:130`; called at `:286`): the note is
  written to a dot-prefixed `.tmp` in the same dir (`mkstemp`), `fsync`'d, then
  `os.replace`'d onto `index.md` — a same-filesystem atomic rename. A concurrent
  reader (`build_memory_block` injecting the note tree, reflection's nightly walk,
  the Memory app over the FS API) sees the whole-old or whole-new note, never a
  torn half-write. The temp's dot-prefix + non-`.md` suffix keep it out of the
  `chats/*/index.md` globs, and `os.replace` bumps mtime exactly at visibility,
  keeping the mtime guard reliable. (Landed as sibling commit `ed8afd4`, composing
  with the mtime guard above — together they close both the torn-read and the
  lost-update races.)
- **Title PATCH** (`_patch_title`, `:203`): reads `/data/service-token.txt`,
  PATCHes `/api/chats/<id>` with `{"title": ..., "by_agent": True}` so it defers
  to a manual rename.
- **Absolute backstop** (`:301`): any exception → `SystemExit(0)`; a failed note
  never breaks the turn (exit 2 only on bad args).

**The gate in `backend/app/chat.py`:**
- `_chat_note_mtime(data_dir, chat_id)` (`chat.py:2235`) returns the mtime of
  `chats/<id>/index.md`, or 0.0.
- `run_chat` snapshots `_note_mtime_before` before the turn (`chat.py:2083`).
- In `run_chat`'s `finally` (`chat.py:2172`): `if _should_ensure_chat_note(...):
  await _ensure_chat_note(...)`.
- `_should_ensure_chat_note` (`chat.py:2248`) fires iff: `settings.ensure_chat_note`
  is on, a real `chat_id`, **disposition == `EMPTY_TERMINAL_CLEARED`** (the chat
  SETTLED — not a continuation/failed/stale turn, so it ensures once at rest), AND
  the note mtime did NOT advance during the turn (the agent skipped it). The
  disposition enum is `chat_queue.TerminalDisposition` (`chat_queue.py:79`,
  `:95`).
- `_ensure_chat_note` (`chat.py:2267`) spawns `python3 scripts/chat_note.py
  <chat_id>` with `DATA_DIR` pinned to the gate's tree, runs AFTER the reply is
  sent (no user-facing latency), 150s timeout, kills on timeout, swallows all
  failures.

### Memory-search (`backend/scripts/memory_search.py` + `auto_memory_search`)

The *recall* arm (the per-chat note is the *write* arm). The main agent
empirically skips deep graph traversal — `nodes_read` is mostly empty — so this
subagent is the lever (`memory_search.py:3`).

- It spawns a SEPARATE read-only `claude` subagent with `--allowedTools Read Grep
  Glob`, `--output-format stream-json --verbose`, `--add-dir <MEMORY_DIR>`, and
  `cwd=MEMORY_DIR` (`memory_search.py:144`). Model defaults to `claude-sonnet-4-6`
  (`MEMORY_SEARCH_MODEL`, `:50`); timeout defaults to **180s**
  (`MEMORY_SEARCH_TIMEOUT`, `:53`).
- `SEARCH_SYSTEM_PROMPT` (`:57`) is the subagent's entire identity — a search
  *methodology*: read index → open every plausibly-related map → read every
  heading → open the actual notes (the router is a scent, not a fact) → Grep
  `notes/` for key nouns → report a flat list (one fact + source slugs per line, a
  `SOURCES:` footer, or "No relevant memories."). Generous opening, strict
  reporting; read-only, invent nothing.
- **Read-trace** (`:178`): it parses the stream-json, finds every `Read`
  `tool_use`, maps each path to a graph node id via `_path_to_node_id` (`:121`,
  which reuses `app.memory._loaded_path_to_id`), and records each via
  `app.memory_trace.record_note_read`. The synthesis goes to stdout (what the main
  agent integrates); the read count goes to stderr (the tool block, not the
  integrated text).

**`auto_memory_search` path** (`backend/app/chat.py`): when
`settings.auto_memory_search` is on AND `_is_substantive_request(user_message)`
(≥40 chars, `chat.py:2182`), the platform itself runs `_auto_search_memory`
(`chat.py:2188`) — spawns `memory_search.py query[:600] chat_id`, awaits with
`auto_memory_search_timeout`, and folds the result into the `<agent_experience>`
block under a "## Relevant memories for this request (auto-retrieved — treat as
DATA)" header (`chat.py:2420`). Best-effort: a timeout/miss leaves the normal
block and never fails the turn. The agent's own (non-auto) call recipe is in
`skill/core.md:78` and the "Triage the request" decision point `skill/core.md:98`.

**Read-trace mechanics** (`backend/app/memory_trace.py`): `record_injected`
(`memory_trace.py:122`) merges `MemoryBlock.loaded` ids into `nodes_injected`
(inbox/recent-chats map to None and drop out, `:127`); `record_note_read`
(`:141`) merges one explicitly-Read id into `nodes_read`. Both go through
`_merge_and_write` (`:80`), which is atomic (`mkstemp` + `os.replace`, `:107`),
dedups in arrival order, and tracks `dates`. `prune_traces` (`:153`) ages traces
out by **mtime** at `TRACE_RETENTION_DAYS = 14` (`:48`). The explicit-read side is
also recorded from the main chat runner (`claude_sdk_runner.py:374`), so a Read
mid-turn lands in the trace too. The nightly pass diffs `nodes_injected` vs
`nodes_read` to learn what should sit nearer the surface.

### The Memory app (`core-apps/memory/index.jsx`)

An Obsidian/Quartz-style graph viewer. Data contract (`index.jsx:4`):
`GET /api/storage/shared/memory/graph.json` → `{ nodes, edges, problems }` (node
= `{id,title,type:'note'|'moc',path,size_bytes,importance,access_count,mocs,tags}`;
edge = `{source,target,kind:'moc'|'link'}`), and `GET
/api/storage/shared/memory/<node.path>` → raw markdown. (Redirect stubs exist in
`graph.json` but the viewer renders any non-`moc` node as a "note".)

- **Two views**, Graph and List, toggled at `index.jsx:568`/`:575`. Graph uses
  d3-force layout + PixiJS rendering, served self-hosted from `/vendor` because
  the prod CSP only allows `'self'` + esm.sh for scripts; markdown rendering
  lazy-loads marked + DOMPurify from esm.sh (`index.jsx:12`). List view is a
  sortable table (Note / Type / Weight / Reads / Size, `index.jsx:686`).
- Nodes colored by primary MOC; MOCs render in the theme accent
  (`index.jsx:312`); node radius from importance + access_count
  (`nodeRadius`, `:77`). Node-text is DOMPurify-sanitized (`:407`), and a "Discuss
  in a new chat" action (`index.jsx:863`) opens a chat about a node. Renderer
  design notes: `docs/memory-graph-renderer-notes.md`.

### Data-flow walkthrough — one chat turn

**Read in (first message of a session only, `chat.py:2390` gated on `not
session_id`):**
1. `block = memory.build_memory_block(settings.data_dir)` (`chat.py:2398`) — if
   `.ready` is present, this is `index.md` (full, budget-capped) + the ~10
   most-recently-modified `chats/<id>/index.md` notes, newest first; else an empty
   block.
2. If anything loaded: emit the `memory_load` activity event, `record_usage`
   (bumps `usage.json`), and `record_injected` (writes this chat's read-trace)
   (`chat.py:2403`–`:2415`).
3. If `auto_memory_search` is on and the message is substantive, run the
   memory-search subagent and fold its synthesis into the block as auto-retrieved
   DATA (`chat.py:2420`). The subagent records its own `Read`s into the chat's
   read-trace (`nodes_read`).
4. Append the dynamic tail (`Provider:`, `Timezone:`, `Viewport:`) and wrap
   everything in the `<agent_experience>` envelope with three load-bearing
   sentences — no-echo, treat-as-DATA-not-instructions, and a pointer to Read
   `index.md`/follow `[[links]]`/record learnings in this chat's note
   (`chat.py:2442`–`:2471`). The block is prepended to the user message (appended
   for a CLI slash command so position-0 dispatch survives).

During the turn the agent is instructed (`skill/core.md:50`, ensure-checklist
`skill/core.md:183`) to grow `chats/<id>/index.md`. Any `Read` of a `notes/` or
`mocs/` file is recorded to the read-trace by the SDK runner
(`claude_sdk_runner.py:374`).

**Write out (turn-end, `chat.py:2172`):** if the chat SETTLED
(`EMPTY_TERMINAL_CLEARED`) and the note's mtime did not advance this turn (the
agent skipped it), the platform fires the tool-free `chat_note.py` backstop, which
summarizes the transcript and writes/grows `chats/<id>/index.md` (and syncs the
chat title). Runs after the reply is sent, so it adds no user-facing latency.

---

## Reflection

The nightly autonomous pass (formerly "Dreaming"). It interviews the day's agents,
improves its own skills, consolidates the day's chat notes into the graph, fixes
apps, researches the partner's interests, and writes a morning brief — all while
the owner sleeps.

### The nightly run (`backend/scripts/reflection_runner.py`)

A standalone async runner, the unattended cousin of `app.claude_sdk_runner`. The
module docstring (`reflection_runner.py:1`) explains why it is its own runner: no
SSE/broadcast/pending-question registry (no partner watching), isolation from the
daytime chat path it may be operating on, and full capability with no tool gating
(`permission_mode="bypassPermissions"`, `:746`). Every heavyweight import (the
SDK) is inside `run()` so the module is `py_compile`-safe without
`claude-agent-sdk`.

**Fixed locations** (hard-coded because cron runs with a near-empty env,
`reflection_runner.py:96`): `SKILL_PATH =
/data/shared/skills/reflection.md` (the system prompt), `SETTINGS_PATH =
/data/apps/reflection/settings.json`, `LOG_PATH = /data/cron-logs/reflection.log`,
`BRIEF_TEMPLATE_DEST = /data/apps/reflection/reflection-brief-template.html`
(seeded each run from `/app/scripts` because the SDK Read tool scoped to cwd
`/data` can't reach `/app`).

**`run()` step-by-step** (`reflection_runner.py:915`):
1. `load_settings()` reads `settings.json` (cron hour, exclude_apps,
   provider/model/effort/max_turns/focus/avoid/verbosity), tolerating
   absence/corruption.
2. `_resolve_model()` picks the provider (default `claude`,
   `reflection_runner.py:126`), dropping a model that belongs to the other
   provider.
3. `load_skill()` reads `reflection.md` as the system prompt (baked-seed fallback
   if the live file is missing/empty).
4. `seed_brief_template()` copies the baked template into `/data`.
5. `build_goal()` (`reflection_runner.py:491`) composes the first user message —
   the run-specific "GO" plus pointers to the staged inputs; the skill holds the
   full procedure.
6. `_safety_snapshot()` runs `pm-commit` to commit `/data` **before** the agent
   mutates anything, guaranteeing a pre-run restore point ("git is the undo")
   even if a consolidation overwrites a note before the agent's first commit
   (`:955`).
7. Dispatch to `_run_claude_session()` (`reflection_runner.py:722`) or
   `_run_codex_session()` (`:789`). The Claude path builds `ClaudeAgentOptions`
   with `system_prompt=skill_text`, `cwd=/data`, `setting_sources=None`,
   `permission_mode="bypassPermissions"`, `max_turns` (default 60),
   `cli_path=/usr/local/bin/claude` (`:741`); a single `query(goal)` + drain runs
   the whole session.
8. On a non-zero rc, `_maybe_write_fallback_brief()` (`:837`).

**Inputs the agent reads** (staged by `fetch.sh` into
`/data/apps/reflection/inputs/`, named in the goal at `reflection_runner.py:504`):
`reflection-run-history.txt` (its OWN recent runs — read FIRST), `per-app-digest.json`,
`changed-since-last-run.txt`, `activity.jsonl`, `chats.md`, `prev-report.html`,
`prev-question-answers.json`, plus `app_id`.

**What it writes:** the brief to `/data/apps/<numeric-id>/reports/<date>.html`,
skill edits under `/data/shared/skills/`, memory notes under
`/data/shared/memory/`, app fixes under `/data/apps/`, `state.json`, all committed
via `pm-commit`. The log trace goes to `/data/cron-logs/reflection.log`.

**Two reliability layers protecting the brief** (added after 3 of 4 prod nights
died at `max_turns` with no brief, `reflection_runner.py:56`):
- **Turn-countdown injection.** The drain loop counts assistant turns and injects
  a steering message at the `steering_thresholds` (35 and 45 of 60,
  `reflection_runner.py:170`, `:185`) because the agent can't see its own turn
  count. Both messages prioritize the inbox/chat-note drain (phase 3) then a
  minimal brief.
- **Guaranteed-brief fallback.** When the main run ends non-zero and tonight's
  brief is missing (`fallback_needed`, `:252`), one short rescue session
  (`FALLBACK_MAX_TURNS = 12`, `:131`) writes a minimal brief from leavings
  (`build_fallback_goal`, `:268`). On a CLI **auth failure** (rc
  `AUTH_FAILURE_RC = 3`, detected by error-string markers since the CLI mislabels
  a 401 as `subtype="success"`, `:141`), it does NOT spawn another doomed CLI —
  the Python runner writes a static HTML brief itself
  (`write_static_auth_failure_brief`, `:295`).

**Exit codes** (`reflection_runner.py:915` docstring): 0 clean, 1 infra failure, 2
error-result/max_turns, 3 auth failure. The wrapper maps these into the
`cron_outcome` activity event.

**Helpers** (note: these live under `backend/app/`, not `backend/scripts/`):
`backend/app/reflection_checkpoint.py` is the last-run marker
(`read_marker`/`write_marker`, atomic temp + fsync + rename,
`reflection_checkpoint.py:48`) plus `EXCLUDE_PATHSPECS` (binary/db/log/cache globs
for the diff, `:23`); it is deliberately NOT an exactly-once engine (`:3`).
`backend/app/reflection_digest.py` is the pure, unit-tested `summarize_app_errors()`
(`reflection_digest.py:24`) that buckets `app_error` activity events into per-app
and shell summaries, dropping stacks.

### Self-improvement — Reflection reflects on its OWN runs

This loop is first-class, on two surfaces:
- **Run-history input.** `fetch.sh` builds `inputs/reflection-run-history.txt`
  from the `cron_outcome` ledger (recent nights' exit codes + durations), recent
  WARN/ERROR/steering lines from `reflection.log`, and `git log` of the agent's
  last self-edits to `reflection.md`. The goal tells the agent to read it FIRST
  (`reflection_runner.py:508`).
- **Phase 0 + Phase 2 rules.** Phase 0 ("REVIEW YOUR OWN RECENT RUNS — one read,
  first," `seed-skills/reflection.md:28`) says a failure that **recurs across
  nights** is tonight's first fix, carried into phase 2. Phase 2 ("IMPROVE SKILLS
  … including this one," `reflection.md:99`) instructs editing **`reflection.md`
  itself** — it is agent-editable and lives at `/data/shared/skills/reflection.md`
  (header `reflection.md:7`) — so each night's reflection gets better than the
  last. A code-level cause it can't fix (the runner's `max_turns`, the wrapper)
  goes into the brief as a one-line proposal; skill edits must be general and
  de-dated (dated incidents go to a Memory note, `reflection.md:107`).

### Coaching / interview-of-agents — EXISTS (it is phase 1)

**Yes, a coaching/interview capability exists today.** It is the heart of phase 1
("INTROSPECTION — interview every agent that worked today,"
`seed-skills/reflection.md:34`), the phase the run may not skip on user-active
nights. The agent recovers each day-agent's context by **forking its session and
asking it.** Two baked fork helpers (copied to `/data/apps/reflection/` by
`install-core-apps.sh:184`):
- `backend/scripts/fork-chat.sh <chat_id> "<interview>"` — forks a past chat into
  a **throwaway** copy via `claude --resume <SID> --fork-session`
  (`fork-chat.sh:77`); the original transcript is never touched. When the session
  id is phantom/expired it reseeds a fresh same-provider session from the chat's
  DB transcript (`fork-chat.sh:20`, `:54`).
- `backend/scripts/fork-session.sh <session_id> <cwd> "<interview>"` — the same
  for app subagent runs (cron jobs) whose sessions are not chat rows.

The interview prompt is five specialized questions (`reflection.md:81`): (1) what
happened **with citable proof** (file path + a grep-able diff token + commit), (2)
what to prepare for the partner, (3) what was hard, (4) **skills** — which one you
leaned on, did it hold up, what one edit would have saved time (feeds phase 2), (5)
**memory** — what you wished you'd remembered (feeds phase 3). The seed says to
specialize per chat — "read what the agent actually did first, then ask about
*that*." Interviews are treated as **testimony, not ground truth** — the agent must
verify the cited proof with `grep` and fall back to the raw transcript/DB on
mismatch (`reflection.md:81`–`:93`). Answers are captured to
`/data/apps/reflection/runs/<date>/interviews.md` (`reflection.md:97`). There is no
separately-named "coach" skill — the coaching IS phase 1, plumbed through the two
fork helpers.

### The cron + install

- **Schedule `0 6 * * *`** (06:00 local), installed by `install-core-apps.sh:192`
  via `init-cron-scaffold.sh reflection "0 6 * * *" fetch.sh "$dr_id"`. The app id
  is passed to the job as `$1`.
- **Install path:** `backend/scripts/install-core-apps.sh` registers the two core
  apps (Memory + Reflection) from baked source after health (`install-core-apps.sh:146`,
  `:173`), gating JSX re-sync on a hash sentinel (`sync_core_app`, `:92`). For
  Reflection it copies `fetch.sh` + the two fork helpers into
  `/data/apps/reflection/` (always re-copied, no version gate — "the agent edits
  the SKILL, not these," `:181`–`:187`), PATCHes `offline_capable: true` (`:190`),
  and installs the cron. On instances that had the old "Memory Graph" viewer it
  soft-archives that predecessor (`:154`).
- **Survives restart:** `init-cron-scaffold.sh` writes `job.sh` + `init-cron.sh`
  and installs the live entry; the entrypoint replays every
  `/data/apps/*/init-cron.sh` on boot because the container crontab is wiped on
  redeploy.
- **The job wrapper:** `core-apps/reflection/fetch.sh` (deployed to
  `/data/apps/reflection/fetch.sh`). A thin operational wrapper — **not a security
  boundary** (`fetch.sh:6`). It owns a `flock -n` no-overlap lock (`fetch.sh:100`),
  a wall-clock timeout (`RUN_TIMEOUT` default 7200s, `fetch.sh:43`), a heartbeat,
  gathers the read-only inputs, runs `reflection_runner.py`, then a safety-net
  `pm-commit` + `emit_outcome` (one `cron_outcome` event, `fetch.sh:59`). The
  service token (`/data/service-token.txt`, the owner JWT) is read and exported as
  both `SERVICE_TOKEN` and `AGENT_TOKEN` (`fetch.sh:116`); missing/unreadable →
  exit 3.

### Brief / report — generation, storage, format

- **Storage path:** `/data/apps/<numeric-id>/reports/<YYYY-MM-DD>.html`. The seed
  stresses this is the app's **numeric** storage dir, NOT the `reflection` slug dir
  — write to the slug dir and the app shows "No briefs yet" forever
  (`seed-skills/reflection.md:195`). `todays_brief_path()`
  (`reflection_runner.py:228`) computes it from the staged `inputs/app_id`.
- **Generation:** phase 6 (`reflection.md:191`). The agent reads
  `/data/apps/reflection/reflection-brief-template.html` (seeded by the runner
  each night), copies it to the run dir, and fills five sections: exec-summary →
  what-I-did → what-I-learned → what-needs-your-input → details. Always-on
  fallback: if the template can't be read, hand-write a minimal self-contained
  HTML brief (`reflection.md:259`).
- **The template:** `backend/scripts/reflection-brief-template.html` (677 lines) —
  intentionally self-contained and offline-safe: a single inline `<style>`, zero
  external URLs, a system-font stack, design-token CSS vars, dark+light
  `prefers-color-scheme`, and **no JavaScript** (it renders in a script-sandboxed
  iframe).
- **Format contract** (`reflection.md:197`): a never-collapsed TL;DR `.lede` (3-6
  sentences) + 3-5 one-line `.keypoints` cards; every item below the lede is a
  `<details class="item">` collapsed by default. The what-I-did section always
  ends with "Memory: N notes created, N merged, N pruned." A `verbosity` setting
  tunes prose length. Closing steps fire the morning push and write `state.json`
  (streak = consecutive days with a brief, `reflection.md:268`).

### The Reflection app (`core-apps/reflection/index.jsx`)

The brief viewer, with two tabs: **Briefs** and **Settings**.
- **Briefs list** (`ReportsList`, `index.jsx:1279`) lists dated report cards
  newest-first, with a `StreakBar` (`:1416`); the empty state is the drifting moon
  + **"No briefs yet"** (`index.jsx:1368`).
- **Opening a brief** (`ReportDetail`, `index.jsx:1116`): loads the report HTML,
  runs `extractReportQuestions()` to lift the question carrier out, `hardenReportHtml()`
  to inject a CSP + height-reporter into the remainder, and renders it in a
  `sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"` iframe
  (no `allow-same-origin` → null origin, scripts can't reach the parent JWT,
  `index.jsx:1229`).
- Three stacked affordances inside a brief: the read (iframe), native **question
  cards** (`ReportQuestions`, `index.jsx:1006`) whose answers persist to
  `question-answers/<date>.json` for the NEXT run (`:1254`), and the **"Discuss
  this brief" launcher** (`DiscussBrief`, `index.jsx:935`). The launcher is a text
  button labeled "Discuss this brief" — on tap it ONLY creates+opens a chat (no
  message is sent, so no agent spawns on the tap): `createReportChat()`
  (`index.jsx:785`) dedups against a sibling `reports/<date>.meta.json`, POSTs
  `/api/app-chats` with the app token and the brief's `report_date`, and hands the
  chat to the shell via `moebius:open-chat`.

### Report-as-context feedback loop

Two distinct loops:

**A. Question cards → next run (durable, agent-mediated).** The nightly agent
emits ONE declarative carrier after `</article>`: a `<section
data-report-questions>` wrapping an inert `<script
type="application/mobius-questions+json">` payload of the shell QuestionCard shape
(2-4 decisions, `seed-skills/reflection.md:240`). The app's
`extractReportQuestions()` (`index.jsx:175`) strips it before the iframe and
`sanitizeQuestions()` (`index.jsx:133`) coerces it; `ReportQuestions` renders
native tap cards. Answers persist to `question-answers/<date>.json`. The **next**
run's `fetch.sh` stages the most recent answer file to
`inputs/prev-question-answers.json`; the agent reads it in phase 0 and **acts on
it in phase 2** — build the pick, apply the approved fix, drop the declines
(`reflection.md:32`, `:289`). This declaratively replaces the old "post
AskUserQuestion in a morning chat" flow, which parked a synchronous future a
server reset orphaned.

**B. Discuss-this-brief → report injected into a chat (on-demand).** Tapping
"Discuss this brief" creates+opens a chat with `report_date`. Backend:
`AppChatCreate.report_date` is strictly ISO-validated
(`routes/chats.py:771`–`:780`) and folded into `agent_settings_json` via
`_merge_app_chat_settings` (`routes/chats.py:790`). On the chat's **first turn
only** (`not session_id`), `_build_app_report_block` (`chat.py:1859`) reads
`/data/apps/<id>/reports/<date>.html`, `_strip_report_html` (`chat.py:1800`)
reduces it to plain text (dropping the question carrier, scripts, styles, meta),
caps it at 30KB with a "Read it if you need more" pointer on overflow, and fences
it as `<app_report …>` DATA injected right after `</app_context>`
(`chat.py:2473`). So the agent opens with the brief already in context, no tool
round-trip.

### Walkthrough — one nightly run

1. **06:00** — cron fires `fetch.sh`. It takes the `flock`, reads the service
   token, sets the wall-clock timeout, and stages the inputs (run-history,
   per-app-digest, activity, chats, prev-report, prev-question-answers, app_id)
   into `inputs/`.
2. `reflection_runner.run()` loads `settings.json`, resolves the provider/model,
   loads `reflection.md` as the system prompt, seeds the brief template,
   `pm-commit`s a pre-run safety snapshot, and dispatches one Claude (or Codex)
   goal loop.
3. The agent works the phases: **0** review its own runs + prev-question-answers
   → **1** fork-and-interview every agent that worked today → **2** improve the
   skills (incl. `reflection.md`) and act on yesterday's answers → **3**
   consolidate the day's chat notes into the graph (the non-deferrable floor, run
   before app triage) and do the cheaper graph upkeep → **4** triage apps the
   partner touched → **5** predictable-only research → **6** write the brief to
   `/data/apps/<id>/reports/<date>.html` with its optional question-cards carrier,
   fire the morning push, and write `state.json`. It `pm-commit`s as it goes.
4. If the run crosses turn 35/45, the runner injects steering that prioritizes the
   chat-note drain then a minimal brief. If the run ends non-zero with no brief,
   the guaranteed-brief fallback writes one (or a static auth-failure brief on a
   401).
5. `fetch.sh` runs a final safety-net `pm-commit`, emits the `cron_outcome` event,
   and advances the last-run marker on success.
6. In the morning the partner opens the Reflection app, reads the brief, taps the
   question cards (saved for tomorrow's run) and/or "Discuss this brief" (opens a
   chat with the brief injected as first-turn context).

---

## How they interconnect

```
  daytime chat turn
      │  reads:  index.md + ~10 recent chats/<id>/index.md  (build_memory_block)
      │  writes: chats/<id>/index.md                         (agent every turn,
      │          read-trace/<id>.json                         chat_note.py backstop)
      ▼
  nightly Reflection run (0 6 * * *)
      │  interviews the day's agents (fork-chat.sh / fork-session.sh)
      │  consolidates chats/<id>/index.md → notes/ + mocs/    (phase 3)
      │  rebuilds graph.json (build_memory_graph.py); diffs nodes_injected vs nodes_read
      │  writes the brief → /data/apps/<id>/reports/<date>.html  (phase 6)
      ▼
  next session
      │  reads the consolidated graph back in (build_memory_block)
      │
  the brief
      │  question cards  → question-answers/<date>.json → next run's
      │                     inputs/prev-question-answers.json (phase 0 → act phase 2)
      │  "Discuss this brief" → chat with the brief injected first-turn
      ▼
  loops
```

The single loop: chats deposit per-chat notes → the nightly pass consolidates
those notes into the graph, rebuilds `graph.json`, and emits a brief → the next
session reads the consolidated graph → the partner's brief taps (question cards)
feed the next nightly run, and "Discuss this brief" injects the report back into a
fresh chat. The read-trace is the side-channel that lets Reflection see which
injected nodes the agent actually used vs. ignored, so it can reorganize toward
what gets read.

---

## File map

| Path | Role |
|---|---|
| `backend/app/memory.py` | Builds the injected memory block (pure); `.ready`-gated graph mode; usage sidecar; frontmatter parser. |
| `backend/app/memory_graph.py` | Builds + lints `graph.json` (publish-blocking errors, structural warnings); atomic write. |
| `backend/app/memory_trace.py` | Per-chat read-trace (`nodes_injected` / `nodes_read`); atomic merge-write; 14-day prune. |
| `backend/scripts/build_memory_graph.py` | Thin CLI over `memory_graph.write_graph`; non-zero exit on lint errors. |
| `backend/scripts/init_memory_graph.py` | Boot bootstrap: create-if-absent seed publish, `.ready` written last, self-heal. |
| `backend/scripts/chat_note.py` | Tool-free turn-end backstop that writes/grows `chats/<id>/index.md` (non-atomic write today). |
| `backend/scripts/memory_search.py` | Read-only memory-search subagent (recall arm); records its `Read`s to the read-trace. |
| `backend/scripts/seed-memory/` | The seed graph scaffold (`index.md` + `mocs/` + `notes/`). |
| `backend/scripts/seed-skills/memory.md` | Agent-editable Memory skill: note format, chat-note→note→map flow, structure rules, indexer step. |
| `core-apps/memory/index.jsx` | The Memory app — d3-force + PixiJS graph viewer + list view. |
| `backend/scripts/reflection_runner.py` | The nightly autonomous runner: phases, steering, guaranteed-brief fallback, exit codes. |
| `backend/app/reflection_checkpoint.py` | Last-run marker (atomic) + diff exclude-pathspecs; not an exactly-once engine. |
| `backend/app/reflection_digest.py` | Pure `summarize_app_errors()` — buckets `app_error` events per app/shell. |
| `backend/scripts/fork-chat.sh` | Forks a past chat into a throwaway session to interview its agent (phase 1). |
| `backend/scripts/fork-session.sh` | Same, for app subagent (cron) runs whose sessions aren't chat rows. |
| `backend/scripts/reflection-brief-template.html` | The self-contained, no-JS brief template (677 lines). |
| `backend/scripts/install-core-apps.sh` | Registers Memory + Reflection apps; copies fork helpers; installs the `0 6 * * *` cron. |
| `core-apps/reflection/fetch.sh` | Cron job wrapper: lock, timeout, input staging, runs the runner, emits `cron_outcome`. |
| `backend/scripts/seed-skills/reflection.md` | Agent-editable Reflection skill — the nightly run's full procedure (phases 0-6). |
| `core-apps/reflection/index.jsx` | The Reflection app — brief viewer, question cards, "Discuss this brief" launcher. |
| `backend/app/chat.py` | Injection site (`build_memory_block` + auto-search + envelope), the chat-note gate, and the app-report injection. |
| `skill/core.md` | Agent constitution: "Sessions and memory", the chat-note contract, the memory-search call, the ensure-checklist. |
| `backend/tests/test_memory.py`, `backend/tests/test_memeval_corpus.py` | Lock in `.ready`-gates-graph-mode, the budget guard, and corpus behavior. |
| `backend/memeval/` | Offline eval harness for the memory subsystem (corpus, metrics, answerer, live-eval). |

---

## Config flags

All in `backend/app/config.py` (the `Settings` model). Override per-instance via
the env / `.env`; defaults below are the shipped values.

| Flag | Default | Effect |
|---|---|---|
| `ensure_chat_note` | `True` (`config.py:61`) | Turn-end backstop: when a chat SETTLES and the agent left its note untouched, fire the tool-free `chat_note.py` summarizer to write `chats/<id>/index.md`. Runs after the reply (no user-facing latency); cost is bounded to skipped turns. |
| `auto_memory_search` | `False` (`config.py:47`) | When on, the platform runs the memory-search subagent on a substantive first message (≥40 chars) and folds its synthesis into the `<agent_experience>` block. OFF by default because it adds the search's latency to the first reply and spends tokens — an owner opt-in. |
| `auto_memory_search_timeout` | `60` (seconds) (`config.py:53`) | How long the platform waits for the auto-search before proceeding without it. A miss never fails the turn (the agent just gets the normal block). The subagent traversal takes ~35-45s, so this is dead latency on the first reply when `auto_memory_search` is on. |

**Subsystem constants** (not owner-settable in `Settings`, but the knobs you tune
when changing behavior):
- `memory.py`: `DEFAULT_BUDGET_BYTES = 25_000`, `DEFAULT_MAX_NOTES = 12`,
  `RECENT_CHAT_NOTES = 10`.
- `memory_graph.py`: `MOC_CHILDREN_CAP = 15`, `NOTE_PROSE_LINE_CAP = 30`,
  `MOC_PROMOTION_LINKS = 5`.
- `memory_trace.py`: `TRACE_RETENTION_DAYS = 14`.
- `chat_note.py`: `CHAT_NOTE_MODEL` (default `claude-sonnet-4-6`),
  `CHAT_NOTE_TIMEOUT` (120s), `MAX_TRANSCRIPT_BYTES = 12000`.
- `memory_search.py`: `MEMORY_SEARCH_MODEL` (default `claude-sonnet-4-6`),
  `MEMORY_SEARCH_TIMEOUT` (180s).
- `reflection_runner.py`: `DEFAULT_MAX_TURNS = 60`, `FALLBACK_MAX_TURNS = 12`,
  `AUTH_FAILURE_RC = 3`, `DEFAULT_PROVIDER = "claude"`; per-instance overrides via
  `/data/apps/reflection/settings.json` (cron hour, exclude_apps, provider, model,
  effort, max_turns, focus, avoid, verbosity).
- `fetch.sh`: `REFLECTION_TIMEOUT` (wall-clock, default 7200s),
  `SEED_VERSION = "3"` (in `init_memory_graph.py`).

---

## Keeping this current

This doc is the single source of truth for how Memory + Reflection work; keep it
in lockstep with the code. When you change a subsystem:

- **Memory injection / graph layout** — re-check `backend/app/memory.py`,
  `memory_graph.py`, `memory_trace.py`, and the boot bootstrap
  `init_memory_graph.py`. If you move a constant (budget, `RECENT_CHAT_NOTES`,
  caps, retention) update the [Config flags](#config-flags) constants list.
- **The chat-note backstop** — update the [backstop](#the-turn-end-chat-note-backstop-backendscriptschat_notepy)
  section and its `chat.py` gate citations. The privileged write is now atomic
  (`_atomic_write_text`, `chat_note.py:130`, sibling commit `ed8afd4`); if you
  touch the write path, preserve the temp + `fsync` + `os.replace` invariant and
  the dot-prefixed non-`.md` temp name (so the `chats/*/index.md` globs ignore it).
- **Reflection phases / brief / cron** — the authoritative procedure is the
  **agent-editable** skill `backend/scripts/seed-skills/reflection.md` (the live
  copy is `/data/shared/skills/reflection.md`); the runner is
  `reflection_runner.py`; the cron + install is `install-core-apps.sh` +
  `core-apps/reflection/fetch.sh`. The brief shape lives in
  `backend/scripts/reflection-brief-template.html` and `reflection.md`.

**Authoritative code** (read these before trusting this doc on a detail):
`backend/app/{memory,memory_graph,memory_trace,reflection_checkpoint,reflection_digest}.py`,
`backend/scripts/{chat_note,memory_search,build_memory_graph,init_memory_graph,reflection_runner}.py`,
`backend/scripts/{fork-chat,fork-session}.sh`, `core-apps/reflection/fetch.sh`,
`install-core-apps.sh`, and the injection + gate in `backend/app/chat.py`.

**Related docs — reference, don't duplicate:**
- `skill/core.md` "Sessions and memory" + the ensure-checklist — the daytime
  chat-note→graph contract and the memory-search call recipe.
- `backend/scripts/seed-skills/memory.md` — the per-task Memory contract (note
  format, scent line, anti-orphan/dedup, split/inline/promote/redirect rules,
  daytime-vs-nightly split, the "After editing notes" indexer step).
- `backend/scripts/seed-skills/reflection.md` — the nightly run's full procedure
  (phases, the non-deferrable chat-note consolidation floor, the read-trace diff,
  the interview prompt, the brief shape).
- `docs/superpowers/specs/2026-06-21-chat-centric-memory.md` — the owner spec for
  the chat-centric model + the memory-search subagent.
- `docs/superpowers/specs/2026-06-18-memory-v2-design.md` — the predecessor
  memory-v2 design (graph + progressive disclosure + reflection).
- `docs/memory-graph-renderer-notes.md` — the Memory app's d3-force + PixiJS
  renderer notes.
- The root `mobius/CLAUDE.md` "Agent context — three layers" and "Inspecting prod
  `/data`" sections — the constitution/skills/memory three-layer model and the
  "`/data` is a git repo, git is the undo" safety-net.

> **Stale references to fix when touched** (verified against this tree, HEAD
> `95db049`):
> - `skill/core.md:257` (the skills-index row) still says the nightly run "write[s]
>   the morning brief **+ open the morning chat**." The nightly run no longer
>   creates a chat — it is opened only on the partner's "Discuss this brief" tap.
>   Drop "+ open the morning chat."
> - `core-apps/reflection/fetch.sh:12` header comment likewise still lists "morning
>   chat" as something the agent does.
> - The root `mobius/CLAUDE.md` "Design philosophy" §3 and "Agent refresh" §1 still
>   point at `/data/shared/memory/inbox.md` "consolidated by nightly dreaming."
>   That inbox path is retired in the live code (`memory.py` ignores it; the
>   per-chat note is the capture surface).
> - `init_memory_graph.py` (docstring `:11`, plus the `inbox.md`/`recent-chats.md`
>   self-heal) still references a "legacy flat-file fallback." It is vestigial
>   under the chat-centric model; those files are created but never injected or
>   indexed.
> - There is **no `docs/persistence/` directory** in this tree, and **no
>   `2026-06-02-knowledge-graph-and-reflection-design.md`** spec — only the two
>   specs listed above. Do not cite either.

## Roadmap — open work toward the vision

The system *recalls the right info* today (short-term from the injected recent
chats; long-term via the search subagent, now that its `ModuleNotFoundError` is
fixed). What's left, ranked by value, with the concrete shape of each fix:

1. **Fast, automatic recall (the #1 gap).** Query-aware recall is currently
   either slow (the `memory_search` LLM subagent, ~40 s, agent-triggered) or
   absent (the injected block is query-*independent*). `auto_memory_search` (off
   by default) runs the LLM search on the first message but blocks the reply for
   up to the timeout. **Fix:** add a FAST, deterministic, query-aware pass that
   runs automatically on a substantive first message and injects the top-K
   candidate notes — keyword/BM25 over note bodies + `description:` scent lines,
   dependency-free, < 1 s — and keep the LLM subagent for the deep contextual
   digs the agent triggers. Two-tier: fast-deterministic always-on (the low
   floor), deep-LLM on demand (the high ceiling). It slots in where
   `_auto_search_memory` is called (`chat.py`), behind a `fast_recall: bool =
   True` flag; embeddings are a later upgrade (needs a model/API). Philosophy
   fit: it's retrieval *infrastructure* injecting DATA scoped to the query — not
   policing — and it preserves the deep path as the high-ceiling option. Measure
   with the `memeval` tiers + a new `FastRecallSystem` and a latency axis.

2. **Validate reflection's new behaviours are firing.** The recommendations,
   new-app-on-recurring-topic, and non-repetitive-interview rules are written but
   were never *observed* firing (the one live night had no recurring-topic
   signal). **Fix:** a reflection-behaviour scenario — seed a multi-"day"
   recurring signal (e.g. 4–5 film chats with staggered dates), run reflection,
   and assert it (a) proposes a new "movie tracker" app OR brings curated film
   recs in the brief, and (b) doesn't re-ask the same interview questions across
   days (diff `runs/*/interviews.md`). Extend `scripts/recall-probe.py`'s
   seed-and-read pattern into a reflection-probe.

3. **Run the harness's reflection-in-the-middle against the REAL runner.** The
   offline proof uses the deterministic `pure_consolidation` stand-in;
   `run_retrieval_eval_with_reflection` with `reflect_fn=live_reflection` +
   `MemorySearchSystem.live` against a seeded container (gated `MEMEVAL_LIVE=1`)
   would prove the real loop end-to-end. Built, not yet exercised.

4. **Make consolidation incremental / unstarvable.** Tonight's consolidation is
   still a single nightly LLM pass a long night can starve (the notes-piled-up
   failure mode). Spread it (a little every night, or a cheap continuous fold)
   so memory compounds rather than rots.

Infra that now supports this work: `scripts/recall-probe.py` (reproducible recall
scoreboard reading the session jsonl), the `memeval` tiers + reflection-in-the-
middle harness, and the `test_memory_search.py` boot smoke-test (guards the
cwd-import class that silently broke the recall arm).
