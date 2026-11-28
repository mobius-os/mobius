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
  on a site the key does not list asks with its `request_key` tool (the
  refusal of its borrow says so), which saves a card in its chat; the owner
  answers there or in Settings › Keys, and approving adds the sites to the
  key's own list and lets agents use it. An agent cannot add sites itself,
  nor refuse or remove a request.
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
   (a top-level agent asks through its `request_key` tool instead, for a
   key on a site the key does not list yet, so its ask is always a card;
   see Contract):
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
   the requests, and the owner is asked in the moment. One app, and the
   owner's agents together, may each have at most ten requests waiting for
   an answer; a refusal doesn't count. A granted app that asks for sites its grant lacks
   keeps using what it has, is told which are missing (`sites_not_allowed`),
   and its ask for them waits for the owner like any request
   (`sites_request`). Asking again only for what it may already do answers
   `granted` and leaves any request of its own alone: the change that widens
   a grant settles every request it then covers, and a refusal stands until
   the owner lets the asker ask again. A waiting request answers for every later ask for the
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
   agent; a `mobius-key-request` fence, a draft of this card that never
   shipped, is shown as ordinary code), and an app's in a dialog over the
   app's window while the
   owner has it open, straight away or when they next open it (the app list
   marks the app as needing them until then). Not now is the dialog's only
   way out. Settings › Keys lists every waiting request an agent made, with
   the chat that asked, so none is ever stranded: its card settled when the
   turn was stopped, its chat was deleted while the vault was unreachable,
   or its card was never saved. A waiting request answers for every later
   ask for its key, and an agent whose ask joins another chat's is told
   where the owner answers it. Deleting a chat withdraws the requests its
   agent made up to the deletion (the vault keeps when the chat last asked
   for each, `asked_at`), so a withdrawal that reaches the vault late never
   removes what the chat asks for once recovered, and recovery calls nothing.
   A card that can't be saved withdraws the request its ask just made. The card asks one thing at a time: whether to allow, then, for a
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
   a newer request under the same id. Only the owner answers or removes a
   request: the server's routes require the owner's own input, and the vault
   refuses an approval or refusal not made as `owner` and a removal made by
   neither the owner nor the platform, which keeps the server's agent callers
   out, but not a local process on its socket (see *Contract*).
3. **Borrow.** The app sends an ordinary request to
   `/api/keys/use/<key>/<host>/<path>?<query>` with its app token. The server
   attests the caller as `app:<id>:<install>` (see *App installations*),
   drops Möbius credentials and routing headers, and relays the request to
   the vault, which checks the app's grant and that grant's sites, adds the
   key and sends it. The answer streams back
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
cannot write back old grants. A header the relay cannot carry (a name that is
not an HTTP token, a value that is not printable ASCII) is refused with 400
before the borrow starts.

## App installations

SQLite reuses app ids once an app is purged, so an id alone never names an
app to the vault. Each app row has an installation id (`apps.install_id`, 32
random hex characters) set when the row is created and never changed: trash,
recovery, a data wipe, a reinstall over the same row and updates all keep it,
unlike the token nonce, which rotates on trash and wipe. Every app grant
(`grants.apps[]`: `id`, `install`, `sites`, `paused`), every app request and
a key's `origin` name the installation beside the id, and the server attests
an app caller as `app:<id>:<install>`, with the installation of the row the
app's token was checked against. The vault refuses an app caller that names
no installation, and an entry matches an app caller only when both match, so
a grant can only ever be used by the installation it was given to. A key
grants an app id once: approving a request replaces any other installation's
grant for that id, which no app can use any more.

The owner's save (`PUT /api/keys/<name>`) names the installation in each app
grant: Settings › Keys saves the grants it lists back as they are, and the
apps list carries each app's `install_id` for a new one. A grant naming an installation no
app holds (a page loaded before its app was purged) is stored but used by no
one. A request card's ticket still guards a card left open while its request
was replaced by a newer ask under the same id.

A store from before installations (version 2 or 1) loads with every app grant,
request and origin unstamped (`install` null), which matches no app caller.
Each time the server starts the vault, once it answers, the server names every app it
holds, trashed ones included, with its installation (`POST /apps/installs`,
platform callers only; `Vault.adopt_installs`): unstamped entries of those ids
are stamped, those of ids no app holds are withdrawn as a purge would (values
kept), requests the stamped grants now cover are settled, and stamped entries
are never changed. Until that call lands on the first start after the update,
those apps borrow nothing and their asks wait; a call the vault can't take
yet is retried (after 5, 15, then every 60 seconds), a call it refuses is
logged and left until it restarts, and a vault that restarts is named again.
The one-time import of the retired per-app secrets gives app rows their
installation ids first, so it never depends on naming having run. An app row a release without the column
created (a rollback) gets its installation id in the same step.

