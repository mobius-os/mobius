# Key vault design

Status: draft for review.

## Contract

- The owner manages keys in **Settings › Keys**: values, how each key is sent,
  the sites, methods and paths the owner and agents may send it to, which apps
  may borrow it, and whether agents may use it. **Each app grant has its own
  sites**, so approving sites for one app never widens another app or the
  agents. The owner's top-level agents may manage keys too, but only the
  owner's own input lets an app use a key or reach more sites (approving a
  request, adding an app, widening an app's sites); agents may remove an app
  or narrow its sites.
- **The owner and the owner's top-level agents may read a key**, unless its
  Agents switch is off; only the owner's own input turns that switch on.
  Delegated helpers may borrow a key but never read its value. Every read and
  change is logged, without its value, under the caller it was made for. An
  agent that replaces a value, changes how a key is sent or adds sites
  through the server notifies the owner. A save names only what it changes:
  on an existing key, an omitted `inject` or `grants` keeps the current one,
  so replacing a value never touches who may use it. An agent's edit always carries the revision it was
  made from, so it cannot undo a change the owner made meanwhile.
- **Apps never receive a value**, in their browser frame, service or job. They
  borrow keys through a handshake (below).
- **The server's HTTP routes enforce who may do what.** The owner-only gate
  (approving requests, letting an app use a key or reach more sites, turning
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

1. **Ask.** An app sends `POST /api/keys/requests` with its own app token:
   the key name, a reason, the sites it needs and how the key is sent. The
   sites are exact hosts: a wildcard (`*.workers.dev`) is refused, because on
   a shared domain the app's author can own a subdomain, and that host reads
   the key from the request itself. Only the owner adds a wildcard, on the
   key's page. The server reads the app's identity from the token, never
   from the request. An app token a shared-browser guest's session minted
   cannot ask (or borrow): the guest works through the app, not as it.
   The answer is `granted`, `pending` or `denied`; asking again is how the app
   checks, and only a new request notifies the owner. One app may hold at
   most ten open requests. A granted app that asks for sites its grant lacks
   keeps using what it has, is told which are missing (`sites_not_allowed`),
   and its ask for them waits for the owner like any request
   (`sites_request`).
2. **Approve.** The owner sees the app's name, reason, requested sites,
   methods and paths and, for a new key, how it would be sent, in Settings ›
   Keys, and approves or denies; only the owner's own input can approve. The
   request only proposes: approval applies the sites and slot the owner's
   answer carries (a new key answered without a slot is sent as
   `Authorization: Bearer`). The sites go to that app's own grant. An existing
   key keeps its value and slot, which only its own page changes. Granting
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
the vault answers 429. Headers
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
through the adapter below, or deleted, is not imported over it. Model providers and the adapter below find a
secret by that origin, not by its vault name. Once the owner or an agent
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
app ids. `restore-data.py` warns, and, with the vault stopped,
withdraws grants and requests for every id the restored database lacks or
gives to another installation (slug and token nonce), using the vault's own
purge step; if it cannot open the vault it changes nothing and asks the owner
to review every grant. It runs as root from `/app/scripts`, so the image ships
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
