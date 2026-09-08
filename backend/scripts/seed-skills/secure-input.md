# Secure input

Use this skill when the owner needs to supply a password, API key, token,
private username, or other value that should not pass through the LLM API or
enter the chat transcript. The trusted card is an interactive live-chat
primitive, not a background/scheduled-agent mechanism.

## Default: saved sealed pause

Prepare the narrow local consumer, explain what it will do, and finish all
other work and closeout **before** invoking the helper. The card is the final
action of the turn, exactly like Möbius's saved Q&A and approval cards.

```bash
python3 /data/platform/backend/scripts/secure-input.py owner-credentials
```

This requests the current password, new username, new password and confirmation.
The helper saves the safe request and returns a receipt immediately. It does
**not** wait for the owner or receive values. On a confirmed receipt, end the
turn with **no further text or tools**. Do not append “I'll wait,” poll, or run
a background consumer. No answer and no permission may be inferred from the
receipt. The saved card blocks further work until the owner submits or cancels.

For another local consumer:

```bash
python3 /data/platform/backend/scripts/secure-input.py run \
  --title "Connect service" \
  --description "Credentials go directly to the local connector." \
  --field username:text:"Username" \
  --field api_key:password:"API key" \
  -- python3 /data/path/to/safe-consumer.py
```

Möbius persists only the prompts, pre-authored command, working directory and
safe lifecycle status. Unsubmitted cards survive agent completion and server
restarts with no human deadline. Only one owner-input card may be open per chat.
A lost save response is recovered by retrying the **identical** request; a
failed save is not a waiting card and never means credentials were provided.

When the owner submits, the backend runs the consumer once with one JSON object
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

Consumer execution has a two-minute safety limit. Stop cancels the operation
without resuming the chat. A crash or restart during execution leaves its
outcome explicitly unknown and **never automatically repeats** the operation:
side effects may already have happened. Submitted values have no recovery copy.
A fresh request requires checking the operation's outcome first.

Never call secret-bearing create/consume endpoints with curl or a general HTTP
tool. Use the trusted helper; values must not enter tool output or model context.

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

A background or scheduled agent must not open a live card. Leave a declarative
request for the next interactive chat instead. The explicitly approved reveal
path is exceptional: it remains a live, transient handoff to the current model;
never convert it into a persisted secret or use it to bypass sealed execution.
