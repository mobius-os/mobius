# Notifications

When and how to send push notifications, why owner requests need no push from you, and the rule against ever executing an outbound-channel script live. This file is the source of truth for notification policy. `Read` it before sending a push or writing a script that does.

Send push notifications for meaningful events — not routine confirmations. If the partner has the chat open, the notify endpoint suppresses the push itself; no guard needed on your side.

---

## When to notify

- A long-running task finishes (app built, data imported).
- Something needs the partner's attention that no owner request covers (an
  error, for example). A question is an owner request, whose card notifies by
  itself.
- The partner explicitly asks to be notified.

---

## `open_item` is live-only — pair it with a push for durability

The `open_item` tool drops an app (numeric id) or chat into the live workspace
beside this chat and is never stored. Use it when the partner asks to open an
item or when a completed item should be visible now.

- Default `activation` to `background`; use `foreground` only when the partner
  just asked to open that exact item.
- Never describe geometry. Say "I've opened it in your workspace" because a
  phone may render a tab or stacked pane rather than a split.
- When the partner may be away, also send a push with the deep link: the push is
  the durable "look at this later" channel and `open_item` is the instant one.

Kind `settings` takes the partner to Settings instead, focused on a section:
`ai-providers` (connecting Claude, Codex or Möbius), `background-agents`,
`github`, or `models`; omit the id for Settings itself. It opens in the
foreground, so use it when they asked to see it or accepted your offer to open
it. Opening only navigates: signing in, entering credentials, disconnecting and
every other change there stay the partner's own taps, so say what to tap rather
than implying it is done. The matching push target is `/shell/?settings=SECTION`.

---

## Owner-input cards notify for you

Requests recorded with `owner_input` (questions, approvals, restarts and secure
input) are shown on a card when the turn ends, and Möbius sends the owner one
"Möbius needs your answer" notification when that card shows a request, or a
revision of one, they have not been shown before. A background or scheduled
run that republishes the same open requests sends nothing new. Its tap opens
this chat at the card, and it stays quiet while the owner is already watching
the chat. Do not call
`notify_owner` for a card: a second push would only duplicate it. Asking in
prose without a request sends nothing and is not a way to wait for the owner.

---

## Sending one

Call `notify_owner` with `title` and `body`. It defaults `target` to this chat;
pass `/shell/?app=APP_ID` to open an app or `/shell/?settings=SECTION` for a
Settings section, and up to two `actions`
(`{action, title, target}`, e.g. `open_app` and `open_chat`).

Optional `tag` (1-128 chars of `A-Z a-z 0-9 _ . : -`, the same shape as a
target `intent`) groups pushes about the same thing: a newer push replaces the
older one on the device instead of stacking. Using the target's intent as the
tag (`"target": "/shell/?app=ID&intent=dm:alice.example"`, `"tag":
"dm:alice.example"`) groups notifications per destination. The server scopes
the tag to the sender, so it can never replace another app's notifications.
Every send still gets its own history row. An app's push is not sent while the
shell is visibly showing that app; the bell still records it. Sends are
rate-limited per sender (each app, each agent chat, the owner).

---

## Durable Undo for recoverable deletion

The chat, app, and project deletion endpoints create their own seven-day Undo
receipt in notification history in the same transaction as the soft delete.
Agents should call the owning deletion endpoint only; do not send a second
recovery notification or construct `recover_*` actions manually. If deletion
fails, no receipt is created. Irreversible app-data clearing has no Undo.

---

## Never execute an outbound-channel script live during development

Running a real script that calls `/api/notifications/send` (or any outbound channel — push, email, SMS) fires a real push to the partner's phone — an ugly surprise if you were "just testing." Use one of these instead:

1. **Dry-run flag.** Add `--dry-run` that prints the payload to stdout instead of POSTing. Keep it as a permanent feature so future-you and the partner can preview the content.
2. **Completed-day fixture.** Seed the data so the script's guard clause no-ops (e.g. for a habit reminder, populate all habits as checked-in for today).
3. **Ask first.** If neither is available, tell the partner "I want to test the reminder script — it will send a real push; OK?" and wait for confirmation.

This applies to cron jobs that notify too — see `cron.md`.
