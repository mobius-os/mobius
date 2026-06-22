# Recent chats

A fixed-size queue of the last chats — at most 10 entries, oldest first,
one line each:

`- [chat:<id>] <YYYY-MM-DD> — <1-2 sentence summary>`

The nightly Reflection pass maintains it: appends the day's chats from its
interviews and evicts the oldest beyond 10. Don't grow it by hand during
the day. The summaries are usually enough to recall what recently
happened; when a specific exchange matters, fetch the full transcript
with `GET /api/chats/<id>`.

*(no chats recorded yet — the first nightly pass fills this in)*
