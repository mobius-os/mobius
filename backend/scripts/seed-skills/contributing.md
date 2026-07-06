# Contributing upstream

How to send an improvement back to the Möbius ecosystem: checking the GitHub
connection, searching for existing work before building, what may leave this
instance (and what never does), the partner-approval gate, and the exact `gh`
sequences for PRs, issues, and comments. `Read` this before ANY public GitHub
action — fork, push, PR, issue, comment. The end-of-task checklist in the
constitution routes you here when a change you just made would help other
Möbius users; a partner saying "share this" or "report that bug upstream"
lands here too.

---

## Check you're set up

```bash
curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/github/status" | python3 -m json.tool
```

Use the `$API_BASE_URL` + `$AGENT_TOKEN` idiom for every chat-context command
in this file — they're in every agent turn; never hardcode localhost. The
payload tells you everything you need:

- `connected: true` with a `login` — the owner has connected GitHub, and `gh`
  is already authenticated as them. You never see or handle the token itself;
  `gh` resolves it from the platform's credential store. Don't go digging for
  it, and never print it. Note the credential is wired GLOBALLY: once connected,
  ANY `git push` to a github.com remote (including a stray push in a normal
  platform-edit turn) authenticates as the owner. Nothing at the git layer gates
  that — the approval gate below is the whole safety net. So NEVER run a bare
  `git push` to a github remote outside the approved fork flow.
