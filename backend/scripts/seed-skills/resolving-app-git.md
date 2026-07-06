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

Conflict resolution is LOCAL work. The repo may or may not have an `origin`
— a cloned catalog app does, a synthetic-upstream app doesn't
(`git remote get-url origin` tells you) — but either way, pushing is never
part of resolving a conflict. Sending an improvement upstream is a separate,
approval-gated flow: `contributing.md`.

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

**Finish by saving — the watcher does the rest.** Once every file is
marker-free, just save. The watcher recompiles and, because a merge is in
progress, its commit finalizes the merge for you. It deliberately refuses to
finalize while ANY tracked file still holds markers — the conflict can live in
a non-entry file (a job script, a sibling module), so resolve them all, not
just `index.jsx`. (An earlier watcher silently missed edits on the data volume
and stalled here routinely; that bug is fixed — a memory note saying
hand-finalizing is the norm predates the fix.)

**Confirm it took — one check is enough.** The merge is finalized when
`MERGE_HEAD` is gone and the tree is clean:

```bash
GIT_CEILING_DIRECTORIES=/data/apps git -C /data/apps/<slug> rev-parse -q --verify MERGE_HEAD; echo "merge_head_exit=$?"
GIT_CEILING_DIRECTORIES=/data/apps git -C /data/apps/<slug> status --porcelain
```

`merge_head_exit=1` (MERGE_HEAD absent) **and** empty status = done. If
`MERGE_HEAD` still resolves (exit 0), it hasn't finalized — almost always
leftover markers in SOME tracked file. Hunt across the whole repo, not just the
file you edited
(`GIT_CEILING_DIRECTORIES=/data/apps git -C /data/apps/<slug> grep -n '<<<<<<<'`),
fix, save again.

**Don't be alarmed the finalizing commit isn't a merge commit.** The watcher
finalizes as a *single-parent replay* — it parents the new commit directly on
the **upstream tip** and squashes the local side, so history stays linear
(`A → B → X`) instead of fanning into a 2-parent merge. So `git log -1
--pretty='%p'` prints exactly ONE short SHA (the upstream tip) and the subject
reads `agent edit`, not `Merge`. That single parent *being* the upstream tip is
what advances the base so the next update won't re-conflict — a 2-parent merge
here would mean something finalized it with a plain `git merge`, which is *not*
what the platform wants.

Optional eyeball that the app rebuilt: `stat -c '%y' /data/compiled/app-<id>.js`
should be fresh and the file a real bundle, not the compile-failed stub (`<id>`
from `curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/apps/"
| python3 -c 'import sys,json;[print(a["id"],a["slug"]) for a in json.load(sys.stdin)]'`).
A nice-to-have, not a gate.

If a merge is still in progress (`MERGE_HEAD` exists) or the recompile failed
(`/data/logs/chat.log` shows `compile failed`) after a clean re-save, the fix is
almost always leftover markers or a syntax error from the merge — open the file,
fix it, and **save again**: re-saving is the normal finalizer, because the
watcher runs the correct single-parent replay for you. Only reach for a manual
finalize if the watcher is genuinely wedged — and do NOT use `git commit
--no-edit` (with `MERGE_HEAD` set that makes exactly the 2-parent merge the
platform avoids). Replicate the single-parent replay instead:

```bash
cd /data/apps/<slug>
export GIT_CEILING_DIRECTORIES=/data/apps
git grep -n '<<<<<<<' && { echo "markers remain — fix first"; exit 1; }
tree=$(git write-tree)
sha=$(git commit-tree "$tree" -p "$(cat .git/MERGE_HEAD)" -m 'agent edit')
git update-ref refs/heads/main "$sha"
rm -f .git/MERGE_HEAD .git/MERGE_MSG .git/MERGE_MODE
```

## Backing out (it's always reversible)

You never have to force a resolution you're unsure about.

- **Mid-resolution, want out:** `GIT_CEILING_DIRECTORIES=/data/apps git -C
  /data/apps/<slug> merge --abort` restores the pre-update version. The app keeps
  serving what it served before; nothing is lost. The new version is still
  recorded on `upstream`, so the partner can retry later.
- **Already finalized and it's wrong:** the finalize is a single-parent replay
  (not a merge commit), so undo it with a plain `git revert <replay-sha>`
  (reversible, keeps history — do NOT use `git revert -m 1`, which errors on a
  non-merge commit) or `git reset --hard <pre-replay-sha>` from the reflog
  (erases the attempt; the pre-replay tip is unreachable after the squash but
  the reflog still has it). Save/touch the source so the watcher recompiles the
  reverted version. `upstream` is untouched, so a retry still works.

## Don't

- Don't `git push` while resolving — whether or not this repo has an
  `origin`, pushing upstream is a separate, approval-gated flow
  (`contributing.md`), never part of conflict resolution.
- Don't commit conflict markers (a failed recompile is the tell — fix and save).
- Don't edit the `upstream` branch.

When done, leave a one-line note in the chat of what you merged and any
alternative you set aside, so the partner knows what changed.
