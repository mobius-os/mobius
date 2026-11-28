# Owner-input requests

Status: implemented. `ARCHITECTURE.md` ("Saved owner-input pauses")
summarizes what ships.

## The contract

**Ordinary meaning.** An agent that needs its owner records what it needs and
keeps doing every piece of work that does not depend on the answer. When it has
nothing left that it may do, its turn ends and the owner sees one card holding
everything still open. Work that restarts by itself (a timer, a helper result,
a scheduled run) carries on around an open card and may revise it.

**State owner.** One durable per-chat list of *input requests*. The agent edits
it; the platform publishes it; the owner (or any participant that can read the
card) answers it.

**Must never happen.**

1. An agent acts on something it asked about before the answer exists. Asking
   is not consent, an unanswered request is not consent, and an edited request
   does not inherit an earlier answer.
2. Assistant output appears after a card in the same turn, or output is hidden
   or dropped to make a card terminal.
3. A secret value reaches the model, the transcript, a log, or this list.
4. A restart or approved action runs twice, or runs for a request the owner
   did not answer in its current form.
5. A request silently disappears: every request ends answered or withdrawn
   (with a reason), and the agent's next turn reports how it ended.

## Why a single card slot could not express it

`Chat.pending_question_id` is a single slot and the card *is* a transcript
block. Saving the block is therefore simultaneously "the agent asked", "the
owner can see it", "the turn ends" and "no other turn may start"
(`StartTurnBlockedByPendingQuestion`). Four meanings on one write is why task
A's question stops tasks B and C, why a scheduled run cannot ask, and why a
card cannot change after it is saved.

## Design: separate asking from publishing

Two layers. The lower one is the existing saved-card machinery, kept because
its guarantees are the ones needed for *showing* a card. The upper one is new.

```
agent ──add/update/withdraw──▶ request list (durable rows)
                                     │  at turn end, if anything is open
                                     ▼
                         one published card (transcript block,
                         save-before-broadcast, terminal by construction)
                                     │  owner submits all or some
                                     ▼
                       answers recorded on rows ──▶ continuation turn
                                                    (gets the open list)
```

### Request list

Table `owner_input_requests`, one row per request:

| column | meaning |
| - | - |
| `id` | stable identity, survives every republication; drafts and answers key on it |
| `chat_id`, `goal_id`, `created_by_run_id` | ownership |
| `key` | agent-chosen stable name, unique per chat among open rows and the only reference an agent passes; `add` with an existing open key merges exactly like `update`, so a retry or a post-compaction re-ask cannot duplicate, and an identical re-add changes nothing |
| `revision` | bumps on every *material* edit (prompt, fields, options, action binding) |
| `spec_json` | header, prompt, context, fields (below) |
| `blocks_json` | Goal task ids and/or a plain-language description of the work that waits |
| `status` | `open`, `answered`, `withdrawn` |
| `answer_json`, `answered_revision`, `answered_by`, `answered_at` | the answer and the revision it was given against |
| `outcome_json` | how it was settled: withdrawal or resolve reason, and who settled it |
| `published_revision`, `published_question_id` | which revision the owner has been shown, and in which card |

Rows are the authority for *what is being asked*. The transcript block is the
authority for *what was shown and answered at that moment* (history must keep
rendering exactly what the owner saw).

### Kinds

Every request has a `kind` (stored in `spec_json`; default `question`):

- `question` (default): one `choice` field (options with immutable ids,
  `multiple: true` allows several selections, free text always allowed
  alongside) or one `text` field.
- `approval`: `prompt`, two or three options, and `work_key`, the exact action
  it authorises.
- `restart`: no other fields.
- `secret`: `title`, `description`, sealed `fields` and the pre-authored
  consumer (`command`, `cwd`) or `preset: owner_credentials`. Only prompts,
  labels and the consumer live here; the value plane is the existing
  `saved_secure_inputs` one-use consumer.

Every kind is an entry of the one request-list card; each entry settles on
its own and the card never holds the chat. See "Sensitive kinds, as built"
below.

### One tool

