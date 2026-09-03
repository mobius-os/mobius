# Secure input

Use this skill when the owner needs to supply a password, API key, token,
private username, or other value that should not pass through the LLM API or
enter the chat transcript. The trusted card is an interactive live-chat
primitive, not a background/scheduled-agent mechanism.

## Default: sealed execution

Tell the owner what the local operation will do, then invoke the trusted helper:

```bash
python3 /data/platform/backend/scripts/secure-input.py owner-credentials
```

That built-in flow requests the current password, new username, new password,
and confirmation. Möbius holds the submitted strings only in server memory
until the helper consumes them once. The helper passes them to the credential
updater as JSON on stdin, discards its stdout and stderr, and maps only fixed
outcome codes to trusted messages. The card's title,
field prompts, and completion state remain as a safe receipt; submitted values
do not enter chat, tool arguments, environment variables, temporary files, or
the durable transcript.
The helper gives a local consumer two minutes to finish; on timeout it
terminates the process, redacts any partial output, and discards the values.

For another local consumer:

```bash
python3 /data/platform/backend/scripts/secure-input.py run \
  --title "Connect service" \
  --description "Credentials go directly to the local connector." \
  --field username:text:"Username" \
  --field api_key:password:"API key" \
  -- python3 /data/path/to/safe-consumer.py
```

The consumer reads one JSON object from stdin. Its source may be durable, but it
must never contain submitted values. It should consume the values immediately
and must not log, persist, cache, shell-expand, or copy them into another
command's arguments or environment. Prefer a narrow operation that writes only
the intended hashed/encrypted destination. The helper discards all consumer
stdout and stderr and reports only a predefined success, failure, or timeout;
a consumer that deliberately writes elsewhere remains outside this boundary.

Never call the create/consume endpoints with curl or a general HTTP tool. The
helper keeps the one-use capability and secret response out of model-visible
tool output. One request may be open per chat. It stays open until it is
submitted or cancelled on Stop; once submitted, transient values are cleared
if the helper does not consume them within two minutes.

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

A background or scheduled agent must not open this card: nobody may be present,
and the transient request cannot survive a restart. Leave a declarative
request for the next live chat instead.
