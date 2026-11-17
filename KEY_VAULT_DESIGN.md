# Key vault design

Status: draft for review.

## Contract

- The owner manages keys in **Settings › Keys**: values, which apps may
  borrow each one and where, and whether agents may use it. Settings there
  mostly narrows: how a key is sent, which apps may borrow it, and the sites the
  owner and agents may send it to are set by the agent's or app's request and
  approved on its card. The one widening Settings keeps is adding a site to an
  app that already holds the key, for an app that never asks (such as one whose
  saved keys moved in without sites). **Each app grant has its own
  sites**, so approving sites for one app never widens another app or the
  agents. The owner's top-level agents may manage keys too, but only the
  owner's own input lets an app use a key or reach more sites (approving a
  request, adding an app, widening an app's sites); agents may remove, pause
  or narrow an app.
- **Adding a key is paste and save.** A key needs no site to be stored: one
  with no sites is kept, readable by the owner and (unless switched off)
  agents, and sent nowhere. Where it may go is decided the first time
  something needs it, on a request card: an app's, or the owner's agent's.
- **Agents ask for sites like apps do.** A top-level agent that needs a key
  on a site the key does not list asks with `POST /api/keys/requests` (the
  refusal of its borrow says so); the owner answers on a card, and approving
  adds the sites to the key's own list and lets agents use it. An agent
  cannot add sites itself.
- **Switching an app off pauses it.** A paused grant keeps its sites but the
  app cannot borrow the key (nor its model provider use it) until the owner
  switches it back on, which needs no new request. Pausing holds for keys
  the owner or another app owns; an app can still delete a key it saved for
  itself and save the value again as a new key, as it always could.
- **The owner and the owner's top-level agents may read a key**, unless its
  Agents switch is off; only the owner's own input turns that switch on.
  Delegated helpers may borrow a key but never read its value. Every read and
  change is logged, without its value, under the caller it was made for. An
  agent that replaces a value or changes how a key is sent through the server
  notifies the owner. A save names only what it changes:
  on an existing key, an omitted `inject` or `grants` keeps the current one,
  so replacing a value never touches who may use it. An agent's edit always
  carries the revision it was made from, so it cannot undo a change the owner
  made meanwhile.
- **Apps never receive a value**, in their browser frame, service or job. They
  borrow keys through a handshake (below).
- **The server's HTTP routes enforce who may do what.** The owner-only gate
  (approving requests, letting an app use a key or reach more sites, adding
  sites for the owner and agents, switching a paused app back on, turning
  the Agents switch on), the owner's notification of an agent's change, and
  logging each read and change under its real caller hold only for calls
  through the server, which attests the caller from its token. The vault
  trusts any local process that can open its socket, and such a process can
  act as any caller: owner tools such as `store-key.py`, and anything with
  the server user's rights or sudo, which agents have. The peer check under
  *Future work* keeps sandboxed app code off the socket; agents stay
  trusted.
- Everything runs in one container, and the agent keeps sudo.

## The handshake

