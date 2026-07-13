# Scheduled tasks (cron)

How to create recurring jobs that survive a rebuild: manifest schedules, supervised app jobs, the scaffold for owner-managed jobs, and the scheduled-app UI rule. `Read` this before scheduling anything.

The container has `cron` installed. Cron tasks run as `mobius` and get the app id as `$1`.

---

## App schedules are declarations, never boot scripts

`/var/spool/cron/crontabs/` lives in the image layer, not on `/data`, so a rebuild starts with an empty crontab. Installed apps should declare `schedule.default` and `schedule.job` in `mobius.json`. The installer persists an `init-cron.sh` declaration, but boot never executes app-owned shell from that file. FastAPI lifespan parses the effective cadence and job, validates the live app/source tree, and rewrites the entry through `app-job-runner.py` before cron starts.

Managed jobs receive a short-lived app-scoped `APP_TOKEN`, not the owner service token. A manifest with `permissions.background_agent: true` also runs inside the reviewed filesystem sandbox. Its job can see its read-only source, numeric app storage, declared Memory mount, and configured provider credentials; it cannot see arbitrary owner/platform state.

For an installable app, use the manifest contract:

```json
{
  "permissions": { "background_agent": true },
  "schedule": {
    "default": "30 5 * * *",
    "user_configurable": true,
    "job": "fetch.sh"
  }
}
```

The job reads its numeric app id from `$1` and uses `$APP_TOKEN` for its own reviewed API routes. Add the job filename to the manifest install inputs as required by the app contract.

The scaffold is for explicit owner-managed platform/legacy jobs that are not installed from a manifest:

**Use the scaffold, never `crontab -u mobius` directly:**

```bash
bash /app/scripts/init-cron-scaffold.sh <slug> "<cron-schedule>"
# e.g. init-cron-scaffold.sh news "*/10 * * * *"
```

It writes `/data/apps/<slug>/job.sh` (stub, if absent), writes the durable declaration, and installs the live entry. Idempotent — re-running with the same args is a no-op. Then edit `job.sh` for the actual work. This manual path has owner authority; do not present it as the security model for an installable app.

To list / remove: `crontab -u mobius -l` and edit the matching `init-cron.sh` (or delete it before re-running the scaffold). Never call `crontab -u mobius` directly without writing an `init-cron.sh` alongside.

Optionally wrap the job command with `cron-emit.sh` (a manual edit — the scaffold writes the entry unwrapped) so outcomes land in the activity log.

---

## Example managed app job

```bash
#!/bin/bash
# /data/apps/myapp/fetch.sh
API_BASE_URL=http://localhost:8000
APP_ID="$1"

curl -fsS \
  -H "Authorization: Bearer $APP_TOKEN" \
  "$API_BASE_URL/api/apps/$APP_ID/job-context"
```

---

## Key details

- **Credentials:** installable jobs use `$APP_TOKEN`. Never read `/data/service-token.txt` from an app job.
- **Logs:** supervised jobs receive `$APP_JOB_STATE_DIR`; keep app-owned logs there.
- **Sub-agents start with no context** — the `--system-prompt-file` is all they get. Spell out the task fully there.
- **Storage from a cron script** uses the raw API (`window.mobius.storage` only exists inside a running app). Enumerate, don't probe — see the storage section of `building-apps.md`.
- App-scoped routes enforce the reviewed capability contract even if a job asks for more.

---

## Scheduled-app UI — never ship a dead time-picker

A mini-app whose cadence is fixed must NOT render a time-picker that writes a file nothing reads. If the cadence is NOT user-editable, show it in words ("Updates daily") plus "ask the Möbius agent to reschedule." If it IS editable, ship a `sync-cron.sh` that actually rewrites the crontab (via the scaffold above). Lead with the cadence either way. (Full app design conventions: `building-apps.md`.)

---

## Testing cron scripts that send notifications

A cron job that fires a push during testing surprises the partner. Never execute an outbound-channel script live to test it — see the testing rules in `notifications.md` (dry-run flag, completed-day fixture, or ask first).
