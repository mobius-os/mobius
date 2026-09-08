---
name: claude
description: Compatibility pointer for handing bounded work to Claude from a Codex turn. Prefer the installed Subagents capability; use the direct connected Claude CLI only when that app is absent.
---

# Delegating to Claude

When the **Subagents** app is installed, read the complete `subagents` skill and
use its guarded helper. It owns provider enablement, configured model/effort,
durable identity, restart recovery, nested work, and parent wake-up. Do not run
`claude -p` alongside that mechanism.

Only when the app is genuinely absent may you use the connected CLI fallback.
Confirm `CLAUDE_CONFIG_DIR` and the `claude` executable exist, preserve any
owner-requested model/effort, and wait for the result inside this turn:

```bash
claude -p --output-format text --model <model-or-alias> --effort <level> "<prompt>"
```

- Omit `--model` when the partner did not name one; Claude's configured default
  is the honest default.
- Effort values are `low`, `medium`, `high`, `xhigh`, and `max`. Omit the flag
  when there is no reason to override the default.
- Do not use `--background`: this fallback must finish before the turn ends.
- Match the current task's authority. For code changes, tell Claude exactly what
  it may edit and how to verify the result. For review or investigation, state
  that it is read-only.
- Let the inherited `CLAUDE_CONFIG_DIR` select the connected account. Never
  inspect, copy, print, or relocate its credential file.

Shape the prompt around the result:

```text
Goal: <specific outcome>
Where: <the files or system to inspect>
Constraints: <read-only or exact write scope; important boundaries>
Done when: <tests, evidence, or decision the response must contain>
```

Keep the prompt lean and point to real files instead of pasting large context.
After Claude returns, assess its work, verify any edits, and tell the partner
which provider did what. Its response is evidence or a candidate change, not a
substitute for your own review.
