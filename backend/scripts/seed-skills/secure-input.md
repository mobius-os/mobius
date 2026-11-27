# Secure input

Use this skill when the owner needs to supply a password, API key, token,
private username, or other secret that must stay out of the LLM API and the
chat transcript, including adding or saving a key in their key vault
(Settings › Keys). The card is a live-chat primitive, not for background work.

## Default: saved sealed pause

Prepare the narrow local consumer, explain what it will do, and finish all
other work and closeout **before** invoking the helper. The card is the final
action of the turn, exactly like Möbius's saved Q&A and approval cards.

Call `request_secret` with `preset: "owner_credentials"`. This requests the
current password, new username, new password and confirmation. The tool saves
the safe request and returns a receipt immediately. It does
**not** wait for an answer or receive values. On a confirmed receipt, end the
turn with **no further text or tools**. Do not append “I'll wait,” poll, or run
a background consumer. No answer and no permission may be inferred from the
receipt. The saved card blocks further work until it is submitted or cancelled.

For another local consumer, call `request_secret` with a `title`, optional
`description`, `fields` such as `[{"name":"api_key","type":"password","label":"API key"}]`,
the consumer `command` as an argv list (`["python3", "/data/path/to/safe-consumer.py"]`),
and an absolute `cwd` when it is not `/data`. The same card is available from
`python3 /data/platform/backend/scripts/secure-input.py run` for shells without
the tool.

Möbius persists only the prompts, pre-authored command, working directory and
safe lifecycle status. Unsubmitted cards survive agent completion and server
restarts with no human deadline. Only one owner-input card may be open per chat.
A lost save response is recovered by retrying the **identical** request; a
failed save is not a waiting card and never means credentials were provided.

Any authenticated participant that can read the card may submit or cancel it;
there is no separate card-answer role. For a value already held in an
authorized local file or environment variable, an agent can keep the bytes out
of model-visible arguments with:

```bash
python3 /data/platform/backend/scripts/secure-input.py submit-saved \
  --chat-id <chat-id> --request-id <card-id> \
  --field-file field=/path/to/value
```

Use `--field-env field=ENV_NAME` for an environment value. This preserves the
submission path; it cannot undo earlier disclosure to a model.

When submitted, the backend runs the consumer once with one JSON object
on stdin. Values exist only transiently in memory. The consumer must never log,
persist, cache, shell-expand or copy them into arguments or environment. Write
only the intended hashed/encrypted destination. Its command source may be
durable but must never contain submitted values. All stdout/stderr is discarded;
only fixed outcome codes become a safe receipt and resume the chat.

The consumer runs from the saved working directory with a minimal runtime
environment, not the publishing agent's session. Do not depend on `AGENT_TOKEN`
or other turn-only credentials. Prepare a self-contained narrow local operation;
if it needs additional authority, resolve that before opening the card rather
than saving credentials in its execution specification.

## API keys and service tokens: save to the key vault, then borrow

