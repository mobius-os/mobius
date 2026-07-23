# Simplification audit — 2026-07-23

This audit started from planned-restart continuation and expanded outward. Its
test is not “can this code be shorter?” It is:

1. What fact is the real authority?
2. Is the code recording that fact at the moment it is known, or reconstructing
   it later from a proxy?
3. Does an existing durable transition already express the outcome?
4. Which compatibility paths can be moved out of steady-state runtime logic and
   into an idempotent migration?

The restart change is the reference example. The drain already knows the exact
run it stopped. Recording that run as due in the existing park/resume state is
enough; a second restart ledger and boot-time transcript classification are not.

## Ranked opportunities

### 1. Make `ChatRun.id` the only turn-ownership authority

**Confidence: high. Impact: high. Risk: medium; do it as a dedicated
state-machine migration.**

The source already calls `Chat.run_status` a transitional dual-write and names
its removal as the Step-3b follow-up (`backend/app/models.py`, `ChatRun`). Today
one logical fact—“which turn owns this chat?”—is spread across:

- `Chat.run_status` / `Chat.run_started_at`;
- the latest `ChatRun` row;
- the writer actor’s `_run_token_owner` map;
- the in-memory generation counter and its stop/restart handoff maps in
  `backend/app/chat.py`.

That split explains much of the recovery union, wedged-marker sweep, compare
logic, and special handling around Stop, stall, delete, provider limits, and
restart. The repository currently has more than one hundred backend references
to `run_status` / `run_started_at`, despite `ChatRun.id` already being the exact
identity carried by the sink and actor commands.

**Simpler shape:** use the latest nonterminal `ChatRun` as durable ownership and
the run token itself as in-memory ownership. First migrate any legacy
`Chat.run_status="running"` row without a matching `ChatRun` into a legacy
recovery row. Then stop writing/reading the chat-level marker. In a later,
separately reviewed step, replace generation-plus-handoff maps with token
compare-and-swap transitions.

**Intuition:** a generation number answers “is this still the same run?” only
because the exact run identity was not used at that boundary. Once every path
already carries a token, compare the token. Do not remove the latest-run fence
or serial transition lock; those are the correctness mechanism, not accidental
complexity.

### 2. Move legacy app identity adoption out of the install hot path

**Confidence: high. Impact: high. Risk: medium-high because self-hosted systems
can skip releases.**

`backend/app/install.py` interleaves ordinary install/update decisions with
many historical identity shapes: raw and canonical manifest URLs, sourceless
rows, baked platform apps, old platform source directories, catalog renames,
and several `adopt_kind` branches. The comments are careful and the guards are
important, but every new install executes and must preserve this history.

**Simpler shape:** add one explicit, idempotent app-identity migration that
normalizes old live rows to a versioned canonical identity before the installer
runs. Keep ancient-upgrade support in that migration (self-hosted installs
cannot rely on having visited an intermediate release). The steady-state
installer can then match canonical identity, handle an explicit
`previous_id`, and perform install vs update without knowing every historical
storage shape.

**Intuition:** compatibility is data normalization, not a permanent dimension
of the product state machine. Normalize once at the boundary, then let all
ordinary operations use one representation. The migration must remain
upgrade-from-any-supported-version safe; simply deleting the legacy branches
after a date would be wrong.

### 3. Close standalone app isolation by reusing the existing frame protocol

**Confidence: high. Impact: high (security and code ownership). Risk: medium.**

`ARCHITECTURE.md` documents that in-shell mini-apps already run through the
opaque `app-frame.html` protocol, while `/apps/<slug>/` executes the component
in a trusted top-level Möbius document. Building a second standalone permission
or sanitization system would duplicate the harder boundary.

**Simpler shape:** make the standalone route a small trusted installable shell
that owns auth, manifest, service worker, and error chrome, then mounts the same
opaque frame/runtime bridge used in-shell.

**Intuition:** isolation belongs at one process/origin boundary. Reusing the
already-tested boundary is both simpler and stronger than recreating its rules
as a list of forbidden capabilities.

### 4. Retire the flat navigation compatibility mirrors after an explicit
workspace migration

**Confidence: medium. Impact: medium. Risk: medium.**

The workspace blob is documented as authoritative, yet
`frontend/src/hooks/useNavigation.js` still reads and continuously writes
`moebius_active_chat`, `moebius_active_view`, and `moebius_active_app`, and has
boot branches for `!blobValid`. These mirrors were useful during the workspace
rollout, but they keep a second representation of the focused destination and
expand cold-boot combinations.