`owner_input` with `op`: `add`, `update`, `withdraw`, `resolve`, `list`, and
`kind` on `add`. A request is named by `key`; `id` appears only in results
(the sealed submit path uses it). Every result returns the complete open list
(id, key, revision, status, what it blocks, whether the owner has seen the
current revision) and names what happened: `add` is `created`, `revised` or
`unchanged` (an identical re-add bumps no revision), with one short
`next_action`; `update` is `revised` or `unchanged`; `withdraw` is `withdrawn`,
plus `released` for an approval (its work claim is released when this chat
owns it and it is neither completed nor answered, so no second call is
needed); `resolve` is `resolved`; `list` returns `open` and `settled`.

**What was settled.** The same open list is injected at the start of every
turn next to `active_waits`, together with the requests settled since the
chat's last successfully completed run (`settled_since_last_turn`: closed
after that run started, newest last, at most ten). Each settled entry names
its `key`, `kind`, `status` (`answered` or `withdrawn`) and who settled it
(`owner`, `agent:owner_message`, `agent:found`, `agent` or `platform`); an
answer carries the selected option ids and labels and any typed text (a
secret only its consumer outcome, never values; a restart its recorded
outcome); an approval carries `option_chosen` (true only when the owner
selected an explicit option of the answered revision; the agent reads which
from its label); a withdrawal or agent resolve carries its reason. The block
renders whenever something is open or was settled. The `list` result, a
steered owner message and the compaction restore carry the same section.
The model's memory is therefore never the source of truth,
which is what makes this survive compaction, restart and provider switch, and
why it is provider-neutral: neither Claude's `AskUserQuestion`/`defer` nor
Codex's native question tool is involved.

`request_question`, `request_approval`, `request_restart` and `request_secret`
are removed, with their agent routes (`question`,
`approval`, `restart-request`, `secure-inputs/{chat}/saved`) and the
`owner_approval.py` helper. `secure-input.py run` and `owner-credentials` now
record a secret request on the list. The answer and settle endpoints the
owner's browser uses are unchanged.