For an API key or token a service call needs, use the vault's consumer
instead of writing your own: `["python3",
"/data/platform/backend/scripts/store-key.py", "NAME"]` (add `--header NAME
[--prefix TEXT]` or `--query NAME` when the service does not take
`Authorization: Bearer`). Save it without sites: the first time you borrow it
for a site, ask the owner with the `request_key` tool and they approve that
site on its card (below). Add `--site api.example.com:GET,POST:/v1/*` only when the
owner told you where the key goes. The owner then sees and manages it in
Settings › Keys. A service that needs more than one token (a username and
password, an id plus a secret, a secret in the request body) is one key with
several parts: give the card one field per part (field and part names are
lowercase letters, digits and `_`) and add `--part NAME` for each
(`--part NAME:plain:LABEL` for an id or username that isn't secret,
`--part NAME:secret:LABEL` to label a secret one), then
`--basic USER_PART PASSWORD_PART` or `--slots JSON` (header templates such as
`Token {id}:{secret}`, query parameters, or JSON or form body fields). Ask for
such a key with `request_key`'s `parts` and a matching `inject`. Read one part
with `?part=NAME`. For services that sign requests or mint tokens, the vault
does it and the secret never leaves: S3-compatible storage (S3, R2, B2, MinIO;
service `s3`, the only AWS service the vault signs for) uses an `aws_sigv4`
recipe, and the access key's own policy is what limits a signed request; a
Google service account (its JSON file as one part, with `claims.scope`), a
GitHub App (its PEM, `claims.iss` and an installation id) or a direct signed
token use `jwt_bearer`. Pass the recipe
to `store-key.py` with `--recipe JSON`; when asking with `request_key`
instead, mark a key-file part `multiline` in its `parts`. Secret parts sent by
these recipes are at least eight characters. It never replaces a key
that already has that name: it leaves that key as it was and fails. A failed
consumer's receipt does not say why and the submitted value is discarded, so
before opening the card check `mapi /api/keys` for the name; if it exists,
ask the owner whether to replace it and add `--replace` only when they said
so. After a failed receipt, check the list again before asking the owner a
second time. It writes to the vault directly, so the owner gets no
notification of a replacement: tell them yourself.

To use a key on a site it does not list yet, or a key the owner has not
saved, call `request_key` with the key's name, a one-sentence `reason`, the
exact `sites`, an optional `inject` for a new key, and your closing `message`.
If you may already use it there, or the owner already said no, it answers at
once with no card (`answered_without_card`, with `granted`, `paused` or
`denied`) and your turn continues. Otherwise it saves a key card in this chat
and ends your turn like any other owner card: the chat waits for the owner,
nothing else tells them (no notification is sent), and their answer resumes
you as **Allow** or **Not now**, so do not poll. A new key is pasted on that
card, never into chat.

Use the key by borrowing it, not reading it: send the request through
`mapi /api/keys/use/NAME/api.example.com/v1/...` with any method, headers and
body; the vault adds the key and redacts it from the answer. Read the value
(`mapi /api/keys/NAME/value`) only when a tool genuinely needs the raw key,
and never print it: anything a command prints stays in the chat log.

Consumer execution has a two-minute safety limit. Stop cancels the operation
without resuming the chat. A crash or restart during execution leaves its
outcome explicitly unknown and **never automatically repeats** the operation:
side effects may already have happened. Submitted values have no recovery copy.
A fresh request requires checking the operation's outcome first.

Prefer the helper when an agent submits an existing local value so credential
bytes do not enter tool output or model context.

## Explicit reveal for debugging

Revealing is an escalation, not the default. First explain that the AI provider
will receive the value and may retain it in its own session even though Möbius
will omit the marked tool result from its live UI, transcript, and chat logs.
Proceed only after the owner explicitly asks for or approves that exposure.
Then use:

```bash
python3 /data/platform/backend/scripts/secure-input.py reveal \
  --title "Reveal credential for debugging" \
  --description "These values will be sent to the AI provider for this turn." \
  --field api_key:password:"API key"
```

The card requires a second confirmation. After reveal, do not repeat the value
in prose, another tool call, a file, or a command. Use it only for the approved
diagnostic and return to sealed execution for any follow-up.

## Boundaries

The ordinary sealed path keeps submitted values out of the LLM API and Möbius
persistence; only safe receipt metadata persists. Values necessarily exist
briefly in the owner's browser DOM, the authenticated request body, server RAM,
helper RAM, and the chosen consumer process. They are cleared or become
unreachable after submission/consumption and are never intentionally written
to disk. A process crash lets the OS reclaim that memory; it does not create a
recovery copy.

A background or scheduled agent must not open a live card, but may answer an
existing card it can access. The explicitly approved reveal
path is exceptional: it remains a live, transient handoff to the current model;
never convert it into a persisted secret or use it to bypass sealed execution.
