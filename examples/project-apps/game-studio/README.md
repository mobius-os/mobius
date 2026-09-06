# Game Studio — example Project app

A complete local example of an installable provider, not a built-in game type.
It contributes `Canvas game` to **Projects + → Project apps**, copies an editable
scene into each new project, and builds that scene into its own playable canvas
Creation. No shell-specific game code is needed.

Its launcher resolves `game` with `window.mobius.projects.templates()` so an
installation with a different local slug works unchanged. The manifest's
`kind: game` remains discoverable without adding it to the four quick starts.

See [the Project apps contract](../../../PROJECT-APPS.md). This example is
not installed automatically. Test it in a disposable instance, or intentionally
apply a copy when you want it as a real app. Never execute a live paid agent
request merely to smoke-test a template action; actions create reviewable drafts.
