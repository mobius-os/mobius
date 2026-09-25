#!/usr/bin/env bash
# Summarize why GitHub Actions failed, without pulling whole logs into context.
#
#   scripts/ci-failures.sh <owner/repo> <pr-number|run-id>
#
# A PR number resolves to the PR's latest head commit and every failed run on
# it; a run id (anything longer than 7 digits) names one run. For each failed
# job this prints the failed step names and bounded failure lines: pytest
# FAILED/ERROR lines with their E-line excerpts, node:test failure blocks
# (✖ ... or not ok ...), and Error: lines. When a failed step has none of
# those, it prints that step's last lines instead. The complete failed-job
# logs are saved to a temp file whose path is printed before the first job
# and again at the end.
#
# Read-only: uses gh pr view, gh run list, and gh run view only. The repository
# is required so a PR number can never resolve against the wrong project.

set -uo pipefail

TAIL_LINES=40      # fallback lines per failed step
MAX_LINES=80       # extracted lines printed per job
LINE_CHARS=300     # longest printed line

usage() {
  echo "usage: scripts/ci-failures.sh <owner/repo> <pr-number|run-id>" >&2
  exit 2
}

[ $# -eq 2 ] || usage
REPO=$1
case "$REPO" in
  */*) ;;
  *) usage ;;
esac
target="${2#\#}"
case "$target" in
  ''|*[!0-9]*) usage ;;
esac
command -v gh >/dev/null 2>&1 || { echo "ci-failures: gh is required" >&2; exit 1; }

if [ "${#target}" -le 7 ]; then
  head_sha="$(gh pr view "$target" -R "$REPO" --json headRefOid --jq .headRefOid)" || {
    echo "ci-failures: cannot read PR #$target in $REPO" >&2
    exit 1
  }
  # Every run for the head commit, newest first; include in-progress runs
  # because their already-finished jobs can have failed.
  mapfile -t run_ids < <(gh run list -R "$REPO" --commit "$head_sha" -L 100 \
    --json databaseId,conclusion,status \
    --jq '.[] | select(.conclusion != "success" and .conclusion != "skipped" and .conclusion != "cancelled") | .databaseId') || {
    echo "ci-failures: cannot list runs for $head_sha" >&2
    exit 1
  }
  echo "PR #$target head ${head_sha:0:10}: ${#run_ids[@]} unsuccessful or unfinished run(s)"
else
  run_ids=("$target")
fi

log_file="$(mktemp --suffix=.log "${TMPDIR:-/tmp}/ci-failures-$target.XXXXXX")" || exit 1

# gh prints "job<TAB>step<TAB>timestamp text" and shows ESC as a literal ^[;
# keep only the text, without colour codes.
plain_log() {
  LC_ALL=C sed -E $'s/^[^\t]*\t[^\t]*\t//; s/^\xef\xbb\xbf//; s/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z ?//; s/(\x1b|\^\[)\[[0-9;]*[A-Za-z]//g'
}

# Prints the bounded failure summary for one plain job log's failed steps.
extract() {
  awk -v tail_lines="$TAIL_LINES" -v max_lines="$MAX_LINES" -v line_chars="$LINE_CHARS" '
    function clip(s) { return length(s) > line_chars ? substr(s, 1, line_chars) " ..." : s }
    function emit(s) {
      if (s in seen_line && s !~ /^ *$/) return
      seen_line[s] = 1
      if (printed < max_lines) print "    " clip(s)
      else dropped++
      printed++
    }
    {
      line[NR] = $0
      # A step section starts after its "Run ..." command/env header group.
      if ($0 ~ /^##\[group\]Run /) { step_start = NR; in_header = 1 }
      else if (in_header && $0 ~ /^##\[endgroup\]/) { step_start = NR; in_header = 0 }
      if ($0 ~ /^##\[error\]Process completed with exit code/) {
        seg_start[++segs] = step_start ? step_start : 1
        seg_end[segs] = NR
        exit_line[segs] = $0
      }
    }
    END {
      # A job can fail without an exit marker (timeout, cancellation).
      if (segs == 0) { seg_start[1] = step_start ? step_start : 1; seg_end[1] = NR; segs = 1 }
      for (s = 1; s <= segs; s++) {
        found = 0; block = 0; e_lines = 0; tests = 0; delete detail; delete order
        for (i = seg_start[s]; i <= seg_end[s]; i++) {
          l = line[i]
          if (block > 0) {
            # node:test details are indented under their header (a crashed
            # file carries up to 40 output lines). The spec
            # reporter prints each failure twice; keep the fuller copy.
            if (l ~ /^[ \t]/ || l ~ /^$/) {
              if (l !~ /^ *$/ && l !~ /^ +at .*node:/) buf = buf "\n" l
              if (--block == 0 && length(buf) > length(detail[key])) detail[key] = buf
              continue
            }
            if (length(buf) > length(detail[key])) detail[key] = buf
            block = 0
          }
          if (l ~ /^_{3,} .* _{3,}$/) { e_lines = 0; header = (l ~ /_ coverage: /) ? "" : l; continue }
          if (l ~ /^(FAILED|ERROR) / || l ~ /^[^ ]+: FAILED - [0-9?]+\/[0-9?]+ passed/) { emit(l); found++; continue }
          if (l ~ /^E   / && e_lines < 4 && !(l in seen_line)) {
            if (e_lines == 0 && header != "") emit(header)
            emit(l); e_lines++; found++; continue
          }
          if (l ~ /^ *(✖|not ok [0-9]+) / && l !~ /✖ [0-9]+ problems? \(0 errors/ && l !~ /✖ failing tests:/) {
            key = l; sub(/ \([0-9.]+m?s\)$/, "", key); sub(/^ +/, "", key)
            if (!(key in detail)) { order[++tests] = key; detail[key] = "" }
            buf = ""; block = 45; found++; continue
          }
          if (l ~ /(^|[^A-Za-z])[A-Za-z]*Error(\[[A-Z_]+\])?: / && l !~ /^##\[error\]Process completed/) {
            emit(l); found++; continue
          }
          if (l ~ /^##\[error\]/ && l !~ /^##\[error\]Process completed/) { emit(l); found++; continue }
        }
        if (block > 0 && length(buf) > length(detail[key])) detail[key] = buf
        for (t = 1; t <= tests; t++) {
          emit(order[t])
          n = split(substr(detail[order[t]], 2), parts, "\n")
          for (j = 1; j <= n; j++) emit(parts[j])
        }
        if (!found) {
          first = seg_end[s] - tail_lines; if (first < seg_start[s]) first = seg_start[s]
          print "    (no recognizable failure lines; last " (seg_end[s] - first) " lines of the step)"
          for (i = first; i < seg_end[s]; i++) if (line[i] !~ /^##\[(end)?group\]/) print "    " clip(line[i])
        }
        if (exit_line[s] != "") print "    " exit_line[s]
      }
      if (dropped) print "    ... " dropped " more lines; see the full log"
    }
  '
}

failed_jobs=0
unreadable=0
for run_id in "${run_ids[@]}"; do
  run_info="$(gh run view "$run_id" -R "$REPO" --json workflowName,displayTitle,url,status,jobs --jq '
    "run \(.url | split("/") | last) \(.workflowName) [\(.status)] \(.displayTitle)",
    (.jobs[]
      | select(.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "startup_failure")
      | [.databaseId, .name, .conclusion,
         ([.steps[] | select(.conclusion == "failure" or .conclusion == "timed_out") | .name] | join("; "))]
      | @tsv)')" || {
    echo "ci-failures: cannot read run $run_id" >&2
    unreadable=1
    continue
  }
  head -n 1 <<<"$run_info"
  jobs="$(tail -n +2 <<<"$run_info")"
  [ -n "$jobs" ] || { echo "  no failed jobs"; continue; }
  while IFS=$'\t' read -r job_id job_name conclusion steps; do
    failed_jobs=$((failed_jobs + 1))
    [ "$failed_jobs" -gt 1 ] || echo "full failed-job logs: $log_file"
    echo "  job \"$job_name\" $conclusion; failed step: ${steps:-(unknown)}"
    job_log="$(gh run view --job "$job_id" -R "$REPO" --log 2>&1)" || {
      echo "    (log unavailable: $(head -c 200 <<<"$job_log"))"
      continue
    }
    job_log="$(plain_log <<<"$job_log")"
    printf '===== run %s job %s (%s): %s =====\n%s\n' \
      "$run_id" "$job_id" "$job_name" "${steps:-}" "$job_log" >>"$log_file"
    extract <<<"$job_log"
  done <<<"$jobs"
done

if [ "$failed_jobs" -eq 0 ]; then
  rm -f "$log_file"
  echo "no failed jobs found"
else
  echo "$failed_jobs failed job(s); full logs: $log_file"
fi
exit "$unreadable"
