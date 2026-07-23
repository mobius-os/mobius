#!/usr/bin/env bash
# Run one disposable Docker probe with a deadline that owns daemon-side cleanup.

set -u

usage() {
  cat <<'EOF'
Usage:
  scripts/docker-probe.sh [--timeout SECONDS] [--name NAME] -- [docker run args...]
  scripts/docker-probe.sh --list

The helper supplies --rm, --name, --cidfile, and probe labels. It returns 124
when the deadline expires and verifies that the exact container disappeared.
EOF
}

list_probes() {
  local ids
  ids="$(docker ps -q --filter label=io.mobius.probe=true)"
  if [ -z "$ids" ]; then
    echo "No active Möbius Docker probes."
    return 0
  fi
  docker ps \
    --filter label=io.mobius.probe=true \
    --format 'table {{.ID}}\t{{.Names}}\t{{.RunningFor}}\t{{.Status}}'
  docker stats --no-stream \
    --format 'table {{.ID}}\t{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' \
    $ids
}

TIMEOUT_SECONDS="${MOBIUS_DOCKER_PROBE_TIMEOUT:-30}"
PROBE_NAME=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --timeout)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --timeout=*)
      TIMEOUT_SECONDS="${1#*=}"
      shift
      ;;
    --name)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      PROBE_NAME="$2"
      shift 2
      ;;
    --name=*)
      PROBE_NAME="${1#*=}"
      shift
      ;;
    --list)
      [ "$#" -eq 1 ] || { usage >&2; exit 2; }
      list_probes
      exit
      ;;
    --)
      shift
      break
      ;;
    -h|--help)
      usage
      exit
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

case "$TIMEOUT_SECONDS" in
  ''|*[!0-9]*|0)
    echo "docker-probe: timeout must be a positive integer, got '$TIMEOUT_SECONDS'" >&2
    exit 2
    ;;
esac
[ "$#" -gt 0 ] || { usage >&2; exit 2; }

if [ -z "$PROBE_NAME" ]; then
  PROBE_NAME="mobius-probe-${BASHPID}-${RANDOM}"
fi
if [[ ! "$PROBE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
  echo "docker-probe: invalid container name '$PROBE_NAME'" >&2
  exit 2
fi

STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mobius-docker-probe.XXXXXX")" || exit 1
CID_FILE="$STATE_DIR/cid"
TIMED_OUT="$STATE_DIR/timed-out"
CLIENT_PID=""
WATCHDOG_PID=""

container_ref() {
  if [ -s "$CID_FILE" ]; then
    head -n 1 "$CID_FILE"
  else
    printf '%s\n' "$PROBE_NAME"
  fi
}

remove_container() {
  local ref attempt
  ref="$(container_ref)"
  docker rm -f "$ref" >/dev/null 2>&1 || true
  for attempt in 1 2 3; do
    if ! docker inspect "$ref" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    docker rm -f "$ref" >/dev/null 2>&1 || true
  done
  echo "docker-probe: container '$PROBE_NAME' survived cleanup" >&2
  return 1
}

cleanup() {
  local rc="$?"
  trap - EXIT HUP INT TERM
  if [ -n "$WATCHDOG_PID" ]; then
    kill "$WATCHDOG_PID" >/dev/null 2>&1 || true
    wait "$WATCHDOG_PID" 2>/dev/null || true
  fi
  if ! remove_container && [ "$rc" -eq 0 ]; then
    rc=1
  fi
  if [ -n "$CLIENT_PID" ] && kill -0 "$CLIENT_PID" >/dev/null 2>&1; then
    kill -TERM "$CLIENT_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "$CLIENT_PID" ]; then
    wait "$CLIENT_PID" 2>/dev/null || true
  fi
  rm -f "$CID_FILE" "$TIMED_OUT"
  rmdir "$STATE_DIR" 2>/dev/null || true
  exit "$rc"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker run --rm \
  --name "$PROBE_NAME" \
  --cidfile "$CID_FILE" \
  --label io.mobius.probe=true \
  --label "io.mobius.probe.started_at=$started_at" \
  --label "io.mobius.probe.owner_pid=$$" \
  "$@" <&0 &
CLIENT_PID="$!"

# Keep the deadline in a child so it survives an unexpected death of the
# wrapper itself. SIGKILL cannot be trapped; the watchdog still removes this
# exact named container when its deadline arrives.
(
  sleep "$TIMEOUT_SECONDS"
  : >"$TIMED_OUT"
  remove_container
  kill -TERM "$CLIENT_PID" >/dev/null 2>&1 || true
  sleep 2
  kill -KILL "$CLIENT_PID" >/dev/null 2>&1 || true
) &
WATCHDOG_PID="$!"

set +e
wait "$CLIENT_PID"
rc="$?"
set -e

if [ -f "$TIMED_OUT" ]; then
  echo "docker-probe: timed out after ${TIMEOUT_SECONDS}s (${PROBE_NAME})" >&2
  exit 124
fi
exit "$rc"