**One exception: the key vault's `request_key`.** Asking to use a saved key on
new sites, or for a key the owner has not saved, stays its own control tool and
route (`key-request`, `routes/key_requests.py`). The vault records the request
and answers at once when it can; otherwise the ask is saved as a barring key
card that ends the turn, exactly as the vault ships it. Its answer is checked
against the vault rather than settled as a list entry, so it is not an entry
kind. Its receipt is the only owner-card receipt that ends a turn (Claude's
turn-end hook, Codex's `turn-end` rendezvous), and the visible-answer check
that runs before a card is saved (`answer_check.reason`, `before_card`)
applies only to it; every other request is shown after the end-of-turn check.

### Turn lifecycle

- **Asking does not end the turn.** No provider cut, no receipt that stops
  generation, for every kind (a key card, above, is not a request kind).
- **Publication happens at natural turn end.** When a root run completes and
  the chat has open requests and no barring card (`pending_question_id`, only
  ever a key card, a card saved by an earlier release or a native
  question), the
  platform appends one card block holding all of them, every kind, to the
  final assistant message through the existing `QuestionCommit` save-before-broadcast path and
  records it as `open_request_card_id` (replacing, and thereby superseding, any
  earlier list card). The same commit stamps each shown row's
  `published_revision`/`published_question_id`. A turn that ends with nothing
  open retires the marker through the writer (`RetireRequestCard`). A barring
  card saved later, or a transcript replacement, also retires it; the rows stay
  open and unshown until the next turn end.
  The card is terminal because the turn is already over, so no provider cut
  is involved.
- **A turn that ends with open requests and no closing text** still publishes;
  guidance asks the agent to say where things stand first.
- **Stop** leaves rows open and publishes nothing; the next turn sees the list.

### Work continuing around an open card

A barring card blocks every turn start through `pending_question_id` checks
at roughly fifteen admission and recovery sites; those guards are unchanged. A request-list card is *shown* but does not *bar*, so it
is tracked by its own marker, `Chat.open_request_card_id`, and never sets
`pending_question_id`. The sites that must treat it like an open card are the
few that project "waiting for the owner" (drawer and Goal handoff state, Goal
settlement at turn end, answer acceptance). They take the list card from the
request rows (`shown_request_card`: the card that still shows an open
request); the drawer and list projections then go through
`questions.owner_waiting_card_id`, the Goal handoff through
`goal_waits_on_request_card`, and answer acceptance checks the exact
`open_request_card_id`. So a card
whose requests were all resolved or withdrawn in a turn that then failed or
stopped never reads as waiting. A list card counts as waiting only while no
turn runs, and a Goal counts as handed to its owner only when the open card
shows a request that Goal recorded (`goal_id` on the row). Transcript-tail
rebuilds of `pending_question_id` (startup reconcile, park, wedged-run
recovery, transcript replacement) skip list cards. Relaxing
the barring guards one by one was rejected: a missed guard would let a turn
run past a real approval or restart card, while a missed projection site only
under-reports waiting.

For a list card:

- An **owner answer** (full or partial) starts the continuation.
- An **owner message** starts a turn immediately. The message is delivered with
  the open list and a note that the owner wrote instead of answering (when the
  run carries `owner_input_at` and its newest user row is visible, i.e. not a
  card answer); the agent settles or withdraws what the message resolves. A
  message steered into a running turn carries the list too, and the note only
  while a card already shows an open request (`open_card_shows_request`): a
  request recorded earlier in that turn has not been shown yet. The
  shown card stays answerable during that turn; at the turn's end it is marked
  superseded and whatever stays open is republished.
- A **platform continuation** (wait fired, helper result, scheduled run,
  dependency result) starts normally. Same supersede-then-republish rule.
- A **branch** copies the card as inert history: the branch has no
  `open_request_card_id` and no request rows, and a list card never uses the
  "unanswered card at the tail" fallback, so the copy refuses answers.

Republishing never loses the owner's draft: drafts key on request id + field
name, and a request whose revision did not change keeps its draft verbatim.

### Answering

- The owner may submit with some requests unanswered. Answered requests are
  recorded and removed from the list handed back as "answered"; unanswered
  ones stay `open` and reappear on the next card ("carried over"). A question
  or approval answer, full or partial, clears `open_request_card_id` unless
  the card still has a secret or unpressed restart entry it did not settle:
  those act on their own, so they stay usable on the same card (their rows
  keep `published_question_id`) until the continuation turn's end republishes.
  A Restart now press or a secret outcome settles only its own entry and
  leaves the card open. An answer to a list card that
  is no longer the current one is refused with the ordinary 410 "no longer
  accepting answers", in the route and again inside the writer. Save-only
  answers (`question-answers`) are refused for list cards, since no turn would
  hear them.
- An answer names the revision it was given against. If any answered request
  has moved on, the whole submission is refused (410, `code: card_out_of_date`)
  before anything is written; the updated card follows at the next turn end
  and keeps the drafts of unchanged requests.
- The continuation's text, composed for the transcript, lists each answered
  request; what the agent acts on is the settled section built from the rows
  (above), keyed by request `key`, plus the remaining open list. Typed replies
  keep the "treat as a message, not a choice" framing.
- `on_answer: "close"` stays per option and applies only when every submitted
  selection is a close option and nothing remains open.

### Not acting on a blocked request

The list records what each request blocks. A Goal task named in
`blocks.tasks` cannot be marked completed while the request is open, and a
Goal cannot complete while a request of that Goal names one of its tasks
there (`require_tasks_not_waiting_on_owner`); with a Goal attached, unknown
task ids are refused. `blocks.work` is a description only. A request with no
task fence, such as an offer made at a handoff, does not hold completion: when
the Goal completes, a still-open approval's work claim is detached from the
Goal and stays held for its answer, unless the Goal names it in
`finished_claims`. Approvals keep their real fence: the work claim exists but
the action has no answer. This is guidance plus state, not a validator on
arbitrary tool calls (Möbius does not police tool use).

### Background and scheduled runs

A run that is not a live owner chat may `add`/`update` requests on its chat's
list. It never blocks its schedule: the request is published on that chat when
the run ends, and later runs see it in their open list and may revise it.
Background runs may record requests but never wait on a card.

What this rests on, as built:

- **Who may record.** Any top-level run is offered `owner_input` and holds the
  owner-scoped run bearer the route requires (`get_agent_run_principal`),
  including Wait, helper-result, dependency, settlement, resume, self check-in,
  queue-promoted and app-started (`initiated_by_app_id`, `created_by_app_id`)
  turns. An app token itself cannot call the route, and a delegated helper is
  neither offered the tool nor accepted by it (it uses `ask_parent`).
- **Nothing holds a wake.** Every admission guard reads `pending_question_id`
  only, so a list card never queues, defers or cancels a Wait result, helper
  result, dependency result or resume; a barring card holds them exactly as
  before.
- **One push per new question.** Each completing turn republishes the open list
  as a new card (it must stay the turn's last item), but the owner is notified
  only when that card shows a revision no earlier card showed. A quiet
  recurring run republishes without pushing; a revised or new request pushes.
- **No "keep the old card".** A completed turn always has renderable output (a
  turn with none is the lost-reply failure and publishes nothing), so leaving
  the previous card current would put that output after the card.
- **Not yet covered.** A run that fails or is parked publishes nothing; what it
  added is shown at the next completed turn. An app whose every run opens a new
  chat (Reflection) gets a fresh list per run, so its requests do not carry
  across runs unless the app reuses one chat (for example `app-chats/start`
  with a stable scope).

## Sensitive kinds, as built

**Asking.** `add` validates with the existing validators (the Approval card's
limits; `secure_inputs.validate_request_spec` with mode `sealed` and
`saved_secure_inputs.validate_consumer_spec`). A restart is refused while no
restart-loadable boot is current, and a chat has at most one open restart
request. A secret is refused in an app-created chat, whose sealed entries no
one can submit. For a new approval, every check that could refuse the request
runs first, then its `work_key` is claimed with `claim_work`: a chat that
loses the claim to a peer, or finds the action completed, records no request
and gets the `held_by_peer`/`completed` follower result (with the open list).
Every check includes `blocks`, and a refusal after the claim (such as losing
a same-key race with a different `work_key`) leaves no claim behind. Re-adding
the same key does not claim again. An approval's `work_key`, a secret's
consumer and every request's kind are immutable: `update` may change wording
only, and changing the action means withdrawing (which releases the claim this
chat holds) and adding a new request. `resolve` is refused for
these kinds; `withdraw` is allowed, except for a secret whose consumer is
running (its fixed outcome settles it). `blocks`, the open snapshot (each
row's `kind`, an approval's `work_key`, a secret's field labels), the
turn-start injection and the task-completion guard treat every kind alike.

**One card.** `chat._publish_open_owner_requests` withdraws, with the reason,
any request that can no longer be shown as asked
(`owner_input_cards.unshowable_reason`: an approval whose claim this chat no
longer holds, a restart with no restart-loadable boot), then shows every open
request on one `input_requests: true` card through `compose_card`, oldest
first. Each entry of `questions[]` is one request (`id` = request id,
`request_key`, `request_revision`) and names its `kind`:

```json
{"type": "question", "question_id": "…", "response_mode": "continuation",
 "input_requests": true,
 "questions": [
  {"id": "<req>", "request_key": "tone", "request_revision": 1, "kind": "question",
   "header": "Question", "question": "How formal?", "options": []},
  {"id": "<req>", "request_key": "publish", "request_revision": 1, "kind": "approval",
   "header": "Approval", "question": "Publish the notes?", "action_key": "site:notes:publish",
   "options": [{"id": "0", "label": "Publish", "description": "…"},
               {"id": "1", "label": "Not now", "description": "…"}]},
  {"id": "<req>", "request_key": "restart", "request_revision": 1, "kind": "restart",
   "header": "Restart Möbius", "question": "Restart Möbius now? …",
   "options": [{"id": "<restart option>", "label": "Restart now", "on_answer": "close",
                "description": "…"}],
   "platform_action": {"version": 2, "type": "restart", "action_id": "platform-restart:…",
     "restart_option_id": "<restart option>", "requirement": {…}, "status": "awaiting_owner",
     "wait_id": "activation-<question_id>"}},
  {"id": "<req>", "request_key": "mail-key", "request_revision": 1, "kind": "secret",
   "header": "Secure input", "question": "Connect the mail service", "options": [],
   "secure_input": {"title": "…", "description": "…", "mode": "sealed",
                    "fields": [{"name": "api_key", "label": "API key", "type": "password",
                                "autocomplete": "off"}]}}],
 "request_states": {"<req>": {"status": "consuming"}}}
```

A restart entry's action is derived at publication
(`owner_input_cards.restart_entry`) for the publishing run and the boot current
then (the card identity includes that boot), and the card commit
(`QuestionCommit` with `activation_wait`) links its activation wait. The same
commit creates each secret entry's private receipt (`SavedSecureInput`, keyed
by the request id, created once and reused by every later card that shows the
request; `owner_input_requests.stage_sealed_entries`). The card never sets
`pending_question_id`: owner messages, new turns, timers, helper results and
wait results proceed around every kind.

**Settling an entry.** Each entry settles on its own, and its row is answered
in the writer transaction that settles it. What one entry has settled on its
own is recorded in the block's `request_states[<request id>]` (carried across
stale streaming snapshots by `_question_answer_fields`, and broadcast as
`input_request_settled {question_id, request_id, kind, state}`); other entries
stay open on the card.

- question and approval: the shared partial-answer path (`answers` keyed by
  prompt, `selected_options` keyed by request id), alone or together.
  An approval row is answered only with an explicit option id of the shown
  revision: selections are validated in the route and re-bound inside the
  writer (`require_card_answers_current` → `validate_saved_answer`), and typed
  text, even text equal to a label, reaches the agent as a message and leaves
  the approval open for the next card. A stale revision refuses the whole
  submission. Declining releases nothing; the agent settles its claim. A
  submitted answer closes the card (`open_request_card_id` cleared) unless an
  unsettled secret or unpressed restart entry remains on it, which stays
  usable there; the rest is republished at the next turn end.
- restart: a message carrying only `selected_options: {<entry id>: [<restart
  option id>]}` and `question_id` is routed to `ResolvePlatformRestartCard`
  with `request_id`. It is accepted only while this card shows the open
  request's current revision (`published_question_id`), approves the wait and
  answers the row in one commit, and dispatches only on that first commit; a
  duplicate click or retry finds `request_states` and returns the press
  without dispatching. Combining a press with other answers is refused.
  Written text for a restart entry is ordinary card feedback: it answers the
  row with the text, and withdraws the unpressed wait, without a restart.
- secret: values go to `POST /api/secure-inputs/{chat}/{request id}/submit`
  only; a secret entry in the answers path is refused (409). The claim
  (`ClaimSecureInput`, entry mode) requires the chat's current card to show
  the revision the owner filled in and moves the one receipt pending →
  consuming; the fixed outcome (`SettleSecureInput`, entry mode) answers the
  row whatever card now shows it. Cancel carries the card id and the entry's
  request revision and is refused, like a submission, when either no longer
  matches. Either outcome records `request_states`, and
  queues one hidden continuation exactly like a partial answer, without
  closing the card. A later ready boot settles an open restart entry
  ("Möbius restarted", `_settle_activation_entry`).

**Guarantees kept per entry.**

| Guarantee a barring card gives | Per-entry mechanism | Test |
| - | - | - |
| A restart card cannot be pressed after it stops being the open card | press requires `published_question_id == card` at the entry's revision | `test_a_superseded_cards_restart_cannot_be_pressed_and_one_wait_stands` |
| One activation wait per open restart | a list card that stops offering an unpressed restart withdraws its wait (`stage_unpressed_restart_wait_withdrawn`, on retire, supersede and answer) | same, and `test_replacing_the_transcript_returns_each_kind_to_the_list[restart]` |
| A sealed submission only for the open card | claim requires the current card at the shown revision | `test_a_secret_is_claimable_only_on_the_current_card_at_the_shown_revision` |
| Sealed values delivered once though turns run | one receipt per request reused across cards; republication shows a running consumer in progress; no withdraw while consuming; entry submission is not fenced by run generation (Stop cancels the task and settles it as unknown) | `test_a_consumer_running_across_a_republish_is_shown_in_progress_and_runs_once` |
| An approval is answered only on its own card | grant only by explicit option id, re-bound in the writer; typed text leaves it open | `test_typed_text_on_an_approval_is_a_message_never_a_grant`, `test_the_writer_rebinds_selections_whatever_a_caller_sends` |
| Stop ended the barring card | Stop leaves the card open: a pending secret receipt stays pending, a running one settles as outcome unknown, an unpressed restart's wait is cancelled (its button still restarts once) | `test_stop_*` in `test_owner_input_sensitive_requests.py` |
| A secret's Cancel only for what the owner was shown | Cancel carries the card id and request revision; the writer refuses a mismatch like a submission | `test_a_stale_tabs_cancel_of_a_reworded_secret_cancels_nothing` |
| No approval claim without its request | every check, `blocks` included, runs before the claim; a refusal after it (a same-key race) releases it | `test_a_refused_approval_never_takes_the_claim`, `test_an_approval_refused_for_its_blocks_takes_no_claim`, `test_an_approval_losing_a_same_key_race_releases_the_claim_it_took` |
| An edit never loses a concurrent change or races an owner answer | `update`, `withdraw`, `resolve` and add-on-existing-key mutate the row only at the revision and status they read (server-side; the merge is retried once, then refused with the current state) | `test_an_update_racing_another_update_keeps_both_changes`, `test_an_edit_racing_an_owners_answer_never_overwrites_it` |

The activation barrier is unchanged: an **approved** wait still holds later
input until the ready-boot continuation (`activation_barrier_wait_id`), and an
unpressed entry's wait is not approved, so it holds nothing.

**Ending without an answer.** A retired or superseded list card, a transcript
replacement or a partial answer leaves its unanswered rows open and unshown,
for the next card; after a partial answer, secret and unpressed restart
entries stay usable on the same card until that next card. Stop leaves the list card open (above). Deletion leaves
rows as they are, and a branch copies no rows and no receipts, so its copy of
the card refuses answers and sealed submissions.

**Legacy cards.** Barring Approval, Restart and Secure input cards saved by
earlier releases have no request row behind them. They keep their marker,
block shape and settle paths (`_apply_platform_restart_feedback`, legacy
`ClaimSecureInput`/`SettleSecureInput` keyed by the card id,
generation-fenced) and still bar turns and wakes; a turn end publishes
nothing over one, and settling one leaves the request list untouched.
`tests/owner_input_fixtures` saves them as those releases did
(`save_legacy_approval`, `save_legacy_restart`, `save_legacy_secret`).
Transcripts are not migrated.

**Turn-ending machinery kept.** A helper's `ask_parent` receipt still ends
its turn, so Claude's PostToolUse hook (`claim_owner_card_end`), the sink's
`_request_finish_turn_after_owner_card`, Codex's `begin_finish_after_owner_card`
and `POST /turn-end` (now `routes/turn_end.py`) and `record_turn_end` remain.
Narrowed: `ChatEventSink.ends_turn` accepts a recorded receipt or a key card
this turn saved (`has_key_card`), `turn_end_receipt_id` recognizes those two
shapes, and the Claude hook watches `ask_parent`, `request_key` and `Bash`.
Removed: `has_continuation_card`; `owner_card_question_id` is stamped only for
the key card's saving tool.
`owner_card_receipt_id` and `chat_transcript`'s fold of the old tools remain
for history.

**Known limits.** A legacy barring card in a recurring or app chat bars its
later runs until settled. Ready-boot observation (`project_restart_observations`)
is projected only for legacy Restart cards; a restart entry shows its
activation through `request_states`. Stopping a chat while its key card waits
clears that card without a turn end, so requests the key card retired stay
open but unshown until the next turn ends.

A chat that follows a list card with `wait_for_decision` resolves on any
answer to that card, not on its own entry: a follower must check that the
entry it follows was answered, and is woken (superseded) when the card is
republished. Following one request rather than its card is not built.

An older client against a backend rolled back across this change could render
a secret entry as a free-text question, and the old backend would accept a
typed answer to it. Rolling back across this change while secret entries are
open is unsupported.

**Keeping the list in view.** The injected copy is a bounded summary: its
body fits `context_budget.OPEN_REQUESTS_CONTEXT_CHARS` (7,000 ASCII
characters; the whole block with its preamble at most about 8,000), so it
stays inline in a Claude hook's additionalContext (spilled to a file above
about 10,000). Long prompts and blocked-work text become marked excerpts;
if that is not enough, rows drop header, option labels, secret field labels
and blocked-work text, then, newest first, keep only their identity. Rows so
cut are marked `shortened` and the preamble says to call `owner_input` list,
whose result stays whole. The settled section (requests settled since the
last completed turn) takes at most 2,000 of those characters (texts cut to
200, oldest rows dropped first) and is shown even when nothing is open. After an in-turn provider compaction the list and the settled section
are restored, read fresh, beside the Goal brief (`compaction_context.py`).
On Codex, a SessionStart(compact) hook restores both in the request that continues the compacted turn. If the hook is not discovered, or does not deliver by the model's first output after a compaction the runner sees in its event stream, the runner steers the same text into the live turn and the model gets it at its next request; the hook and the steer never both deliver. If Codex accepts the hook's text but drops it, or nothing follows the compaction in that turn, the next turn's start restores it. A steer that arrives during the final answer adds one model request.
An owner message steered into a running turn carries
both with the "the owner wrote" instruction (`routes/chats_stream.py`).
Helpers never receive them.