**Simpler shape:** when no valid workspace blob exists, run one small migration
from the three legacy keys into a valid single-pane workspace blob, record that
the migration succeeded, and thereafter boot from/depend on the workspace
shape. Keep a deliberately simple corrupt-blob fallback (for example Home), not
a permanently dual-written navigation model.

**Intuition:** a compatibility read can be a one-time importer; it does not need
to remain a live mirror forever. Validate this against PWA rollback and old
service-worker behavior before removal.

### 5. Retire the legacy provider-switch bridge behind a served-client floor

**Confidence: medium. Impact: medium. Risk: medium.**

The current provider switch has an authoritative, versioned atomic protocol
(`provider-switch-v1`) plus the older bodyless `/compact` route and
`legacy_switch_ready` compatibility state in `backend/app/routes/chats.py`.
Keeping both means ordinary patch/switch logic must continue recognizing a
historical two-step handoff.

**Simpler shape:** first make served frontend/backend version mismatch explicit
and force a safe client refresh when an old cached shell cannot speak the
current protocol. Once that floor is proven, move any stored legacy handoff to
an idempotent migration and delete the bodyless bridge.

**Intuition:** rolling compatibility should end at a measured client-version
boundary. Without that boundary, “temporary” protocol branches become permanent
state. Do not remove the bridge merely because the current source no longer
calls it; installed PWAs can retain old bundles.

### 6. Give transcript rows shared semantic predicates

**Confidence: high. Impact: medium. Risk: low.**

Automatic continuation exposed a hidden assumption: `role="user"` means the
provider should treat a row as user input, but it does not necessarily mean the
owner authored it. Title selection, elapsed-time context, compaction,
provider-switch reseeding, scrolling, copying, timestamps, and styling can all
accidentally ask the wrong question.

This change introduces shared frontend predicates for automatic-continuation
and owner-user rows, but backend consumers still encode some semantic checks
locally.

**Simpler shape:** put small pure predicates such as
`is_owner_user_message`, `is_product_marker`, and a single transcript label
function in `backend/app/chat_transcript.py`, with the matching frontend helper
as the UI boundary. Consumers ask the semantic question instead of repeating
field combinations.

**Intuition:** do not force one storage field to carry two meanings. A tiny
classification function is simpler than adding more role values or teaching
every consumer about every product-owned row kind.

### 7. Extract a small supervised-loop helper, but keep maintenance jobs
independent

**Confidence: medium. Impact: low-medium. Risk: low.**

The lifespan in `backend/app/main.py` repeats cancellation/error logging/session
open-close scaffolding across wedged-marker, stalled-live, continuation, writer,
and browser-profile jobs. A small helper could own the repeated supervision and
make immediate-vs-delayed first-run behavior explicit.

**Simpler shape:** a `run_supervised(name, tick, wait)` helper, while each job
retains its own task, cadence, database session, and failure isolation.

**Intuition:** share lifecycle boilerplate, not operational fate. Combining all
maintenance work into one scheduler or transaction would look smaller but would
couple unrelated failures and make slow quota work delay chat recovery.

## Things that look simpler but are not recommended

- **Do not add a restart ledger.** The exact `ChatRun` transition is already the
  durable intent boundary.
- **Do not infer restart intent from the pause-card text.** Presentation can be
  missing, duplicated, historical, or written before the authoritative state
  commit.
- **Do not rename `auto_resume_on_limit` in storage/API during this change.** The
  product label can broaden while the legacy wire name remains a harmless
  compatibility detail; a rename would add migration and rolling-client work
  without improving behavior.
- **Do not merge the maintenance loops into one failure domain.** A shared
  wrapper is useful; one giant loop is not.
- **Do not split large files merely to reduce line counts.** `chat.py`,
  `ChatView.jsx`, and `Shell.jsx` are large, but extraction is valuable only
  when it creates a clearer authority boundary (for example transcript
  semantics or run ownership), not when it scatters the same state machine
  across more files.

## Suggested order

1. Ship and observe planned-restart continuation.
2. Do the `ChatRun` single-authority transition as its own expanding-scope
   review, starting with a transition table for every writer of run state.
3. Normalize app identity in a boot migration, then simplify the installer.
4. Reuse the opaque app frame for standalone.
5. Use telemetry/version floors before retiring navigation and provider-switch
   compatibility.
6. Take the transcript predicates and supervised-loop helper opportunistically
   when touching their consumers.
