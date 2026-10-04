# Per-message transcript storage

## Ownership and compatibility

Chats remain in the shared application database. Each saved transcript item is
a `chat_messages` row keyed by `(chat_id, seq)`. Optional `id` and `cid` values
are independent lookup hints: missing, duplicate, numeric and string identities
must retain their original meanings. SQLite bodies and scalar projections use
TEXT-backed JSON to preserve scalar types and numeric precision.

`chat_transcript_state` owns the saved count and revision. `chat_writer`
domain commands commit row changes, count/revision and the chat's scalar state
together. Creation uses `chat_writer.create_chat`. No runtime `Chat.messages`
alias or second write mechanism exists.

The existing bounded `chat_live_assistants` snapshot remains separate. Terminal
replies and question commits update the matching message row or append one row;
they do not decode or rewrite every settled message. Steering inspects identity
metadata rather than historical bodies. Provider turn admission, compaction and
deliberate history repairs still have explicit full-context reads where their
contracts require them. This change does not promise faster full-context work.

## Reads

`transcript_rows` provides targeted identity/position reads, bounded windows,
64-row iteration and reverse-page scans. Display pages fetch their selected
bodies and small identity/lifecycle coordinates. The live assistant overlays
its existing position, or appends one visible position, without modifying
settled storage. API message shapes, anchors and ordering remain unchanged.

Read-only detail/log owners pin an actual SQLite read transaction around their
scalar, live and body reads. Generic history views do not pin transactions:
callers that await an external writer must not keep an old read snapshot.
Count and targeted body reads bypass stale ORM identity-map values after such
an acknowledgment.

## Upgrade

The registered level-1 `TranscriptStep` uses the lifecycle in
`ONE_WAY_UPGRADES_DESIGN.md`. While preparing, original `chats.messages` values
remain authoritative and untouched. Each converted unit records its source
fingerprint, compressed byte-exact archive, normalized messages and state in
one transaction. Damaged JSON retains its exact original bytes in quarantine
and produces a visible recovery placeholder.

Resuming verifies preserved raw bytes and the dense normalized artifacts, not
just a saved fingerprint. A pinned connection rechecks concurrent source
changes before the short activation transaction. Activation renames the old
column to `messages_v1`, switches derived search state, and raises the floor
together. Startup rechecks every mapped schema gap before starting the writer.

Fresh and upgraded databases retain an empty, deferred `messages_v1` mapping
with a Python default, so ordinary ORM inserts satisfy its NOT NULL constraint.
It is recovery shape, not current authority. A future retirement step must
remove the mapping and column together.

Post-activation tasks clear hot legacy values only after archive verification,
build the new search index, retire old derived search tables and sweep deleted
chats. Cleanup loads one large unit at a time. Search batches limit source
bodies to 8 MiB and 20 chats, admitting a single oversized chat when necessary
to make progress. Disk reserves account for the next batch and its WAL writes.
Task progress and failures are durable and visible through diagnostics.

Initial search indexing belongs to the background task. Only after its durable
completion may request-time reconciliation index newly created chats, using
the same bounded batch. Results require the indexed revision and timestamp to
match current chat state; stale documents are never returned as fresh matches.

## Release gates

This is an image-requiring, one-way representation change. Source support,
baked fallback support and `backend/runtime/one_way_capability.json` advance
together. A server restart on an older image is not activation.

Conversion cost includes parsing, compression and independent artifact
verification; a hashing-only estimate is insufficient. Measure the full gate
on a representative disposable copy before choosing a deployment window.
The default `deploy-prod.sh` cutover allowance is 120 seconds; do not enlarge
an operator's configured deadline or weaken readiness implicitly. A slower
installation needs an explicitly reviewed window or a separately implemented
maintenance-mode lifecycle.

Before release, validate a matching image, supported architectures, real
container cutover and floor-aware rollback. Host-only pytest proves application
contracts, not the completed image or production deployment envelope. Concrete
step tests use hybrid legacy-column/current-auxiliary-schema fixtures; the
frozen-schema boot test separately covers ledger/gate ordering. Neither
substitutes for a full previous-image-to-candidate cutover.


## First-upgrade Host prerequisite

The initial upgrade must install a floor-aware Host worker as ACTIVE before
activation, using the existing explicit helper-install marker. A trial cannot
protect interruption recovery by the older active worker. The installer pins
this prerequisite in its frozen Compose override, then finishes the update
the older release's Settings refuses; the baked root entrypoint verifies the
mounted private Host state on each boot, and the gate consumes only that
boot's root-owned proof. See `scripts/TRANSCRIPT_HOST_TEST.md`.

Archive compression uses zlib level 1 to reduce the measured conversion CPU
cost, retaining exact decoded originals and all verification passes. This
trades a modest archive-size increase for time; it does not enlarge deployment
deadlines. Normalized bodies and projections use matching escaped JSON, even
for valid legacy escaped surrogates. Indexed identity hints are bounded hashes;
original `id`/`cid` values and post-lookup comparison remain authoritative.
JSON comparison preserves bool, integer, float and signed-zero changes on
single-row updates rather than conflating them through Python equality.
