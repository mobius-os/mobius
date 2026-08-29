---
name: live-screen-control
description: Use when the partner explicitly activates the shell's Share screen button and asks the agent to inspect or drive their exact current Möbius browser session. Operate only the chat-bound, owner-visible live screen; capture evidence through the live helper and release control before the turn ends.
---

# Live screen control

Use this only after the partner deliberately clicks **Share screen** for the
current chat and asks you to inspect or drive the exact screen they are using.
It is a partner-owned, 15-minute consent window—not ambient browser access.

This is distinct from `agent-browser`: live screen control sees the partner's
exact signed-in tab, current scroll position, and unsaved UI state. Headless
visual testing remains the right path when exact session state is unnecessary.

## Check and inspect

```bash
python3 "$SCRIPTS_DIR/screen-control.py" status
python3 "$SCRIPTS_DIR/screen-control.py" snapshot
python3 "$SCRIPTS_DIR/screen-control.py" screenshot
```

`snapshot` returns current interactive elements with ephemeral refs. Re-run it
after every mutation before using another ref. `screenshot` writes the final
image directly under this chat's served `media/` directory and prints the
ready-to-use embed. View that exact file before describing it, verify its
served media URL, and put the embed before the description in the same message,
following `visual-testing.md`.

## Drive

```bash
python3 "$SCRIPTS_DIR/screen-control.py" click e4
python3 "$SCRIPTS_DIR/screen-control.py" click app:42:e3
python3 "$SCRIPTS_DIR/screen-control.py" click-at 520 410
python3 "$SCRIPTS_DIR/screen-control.py" type "Replacement text" --ref e7
python3 "$SCRIPTS_DIR/screen-control.py" type " more" --append
python3 "$SCRIPTS_DIR/screen-control.py" scroll 560
python3 "$SCRIPTS_DIR/screen-control.py" press Enter
```

Prefer semantic refs from a fresh snapshot over coordinates. The bridge accepts
only the closed actions above; it never evaluates arbitrary JavaScript. Password,
payment, and one-time-code fields stay masked and refuse agent typing. The
partner's local input always takes priority, and their visible header button or
browser sharing indicator can stop the session immediately.

The ordinary destructive-action rules still apply. A shared screen is consent
to inspect and drive the requested investigation, not approval to delete data,
send messages, submit purchases, change credentials, or perform another
irreversible action.

## Closeout

Always release the live browser before handing control back, even after an
error. Do not leave it open for a possible follow-up:

```bash
python3 "$SCRIPTS_DIR/screen-control.py" stop
```

If the partner stopped it first, `status` ordinarily reports no active session;
that is a normal outcome. State any device/browser behavior that could not be
exercised and do not replace exact-session evidence with a headless proxy.
