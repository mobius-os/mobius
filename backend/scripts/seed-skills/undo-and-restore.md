# Undo and restore

Use this for path-scoped Git undo, accidental chat deletion, app restoration,
and deciding when a full owner-data backup restore is actually required.

---

## Commit agent-owned changes before they need undo

`/data/platform` and `/data` are separate Git repositories. Record the starting
revision before editing and commit only paths owned by the current task:

```bash
git -C /data/platform rev-parse HEAD
PM_COMMIT_ROOT=/data/platform pm-commit --from <starting-sha> \
  'one-line what and why' -- <exact platform paths>

git -C /data rev-parse HEAD
pm-commit --from <starting-sha> 'one-line what and why' -- <exact data paths>
```

`pm-commit` preserves unrelated staged and unstaged work and stops when another
commit touched one of the declared paths after the recorded starting revision.
Never use `git add -A` or a broad reset in either shared worktree.

To restore one path, inspect its history and take only the intended version:

```bash
git -C /data log --oneline -- shared/<path>
git -C /data restore --source=<good-sha> -- shared/<path>
```

Review the diff, then commit that exact restored path. Do not roll the whole
repository backward to repair one file.

## Restore a deleted chat

Deleted chats remain recoverable for **7 days**:

```bash
curl -s -X POST "$API_BASE_URL/api/chats/{chat_id}/recover" \
  -H "Authorization: Bearer $AGENT_TOKEN"
```

Tell the partner about the retention window when an accidental deletion is the
reason for the restore.

## Restore a deleted app

Deleted apps are tombstoned for **7 days** with source and saved data intact:

```bash
curl -s -X POST "$API_BASE_URL/api/apps/{app_id}/recover" \
  -H "Authorization: Bearer $AGENT_TOKEN"
```

Reinstalling a store app with the same `manifest_url` is equivalent: it
reattaches to the tombstoned row with the same id and data. Use the exact app id
from the deletion receipt, never list position. After seven days the tombstone
is purged. Confirm before deleting even though the short retention window makes
uninstall reversible.

## Full owner-data restore is separate

External Recovery is an emergency access path into the live container, not a
backup format or cold-restore service. For an owner-data disaster, inspect the
backup first and use `/app/scripts/restore-data.py` according to its built-in
help. Preserve any `.restore-rollback.*` directory until the restore reports
complete; it can contain originals that still require manual reconciliation.
