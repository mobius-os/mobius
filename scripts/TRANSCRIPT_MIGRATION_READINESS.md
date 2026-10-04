# Private migration timing rehearsal

Run from the reviewed candidate checkout, with its locked Python dependencies:

```sh
python scripts/test-transcript-migration.py /private/legacy-transcript-copy.db /private/new-proof-dir
```

Input must be a disposable legacy database copy with `chats.messages`, never a
serving instance. The tool refuses the inherited serving database and its
directory, including symlink aliases. It opens the input read-only and keeps
backup and verification on one pinned snapshot, so a concurrent change cannot
silently make them compare different inputs. The run directory must not already exist; prior
proofs are never overwritten. Keep the copy, generated database and report
private. They are not source and must not be committed or uploaded to CI.

The tool runs the actual level-1 application gate, independently verifies
message values, JSON types, positions, counts and byte-exact archives, and
writes aggregate evidence in `result.json`. Corrupt legacy units must retain
their exact archive and match the deliberate recovery placeholder. Optional
`--post-tasks` also runs bounded background tasks and repeats verification.
This background cost is reported separately, not charged to initial readiness.

Exit 0 means the verified **application gate** fits the supplied budget; exit 3
means data verification passed but timing exceeded it. Neither result proves
container startup, live `/api/ready`, image compatibility, real Host cutover or
rollback. The image capability is deliberately simulated in this host test.
Any other failure is a test/setup failure, not a timing verdict.

The default measurement budget is 120 seconds. An explicit measurement budget
can be supplied with `--readiness-budget-seconds`; this does not configure or
relax any runtime deadline. `deploy-prod.sh` has distinct 120-second preflight
and cutover defaults; the Host worker's healthy wait is 180 seconds, with a
120-second rollback readiness wait. Allow for container/app initialization and
other startup work in addition to the measured gate. One successful warm run
is not a worst-case readiness guarantee.

Size the disposable disk for the original copy plus normalized data, archives
and WAL headroom. The gate's own free-space refusal remains enabled. A failure
leaves its new proof directory intact for inspection; never automatically remove
an existing proof or source copy to get a pass.

See `TRANSCRIPT_HOST_TEST.md` for the real disposable-host cutover test.
