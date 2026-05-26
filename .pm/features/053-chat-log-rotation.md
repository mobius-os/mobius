---
id: "053"
title: chat.log uses plain FileHandler — switch to RotatingFileHandler
status: done
priority: 6
hook: chat.py:52 uses logging.FileHandler with no rotation. With MOEBIUS_CHAT_DEBUG=0 (default) it's ~15 KB/day — fine. With MOEBIUS_CHAT_DEBUG=1 left on by mistake, it grows to GB in days.
created: 2026-05-25
---

## Why

Surfaced by the 10th (wide-scope, operational longevity) review
pass.

`backend/app/chat.py:52` (approximate):

```python
handler = logging.FileHandler("/data/logs/chat.log")
```

No rotation. INFO logging is sparse (10-20 lines per chat
session), so the file grows ~15 KB/day in normal use. Manageable
but unbounded.

The real hazard: `MOEBIUS_CHAT_DEBUG=1` enables per-delta DEBUG
logging — 50-200+ lines per turn. If a debugging session leaves
this env var set in `.env` or in the docker-compose override, the
log file grows into the GB range within days and could exhaust
the volume.

## What

Switch to `logging.handlers.RotatingFileHandler`:

```python
from logging.handlers import RotatingFileHandler

handler = RotatingFileHandler(
  "/data/logs/chat.log",
  maxBytes=50 * 1024 * 1024,  # 50 MB
  backupCount=3,
)
```

50 MB × 4 files = 200 MB cap. Covers debug-on accidents without
losing the recent history.

Same pattern is worth applying to `/data/cron-logs/*` if any
cron script writes there with an unbounded FileHandler (out of
scope for this ticket; flag as a follow-up if found during
implementation).

## Done when

- [ ] `chat.log` uses RotatingFileHandler with 50 MB × 4 cap.
- [ ] Container restart picks up the new handler cleanly
      (FileHandler → RotatingFileHandler swap is safe; existing
      log file is preserved and continues from the same point).
- [ ] No test required — logging configuration is a runtime
      concern, not behavior.
- [ ] Note in CLAUDE.md "Development notes" that `MOEBIUS_CHAT_DEBUG=1`
      is bounded by rotation but should still be turned off when
      done debugging.
