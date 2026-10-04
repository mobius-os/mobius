# Transcript-row Host cutover tests

The transcript-row release raises the database compatibility floor to 1 on
its first boot. From then on no earlier image may run on that database. These
tests prove that a self-hosted owner reaches it safely. Never run them on
`/data/platform`, a production host, or any host with existing Möbius data.

## Prerequisite

The Host worker installed before this release (revision 2) restores the
previous image unconditionally when a replacement fails or is interrupted.
It must not perform the first level-0 to level-1 replacement, and a newer
worker offered by an image becomes ACTIVE only after a later successful
replacement. So the release advances `deployment/self-hosted-helper.required`
to 2:

- Older releases refuse it in Settings (`external_activation_required`).
- The owner updates the trusted host checkout and runs
  `sudo scripts/install-rebuild-helper.sh`. It seeds and verifies the
  checkout's floor-aware worker as ACTIVE (a pending or dropped candidate is
  never promoted; the high-water and candidate-fallback guards are unchanged),
  freezes `MOBIUS_HOST_RECOVERY_REQUIRED=1` and a read-only mount of
  `/var/lib/mobius-rebuild` at `/run/mobius-rebuild-host`, and then starts the
  waiting update through the running app's own reviewed updater
  (`scripts/finish-helper-update.py`).
- On every container boot the root entrypoint verifies the mounted ACTIVE
  worker's index and bytes and publishes a proof bound to `MOBIUS_BOOT_ID`.
  The storage gate converts only with that proof; otherwise legacy data stays
  authoritative and untouched.
- If the floor has risen when a replacement fails, the worker refuses to
  start the previous image and settles forward on the new one.

`deploy-prod.sh` does not carry the Host mount, so on a helper-installed host
the gate refuses its conversion (with a message naming the installer) and its
own floor-aware rollback restores the previous image. Managed deployments with
no Host controller use their own deployment and rollback contract.

## What runs where

- Any checkout: `scripts/wt-pytest.sh backend/tests/test_transcript_host_cutover.py
  backend/tests/test_transcript_host_prerequisite.py
  backend/tests/test_mobius_rebuild_launcher.py -q` runs the real gate and
  SQLite floor probe with Docker seams simulated. One test loads the exact
  historical revision-2 worker and is a **strict expected failure**: it
  documents the unsafe old behavior the prerequisite avoids, not a pass.
- Hosted pull-request checks: `scripts/test-upgrade-path.sh` boots the
  previous official image, requires Settings to refuse this release, seeds
  and verifies the real worker into a Host-state volume, runs the installer's
  bridge inside the old app, and boots the candidate with the read-only mount.
  A typed fixture chat must convert exactly. The worker's replacement loop and
  systemd are stood in for.
- Disposable systemd host:
  `sudo scripts/test-transcript-host-cutover.sh <previous-sha> <target-sha>`
  runs the real installers, units, worker and images. Each scenario deploys
  `<previous>` like an owner (typed fixture chat, bulk history, helper from
  that checkout), then:
  - `upgrade`: `<target>`'s installer finishes the waiting update; a later
    request for `<previous>` must roll back onto `<target>`.
  - `before`: the new container stops while converting; `<previous>` returns
    on exact legacy data, and a second attempt resumes and completes.
  - `after` / `interrupted`: the container stops, or the worker is killed,
    once the floor rose; the worker must settle forward without ever starting
    `<previous>`.
  Both images must be pullable under their official names. The manual
  `transcript_host_proof` input of `.github/workflows/test.yml` builds the
  commit on a hosted runner and serves both images from a registry on that
  runner, so the unmodified worker still pulls and checks their labels.

Record elapsed times. The worker waits 180 s for Docker health and 120 s for a
rollback or settle-forward to become ready; `deploy-prod.sh` allows 120 s;
Railway's health check allows 300 s. Do not enlarge them to hide a failure.

## Architecture

Official images are published for amd64 only and the installer refuses other
architectures, so there is no ARM64 Host path to prove. Supporting ARM64
needs multi-architecture publication and explicit installer and worker
support first.
