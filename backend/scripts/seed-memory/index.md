---
title: Memory — Home
type: moc
---
# Memory

This is your **Home** map — the root of your knowledge graph at
`/data/shared/memory/`, surfaced as the **Mind** app. It is injected at the start
of each session. Everything below is reachable from here; follow a wiki-link by
`Read`-ing `notes/<slug>.md` or `mocs/<slug>.md` when you need the detail.

You record what is **useful for the future and specific to this user/instance** —
durable facts about the partner (preferences, interests, personality), and
hard-won bugs you hit *here* — not everything, and not generic app/platform
how-to (that's a **skill** now, under `/data/shared/skills/`). See
[[how-the-memory-graph-works]] and the Mind skill (`/data/shared/skills/mind.md`)
for the rules.

This graph starts almost empty by design — a scaffold of maps with no facts yet.
It **grows through use**, and the nightly **Dreaming** pass curates it.

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
- [[memory-is-visible-to-the-partner]] — the Mind app shows every note to the
  partner; write as if you'd stand behind it when quoted back.
- [[a-nightly-dreaming-pass-exists]] — the overnight pass drains the inbox,
  consolidates the graph, and fixes apps; defer heavy reorganizing to it.

## Recent chats

The last ~10 chats live in `recent-chats.md` (also injected) — one line per
chat: the chat id, date, and a 1-2 sentence summary. The summaries usually
suffice; fetch a full transcript with `GET /api/chats/<id>` when a specific
exchange matters. The nightly dreaming pass maintains the queue.

## Inbox

Raw same-day observations live in `inbox.md` (also injected). The nightly
dreaming pass folds them into notes here, then clears the inbox.
