# Möbius Memory v2 — Consolidated Design (2026-06-18)

Status: design, ready to build on `mobius-test-memv2` then iterate live.
Supersedes the scored/importance model. Synthesises: the owner's design intent,
the in-product agent's own introspection (`demo-logs/19-.../introspection-chat-*.md`),
the OKF review (`.../OKF-learnings-memo.md`), and the build-and-maintain research
(`.../build-and-maintain-protocol.md`).

---

## 0. The simplification lens (philosophy first)

Möbius's rule: **code empowers the agent; it does not police it.** Prevention lives
in the instruction layer + learned memory, not server-side validators. Build for
reversibility (git), not prevention. Reach for a mechanical check ONLY when the
failure is silent + catastrophic + indistinguishable from intentional.

The research protocol was excellent but heavy. We deliberately CUT, for v2.0:

| Research mechanism | Why we cut it now | Where it goes |
|---|---|---|
| `claim_key` / `value_hash` content hashes | Code policing; makes the agent serve the schema instead of the reverse | Future exp. #2/#3 — add only if dedup/staleness actually rots |
| `derivation-hashing` of MOCs | Same; clever but premature for a graph of <50 notes | Future exp. #3 — the smallest-experiment is already specified |
| `entity_keys` synonym index + embedding sweeps | Adds an embedding pipeline; not needed at our scale | Future exp. #2 |
| embedding recall-floor for retrieval | We have no scale problem yet | Future exp. #4 |
| a hard server-side validator/gate | Violates "code doesn't police" | A LINT the reflection agent *runs as a tool*, never a gate |

