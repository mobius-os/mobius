---
title: Every cron task needs an init-cron.sh or it vanishes on rebuild
type: note
importance: 4
access_count: 0
last_accessed: null
tags: [platform, cron, gotcha]
mocs: [mobius-platform]
created: 2026-06-02
updated: 2026-06-02
---
`/var/spool/cron/crontabs/` lives in the image layer, not on `/data`, so any rebuild
(deploy, image pull) starts with an empty crontab. The entrypoint replays
`/data/apps/*/init-cron.sh` on boot to restore entries — so a cron task without a
matching `init-cron.sh` silently disappears on the next rebuild.

**Why:** a scheduled job that just stops running with no error is hard to notice.

**How to apply:** use `bash /app/scripts/init-cron-scaffold.sh <slug> "<schedule>"` — it
writes `job.sh` + `init-cron.sh` + installs the live entry, idempotent. Never call
`crontab -u mobius` directly without writing the matching `init-cron.sh`. Wrap the
command with `cron-emit.sh` so outcomes land in the activity log.
