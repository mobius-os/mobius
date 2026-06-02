---
title: create_all never ALTERs an existing table — new columns need manual ALTER
type: note
importance: 3
access_count: 0
last_accessed: null
tags: [platform, backend, gotcha]
mocs: [mobius-platform]
created: 2026-06-02
updated: 2026-06-02
---
SQLAlchemy `create_all` only CREATEs missing tables; it never adds a column to an
existing one. A new model field won't appear on an existing `/data/db/ultimate.db`.

**Why:** the column is silently missing in prod and queries fail or read NULL.

**How to apply:** run a manual `ALTER TABLE <t> ADD COLUMN <c> ...` against the existing
DB when you add a model field, or ship a tiny migration step.
