#!/usr/bin/env bash
# Run an npm command against this checkout only when its dependency tree is
# complete and built from this exact package-lock.json.

set -uo pipefail

ROOT="$(git rev-parse --show-toplevel)" || {
  echo "wt-npm: not inside a git checkout" >&2
  exit 1
}
FRONTEND="$ROOT/frontend"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=frontend-deps.sh
source "$SCRIPT_DIR/frontend-deps.sh"

if ! command -v flock >/dev/null 2>&1 \
    || ! command -v setsid >/dev/null 2>&1; then
  echo "wt-npm: flock and setsid are required to coordinate dependency borrowing" >&2
  exit 1
fi

# A command owns the checkout's dependency path until it has finished and any
# temporary loan has been released. This prevents two agents in the same
# worktree from unlinking node_modules out from under each other.
lock_path="$(git rev-parse --path-format=absolute --git-path mobius-wt-npm.lock)" || exit 1
# Append mode never truncates a file if an abandoned or hostile symlink exists
# at this predictable Git-admin path. flock owns coordination; it writes no
# lock payload.
exec 9>>"$lock_path" || {
  echo "wt-npm: cannot open worktree dependency lock" >&2
  exit 1
}
flock 9 || {
  echo "wt-npm: cannot acquire worktree dependency lock" >&2
  exit 1
}

borrowed_modules=""
borrowed_temp=""
borrowed_owner=""
borrowed_link_identity=""
npm_pid=""
pending_signal_name=""
pending_signal_status=""

symlink_identity() {
  stat -c '%d:%i' "$1" 2>/dev/null || true
}

release_borrowed_modules() {
  local current_identity current_target

  # The temporary name is unique to this process. It can remain only when a
  # signal arrived before the atomic rename.
  if [ -n "$borrowed_temp" ] && [ -L "$borrowed_temp" ]; then
    current_target="$(readlink -f "$borrowed_temp" 2>/dev/null || true)"
    if [ "$current_target" = "$borrowed_modules" ]; then
      unlink "$borrowed_temp"
    fi
  fi

  # Delete only the exact symlink inode installed by this process. Checking
  # both identity and target avoids removing a later agent's replacement.
  if [ -n "$borrowed_link_identity" ] && [ -L "$FRONTEND/node_modules" ]; then
    current_identity="$(symlink_identity "$FRONTEND/node_modules")"
    current_target="$(readlink -f "$FRONTEND/node_modules" 2>/dev/null || true)"
    if [ "$current_identity" = "$borrowed_link_identity" ] \
        && [ "$current_target" = "$borrowed_modules" ]; then
      unlink "$FRONTEND/node_modules"
    fi
  fi

  # This private hard link pins the borrowed symlink's inode, preventing an
  # unlink-and-recreate replacement from receiving the same inode number (ABA).
  if [ -n "$borrowed_owner" ] && [ -L "$borrowed_owner" ] \
      && [ "$(symlink_identity "$borrowed_owner")" = "$borrowed_link_identity" ]; then
    unlink "$borrowed_owner"
  fi
}

forward_signal() {
  local signal_name="$1"
  local signal_status="$2"
  if [ -z "$npm_pid" ]; then
    exit "$signal_status"
  fi
  pending_signal_name="$signal_name"
  pending_signal_status="$signal_status"
  # Bash may run a pending trap in the instruction-sized window between
  # starting the group and assigning $!. Defer forwarding until its PID is
  # known instead of releasing the loan beneath the just-started command.
  if [ "$npm_pid" = "starting" ]; then
    return
  fi
  # npm and its descendants run in a separate process group. Keep our lock and
  # dependency loan until that whole command group has received the signal and
  # stopped using the checkout.
  kill -s "$signal_name" -- "-$npm_pid" 2>/dev/null \
    || kill -s "$signal_name" "$npm_pid" 2>/dev/null \
    || true
}

# Install cleanup before creating any path. SIGKILL cannot be trapped, but it
# leaves only a tiny symlink which a later invocation can validate and use.
trap release_borrowed_modules EXIT
trap 'forward_signal HUP 129' HUP
trap 'forward_signal INT 130' INT
trap 'forward_signal TERM 143' TERM

