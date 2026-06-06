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

## Resolve it

Everything is LOCAL — this repo has no `origin`, no GitHub. Never `git push`.

```bash
cd /data/apps/<slug>
# /data is ITSELF a git repo, so pin the ceiling or git may walk up to it and
# report the wrong repo's state. Do this before any git command here.
export GIT_CEILING_DIRECTORIES=/data/apps

git status        # shows "Unmerged paths" — the files to resolve
git diff          # shows the conflict hunks
```

A conflict hunk looks like this (shown indented; in the real file the marker
lines sit at column 0):

    <<<<<<< HEAD
      const title = 'the local version'
    =======
      const title = 'the upstream version'
    >>>>>>> upstream

Edit each conflicted file (usually `index.jsx`, sometimes sibling modules):
keep what's right — often a genuine merge of both sides, not just one — and
**delete the `<<<<<<<`, `=======`, `>>>>>>>` lines**. Re-read the surrounding
code so the result is coherent, not just marker-free.

**Finishing the merge — the easy path:** once a file is marker-free, just save
it. The watcher recompiles on save and, because a merge is in progress, the
recompile's commit finalizes it as a proper merge commit — the update is now
applied and the base advances so the *next* update merges cleanly. Confirm:

```bash
git status                              # clean, no "unmerged paths", no MERGE_HEAD
stat -c '%y' /data/compiled/app-<id>.js # timestamp should be fresh (just recompiled)
```

(Find `<id>` with `curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/apps/" | python3 -m json.tool`.)

If `git status` still shows `MERGE_HEAD` after a moment (or the recompile
failed — check `/data/logs/chat.log` for `compile failed`), finalize by hand:

```bash
git add -A
git -c user.name=Mobius -c user.email=mobius@localhost commit --no-edit
```

A failed recompile almost always means leftover markers or a real syntax error
from the merge — open the file, fix it, save again.

## Backing out (it's always reversible)

You never have to force a resolution you're unsure about.

- **Mid-resolution, want out:** restore the pre-update version and walk away —
  ```bash
  git merge --abort
  ```
  The app keeps serving what it served before; nothing is lost. The new version
  is still recorded on `upstream`, so the partner can retry the update later.

- **Already finalized and it's wrong:** undo the merge commit —
  ```bash
  git revert -m 1 <merge-commit-sha>     # preferred — reversible, keeps history
  # or, to erase the attempt entirely:
  git reset --hard <pre-merge-sha>
  ```
  Either way, save/touch the source so the watcher recompiles the reverted
  version. `upstream` is untouched, so a future update can try again.

## Don't

- Don't `git push` — there's no remote; this is the partner's local instance.
- Don't commit conflict markers (a failed recompile is the tell — fix and save).
- Don't edit the `upstream` branch.

When done, leave a one-line note of what you merged and why in the chat so the
partner knows what changed.
