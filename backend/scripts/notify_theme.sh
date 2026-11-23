#!/bin/sh
# Notify the frontend that theme.css has changed.
# Call this after writing /data/shared/theme.css so the
# user sees the change immediately mid-conversation.

"$(dirname "$0")/mapi" -X POST /api/notify -d '{"type":"theme_updated"}' \
  >/dev/null 2>&1 || true
