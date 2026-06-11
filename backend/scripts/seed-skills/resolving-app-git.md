# Resolving an app update conflict

When the partner updates an installed app and the new version touches the same
lines as local edits, the update can't apply cleanly — exactly like a `git pull`
that conflicts. Möbius doesn't paper over it: it leaves a **real merge conflict**
in the app's source and opens a chat (this one) so you resolve it the normal way.
`Read` this before resolving one.

## How app updates work (so the conflict makes sense)

Each installed app is its own git repo at `/data/apps/<slug>/.git` with two
branches:

- **`upstream`** — the pristine bytes of each installed version (the installer
  commits here; you never touch it).
- **`main`** — the working branch with the local edits you and the partner have
  made. This is what's checked out.

An update records the new version on `upstream` and merges it into `main`. A
clean merge just applies. A conflict leaves `main`'s working tree mid-merge:
conflict markers in the files + a `MERGE_HEAD`. **The app keeps serving its
previous (working) version the whole time** — the marker-bearing source won't
compile, so the file watcher holds the last good bundle. Nothing is broken for
the partner while you work; you're just finishing the merge.

## Look at the conflict

Everything is LOCAL — this repo has no `origin`, no GitHub. Never `git push`.

Each app has its own `.git`, so git normally stays scoped to it. As cheap
insurance against a missing/corrupt app repo silently committing into `/data`
(which is *itself* a git repo), pin `GIT_CEILING_DIRECTORIES=/data/apps`. Shell
state does NOT persist between separate commands here, so set it **inline on
every git command** — the self-contained form below works in any single call:

```bash
GIT_CEILING_DIRECTORIES=/data/apps git -C /data/apps/<slug> status   # "Unmerged paths"
GIT_CEILING_DIRECTORIES=/data/apps git -C /data/apps/<slug> diff      # the conflict hunks
GIT_CEILING_DIRECTORIES=/data/apps git -C /data/apps/<slug> log --oneline -5
```

A conflict hunk looks like this (shown indented; in the real file the marker
lines sit at column 0):

    <<<<<<< HEAD
      const title = 'the local version'
    =======
      const title = 'the upstream version'
    >>>>>>> upstream

## Decide what to keep — read intent first, don't reflexively blend

**First classify the two sides: ADDITIVE or MUTUALLY EXCLUSIVE?** Additive means
both can coexist (a new feature + a customization) → layer both. Mutually
exclusive means picking one discards the other (two different titles, two colors)
→ choose one. This classification, not a reflex, decides everything below.

`git log` (above) tells you the partner's INTENT: if the local side is a
deliberate commit (e.g. `local: rename title + green accent`), they *meant* it.
Then resolve by the type you classified:

- **Cosmetic / either-or** (a title, a color, a copy string — you can't blend two
  titles): keep the partner's deliberate local choice and **surface the upstream
  alternative in chat** ("the update wanted X; I kept your Y — say the word and
  I'll switch"). Don't invent a blend.
- **Functional / additive** (logic, a new feature, added lines): **layer both** —
  keep the partner's customization AND fold in upstream's new behavior. When you
  splice a block from one side into the host file, reconcile hook/import naming so
  the result is consistent (e.g. don't leave a pasted `React.useEffect` next to
  the file's bare `useEffect` — match whichever the host already imports), or the
  recompile fails on an undefined reference.
- **Can't read the intent, or it's risky:** `git merge --abort` (below) and ask
  the partner rather than guessing.

## Resolve + finish

Edit each conflicted file (usually `index.jsx`, sometimes sibling modules) to the
result you decided on, **deleting the `<<<<<<<`, `=======`, `>>>>>>>` lines**.
Re-read the surrounding code so the result is coherent, not just marker-free.

**Save, then VERIFY — the auto-finalize watcher is unreliable, so never assume
it fired.** Once a file is marker-free, save it. The watcher *may* recompile and,
because a merge is in progress, finalize the merge for you — but in practice it
often stalls and doesn't, so the manual fallback below is the normal case, not
an edge case. After saving, always check that all three landed: `MERGE_HEAD` is
gone, the finalizing commit is a 2-parent merge, and a fresh non-stub bundle was
compiled. If any didn't, run the manual finalize + manual recompile yourself.

**Confirm it took.** The single fact that proves the merge finalized is
that the finalizing commit is a **2-parent merge** (that's what advances the
base so the *next* update won't re-conflict):

```bash
GIT_CEILING_DIRECTORIES=/data/apps git -C /data/apps/<slug> log -1 --pretty='%p'
```

TWO short SHAs printed = merge finalized, `MERGE_HEAD` gone, base advanced. ONE
SHA means the watcher didn't finalize — go finish by hand (below). (When it did
finalize, the commit subject reads `agent edit`, not `Merge` — that's the
watcher; expected, don't let it fool you.)

Then confirm the app actually rebuilt — this is a gate, not a nice-to-have,
because a stalled watcher leaves the previous bundle in place: `stat -c '%y'
/data/compiled/app-<id>.js` must be FRESH and the file must be a real bundle, not
the compile-failed stub (`<id>` from `curl -s -H "Authorization: Bearer
$AGENT_TOKEN" "$API_BASE_URL/api/apps/" | python3 -c 'import sys,json;[print(a["id"],a["slug"]) for a in json.load(sys.stdin)]'`).
If it's stale, recompile by hand (the manual finalize below re-touches the source
so the watcher recompiles; if it still doesn't, save/touch the file again).

When the watcher didn't finalize (a merge still in progress — `.git/MERGE_HEAD`
exists) or the recompile failed (`/data/logs/chat.log` shows `compile failed`),
finish by hand. Expect to do this regularly:

```bash
GIT_CEILING_DIRECTORIES=/data/apps git -C /data/apps/<slug> add -A
GIT_CEILING_DIRECTORIES=/data/apps git -C /data/apps/<slug> \
  -c user.name=Mobius -c user.email=mobius@localhost commit --no-edit
```

A failed recompile almost always means leftover markers or a syntax error from
the merge — open the file, fix it, save again.

## Backing out (it's always reversible)

You never have to force a resolution you're unsure about.

- **Mid-resolution, want out:** `GIT_CEILING_DIRECTORIES=/data/apps git -C
  /data/apps/<slug> merge --abort` restores the pre-update version. The app keeps
  serving what it served before; nothing is lost. The new version is still
  recorded on `upstream`, so the partner can retry later.
- **Already finalized and it's wrong:** undo the merge commit — `git revert -m 1
  <merge-sha>` (reversible, keeps history) or `git reset --hard <pre-merge-sha>`
  (erases the attempt). Save/touch the source so the watcher recompiles the
  reverted version. `upstream` is untouched, so a retry still works.

## Don't

- Don't `git push` — there's no remote; this is the partner's local instance.
- Don't commit conflict markers (a failed recompile is the tell — fix and save).
- Don't edit the `upstream` branch.

When done, leave a one-line note in the chat of what you merged and any
alternative you set aside, so the partner knows what changed.
