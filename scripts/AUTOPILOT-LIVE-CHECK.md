# Autopilot live check — runbook

`scripts/autopilot-live-check.sh` is the **Stage-2** smoke test for the Contribute
autopilot loop: it proves the parts the automated tests stub out — a **real git
push updating a real PR**, and **`job.sh` detecting a real review** — against a
running Docker stack + a scratch GitHub account. It plays the follow-up agent
*mechanically* (no reasoning, no agent tokens), and it **cleans up after itself**,
so you can re-run it any time.

The automated Stage-1 test (`backend/tests/test_autopilot_loop.py`) already proves
the endpoint wiring + state machine with stubs on every push; this is the live
counterpart. Stage-3 (a real agent driving the round from the skill) is separate
and manual — see the plan.

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
4. **`run-job`** → a dedicated `Autopilot: …` chat is created (detection →
   `/respond` → claim → spawn).
5. **Mechanical round**: `/respond` (gets `run_id`) → a real follow-up commit +
   `/update` → **the PR head advances on GitHub** → `/reply` → `/complete`.
6. **Re-run `run-job`** → the record stays `idle` (the agent's own reply is
   filtered — no self-re-trigger).

Any failed step aborts with a red `✗` and the offending HTTP body.

## First-run caveat

This harness has not yet had a live shakeout run (no scratch stack was available
when it was written). Treat the **first** run as validation of the script itself:
the API contracts and git steps match the code, but environment specifics — the
exact storage PUT body shape, `register_app` id, container paths — may need a
small tweak on first contact. Run it with a throwaway PR and expect to iterate
once; after that it's a repeatable one-command check.

## Troubleshooting

- **`/respond` returns `deferred`** — the weekly-allowance budget is exhausted or
  `percent` is 0. Raise it in Settings (or `agent-settings.json`) and re-run.
- **No `Autopilot: …` chat in step 4** — detection still works if step 5 drives
  the round manually; the chat only appears when a provider is configured to run
  the spawned turn. For a pure mechanical check this is informational.
- **`PR head on GitHub is … expected …`** — the push didn't land; check the app
  container's `gh` auth (`/data/cli-auth/gh`) and that the fork remote exists.
