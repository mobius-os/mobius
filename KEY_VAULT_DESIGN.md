# Key vault design

Status: draft for review.

## Contract

- The owner manages keys in **Settings › Keys**: values, how each key is sent,
  the sites, methods and paths it may reach, which apps may borrow it, and
  whether agents may use it through the vault.
- **The owner and the owner's agents may read a key.** Every read is logged
  without its value; an agent replacing an existing key notifies the owner.
- **Apps never receive a value**, in their browser frame, service or job. They
  borrow keys through a handshake (below).
- Everything runs in one container, and the agent keeps sudo.

## The handshake

1. **Ask.** An app sends `POST /api/keys/requests` with its own app token:
   the key name, a reason, the sites it needs and how the key is sent. The
   server reads the app's identity from the token, never from the request.
   The answer is `granted`, `pending` or `denied`; asking again is how the app
   checks, and only a new request notifies the owner.
2. **Approve.** The owner sees the app's name, reason and requested sites,
   methods and paths in Settings › Keys, and approves or denies. A new key
   gets its value and the requested sites. An existing key stays as it is:
   its sites are shared by every app and agent that may use it, so approving
   one app never widens them, and the app can use the key only where it
   already works. The card lists anything the app asked for beyond that; only
   the owner's own edit of the key's sites adds it. Sites bound to each grant
   are a planned follow-up. Granting the app from the key editor settles its
   request too.
3. **Borrow.** The app sends an ordinary request to
   `/api/keys/use/<key>/<host>/<path>?<query>` with its app token. The server
   attests the caller as `app:<id>`, drops Möbius credentials and routing
   headers, and relays the request to the vault, which checks the grant and
   the key's sites, adds the key and sends it.

Approving, denying and changing keys is refused to apps, shared-browser
guests and delegated helpers. An edit carries the revision of the key it was
made from, and the vault refuses it if the key changed since, so a stale page
cannot write back old grants. App ids can be reused after a purge, so purging
an app removes it from every grant and request. A purge also waits until every
borrow the app started has been answered, even one whose caller gave up: the
vault knows the app only by its id, so a borrow still on its way could
otherwise use the grant of an app that reuses the id. A borrow whose answer
never came back keeps the app until the server restarts, which ends the vault
process and anything it was still doing.

## Where it lives

- `backend/vault/keyvault.py`: the standalone vault, which imports nothing
  from the server. Its folder `/data/.vault/` (mode 0700, reasserted at
  start, with its own `.gitignore`) holds `master.key`, `keys.json` (AES-GCM
  per value, the key name as associated data), `requests.json`, `usage.jsonl`
  (rotated at 512 KiB, never values), `vault.lock` (one vault per folder)
  and `vault.sock` (mode 0600). There is no TCP port. The vault trusts any
  process that can open the socket: the server, which states the attested
  caller in `X-Vault-Caller` (`app:<id>`, `agent:<chat>`, `owner`,
  `platform:<purpose>`), and owner tools running as the same user, such as
  `store-key.py`. The peer check under *Future work* narrows that.
- `backend/app/key_vault.py`: supervises the vault (task `key-vault`,
  restarts with backoff; the vault exits when the server is no longer its
  parent; output in `/data/logs/key-vault.log`) and is the server's client.
- `backend/app/routes/keys.py`: the owner routes, reveal, the handshake and
  the use relay.
- `frontend/src/components/SettingsView/KeysSection.jsx`: Settings › Keys.
- `backend/scripts/store-key.py`: a sealed-card consumer that saves an
  owner-provided key straight into the vault.

The vault's use path keeps its earlier rules: HTTPS on port 443 only, one
injection slot (a header with an optional prefix, or a query parameter),
default-deny site/method/path globs on the decoded path with dot segments,
encoded slashes and empty segments refused first, public-address DNS pinning,
no redirects, identity encoding and redaction of echoed values. A destination
given as a NAT64 address must embed a public IPv4 address. A value with line
breaks (a PEM file, JSON credentials) can be stored and read but is never sent
in a header. The server
serves answers with a sandboxing CSP, `nosniff`, `no-store` and no cookies.
Bodies are buffered (10 MiB up, 20 MiB down).

## What replaced the per-app secret store

The per-app store's encryption module and the
`permissions.job_secret_read` manifest grant are gone; `/api/apps/{id}/secrets`
survives for one release only as the adapter described under *Transition*. A
manifest that still declares the grant is refused with a pointer to the
handshake. An app's model provider names its vault key in
`model_provider.secret_name`; the platform reads that key at turn launch
(logged as `platform:app-<id>`) when it is the app's own key, or when the
owner granted it to the app and its sites allow `POST <base_url>/responses`.

**Upgrade.** Once per server start, after the vault answers, values in
`/data/app-secrets/<id>/<name>` move into the vault (`import_legacy_app_secrets`):
each becomes a key granted to its own app and the owner's agents, sent as a
bearer token, with no sites until the owner adds them. Each key records its
origin (the app and its exact old name), set at creation. The vault
finds or creates that key in one locked step (`Vault.save_owned`), at the
first free name (`api.key` → `api-key`, then `app<id>-api-key`, then
numbered), so distinct old names never merge, simultaneous saves never leave
two keys for one secret, and no existing key is overwritten. Model providers and the adapter below find a
secret by that origin, not by its vault name. Once the owner or an agent
stores a new value in such a key, its origin and the app's own grant are
cleared: the key is the owner's, and the app uses it only if the owner grants
it. An app holds at most 16 keys of its own, as in the per-app store. Wiping an app's data or
purging the app erases its own keys (a key the owner shared with other apps
stays for them), as the per-app store did. A ledger
(`.vault-import.json`) next to the old files means a key the owner deletes is
never re-imported, unreadable values are retried, and a purged app's leftover
values are skipped because its id may be reused. The old ciphertext stays in
place for one release.

**Transition.** For one release, `/api/apps/{id}/secrets/{name}`
(`routes/app_secrets_compat.py`, marked `Deprecation: true`) answers from the
vault: apps may still save, check and delete their own values, and the owner
and agents may read them, but no app token reads a value back (services and
jobs included); they borrow it through `/api/keys/use`. These routes see only
keys the app itself created and still holds: a key the owner made, or merely
lets the app borrow, can never be replaced through them.

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