Purging an app erases the keys it saved for itself, as its other data, and
drops its id from every grant and request. That is tidying, not the boundary:
a borrow still on its way when the app is purged names the purged
installation, so the purge does not wait for it.

## Where it lives

- `backend/vault/keyvault.py`: the standalone vault, which imports nothing
  from the server. Its folder `/data/.vault/` (mode 0700, reasserted at
  start, with its own `.gitignore`) holds `master.key`, `keys.json` (AES-GCM
  per value, the key name as associated data; store version 2 gave each app
  grant its own sites, a version 1 store migrating with each app keeping the
  sites it could reach; version 3 names each app's installation, see *App
  installations*; version 4 holds each key as named parts, see *Keys with
  several parts*), `requests.json`, `stats.json` (use
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
  server, which states the attested caller in `X-Vault-Caller`
  (`app:<id>:<install>`, `agent:<chat>`, `owner`, `platform:<purpose>`), and
  every other local
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
take, so it never gives up on one the vault is still working on. A use whose
caller hung up before the answer is still logged. Headers
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
origin (the app, its installation and its exact old name), set at creation. The vault
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
for good, because the id may be reused and the value was never that app's.
The old ciphertext stays in place for one release.

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

**Restore.** Backups carry the vault in the secrets archive: encrypted by
default, or readable by anyone holding a `--plaintext-secrets` backup, which
then holds every key (`.vault/master.key` beside `.vault/keys.json`).
A restore that brings no vault (a backup without secrets, or with secrets from
before the vault) keeps the current vault, whose grants name app
installations. `restore-data.py` warns and keeps a grant or request only when
the restored database gives its id the installation it names; every other app
grant and request is withdrawn (`Vault.withdraw_apps`): those naming an
installation the restored database lacks, those never stamped with one (a
store no server start has adopted), and all of them when the restored
database, or its apps, can't be read (a backup from before installation ids
names none, and a backup taken after a rollback can hold rows an older
release wrote: the restore then names those apps, as it checks the vault and
again at its end, and says how to let each use its keys again). Only the restored database is read, so a lost or corrupt current
database withdraws nothing it holds, nor does an app recovered from the trash
since the backup. Values are never erased: a key an app saved for itself
stays, as the owner's, and the owner can let an app use it again. It withdraws before the swap, reading
the restored database from staging, so a vault that can't be opened (a vault
still running, or a store newer than the restore's vault program: a store
this release wrote, for an older image's restore) or updated stops the
restore with `/data` unchanged, and a swap that fails
afterwards leaves only grants withdrawn. It runs as root from `/app/scripts`,
so the image ships the vault program beside it (`/app/vault/keyvault.py`),
never loading it from the editable checkout; `--vault-program` names another
trusted copy deliberately.

## Keys with several parts

Status: phases 1 (parts, `basic`, `slots`, the multi-field card), 2
(`aws_sigv4`, `jwt_bearer`) and 3 (`oauth2_refresh` and the sign-in finish)
are built.

**The gap.** A key today is one value sent in one slot, and a borrow uses one
key. Many services need more than that:

| Shape | Examples | Today |
|---|---|---|
| Secret plus an identifier that isn't secret | Algolia app id, Twilio account SID in the path, a version header | Works: the caller sends the identifier itself. |
| Username and password (Basic) | Twilio SID:token, Atlassian email:token, Mailgun, OAuth `client_secret_basic` | Only if the owner pastes base64 of `user:pass` and the slot has a `Basic ` prefix. |
| Two secrets in two places | Trello key and token, Discogs consumer key and secret | Impossible: one key per borrow. |
| Secret in the body | Plaid `client_id`/`secret`, OAuth `client_secret_post` | Impossible: only headers and query parameters. |
| A signature over the request | AWS SigV4 for S3 and every S3-compatible store, other AWS APIs, exchange HMACs, OAuth 1.0a | Impossible: the vault adds a fixed value. |
| A private key that mints short-lived tokens | Google service accounts, GitHub Apps, App Store Connect | Stored and readable, never sent (multiline). |
| A refresh token | OAuth 2 APIs | Impossible: values never change by themselves. |