1. **Ask.** An app sends `POST /api/keys/requests` with its own app token
   (a top-level agent sends the same request with its own token, for a key
   on a site the key does not list yet; see Contract):
   the key name, a reason, the sites it needs and how the key is sent. The
   sites are exact hosts: a wildcard (`*.workers.dev`) is refused, because on
   a shared domain the app's author can own a subdomain, and that host reads
   the key from the request itself. Only the owner adds a wildcard, on the
   key's page. The server reads the app's identity from the token, never
   from the request. An app token a shared-browser guest's session minted
   cannot ask (or borrow): the guest works through the app, not as it.
   The answer is `granted`, `pending`, `denied` or (for an app the owner
   switched off on that key) `paused`; asking again is how the app
   checks. No request notifies the owner: a new one only nudges an open
   shell (a content-free `key_requests_changed` system event) to re-read
   the requests, and the owner is asked in the moment. One app may hold at
   most ten open requests. A granted app that asks for sites its grant lacks
   keeps using what it has, is told which are missing (`sites_not_allowed`),
   and its ask for them waits for the owner like any request
   (`sites_request`). A waiting request answers for every later ask for the
   same key until the owner answers it, and every answer about it carries
   the `sites` and `reason` it stores, so whatever shows a request (an
   agent's card, the dialog over an app) shows exactly what approving it
   grants. A refused request stands until the owner lets the asker ask again.
2. **Approve.** The owner sees the app's name, reason, requested sites,
   methods and paths and, for a new key, how it would be sent, and approves
   or denies; only the owner's own input can approve. The card asks where
   the request was made, answered through the owner-only routes: an agent's
   in the chat that asked (the vault keeps that chat; the agent's
   `request_key` control saves an ordinary owner card, and the card records
   the owner's answer only once the vault agrees with it, which resumes the
   agent; the older `mobius-key-request` block is still rendered only so
   chats that used it read as they did, and agents are no longer told to
   write it), and an app's in a dialog over the app's window while the
   owner has it open, straight away or when they next open it (the app list
   marks the app as needing them until then). Not now is the dialog's only
   way out. Settings › Keys answers only an agent's request that names no
   chat. Deleting a chat withdraws its agent's waiting requests: their card
   is the only place they could be answered, and a waiting request answers
   for every later ask for its key. If the vault can't be reached then,
   the next agent ask for that key withdraws the leftover request and gets
   its own card. The same holds for a request whose card was never saved
   (the vault records the ask first, and the turn can stop before its card
   is saved): a failed save withdraws it, and an ask that finds another
   live chat's request with no open card there withdraws that exact request
   (by its ticket) and asks again. Asks for one key run one at a time
   through to their card, so a request is never taken for cardless while
   its card is still being saved. Recovering the chat (or its project) first waits for a
   withdrawal still on its way, then tells the vault that deletion was undone:
   each withdrawal names its deletion, and the vault ignores one for an undone
   deletion that reaches it late, so it never removes a request the recovered
   chat makes. The card asks one thing at a time: whether to allow, then, for a
   key the owner has no value for yet, the value. A chat only answers its own agent's request; an
   app's shown in a chat is read-only. Every card's answers wake only after
   the card has been still and untouched for about a second (since it
   appeared, last moved on screen or was last pressed anywhere) and ignore a
   press that began while they slept, as a browser's permission prompt does,
   so a card appearing or moving under the pointer takes no click meant for
   what was there, and steady clicking never answers. The
   request only proposes: approval applies the sites and slot the owner's
   answer carries (a new key answered without a slot is sent as
   `Authorization: Bearer`). The sites go to that app's own grant. An existing
   key keeps its value, and its slot unless nothing can send it yet (no site
   of its own and no app with a site, as with a key an app saved or that was
   imported without sites): then the request's proposed slot is shown on the card
   and Allow sets it, which is how a key saved ahead of time (always as a
   bearer token) gets the slot its first user needs. A key already in use
   keeps its slot under it, and the card says so. Granting
   the app those sites from the key editor settles its request too. When the
   key exists because another app saved it for itself, the card says so
   (the vault lists the key's `key_origin`): allowing would send this app's
   requests with that app's value, which that app can still change. Approve
   and deny both carry the request's ticket, so a card left open never answers
   a newer request under the same id. The vault also refuses an approval not
   made as `owner`, which keeps the server's agent callers out, but not a
   local process on its socket (see *Contract*).
3. **Borrow.** The app sends an ordinary request to
   `/api/keys/use/<key>/<host>/<path>?<query>` with its app token. The server
   attests the caller as `app:<id>`, drops Möbius credentials and routing
   headers, and relays the request to the vault, which checks the app's grant
   and that grant's sites, adds the key and sends it. The answer streams back
   as it arrives (server events and streamed model output work), with echoes
   of the key redacted even when they straddle two pieces. To do that the
   vault always holds back the last few hundred bytes of each piece (the
   length of the key's longest encoded form, minus one) until the next piece
   or the end, whatever they contain, so how fast text arrives never hints at
   whether it begins the key; a server event can arrive an event or two late.
   An answer the site or vault breaks off ends as a broken response, never as
   a complete one.
   A sandboxed app frame preflights every header it sends; a borrow's
   preflight allows whichever header names the app asks for, so a site's own
   headers (a version header, say) reach it, while every other route keeps
   the platform's fixed list.

Approving, denying and changing keys is refused to apps, shared-browser
guests and delegated helpers. An edit carries the revision of the key it was
made from, and the vault refuses it if the key changed since, so a stale page
cannot write back old grants. App ids can be reused after a purge, so purging
an app removes it from every grant and request. A purge also waits until every
borrow the app started has been answered, even one whose caller gave up: the
vault knows the app only by its id, so a borrow still on its way could
otherwise use the grant of an app that reuses the id. A borrow whose answer
never came back keeps the app until the server restarts, which ends the vault
process and anything it was still doing; one that failed before it was sent
releases the app at once. A header the relay cannot carry (a name that is not
an HTTP token, a value that is not printable ASCII) is refused with 400
before the borrow starts.

## Where it lives

- `backend/vault/keyvault.py`: the standalone vault, which imports nothing
  from the server. Its folder `/data/.vault/` (mode 0700, reasserted at
  start, with its own `.gitignore`) holds `master.key`, `keys.json` (AES-GCM
  per value, the key name as associated data; store version 2 gives each app
  grant its own sites, and a version 1 store is migrated at start with each
  app keeping the sites it could reach), `requests.json`, `stats.json` (use
  counters), `usage.jsonl` (use traffic, rotated at 512 KiB), `audit.jsonl`
  (reads, changes, grants and requests, rotated at 2 MiB, kept apart so
  refused traffic cannot push it out; neither log holds values or unbounded
  fields), `vault.lock` (one vault per folder) and `vault.sock` (mode 0600).
  A change is made on copies, written atomically and only then applied, so a
  failed write (a full disk) leaves the vault exactly as it was on disk and
  answers an error. Its audit lines are appended only after it is saved; one
  that then fails to append is reported in the vault's log, and the change
  still answers success. At start the vault refuses a master key that opens none
  of the stored keys, and sets aside a damaged `requests.json` rather than
  stopping.
  Every change names its caller; an app caller cannot change keys. There is
  no TCP port. The vault trusts any process that can open the socket: the
  server, which states the attested caller in `X-Vault-Caller` (`app:<id>`,
  `agent:<chat>`, `owner`, `platform:<purpose>`), and every other local
  process that can open it, which can state any caller and meets none of the
  routes' rules (see *Contract*). The peer check under *Future work* narrows
  that.
- `backend/app/key_vault.py`: supervises the vault (task `key-vault`,
  restarts with backoff; the vault exits when the server is no longer its
  parent, replacing a stale socket at start and removing at exit only the
  socket it bound; output in `/data/logs/key-vault.log`) and is the server's
  client.
- `backend/app/routes/keys.py`: the owner routes, reveal, the handshake and
  the use relay.
- `frontend/src/components/SettingsView/KeysSection.jsx`: Settings › Keys.
- `backend/scripts/store-key.py`: a sealed-card consumer that saves an
  owner-provided key straight into the vault, as `owner` and without
  notifying anyone. It creates a new key only if the name is free and
  replaces an existing one only with `--replace`, and then only the revision
  it read.

The vault's use path keeps its earlier rules: HTTPS on port 443 only, one
injection slot (a header with an optional prefix, or a query parameter),
default-deny site/method/path globs on the decoded path (`*` also matches
across `/`) with dot segments (also before a `;` parameter), encoded slashes,
`%25`, invalid UTF-8 escapes and empty segments refused first, public-address
DNS pinning, no redirects (a site's redirect reaches the app as
`X-Vault-Location`, never as a `Location` a browser would follow), identity
encoding and only `chunked` transfer coding, and redaction of echoed values
matched character by character in any mix of raw, percent-encoded, JSON- and
HTML-escaped forms, either case, plus hex, UTF-16 and base64 at every
alignment. The caller's path-override headers (`X-Original-URL`,
`X-Rewrite-URL`, `X-Override-URL`) are dropped like method overrides, and
folded header lines are refused. Of the site's answer, only an allowlist of
headers reaches the app, the site's own `X-Vault-*` headers never do, the
status line carries the standard reason, and informational `1xx` answers are
skipped. A borrow ends 300 seconds after it starts, however slowly the site
connects, sets up TLS or sends its answer (the vault shuts the site's
connection then), and a caller that takes
60 seconds to accept the next piece is cut off. At most 32 borrows run at
once, and at most 8 for one caller (an app, one agent's chat); beyond either
the vault answers 429. The server waits 30 seconds longer than a borrow can
take, so it never gives up on one the vault is still working on; a borrow
that may still be with the vault keeps its app from being purged only until
that time has passed. A use whose caller hung up before the answer is still
logged. Headers
sites log as metadata (`User-Agent`, `Referer` and similar) cannot carry a
key. An app without a grant gets the same refusal whether or not a key
exists. A site slower than the relay is reported as a timeout (504), not as a
stopped vault. The method
rule checks the wire method, so the vault drops the caller's method-override
headers (`X-HTTP-Method-Override`, `X-HTTP-Method`, `X-Method-Override`)
rather than let a site run another method with the key; a site that reads the
method from the body or query cannot be policed this way, so allow only the
methods you would accept for any of its requests. A destination
given as a NAT64 address must embed a public IPv4 address. A value with line
breaks (a PEM file, JSON credentials), or with characters a header cannot
carry, can be stored and read but is never sent in a header. The server
serves answers with a sandboxing CSP, `nosniff`, `no-store` and no cookies.
Request bodies are buffered (10 MiB); answers stream, up to 20 MiB each.

## What replaced the per-app secret store

The per-app store's encryption module and the
`permissions.job_secret_read` manifest grant are gone; `/api/apps/{id}/secrets`
survives for one release only as the adapter described under *Transition*. A
manifest that still declares the grant is refused with a pointer to the
handshake. An app's model provider names its vault key in
`model_provider.secret_name`; the loopback model relay reads that key
(logged as `platform:app-<id>`) when it is the app's own key or one the owner
granted to the app, and in either case that app's grant sites allow every
path the relay calls for the provider's protocol (`POST <base_url>/responses`,
or `POST <base_url>/v1/messages` and `/v1/messages/count_tokens` for
`anthropic_messages`); checking availability never reads the value. A new key
an app saves for its own provider's `secret_name` starts with exactly those
paths, so the provider works at once and
the owner sees, and can narrow, where the key goes. An app update that moves
its provider to another address does not move the key: the owner adds the new
site.

**Upgrade.** Once per server start, after the vault answers, values in
`/data/app-secrets/<id>/<name>` move into the vault (`import_legacy_app_secrets`):
each becomes a key granted to its own app and the owner's agents, sent as a
bearer token, with no sites until the owner adds them, except that the app's
model-provider key starts on its provider's site. When a start imports keys
that have no sites, the owner gets one notification naming their apps, which
cannot use them until the owner adds sites in Settings › Keys; the ledger
below keeps a later start from repeating it. Each key records its
origin (the app and its exact old name), set at creation. The vault
finds or creates that key in one locked step (`Vault.save_owned`), at the
first free name scoped to the app (`api.key` → `app<id>-api-key`, then
numbered): never a plain name another app could later ask for. Distinct old
names never merge, simultaneous saves never leave two keys for one secret,
and no existing key is overwritten. An old value the app already replaced
through the adapter below, or deleted, is not imported over it. Model
providers and the adapter below find a secret by that origin, not by its
vault name. Once the owner or an agent
stores a new value in such a key, its origin and the app's own grant are
cleared: the key is the owner's, and the app uses it only if the owner grants
it. An app holds at most 16 keys of its own, as in the per-app store. Wiping an app's data or
purging the app erases its own keys (a key the owner shared with other apps
stays for them), as the per-app store did. A ledger
(`.vault-import.json`) next to the old files means a key the owner deletes is
never re-imported, unreadable values are retried, and values of an app id no
app holds (a purge, or a restore that dropped the app) are settled as skipped
for good, because the id may be reused. The old ciphertext stays in
place for one release.

**Transition.** For one release, `/api/apps/{id}/secrets/{name}`
(`routes/app_secrets_compat.py`, marked `Deprecation: true`) answers from the
vault: apps may still save, check and delete their own values with their own
token, and the owner and top-level agents may read them, but no app token
reads a value back (services and jobs included); they borrow it through
`/api/keys/use`. The owner and agents never write through these routes: such
a write would create a key the app owns, with the app's grant, without the
owner's own input. They use Settings › Keys or `PUT /api/keys/<name>`. These routes see only
keys the app itself created and still holds: a key the owner made, or merely
lets the app borrow, can never be replaced through them.

**Restore.** Backups carry the vault only in the encrypted secrets archive.
A restore that brings no vault (a backup without secrets, or with secrets from
before the vault) keeps the current vault, whose grants name this install's
app ids. `restore-data.py` warns and keeps a grant only when the databases
from before and after the restore both give its id to the same installation
(slug and token nonce): every other app grant and request is withdrawn with
the vault's own purge step, all of them when either database can't be read.
It fails closed: it opens the vault, under its lock, before the swap, and
refuses the restore with `/data` unchanged when it can't (a vault still
running, say); if withdrawing fails after the swap, it undoes the restore.
It runs as root from `/app/scripts`, so the image ships
the vault program beside it (`/app/vault/keyvault.py`), never loading it from
the editable checkout.

## Future work: sandbox for app server code

App frames are fully outside the value's reach. App services and jobs still
run as the server's user, which has passwordless sudo by default, so
determined server-side app code could read `/data/.vault/`. A kernel sandbox
around app services and jobs closes that.

**Why not a separate user.** The server stops app processes with `killpg`
for timeouts and cancellation; a process under another uid cannot be
signalled without root helpers.

**Mechanism.** Landlock plus `no_new_privs`, applied in a small launcher that
then `exec`s the app command (not `preexec_fn`: the server is threaded).
`no_new_privs` makes setuid `sudo` refuse to run. Landlock (ABI 6 or later)
also blocks ptrace and `/proc/<pid>/environ|mem` of processes outside the domain,
and scopes signals and abstract sockets. Processes outside the domain (the
server) can still signal and stop those inside.

**Where it hooks in.** `app_services._run_spawned`, the preloaded service
hosts in `service_preload`, and the job child that `app-job-runner.py`
starts. The runner itself stays outside: it mints the job's app token.

**Policy: apps cannot read keys, nothing more.** Three rules, each closing one
way to a value:

1. No access to `/data/.vault` (the store itself).
2. `no_new_privs`, so `sudo` refuses to run (root would ignore rule 1).
3. No access to what would let app code act as the owner or an agent and ask
   for a reveal: `/data/.secret-key` (signs logins),
   `/data/service-token.txt` (mints any app's job token) and `/data/cli-auth`
   (agent transcripts, which can contain a key an agent read). Apps whose jobs
   run provider CLIs themselves (Memory today) keep `/data/cli-auth` through
   an allowlist the app cannot write.

Indirect paths (for example planting code an agent later runs) are out of
scope for this step.

**Vault caller check.** Sandboxed code can still connect to `vault.sock`.
The vault should read the peer's pid (`SO_PEERCRED`) and refuse any peer
whose `/proc/<pid>/status` shows `NoNewPrivs: 1`. Agents and the server run
without it.

**Done when** every installed app's services and jobs still work, Memory's
nightly run completes, and a test job cannot read the vault folder, connect
to its socket, read the files in rule 3, or use sudo.
