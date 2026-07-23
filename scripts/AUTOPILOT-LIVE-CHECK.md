# Autopilot live check — runbook

`scripts/autopilot-live-check.sh` is the **live** smoke test for the Contribute
autopilot loop, against a running Docker stack + scratch GitHub accounts. It
seeds a fresh contribution, opens a real PR, posts a review, then triggers
`job.sh` and **observes the real review-followup agent** detect the review, push
a fix to the PR, reply, and complete the round — verifying the parts the
automated tests stub out (real git push to a real PR, real review detection, the
self-activity filter). It **cleans up after itself**, so it's re-runnable any
time.

> **Note on providers.** When a chat provider is authenticated in the instance
> (e.g. `/data/cli-auth/claude`), `job.sh` → `/respond` spawns a **real agent**
> that drives the round — so this script *observes* that agent (it does not, and
> must not, mechanically drive `/update` itself, which would race the agent).
> Each run therefore also spends some agent budget. On an instance with **no**
> provider authed, the round can't complete on its own; use the automated
> `backend/tests/test_autopilot_loop.py` for the stubbed, token-free path.

The automated Stage-1 test (`backend/tests/test_autopilot_loop.py`) proves the
endpoint wiring + state machine with stubs on every push; this is the live
counterpart that also exercises real GitHub and a real agent.

## Prerequisites (one-time)

1. **Build + run the test stack from the feature backend.**
   ```bash
   cd mobius
   git checkout feat/contribute-autopilot
   DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose -f docker-compose.test.yml up -d --build
   ```
   Note the container name (default `mobius-test`) and the port (`TEST_PORT`,
   default `8001`).

2. **Install the Contribute app from the `feat/autopilot` working tree** (so it
   ships the new `job.sh`, `autopilot.js`, `review-followup.md`). Use
   `register_app.py` against the running stack, then note the app's numeric id.

3. **Connect the scratch GitHub account** in the Contribute app's Connect card
   (or via the device flow), and mint an **app-scoped token** with
   `github_access` for the Contribute app.

4. **Scratch repo + reviewer account.** Have a throwaway upstream repo
   (`owner/name`) the scratch account can fork/PR against, and a **second**
   GitHub account with a classic PAT (`public_repo`) to post the review.

## Run

```bash
export API_BASE=http://localhost:8001
export APP_ID=<contribute app id>
export APP_TOKEN=<app-scoped token, github_access>
export CONTAINER=mobius-test
export UPSTREAM_REPO=<owner>/<scratch-repo>
export REVIEWER_TOKEN=<PAT for the reviewer account>

./scripts/autopilot-live-check.sh
```

`KEEP=1 ./scripts/autopilot-live-check.sh` leaves the PR + record for inspection
(otherwise everything it created is removed on exit).

## What it asserts

1. **Seed** a fresh reviewable contribution (unique branch `autopilot-check/<ts>`).
2. **Send** → a real PR opens and the grant mirror appears on the record.
3. Reviewer posts **changes requested**.
4. **`run-job`** → job.sh detects the review, `/respond` claims a round
   (`state=responding`) and spawns the agent.
5. **Observe** the round complete: `state` returns to `idle` with
   `rounds_used ≥ 1`, the **PR advances to ≥ 2 commits** on GitHub, and the
   follow-up commit carries the `Co-authored-by: Möbius Agent` trailer.
6. **Re-run `run-job`** → the record stays `idle` (the agent's own reply is
   filtered — no self-re-trigger).

Any failed step aborts with a red `✗` and the offending HTTP body.

## Topology

The autopilot flow contributes via a **fork**, so the upstream repo must be owned
by a **different** account than the one connecting. Use the **reviewer** account
(scratch #2) as the upstream owner and connect as scratch #1; scratch #1 forks it
and opens the PR, and scratch #2 (the repo owner) reviews. `UPSTREAM_REPO` should
therefore be `scratch2/<repo>`, not the connected account's own repo (you can't
fork your own repo).

## Status: validated

This harness had its first successful live run on 2026-07-23 against
`mobius-scratch2/autopilot-upstream`: job.sh detected the review, a real agent
reworded the file, pushed a second commit (with the co-author trailer), replied
on the PR, completed the round, and the re-run did not re-trigger. The merge path
fired the 🎉 notification and closed out the DB row. Budget accrued (~79k tokens,
`chat_runs.cost_usd` recorded).

## Troubleshooting

- **Round claims but never completes** — no provider is authed, so the spawned
  turn can't run. Authenticate a provider, or use the automated
  `test_autopilot_loop.py` for the token-free path.
- **`/respond`/job.sh defers** — the weekly-allowance budget is exhausted or
  `autopilot_budget.percent` is 0. Raise it in Settings and re-run.
- **Submit 409 "no longer waiting for approval"** — the ledger record wasn't
  written raw; the storage PUT body must be the record JSON itself (no envelope).
- **git "dubious ownership"** — the staging worktree must be owned by `mobius`
  (the user the backend runs git as); the script creates it accordingly.
- **PR didn't advance** — check the app container's `gh` auth
  (`/data/cli-auth/gh`) and that the owner's fork of the upstream exists.