For the impossible rows the only path is a top-level agent reading the value
and doing the work itself. That puts the secret in the agent's processes,
and apps and delegated helpers, which never receive values, cannot use these
services at all.

**Approach.** A key holds up to eight named **parts**, and its slot becomes a
**send recipe** that says how the vault builds the request from them. Every
recipe is platform code in the vault: nothing an app or agent supplies runs
there, so a recipe cannot carry a value out. The vault never returns a part,
a signature or a minted token to the caller, and it adds each secret part and
each minted token to the key's redaction set.

### Parts

- A part has a name (`NAME_RE`), a label the card shows, and whether it is
  `secret`. Secret parts are sealed one by one under the key name and part
  name as associated data. Plain parts (a client id, an access key id, an
  account SID, a region) are stored in clear, shown in Settings, never
  redacted, and may be short. The part a recipe treats as the secret itself
  (Basic's password, `secret_access_key`, `session_token`, `private_key`) is
  always secret, whoever proposes it, and the card names every plain part
  before the owner pastes into it ("shown as typed and not hidden from
  answers"). A secret part sent by `basic`, `slots` or a signer is at least
  eight characters: a site that echoes what it is sent could otherwise tell a
  caller which of its guesses is a short secret (the one that comes back
  redacted). A single value sent as before keeps its old rules.
- Every key that exists today is a key with one secret part named `value`.
  It stays sealed under the key name alone, exactly as before, so nothing is
  re-encrypted; any other part is sealed under `<key>#<part>`. The store is
  still written as version 3 while every key is that one value sent in a
  header or query parameter, so an older vault (the recovery fallback, a
  restore from an older image, a rollback) keeps reading it. It is written as
  version 4 once a key has more parts or another recipe, and goes back to 3
  when no key does. An older vault meeting a version 4 store refuses to start
  ("unknown store version"); deleting or simplifying the keys that need
  version 4 with this vault, before going back, avoids that. A damaged old
  record still loads, and only its own use fails. Each single-value route keeps
  its meaning for a one-part key; for others, reading names the part
  (`GET /api/keys/<name>/value?part=<part>`), and the same read rules apply
  (owner and top-level agents unless the Agents switch is off; never helpers
  or apps).
- Replacing one part keeps the others, so rotating a secret never retypes its
  id, and a part's label can change, or a plain part become secret, without
  its value (an item with no `value`), which counts as no new value. A secret
  part becomes plain only when the same change gives its value again: nothing
  sealed is ever turned into listed text, so an agent can't use the change to
  read a key whose Agents switch is off. Settings › Keys replaces a
  part; it still only narrows. A request proposes parts only for a key it
  creates (kept with the request, so a key deleted before the owner answers
  is created from them); parts are otherwise added or removed with
  `PUT /api/keys/<name>` (`parts`, `remove_parts`) or `store-key.py`.
- A part with line breaks (a PEM key) is never placed in a header; a query or
  body slot can carry it, and the signing recipes read it.

### Send recipes

`inject` keeps its name and gains types. `header` and `query` stay as they are
(using the part `value`).

- **`basic`** `{username: <part>, password: <part>}`: the vault sends
  `Authorization: Basic base64(username:password)`, so the owner pastes the two
  halves as two fields. The redaction set includes the encoded pair as well as
  the secret halves. A username with a colon is refused.
- **`slots`** `[slot, ...]` with at most eight slots. Each slot is one of:
  - `{type: header, name, template}`: `template` is fixed text with `{part}`
    placeholders, so `Token {id}:{secret}` and `Bearer {token}` both fit. A
    header slot obeys today's rules: the forbidden names (now also the
    method- and path-override headers and `Range`, which the vault strips
    from callers), one line, latin-1. A rendered header holding a secret part
    is redacted whole.
  - `{type: query, name, part}`.
  - `{type: body, format: json|form, field, part}`: `field` is a top-level
    JSON member or form field. The vault already buffers the whole body
    (10 MiB). It parses the caller's body as the declared `Content-Type`,
    refuses a body that doesn't parse, replaces any value the caller gave for
    that field, serializes again and sets `Content-Length`. JSON with a
    duplicate member at the top level is refused, so a parser difference
    between the vault and the site can't decide which value wins. Strings are
    written back escaped, so a lone surrogate stays as sent; a body nested
    too deeply, or a form that isn't UTF-8, is refused. A request with no body
    (a GET included) gets one holding only the fields. No nested paths in
    this step.

  As today, the caller's own header with a slot's name is dropped, and the
  caller cannot name a slot's field or part: both come from the approved
  recipe.
- **Signers**, in the vault itself, checked against published test vectors
  or by verifying what they sign:
  - **`aws_sigv4`** `{access_key_id: <part>, secret_access_key: <part>,
    session_token?: <part>, region, service: s3}`, for S3-compatible storage
    only: the vault signs last, over the
    request it is about to send (the pinned host, the method, the path and
    query it checked, `x-amz-date`, `x-amz-content-sha256` of the buffered
    body, and every caller `x-amz-*` header). The caller can't send its own
    `Authorization`, `X-Amz-Date`, `X-Amz-Security-Token` or
    `X-Amz-Content-Sha256`. Each path segment is encoded the AWS way and sent
    as signed, and the query is sent sorted, exactly as signed, with the
    payload hash as a header, which S3 requires. This covers S3, R2, B2,
    Wasabi, MinIO and Spaces with one module. The service is always `s3`: in
    S3 the path names the bucket and object, which a site rule can narrow,
    while other AWS APIs put the operation in `X-Amz-Target` or `Action=` on
    `POST /`, where no rule can tell reading a table from deleting it or
    listing images from minting a registry password (ECR, CodeArtifact). A
    key stored with another service is refused when borrowed. Even for S3 the
    sites limit only which buckets and objects a request reaches: what it may
    do there is whatever the access key's own policy allows, so the owner
    gives that key only the buckets and actions it needs, and the approval
    card says so. The vault never
    produces presigned URLs: a presigned URL is a portable credential, so a
    caller's own `X-Amz-*` query parameter is refused, and the signature
    (valid for that exact request for a while, and echoed by S3 errors) is
    redacted. A caller's `x-amz-copy-source*` header, which would read from
    any bucket the access key can, is refused, and so is a query naming an S3
    configuration sub-resource that changes who may reach a bucket or object
    (`acl`, `policy`, `cors`, `website`, `publicAccessBlock`, `lifecycle`,
    `versioning`, ...), which a path rule can't tell apart, as are their
    header forms on an upload (`x-amz-acl`, `x-amz-grant-*`,
    `x-amz-website-redirect-location`, `x-amz-object-lock-*`,
    `x-amz-bypass-governance-retention`). Signed header values must be ASCII
    and the path and query UTF-8.
  - **`jwt_bearer`** `{private_key: <part>, alg: RS256|ES256, claims,
    exchange?}`: the vault signs a short-lived JWT with fixed claims from the
    recipe (`iss`, `sub`, `aud`, `scope`, and `iat`/`exp` it sets itself),
    then either sends it as `Authorization: Bearer` (App Store Connect, a
    GitHub App's own endpoints except the installation-token route, which
    only the `github` exchange reaches) or exchanges it at a fixed token endpoint
    (Google's `oauth2.googleapis.com/token`, GitHub's installation-token
    route) and sends the access token it gets back. For a Google
    service-account JSON pasted whole, the vault reads `client_email` and
    `private_key` out of it, but the token endpoint is the scheme's, never
    the file's `token_uri`. `exchange` is `{kind: google}` (RS256, `scope`
    required, audience fixed to the token endpoint) or `{kind: github,
    installation_id}` (RS256, `iss` the app; the JWT lives at most ten
    minutes and starts a minute early). Without an exchange, `iss` is
    required. `lifetime_secs` is 60 to 3600 (default 600). A request may mark
    a part `multiline` so its card shows a box for the key file.
  - **`oauth2_refresh`** `{client_id: <part>, client_secret: <part>,
    refresh_token: <part>, token_endpoint, revoke_endpoint?, client_auth?:
    post|basic}`: a user's own sign-in (Gmail, Calendar). The vault spends
    the refresh token at the token endpoint (`grant_type=refresh_token`, the
    client in the form or, with `basic`, as `Authorization: Basic` of the
    form-encoded id and secret) and sends the access token it gets as
    `Authorization: Bearer`, cached for its `expires_in` (an hour when the
    provider doesn't say, a day at most). The endpoints are `https` on an
    exact host and its usual port, with a path and nothing else (no user,
    query or fragment, at most 512 characters). `client_secret` and
    `refresh_token` are always secret and `client_id` always plain (the app
    shows it to the consent page). Public clients, which have no secret, are
    not supported. Renewals of one key run one at a time, by key name,
    and each reads the refresh token afresh, so a provider that rotates it
    (and may revoke the whole sign-in when one is spent twice) never sees
    one reused. When it rotates, the vault saves the new one as a change by
    `platform:oauth-refresh` (`sign-in renewed` in the audit log), only
    while the key still holds the one it spent, so a renewal never undoes a
    sign-out or a newer sign-in; the token it bought is cached under the
    renewed key too. A borrow before anyone signed in answers 409 with code
    `not_signed_in`, and a refresh token the provider refuses
    (`invalid_grant`) 409 with code `reauth_required`. That refusal is
    remembered in memory for exactly the parts and recipe refused, so later
    borrows hear it without asking the provider until the key's parts or
    recipe change or it is signed in again. Any other failure is a 502 like
    an exchange's. A refusal's `code` is in the vault's denied JSON
    and the server relays it unchanged.

    **Signing in.** A sign-in key is saved and approved before anyone signs
    in: its recipe fits without the refresh token, and a request that lists
    that part is refused, so the card only ever asks for the client. The
    consent itself happens in the browser, started by the app (it builds
    the provider's consent address; `GET /keys/<key>/oauth2` gives it the
    client id); the vault finishes it:

    - `GET /api/keys/<key>/oauth2` → `{signed_in, token_host, client_id}`.
    - `POST /api/keys/<key>/oauth2/exchange` `{code, redirect_uri,
      code_verifier?}` (a printable code of at most 2048 characters, an
      `https` redirect of at most 2048, a PKCE verifier): the vault posts
      `grant_type=authorization_code` to the token endpoint and keeps the
      refresh token as the key's (a secret part labelled "Sign-in"), audited
      as `signed in` under the attested caller, and caches the access token.
      An answer without a refresh token is a 502 saying the provider gave no
      lasting sign-in (offline access, or consent again, was not asked for).
      It answers only `{signed_in: true, scope, email}`: `email` comes from
      the `id_token`'s claims, read without checking its signature because it
      arrived straight from the approved token endpoint over TLS. Each is
      relayed only in a strict form (space-separated scope names, a plain
      address, both bounded) and is null if it holds any secret the vault
      knows (the client secret, a refresh, access or id token, the code). No
      access, refresh or id token is ever returned.
    - `POST /api/keys/<key>/oauth2/disconnect`: revokes the refresh token at
      the revoke endpoint when the recipe names one (best effort), removes
      it (`signed out`) and forgets the cached access tokens; answers
      `{signed_in: false, revoked}`.

    These are open to the owner, top-level agents while the key's Agents
    switch is on (an agent's sign-in or
    sign-out notifies the owner, as any value it replaces does) and an app
    with a live (not paused) grant that has a site on the key; an app without
    one hears what a borrow without a grant hears. Delegated helpers, app
    tokens a helper minted and an app's own agent may check but never sign in
    or out, and shared-browser guests are refused as for a borrow. The
    server waits for them as long as for a borrow, since they reach a site.
    Settings › Keys shows a sign-in key's state instead of its refresh token,
    with Sign out.

  Minted access tokens live only in the vault's memory, cached until about a
  minute before they expire, keyed by the key, its recipe and its private
  part, so any change mints afresh; simultaneous borrows wait for one mint,
  and a token the site answers 401 to is dropped. The card shows every
  claim, the key id and the exchange, since `sub` acts as that user. The exchange endpoint is
  part of the recipe the owner approves and is checked like a site (HTTPS,
  443, public address, pinned), but it is not a site the caller can borrow
  against. A failed exchange (and a failed sign-in renewal) answers 502 in
  fixed words with only the provider's OAuth error code, and only a standard
  one (`invalid_client`, `invalid_grant`, ...) that equals no secret it was
  sent: its free text could repeat a secret in a form redaction misses.
- **Exchange HMACs and OAuth 1.0a** have no common shape (which fields are
  signed, in what order, how the signature is encoded and named), so they are
  added one provider at a time when someone needs one, not as a generic
  template. A generic HMAC recipe would let a caller choose the string to be
  signed, which makes the vault a signing oracle.

### Asking and approving

- `POST /api/keys/requests` and the agent's `request_key` tool carry the
  proposed parts (name, label, `secret`) and the recipe in `inject`. Proposed
  parts apply only to a key the request creates; approving takes `parts` as a
  map of part name to pasted value. A recipe that doesn't fit an existing
  key's parts is never set: the card says the key keeps its own. The rules
  that decide when a request's slot applies stay the same: a recipe applies
  only to a key that nothing can send yet, and a key in use keeps its recipe,
  with the card saying so.
- The card's details say how the key is sent, naming parts by their labels
  (`Authorization: Basic (Account SID and Auth token)`, "an AWS signature
  (s3, region auto)", "a Google access token for <scope>", "a sign-in renewed
  by the vault at oauth2.googleapis.com" with its endpoints), and, for a new
  key, give one paste field per part, labelled by the request. Plain parts
  are visible as typed, with the warning above; secret parts are password
  fields, and a multiline part gets a text area that takes a dropped key file
  (read into the box, never opened by the browser or attached to a chat
  message). Saving is still paste and save: no extra settings.
- Asking again while a request for the key waits with another recipe or
  other parts is refused (409): the owner answers the waiting one first.
- `store-key.py` maps the sealed card's fields (at most eight, matching
  `request_secret`, so part names are lowercase letters, digits and `_`)
  onto parts one to one, with `--part NAME[:plain|:secret[:LABEL]]` (an
  existing part keeps its secrecy unless told), and takes `--basic USER_PART
  PASS_PART`, `--slots JSON` or `--recipe JSON` for the recipe.
- Sites, methods and paths are unchanged and still checked first; a recipe
  only decides what the vault adds to a request that is already allowed.

### Security notes

- Redaction covers every secret part, every composite the vault builds from
  them (the Basic pair, each header built around a secret), every minted
  token and every AWS signature. Plain parts are not redacted, so a short id
  never mangles answers.
- The model relay sends a key's `value` as a bearer token only for a key
  sent that plainly (`header` or `query`), never one the vault signs or
  builds from parts.
- A signature or minted token never leaves the vault, and the vault never
  makes a presigned URL. A caller can still make a site act within the
  approved sites, as with any key today, and that includes any approved
  endpoint that mints a credential: redaction hides only values the vault
  knows, so a new credential in an answer reaches the caller whole. The known
  cases are refused (any AWS service but S3, a direct GitHub App token on the
  installation-token route, a Google token sent to
  `iam.googleapis.com` or `iamcredentials.googleapis.com`), but the rule is
  the owner's: approving a site that mints credentials hands those
  credentials out, so approve only the paths a key needs. For a signed S3
  request the access key's own policy is the real limit on what it may do.
- A sign-in's token and revoke endpoints are approved with the recipe,
  connected to by the vault itself with the same pinned public-address rules
  as a borrow, and never reachable by a borrow of that key (nor are Google's
  credential-minting hosts). Approving a token endpoint trusts it with the
  client secret and every refresh token, as approving a site trusts it with
  a key.
- An app steers a sign-in only into a key it may already use, and never
  receives its value. Signing in does choose the account: an app that may
  use the key can sign it in as any account whose consent it obtains, and
  every other user of the key (agents, other apps) then acts as that
  account. The card says so, the audit log names who signed in, and the
  exchange's answer names the account's email.
- Body injection widens what a caller can steer only by the declared field.
  A site that stores a request body and shows it later can leak that field,
  just as one that stores headers can leak a header today. The defence is the
  same: approve only the paths that need the key.
- The vault gains no new trust: it already holds the values and sends
  requests. The extra code (body rewriting, three signers) is in the vault
  process and keeps its rule of importing nothing from the server.

### Phases and done-when

1. **Parts, `basic`, `slots` (header template, query, body), the multi-field
   card, `store-key.py`, the store migration.** Done when an existing vault
   migrates with every key and grant unchanged; Twilio-style Basic, a
   two-header key, a JSON body key and a form body key each work for the
   owner, an agent, a helper and an app through a live borrow; echoes of each
   part and of the Basic pair come back redacted; and Settings can replace one
   part without touching the others. Built and used live by an agent and a
   helper, and by the owner on the card; an app's borrow is covered by tests
   through the real routes.
2. **`aws_sigv4` and `jwt_bearer`.** Done when the signer passes AWS's SigV4
   test suite, a real S3-compatible store and a Google service account work
   from an app that never sees a value, and the minted token never appears in
   any answer or log. Built: the signer matches AWS's published vectors and
   botocore; tokens verify against the key's public half, and the Google and
   GitHub exchanges are tested against a stand-in for their endpoints. A
   real store and a real service account still need the owner's
   credentials to check.
3. **`oauth2_refresh`**, then any per-provider signers that are actually
   asked for. Built: renewal, rotation, sign-in and sign-out are tested
   against a stand-in for a provider's token and revoke endpoints, through
   the vault socket and the server's routes, and checked by hand with a
   real Google client (Gmail and Calendar borrows, a renewed refresh token).

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