**What we KEEP (because it's instruction-shaped, not code-shaped):** the consolidation
*decision protocol*, hub-promotion, supersede-not-delete, the orphan-avoidance gate,
bootstrap retirement, MOC growth-at-the-squeeze-point. These become **guidance the
reflection agent reads and applies with judgment** — the agent IS the product. Git is the
undo for every bad call.

Net: v2.0 is mostly *removing* code (the scored selector) and *adding instructions*.

---

## 1. Format — OKF-aligned, minimal

Files under `/data/shared/memory/`, git-tracked. This IS an OKF bundle (free portability,
opens in Obsidian; their parser/viewer work on it). Conventions:

- **Note** = markdown + YAML frontmatter. Required: `type`. Recommended: `title`,
  `description`, `tags`, `timestamp`. **`description` is the scent line** (OKF already
  defines `description` as the index/search snippet — reuse it, don't invent a field).
- **`type` is a small pinned enum** (`fact`, `hub`, `moc`, `bootstrap`) — NOT OKF's
  free-string. Reflection normalises stragglers. (Pinning the enum is the one place we're
  stricter than OKF, on purpose: stops 30 near-synonym types.)
- **Links: relative markdown links, the relationship TYPED IN PROSE** —
  `see also for units: [units preference](../hubs/units.md)`. Stays OKF-conformant
  (OKF says relationship meaning lives in prose) AND keeps our retrieval cue. Relative,
  because OKF's shipped viewer rejects `/`-absolute.
- **`index.md` per directory = the MOC / router.** Top-level `index.md` = the root
  router. (Confirms the owner's guess: OKF's per-dir `index.md` *is* the MOC.)
- **`log.md`** (newest-first, ISO-date) = the reflection/consolidation audit trail. New
  surface we were missing; free from OKF.
- **`# Citations`** section in a note = provenance (which chat/source; "user stated on
  <date>").
- **`README.md`** in the memory root = self-describing schema (the conventions below), so
  the owner or any future agent understands the bundle without our code.

No `claim_key`/`value_hash`/`derived_from`/`importance`/`access_count`. Frontmatter stays
human-writable.

## 2. Capture — turn-level reflex, dumb + append-only (the proven fix)

The introspection proved the bug: capture is gated on *tool/build activity*, so
pure-conversation facts (the dog, metric units, espresso) are dropped. Fix, in the agent's
own words: decouple capture from the build pipeline.

- **Where:** the system prompt's "Sessions and memory" section (core.md), as an
  always-on reflex — **NOT** the ensure-checklist (the checklist is conceptually "after I
  built something"; a no-tools turn never walks it). Leave a pointer in the checklist.
- **Trigger:** the partner states a stable first-person fact (preference, possession,
  routine, identity, relationship, location, recurring tool) **that would change how
  you'd answer a future unrelated chat.** That clause is the noise filter (metric units +
  a dog pass; "busy today" doesn't).
- **Action:** one atomic line appended to `inbox.md`, fire-and-forget, as part of that
  turn's reply. Carry enough context that the nightly pass can resolve it later (don't
  write a bare fragment). Don't interrogate — capture only what's volunteered.
- **Name the failure mode IN the rule:** "...this fires on conversational turns with no
  tools, which is exactly where it's currently missed" — naming it is what makes a future
  agent catch it.
- **Daytime MUST NOT** decide add-vs-merge, resolve entities, write typed links, or touch
  notes/MOCs. All structure is deferred to reflection (avoids the MemGPT "hurried myopic
  edit" failure). Capture is the Generative-Agents memory stream: append now, structure
  at night.

## 3. Retrieval — inject the router + recency, then traverse; NO ranking

This is mostly *deleting* today's scored selector.

- `build_memory_block` injects: **the root `index.md` (router) + `recent-chats.md`
  (recency) + the persistent `inbox.md` tail.** That's it. No hot-note scoring, no
  `importance`, no `access_count`, no `usage.json`-driven selection.
- The agent **traverses on demand**: it reads the router's scent lines, decides which
  topics the current conversation touches, and opens those notes (and their direct
  `see also` targets — **one hop**) with its own Read tool. Retrieval is conditioned on
  the actual question, not a fixed bundle.
- The router must be a **ROUTER, not a passive listing**: each line = topic label in the
  user's words + a one-line scent (decide open/skip without opening) + the link.
- Recency stays a **separate channel** (recent-chats summaries) for time-sensitive facts
  before they graduate into notes.
- Budget governs the always-loaded layer (router + recency + inbox), not total notes —
  the graph can grow unbounded; only the router competes for the injection budget.

## 4. Consolidation — the reflection skill (judgment, not hashes)

`reflection.md` gains a "consolidate the inbox" protocol the agent follows with judgment.
For each inbox line, pick exactly one:

- **drop** — already known, or not reducible to one durable fact.
- **append** — same fact, new evidence → add a dated bullet + citation to the note.
- **new note** — genuinely new concept. **Hard rule: never create a note without either
  matching an existing concept OR giving it ≥1 typed outbound link** (Luhmann: no slip
  without a neighbour → orphans are structurally impossible).
- **link** — concept exists, only a typed relation is missing.
- **merge → hub** — the same shared fact is referenced by ≥3 topics → make it ONE hub note
  many topics point at (the anti-flood mechanism: one hub, not N×N cross-links). Don't
  duplicate a shared preference into each topic.
- **supersede** — a fact that changes over time got a new value → mark the old note
  `superseded`, link `supersedes`/`superseded_by`, **never delete** (git keeps history;
  the owner is authoritative, so mark which is current — never average/hedge).
- **contradiction** — a non-changing fact conflicts → keep both, note the conflict under
  the relevant hub, flag for the owner.
- **uncertain** — can't resolve cleanly → keep it marked uncertain, never force a merge.

Growth + upkeep (also judgment):
- **A topic earns its own MOC/`index.md`** when it has ~7-10 notes (the "squeeze point") —
  assemble the MOC and repoint the router line at it.
- **Promote to a hub** at fan-in ≥3.
- **Retire the bootstrap note (F5):** if the graph holds real user notes, archive
  `this-instance-is-fresh` and drop its router line. (The note already says to; reflection
  just never did it.)
- **Run a light LINT as a TOOL** (not a gate): a small script the reflection agent invokes
  to list dangling links + orphan notes (notes with no inbound link) + router lines whose
  target is gone. The agent fixes what it surfaces. Cheap grep, files-only.
- Write a `log.md` line for the night; `pm-commit` so the consolidation is reversible.

Reflection may **fire early** when the inbox gets heavy (salience as a *consolidation
trigger only* — never at retrieval). Owner decision (see §6).

## 5. Transferable subgraphs — honest stance (future)

Anti-flood hubs and self-contained directories are in direct opposition; you can't have
both. v2 ships **lossy interface export**: a directory's `index.md` (MOC) is its public
API; cross-cutting links target the MOC; on export, cross-cutting links degrade to *named
stubs* the importing agent re-resolves. Invariant the lint can check cheaply: cross-dir
inbound links should target an MOC, not a deep note. Not on the v2.0 critical path.

## 6. Two decisions for the owner (small, principled exceptions to "no heuristic")

Both preserve **no ranking of retrieval outputs**:
1. **Early-fire consolidation** on a heavy inbox (salience as a consolidation *trigger*).
   Recommend: yes, simple line-count threshold.
2. **Embedding recall-floor** to widen the traversal seed-set when the router comes up
   thin at scale. Recommend: **defer** — it adds an embedding dependency; revisit only if
   experiment #4 shows the cliff.

## 7. Build order (vertical slices, each tested live on mobius-test-memv2)

1. **Capture reflex** (core.md + memory.md) — fixes the proven gap; lowest risk. Test: fresh
   chats sharing personal facts → confirm they land in `inbox.md` on no-tool turns.
2. **Retrieval simplification** (memory.py) — delete the scored selector; inject
   router+recency+inbox. Test: `build_memory_block` returns router+recency; full suite still
   compiles/serves.
3. **Consolidation protocol + F5** (reflection.md) + **seed-memory** (router-format
   `index.md`, note frontmatter w/ `description` scent, bootstrap-with-retirement). Test:
   run a real reflection pass on memv2's existing RICH inbox → inspect the graph it builds
   (decision-table behaviour, a hub, the bootstrap retired) + the `log.md`.
4. **Lint tool** (a small script in the agent's toolbox) — dangling/orphan/router check.

Each slice: edit in the `memv2-impl` worktree → `docker cp` into `mobius-test-memv2`
(code → `/app/app`, skills → `/data/shared/skills`, seed-memory → fresh `/data/shared/memory`
when testing capture from scratch) → restart uvicorn → drive via the API → introspect →
refine. Confirm with the owner before anything touches prod.

## 8. Deferred research (the 5 experiments — only build if the simple version rots)

1. Granularity oscillation (split↔merge churn on unchanged data) → hysteresis rule.
2. Entity-resolution recall ceiling → `entity_keys` + embedding sweep.
3. Does derivation-hashing bound scent-line lies → add `derived_from` hashes.
4. Retrieval cliff size + does the embedding seed-floor restore it.
5. Inbox-clearing as the binding constraint (super-linear cost) → incremental + weekly
   deep-dream split.

Each has a smallest files-only experiment specified in `build-and-maintain-protocol.md §5`.

---

## 9. Live-iteration learnings (memv2 build, 2026-06-18→20)

**Slice 1 (capture reflex) — built + iterated live on `mobius-test-memv2`:**
- Baseline (introspection): pure-conversation facts captured ~0% (capture was gated on tool/build activity).
- v1 reflex (decoupled, in core.md "Sessions and memory"): 1/3 — caught a standalone preference, missed a fact-in-a-question and a preference-aimed-at-the-agent.
- Introspected the miss; the agent diagnosed "my reflex keyed on **speech-act, not content** — it fires for disclosures *shaped like* disclosures." v2 added its two fixes: "capture by what's revealed, not how it's said" + name the two blind spots (preference-aimed-at-you → *acting on it doesn't discharge it*; fact-buried-in-a-question). → 2/3.
- v3 added the agent's end-of-turn test ("did the partner reveal anything that should still be true three chats from now?"). Result was NOISY (0/3 on one run) — on a quick question the agent frames it "no build involved, I'll just answer" and skips capture.

**THE KEY LEARNING: a pure daytime prompt-reflex is inherently unreliable for facts-in-questions** — the agent's "just answer efficiently" drive overrides it, with high run-to-run variance. **Do NOT chase a perfect daytime reflex.** The robust design is two-layer:
- **Daytime reflex = cheap, best-effort, same-day hint.** Keep the improved wording (it reliably catches *explicit* disclosures + makes them available before the nightly pass). Accept that it misses subtle ones.
- **Reflection-reads-TRANSCRIPTS = the reliable capture.** Proven in session-memdata: reflection extracted the full user model from the day's chat transcripts even when the inbox held only 2 lines. So consolidation's input is the day's transcripts + the inbox, and the inbox is an optimization, not the source of truth. This matches the build-and-maintain "daytime stays dumb; reflection owns judgment" spine and OKF's `conversation_learner` (LLM-judge over trajectories).

Implication for §2/§4: stop treating the inbox as the capture mechanism. The inbox is a best-effort fast-path; **reflection must read the day's transcripts as its primary capture source.** (This also removes the pressure to over-engineer the daytime prompt — philosophy-aligned: the cheap reflex empowers, the nightly judgment guarantees.)

## 10. Refinements folded in from the adversarial review (Codex)

Cheap, philosophy-aligned safety the simplification can keep without the hashing machinery:
- **`title:` is the hand-maintained canonical claim key.** Before creating a new note, reflection greps normalized titles for overlap (instruction). The lint reports duplicate normalized `title:` values (a grep, not a gate). Recovers most dedup safety without `claim_key`/`value_hash`.
- **Stale-scent-line guard via git, no hashing:** during reflection, if a note linked from an `index.md` has a commit newer than that `index.md`'s last commit, re-read the note and refresh/confirm its router scent line before committing. A `git log` comparison — closes the #2 "lying scent line misroutes the agent" failure as an instruction, not code.
- **Flag (don't fix yet):** entity-resolution surface-form drift ("Memv2" vs "memory v2") silently forks hubs and suppresses fan-in promotion — highest long-term rot risk, but not catastrophic <50 notes. The title-grep above is a partial guard; the full embedding entity-sweep stays deferred (experiment #2) until the graph is large enough to need it.

**Slice-1 capture reflex — VALIDATED across two sessions (2026-06-20).** Session 2 (fresh
persona) captured 5/6 incl. both previously-failing blind spots (fact-in-question, preference-
aimed-at-agent), plus auto cross-linking + a supersession flag. The improved wording
(revealed-not-said + 2 named blind spots + end-of-turn test) generalizes. Daytime reflex is a
solid best-effort hint; reflection-reads-transcripts remains the reliable backstop. Done.
