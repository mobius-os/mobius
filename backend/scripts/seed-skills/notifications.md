# Notifications

When and how to send push notifications, including firing the push yourself when you end a turn on an open question, and the rule against ever executing an outbound-channel script live. This file is the source of truth for notification policy. `Read` it before sending a push or writing a script that does.

Send push notifications for meaningful events — not routine confirmations. If the partner has the chat open, the notify endpoint suppresses the push itself; no guard needed on your side.

---

## When to notify

- A long-running task finishes (app built, data imported).
- Something needs the partner's attention (error, question).
- The partner explicitly asks to be notified.

---

## `open_item` is live-only — pair it with a push for durability

The `open_item` system event (see `core.md`, "Opening something in the partner's workspace") drops an app or chat straight into the workspace, but only for a partner whose session is live right now — it fires once and is never stored. When the open matters and the partner may be away, ALSO send a push notification with the deep link here: the push is the durable "look at this later" channel, `open_item` is the instant one. They compose — fire both.

---

## Ending a turn with an open question — you fire the push yourself

The platform does NOT auto-notify when you call `AskUserQuestion` or end a turn with a prose clarifying question. You own this explicitly: same `curl POST /api/notifications/send` pattern you use after building an app, with a question-shaped title and body. Firing it from bash means the HTTP response lands in your tool output, so you see success/failure and can react (re-try, fall back to text) on the same turn.

Title: "Möbius needs your answer". Body: the first ~80 chars of your question. Include `source_id: "$CHAT_ID"` and `target: "/shell/?chat=$CHAT_ID"` so the tap routes back here **inside the PWA** — the bare `/chat/<id>` form escapes the service-worker scope and a cold tap opens a browser tab instead. Skip the notify only when you delivered something useful in the same turn AND that delivery already sent a notification.

---

## The curl forms

Minimum viable:

```bash
curl -s -X POST "$API_BASE_URL/api/notifications/send" \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"Task complete","body":"Your expense tracker app is ready."}'
```

`source_type` defaults to `"agent"`; `source_id` is optional. Full form when you want a deep link + actions:

```bash
curl -s -X POST "$API_BASE_URL/api/notifications/send" \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Task complete",
    "body": "Your expense tracker app is ready.",
    "source_id": "'"$CHAT_ID"'",
    "target": "/shell/?app=APP_ID_HERE",
    "actions": [
      {"action": "open_app", "title": "Open App", "target": "/shell/?app=APP_ID_HERE"},
      {"action": "open_chat", "title": "View Chat", "target": "/shell/?chat='"$CHAT_ID"'"}
    ]
  }'
```

---

## Never execute an outbound-channel script live during development

Running a real script that calls `/api/notifications/send` (or any outbound channel — push, email, SMS) fires a real push to the partner's phone — an ugly surprise if you were "just testing." Use one of these instead:

1. **Dry-run flag.** Add `--dry-run` that prints the payload to stdout instead of POSTing. Keep it as a permanent feature so future-you and the partner can preview the content.
2. **Completed-day fixture.** Seed the data so the script's guard clause no-ops (e.g. for a habit reminder, populate all habits as checked-in for today).
3. **Ask first.** If neither is available, tell the partner "I want to test the reminder script — it will send a real push; OK?" and wait for confirmation.

This applies to cron jobs that notify too — see `cron.md`.
