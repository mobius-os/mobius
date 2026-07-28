---
name: claude
description: Read before handing a task to Claude Code from a Codex turn. Use the connected Claude CLI non-interactively, preserve the owner's requested model and effort, give it a bounded outcome-first prompt, wait for the result inside this turn, and report which provider did what.
---

# Delegating to Claude

Use this when the partner explicitly asks Codex to consult or delegate to
Claude, or when an independent Claude pass would materially improve an
authorized task. Möbius exposes `CLAUDE_CONFIG_DIR` to Codex only when Claude is
connected, so first confirm that variable exists and the `claude` executable is
available. If either is absent, say Claude is not connected rather than trying
another credential path.

Run Claude non-interactively and wait for it in the current turn:

```bash
claude -p --output-format text --model <model-or-alias> --effort <level> "<prompt>"
```

- Omit `--model` when the partner did not name one; Claude's configured default
  is the honest default.
- Effort values are `low`, `medium`, `high`, `xhigh`, and `max`. Omit the flag
  when there is no reason to override the default.
- Do not use `--background`: a Möbius helper must finish before this turn ends.
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
After Claude returns, assess its work yourself, run the relevant verification,
and tell the partner which part came from Claude. Claude's response is evidence
or a candidate change, not a substitute for your own review.
