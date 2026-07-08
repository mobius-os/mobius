# Scheduled tasks (cron)

How to create recurring jobs that survive a rebuild: the scaffold, why every cron task needs an `init-cron.sh`, the service token, and the scheduled-app UI rule. `Read` this before scheduling anything.

The container has `cron` installed. Cron tasks run as `mobius` and get the app id as `$1`.

---

## Every cron task needs an `init-cron.sh` or it vanishes on rebuild

`/var/spool/cron/crontabs/` lives in the IMAGE layer, not on `/data`, so any rebuild (deploy, image pull) starts with an empty crontab. The entrypoint replays `/data/apps/*/init-cron.sh` on every boot to restore entries — so a cron task without a matching `init-cron.sh` silently disappears on the next rebuild, with no error. A scheduled job that just stops running is hard to notice, so this is the cardinal cron rule.

**Use the scaffold, never `crontab -u mobius` directly:**

```bash
bash /app/scripts/init-cron-scaffold.sh <slug> "<cron-schedule>"
# e.g. init-cron-scaffold.sh news "*/10 * * * *"
```

It writes `/data/apps/<slug>/job.sh` (stub, if absent), writes `/data/apps/<slug>/init-cron.sh` (the replay script), and installs the live entry. Idempotent — re-running with the same args is a no-op. Then edit `job.sh` for the actual work.

To list / remove: `crontab -u mobius -l` and edit the matching `init-cron.sh` (or delete it before re-running the scaffold). Never call `crontab -u mobius` directly without writing an `init-cron.sh` alongside.

Optionally wrap the job command with `cron-emit.sh` (a manual edit — the scaffold writes the entry unwrapped) so outcomes land in the activity log.

---

## Example cron script

```bash
#!/bin/bash
# /data/apps/myapp/job.sh
SERVICE_TOKEN=$(cat /data/service-token.txt)
API_BASE_URL=http://localhost:8000
APP_ID=<numeric app id>

claude -p "Fetch today's data, process it, and write the result to \
  the storage API at $API_BASE_URL/api/storage/apps/$APP_ID/data.json \
  using bearer token $SERVICE_TOKEN" \
  --system-prompt-file /data/apps/myapp/prompt.md \
  --allowedTools "Bash(command)" \
  --max-turns 30 \
  2>> /data/cron-logs/myapp.log
```

---

## Key details

- **Service token:** `/data/service-token.txt` (chmod 600 — do NOT move to `/data/shared/`).
- **Logs:** write stderr to `/data/cron-logs/`.
- **Sub-agents start with no context** — the `--system-prompt-file` is all they get. Spell out the task fully there.
- **Storage from a cron script** uses the raw API (`window.mobius.storage` only exists inside a running app). Enumerate, don't probe — see the storage section of `building-apps.md`.
- After setting up a scheduled task, record it in this chat's note (`chats/$CHAT_ID/index.md`) — grow its `## Summary`; see the `memory.md` skill. (There is no inbox.)

---

## Scheduled-app UI — never ship a dead time-picker

A mini-app whose cadence is fixed must NOT render a time-picker that writes a file nothing reads. If the cadence is NOT user-editable, show it in words ("Updates daily") plus "ask the Möbius agent to reschedule." If it IS editable, ship a `sync-cron.sh` that actually rewrites the crontab (via the scaffold above). Lead with the cadence either way. (Full app design conventions: `building-apps.md`.)

---

## Testing cron scripts that send notifications

A cron job that fires a push during testing surprises the partner. Never execute an outbound-channel script live to test it — see the testing rules in `notifications.md` (dry-run flag, completed-day fixture, or ask first).
