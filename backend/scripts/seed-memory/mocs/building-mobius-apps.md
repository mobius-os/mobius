---
title: Building Möbius apps
type: moc
tags: [apps]
---
# Building Möbius apps

Hard-won contracts for building mini-apps. Each note below prevented a real
bug or a wasted tool-call loop. Read the relevant one before building.

## Storage

- [[window-mobius-storage-is-default]] — the default persistence layer.
- [[storage-json-no-envelope]] — the `.json` envelope trap (silent data loss).
- [[storage-enumerate-dont-probe]] — list children; never brute-force filenames.

## UI & rendering

- [[theme-aware-css-vars]] — use `var(--…)`, never hardcode colors.
- [[mini-apps-no-native-dialogs]] — no `confirm/alert/prompt`; build in-app modals.
- [[three-bare-specifier]] — import `'three'`, never a versioned vendor URL.

## Lifecycle

- [[register-app-only-on-create]] — re-running `register_app.py` makes duplicates.
- [[offline-capable-is-for-code-not-data]] — what the flag actually does.