## Saved data

Existing chats keep their question blocks and `pending_question_id`; nothing is
rewritten. A card without `input_requests` follows the all-or-nothing answer
path. `owner_input_requests` is a new table (`create_all`); the
`open_request_card_id` column on `chats` is migration
`0111_chat_open_request_card`. Every view reads the list card from the
request rows (`shown_request_cards`), which also keeps the drawer's chat query
on its covering index. Chat detail, `/runtime` and list rows carry
`open_request_card_id` beside `pending_question_id`; `chat_owner_input_changed` carries it as
`requestCardId`, never as `questionId`.

## Failure cases covered

- **Concurrency/ordering:** answer arrives while a run is active; two tools
  `add` the same key at once; two edits of one request at once, or an edit
  racing an owner answer (each edit is conditional on the revision and status
  it read); a continuation starts between supersede and republish; Stop races
  publication.
- **Partial failure/retry:** `add` response lost and retried (same key, no
  duplicate); card commit fails at turn end (rows stay open, unpublished, next
  turn end retries; no orphan card); a submission naming a revised request
  (refused whole, `card_out_of_date`).
- **Unbounded growth:** cap on open requests per chat and on spec size;
  settled rows retained for history; only those settled since the last
  completed turn are injected, at most ten.
- **Restart/shutdown:** process dies after rows commit and before publication:
  boot invents no turn or card (open requests no card shows are routine: after
  Stop, a partial answer, a failed turn), the next turn start hands the rows
  to the agent and its end shows them. Publishing onto an idle chat's last assistant message at boot is
  the smallest real recovery, not built yet. A restart or recovery pass with a card open keeps
  the marker (a column) and stays answerable
  (`test_recovering_a_later_wedged_run_keeps_the_card_open_and_answerable`).
- **Serialization/migration:** a fresh database, the `schema_0013` fixture, and
  an upgrade from the migration before `0111` with existing chats, which keep
  `open_request_card_id` NULL; legacy cards answerable; branch/compaction/archive of a chat with
  open requests.
- **Negative:** a request from one chat never appears in another chat's list
  or card; a branch does not inherit the source chat's open requests' answers.
