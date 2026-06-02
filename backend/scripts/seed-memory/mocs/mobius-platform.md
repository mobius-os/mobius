---
title: The Möbius platform
type: moc
tags: [platform]
---
# The Möbius platform

How the platform itself behaves — the operational facts that bite you when you
edit the shell, backend, cron, or storage.

## Deploy & lifecycle

- [[shell-rebuild-needs-restart]] — `rebuild_shell.sh` doesn't live-reload.
- [[backend-edits-need-restart-and-host-patch]] — writable-layer + boot risk.
- [[cron-survives-rebuild-via-init-cron]] — crontab is wiped on rebuild.

## Data

- [[sqlite-needs-manual-alter]] — `create_all` never ALTERs an existing table.
- [[data-is-a-git-repo]] — commit agent-owned state with `pm-commit`.

## Conventions

- [[describe-tree-over-hardcoded-lists]] — list dirs live; docstring new files.
