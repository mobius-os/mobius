---
title: Memory — Home
type: moc
---
# Memory

This is your **Home** map — the root of your knowledge graph at
`/data/shared/memory/`, surfaced as the **Memory** app. It is injected at the start
of each session. Everything below is reachable from here; follow a wiki-link by
`Read`-ing `notes/<slug>.md` or `mocs/<slug>.md` when you need the detail.

You record what is **useful for the future and specific to this user/instance** —
durable facts about the partner (preferences, interests, personality), and
hard-won bugs you hit *here* — not everything, and not generic app/platform
how-to (that's a **skill** now, under `/data/shared/skills/`). See
[[how-the-memory-graph-works]] and the Memory skill (`/data/shared/skills/memory.md`)
for the rules.

This graph starts almost empty by design — a scaffold of maps with no facts yet.
It **grows through use**, and the nightly **Reflection** pass curates it.

## Maps

- [[about-the-user]] — who the user is: preferences, interests, personality,
  how they want you to work. *The primary map — start here when a chat hints at
  a durable preference, and grow it first.*
- [[building-mobius-apps]] — app facts specific to this user/instance (general
  app-building technique lives in skills, not here).
- [[mobius-platform]] — operational facts specific to this deployment (general
  platform how-to lives in skills, not here).
- [[maintaining-memory]] — how this graph works and how to grow it.

## Notes

- [[this-instance-is-fresh]] — you know nothing durable about this partner yet;
  weight observation extra in early chats.
- [[memory-is-visible-to-the-partner]] — the Memory app shows every note to the
  partner; write as if you'd stand behind it when quoted back.
- [[a-nightly-reflection-pass-exists]] — the overnight pass consolidates the
  chat notes into the graph and fixes apps; defer heavy reorganizing to it.

## Recent chats

Each chat keeps its own note (`chats/<id>/index.md`: a growing summary + the
facts it surfaced). The ~10 most-recently-touched are injected at session start,
so recent context is already in front of you; fetch a full transcript with
`GET /api/chats/<id>` when a specific exchange matters. There is no separate
recent-chats queue and no inbox — the chat note IS the daytime capture surface.