status="$(mobius_frontend_deps_status "$FRONTEND")"
if [ "$status" = "missing" ]; then
  # Git's registered worktrees are the bounded candidate set, primary first.
  # Reviews on a different lock can reuse a sibling's proven install instead
  # of each allocating another ~500 MiB. Never discover candidates by walking
  # the data volume or borrow a semver-only match. The normal loan lifetime
  # and before/after proof below remain identical for every candidate.
  shared_modules=""
  while IFS= read -r -d '' field; do
    case "$field" in
      "worktree "*) shared_frontend="${field#worktree }/frontend" ;;
      *) continue ;;
    esac
    [ "$shared_frontend" != "$FRONTEND" ] || continue
    [ -f "$FRONTEND/package-lock.json" ] || break
    [ -f "$shared_frontend/package-lock.json" ] || continue
    cmp -s "$FRONTEND/package-lock.json" \
      "$shared_frontend/package-lock.json" || continue
    [ "$(mobius_frontend_deps_status "$shared_frontend")" = "ready" ] || continue
    shared_modules="$(readlink -f "$shared_frontend/node_modules" 2>/dev/null || true)"
    [ -n "$shared_modules" ] && break
  done < <(git worktree list --porcelain -z)
  if [ -n "$shared_modules" ]; then
    borrowed_modules="$shared_modules"
    for attempt in 1 2 3 4 5; do
      borrowed_temp="$FRONTEND/.node_modules.borrow.${BASHPID:-$$}.$RANDOM.$attempt"
      if ln -s "$borrowed_modules" "$borrowed_temp" 2>/dev/null; then
        borrowed_link_identity="$(symlink_identity "$borrowed_temp")"
        borrowed_owner="$borrowed_temp.owner"
        if [ -n "$borrowed_link_identity" ] \
            && ln -P "$borrowed_temp" "$borrowed_owner" 2>/dev/null; then
          break
        fi
        unlink "$borrowed_temp"
        borrowed_link_identity=""
        borrowed_owner=""
      fi
      borrowed_temp=""
    done
    if [ -n "$borrowed_temp" ] \
        && [ -n "$borrowed_link_identity" ] \
        && mv -Tn "$borrowed_temp" "$FRONTEND/node_modules" \
        && [ "$(symlink_identity "$FRONTEND/node_modules")" = "$borrowed_link_identity" ]; then
      borrowed_temp=""
      status="$(mobius_frontend_deps_status "$FRONTEND")"
      echo "wt-npm: borrowing exact-lock dependencies for this command" >&2
    else
      release_borrowed_modules
      borrowed_modules=""
      borrowed_link_identity=""
      borrowed_owner=""
      status="$(mobius_frontend_deps_status "$FRONTEND")"
    fi
  fi
fi

case "$status" in
  ready) ;;
  missing)
    echo "wt-npm: this checkout has no frontend dependency tree" >&2
    echo "  no registered worktree has a verified matching install; install only for the" >&2
    echo "  required check, then remove $FRONTEND/node_modules" >&2
    exit 2
    ;;
  lock-mismatch)
    echo "wt-npm: frontend dependencies do not match this package-lock.json" >&2
    echo "  replace a borrowed symlink or run: (cd \"$FRONTEND\" && npm ci)" >&2
    exit 3
    ;;
  incomplete)
    echo "wt-npm: this checkout's frontend dependency tree is incomplete" >&2
    echo "  repair it with: (cd \"$FRONTEND\" && npm ci)" >&2
    exit 4
    ;;
  *)
    echo "wt-npm: could not determine frontend dependency state" >&2
    exit 1
    ;;
esac

# Record the proof used by a linked dependency tree. If the canonical install
# or either lock changes while npm is running, a successful command is not a
# trustworthy validation of this checkout.
proof_modules=""
proof_modules_identity=""
proof_review_lock=""
proof_source_lock=""
if [ -L "$FRONTEND/node_modules" ]; then
  proof_modules="$(readlink -f "$FRONTEND/node_modules" 2>/dev/null || true)"
  proof_source_frontend="$(dirname "$proof_modules")"
  proof_modules_identity="$(stat -Lc '%d:%i' "$proof_modules" 2>/dev/null || true)"
  proof_review_lock="$(sha256sum "$FRONTEND/package-lock.json" | awk '{print $1}')"
  proof_source_lock="$(sha256sum "$proof_source_frontend/package-lock.json" | awk '{print $1}')"
fi

cd "$FRONTEND" || exit 1
# Descendants must not inherit the lock. A package script may intentionally
# daemonize; only this waiting wrapper owns the dependency-path lifecycle.
# A separate process group lets a PID-only signal to this wrapper be forwarded
# through npm to its descendants before cleanup occurs.
npm_pid="starting"
setsid npm "$@" 9>&- &
npm_pid=$!
if [ -n "$pending_signal_name" ]; then
  kill -s "$pending_signal_name" -- "-$npm_pid" 2>/dev/null \
    || kill -s "$pending_signal_name" "$npm_pid" 2>/dev/null \
    || true
fi
while true; do
  wait "$npm_pid"
  npm_status=$?
  if kill -0 "$npm_pid" 2>/dev/null; then
    continue
  fi
  break
done
if [ -n "$pending_signal_status" ]; then
  while kill -0 -- "-$npm_pid" 2>/dev/null; do
    sleep 0.05
  done
  npm_status="$pending_signal_status"
fi
npm_pid=""

if [ "$npm_status" -eq 0 ] && [ -n "$proof_modules" ]; then
  current_modules="$(readlink -f "$FRONTEND/node_modules" 2>/dev/null || true)"
  current_modules_identity="$(stat -Lc '%d:%i' "$current_modules" 2>/dev/null || true)"
  current_review_lock="$(sha256sum "$FRONTEND/package-lock.json" 2>/dev/null | awk '{print $1}')"
  current_source_lock="$(sha256sum "$(dirname "$current_modules")/package-lock.json" 2>/dev/null | awk '{print $1}')"
  current_status="$(mobius_frontend_deps_status "$FRONTEND")"
  if [ "$current_modules" != "$proof_modules" ] \
      || [ "$current_modules_identity" != "$proof_modules_identity" ] \
      || [ "$current_review_lock" != "$proof_review_lock" ] \
      || [ "$current_source_lock" != "$proof_source_lock" ] \
      || [ "$current_status" != "ready" ]; then
    echo "wt-npm: dependency proof changed while the command was running" >&2
    exit 5
  fi
fi

exit "$npm_status"