- `connected: false` — point the partner to the **Contribute app** (install
  it from the App Store if they don't have it) and its Connect GitHub card.
  You can still prepare a contribution (branch, commit, record it as
  `prepared` in the ledger below); nothing goes public until they connect
  AND approve.
- `gh_version: null` — the platform image predates GitHub support. Tell the
  partner a platform update is needed; don't improvise around it.

---

## Search before building

Before writing a new app or a fix from scratch, spend a minute checking
whether the ecosystem already has it:

```bash
gh search issues --owner mobius-os "<a few words describing the problem>"
gh search prs --owner mobius-os "<the same words>"
curl -s https://raw.githubusercontent.com/mobius-os/app-store/main/catalog.json | python3 -m json.tool
```

The catalog lists installable apps with their manifests — if one already does
what the partner wants, installing it beats rebuilding it. If a matching
issue, PR, or discussion exists, tell the partner and offer to add their
experience as a comment instead of duplicating the work. Empty results are
normal early — the ecosystem is young; silence means "nothing found", not
"search is broken".

Searching is read-only and needs no approval. Everything from the fork
onward does.

---

## What may leave this instance — the privacy allowlist

**Contributable: source code only.** Source diffs of apps
(`/data/apps/<slug>/` — the code, not the data), the platform
(`/data/platform/`), and the shell. That is the whole allowlist.

**Never contributable, no exceptions:** anything under
`/data/shared/memory/`, app storage (`/data/apps/<int-id>/` — the numeric-id
dirs are runtime data, not source), the database, logs, `/data/cli-auth/`,
chat content, and anything personal — names, schedules, health data,
locations, habits, the partner's writing. Commit messages, branch names, and
PR bodies can leak too: keep them generic ("fix empty-state crash", not "fix
crash when <partner's name>'s workout log is empty").

**Re-read the FULL diff before proposing anything public.** Not the file
list — every changed line (in an installed app's repo,
`git diff upstream...HEAD` shows everything local; in a `/tmp` clone, diff
against `origin/main`). Local commits routinely carry partner data into
source files: a seeded example, a hardcoded name, a test fixture with real
entries. If anything personal is in the diff, strip it first or don't
propose.

---

## The approval gate — nothing public without a yes

**You NEVER fork, push, comment, or open anything public without the
partner's explicit yes for THAT action, in this conversation.** Not a
standing preference, not "they approved a PR last week", not an inferred
"they'd probably want this" — a yes, for this specific action, now. Ask in
plain words ("Want me to open this as a draft PR on <repo>?") and wait. If
the answer is anything but yes, stop: prepared-but-unapproved work is
recorded as `status: "prepared"` in the ledger (below) and waits — it costs
nothing to leave it there.

PRs open as **drafts** by default; the partner decides when one is ready for
review.

---

## The command sequences

### An app with a real origin

Most catalog apps are real clones — `git -C /data/apps/<slug> remote get-url
origin` succeeds. Work in the app's own repo:

```bash
cd /data/apps/<slug>                      # the app's own repo; main is checked out
git checkout -b fix/<slug>-<short>        # branch from main
# Squash the watcher's incremental commits into ONE clean commit for the PR —
# reviewable upstream, and it carries the Möbius co-author mark:
git reset --soft "$(git merge-base HEAD upstream)"
git commit -m "<one line, generic>" \
  -m "Co-Authored-By: Möbius Agent <mobius-agent@users.noreply.github.com>"
gh repo fork --remote --remote-name fork  # inside the clone; idempotent
git push fork HEAD   # forks are created async: on failure wait 2s, retry (3x)
gh pr create -R <upstream-owner>/<repo> -H <login>:fix/<slug>-<short> --draft \
  --title "<one line, generic>" \
  --body "<what / why / how you tested it>

Prepared by a Möbius agent with owner review."

git checkout main    # INVARIANT — see below
```

`<login>` is the owner's GitHub login from the status payload. The
`Co-Authored-By: Möbius Agent` trailer goes on every contributed commit — it
is how a commit visibly carries the Möbius mark on GitHub (the partner stays
the author; Möbius appears as co-author).

**`git checkout main` before the turn ends is an invariant, not a
courtesy.** The watcher auto-commits partner edits onto whatever branch is
checked out, and store updates merge into `main` — leave the app dir on
`fix/…` and the partner's next edit lands on the wrong branch while the next
update conflicts. The `fix/` branch itself stays around for PR follow-ups;
only the checkout returns to `main`.

### An app without an origin

Some apps were installed from a manifest with no clone — no `origin`. Derive
the repo from the app's `manifest_url`: a manifest at
`https://raw.githubusercontent.com/<org>/<repo>/<ref>/mobius.json` means the
source lives at `https://github.com/<org>/<repo>`. Clone to `/tmp`, apply the
local source diff there, then the same fork/push/PR steps from the `/tmp`
clone:

```bash
git clone https://github.com/<org>/<repo> /tmp/<repo> && cd /tmp/<repo>
git checkout -b fix/<slug>-<short>
# copy the changed source files over (or git apply a diff you generated),
# re-read the result against the privacy allowlist, then commit:
git commit -am "<generic message>" \
  -m "Co-Authored-By: Möbius Agent <mobius-agent@users.noreply.github.com>"
# then, from /tmp/<repo>: gh repo fork --remote --remote-name fork;
# git push fork HEAD (same 2s-retry rule); gh pr create ... --draft
```

The live app dir never leaves `main` on this path — you never branched it.

### Platform / shell

Only when `/data/platform` has a real origin — `git -C /data/platform remote
get-url origin` succeeds. Then it's the same sequence from a branch there:
branch off `main`, fork, push, draft PR against `mobius-os/mobius`, and the
same back-to-`main` invariant before the turn ends. If there is no origin,
be honest with the partner: platform contributions need the updated platform
bootstrap; app contributions still work.

### Commenting on an issue or discussion

Only after partner approval, and **quote the exact text to the partner
first** — a comment publishes their words under their name.

```bash
gh issue comment <issue-url> --body "<the approved text>"
```

Discussions go through GraphQL (`gh` has no discussion-comment subcommand):

```bash
gh api graphql \
  -f query='mutation($id: ID!, $body: String!) {
    addDiscussionComment(input: {discussionId: $id, body: $body}) {
      comment { url } } }' \
  -F id=<discussion-node-id> -F body="<the approved text>"
```

---

## When something fails

- **403 mentioning "OAuth App access restrictions"** — the org hasn't
  approved the Möbius OAuth app. Suggest the partner reconnect with a
  classic PAT instead (the Contribute app's Connect card has that option). A
  **`public_repo`-scoped** PAT is enough for contributing and is the safer
  choice — a full `repo` token also grants read of the owner's PRIVATE repos
  through the read passthrough, which upstream contribution never needs.
- **`gh: command not found`** — the platform image is too old; a platform
  update is needed.
- **`git push fork` fails right after the fork** — forks are created
  asynchronously. Wait 2s and retry, up to 3 times, before treating it as a
  real failure.
- **Empty search results** — normal while the ecosystem is young; not an
  error.

---

## The ledger

The Contribute app tracks every contribution so the partner can see status
at a glance. Find its id (slug `contribute`):

```bash
curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/apps/" \
  | python3 -c 'import sys,json;[print(a["id"]) for a in json.load(sys.stdin) if a.get("slug")=="contribute"]'
```

If it's installed, write one JSON per contribution — on prepare, on submit,
and on any status change:

```bash
curl -s -X PUT "$API_BASE_URL/api/storage/apps/<id>/contributions/<record-id>.json" \
  -H "Authorization: Bearer $AGENT_TOKEN" -H "Content-Type: application/json" \
  -d '{
    "id": "<record-id>",
    "type": "pr",
    "repo": "<owner>/<repo>",
    "number": 12,
    "url": "https://github.com/<owner>/<repo>/pull/12",
    "title": "<the PR/issue title>",
    "status": "draft",
    "branch": "fix/<slug>-<short>",
    "chat_id": "'"$CHAT_ID"'",
    "created_at": "<ISO timestamp>",
    "updated_at": "<ISO timestamp>",
    "summary": "<one line for the partner>"
  }'
```

`type` is one of `pr | issue | issue_comment | discussion_comment`; `status`
is one of `prepared | draft | open | merged | closed | abandoned`; `number`,
`url`, and `branch` are optional until they exist (a `prepared` record has
no URL yet). If the app isn't installed, suggest installing it from the App
Store for tracking — and proceed without it; the ledger is bookkeeping, not
a gate.

---

## After submitting

Tell the partner what was opened and give them the URL, in partner-facing
language: "I opened the fix as a draft on the notes app's project page —
here's the link" beats a recitation of branches and remotes. If they want
changes, the `fix/` branch is still there — push follow-up commits to the
fork and the PR updates itself.
