# Workflows app

When a turn leaned on background helpers, the Workflows app is where the partner watches what those helpers did. `Read` this before ending any turn in which you ran background work, so you leave the partner a way to look in.

This skill is only about *surfacing* that work — it does NOT teach you how to run it. The Claude CLI ships the guidance for orchestrating helpers, and arming an orchestrated run is the highest effort tier. Your job here is the after-the-fact link, not the run.

<!-- This file is Read on every background-work turn, so it deliberately avoids
     the literal top-effort-tier keyword: that word anywhere in the turn's
     context can arm the orchestration tool on its own. Describe the tier;
     never name it. -->


---

## When this applies

Any turn where you fanned work out to background helpers or an orchestrated run — the Task/Agent tools, an orchestrated Workflow fleet, or a Codex collab (spawn/send/resume). A plain turn with ordinary tool calls does not count; leave the Workflows link off those.

---

## Before you hand back

1. **Find the app.** It may not be installed — if this comes back empty, skip the rest **silently**. No install prompt, no apology; the link just isn't offered this turn.

   ```bash
   WF_ID=$(curl -s -H "Authorization: Bearer $AGENT_TOKEN" \
     "$API_BASE_URL/api/apps/" \
     | python3 -c 'import sys,json; print(next((a["id"] for a in json.load(sys.stdin) if a.get("slug")=="workflows"), ""))')
   ```

2. **Nudge it to refresh (best-effort).** The app self-refreshes when the partner opens it, so this only warms it early — ignore any non-2xx, and never block the reply on it.

   ```bash
   [ -n "$WF_ID" ] && curl -s -X POST -H "Authorization: Bearer $AGENT_TOKEN" \
     "$API_BASE_URL/api/apps/$WF_ID/run-job" >/dev/null
   ```

3. **Leave the link.** End your reply with a plain-language pointer carrying the resolved id:

   ```markdown
   [See how the helpers did](/shell/?app=WF_ID_HERE)
   ```

   Write the real number in place of `WF_ID_HERE` — an unexpanded `$WF_ID` in the markdown matches no app and dead-links.

---

## Register

Stay in partner language: "helpers", "background work" — never subagents, Task tools, SDK, or workflow-engine vocabulary. The link is the durable artifact; the refresh is a courtesy. If you can only do one, do the link — the app refreshes itself on open.
