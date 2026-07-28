#!/bin/sh
# Railway's single container needs two levels of supervision:
# - this component supervisor lets recoveryd exercise its trusted-live fallback;
# - pid 1 restarts the whole service only when an essential component cannot
#   recover locally.
#
# This file is sourced by entrypoint.sh. Keep it POSIX-sh compatible.

railway_child_running() {
  _railway_child_pid="$1"
  _railway_child_state=$(
    awk '/^State:/ { print $2; exit }' \
      "/proc/${_railway_child_pid}/status" 2>/dev/null
  ) || return 1
  [ -n "$_railway_child_state" ] && [ "$_railway_child_state" != "Z" ]
}

railway_recovery_live_attempts() {
  _railway_attempts_file="${RECOVERY_LIVE_ROOT:-/recovery-live}/.attempts"
  _railway_attempts=$(
    cat "$_railway_attempts_file" 2>/dev/null
  ) || _railway_attempts=0
  case "$_railway_attempts" in
    ''|*[!0-9]*) _railway_attempts=0 ;;
  esac
  printf '%s\n' "$_railway_attempts"
}

railway_supervise_recovery() {
  _railway_recovery_health_url="$1"
  shift
  if [ "$#" -eq 0 ]; then
    echo "FATAL: Railway recovery supervisor has no child command." >&2
    exit 64
  fi

  _railway_recovery_child=""
  _railway_recovery_stopping=0

  _railway_stop_recovery() {
    _railway_recovery_stopping=1
    if [ -n "$_railway_recovery_child" ]; then
      kill "$_railway_recovery_child" 2>/dev/null || true
    fi
  }
  trap '_railway_stop_recovery' TERM INT

  while [ "$_railway_recovery_stopping" -eq 0 ]; do
    _railway_attempts_before=$(railway_recovery_live_attempts)
    "$@" &
    _railway_recovery_child=$!
    _railway_recovery_ready=0

    while [ "$_railway_recovery_stopping" -eq 0 ] &&
          railway_child_running "$_railway_recovery_child"; do
      if curl -fsS --max-time 1 "$_railway_recovery_health_url" \
          >/dev/null 2>&1; then
        _railway_recovery_ready=1
        break
      fi
      sleep 1
    done

    wait "$_railway_recovery_child" 2>/dev/null
    _railway_recovery_status=$?
    _railway_recovery_child=""

    if [ "$_railway_recovery_stopping" -eq 1 ]; then
      exit 0
    fi

    if [ "$_railway_recovery_ready" -eq 1 ]; then
      if [ "$_railway_recovery_status" -eq 0 ]; then
        echo "Railway recovery process requested a planned reload." >&2
        continue
      fi
      echo "FATAL: Railway recovery process exited after readiness with status $_railway_recovery_status." >&2
      exit "$_railway_recovery_status"
    fi

    _railway_attempts_after=$(railway_recovery_live_attempts)
    if [ "$_railway_attempts_after" -gt "$_railway_attempts_before" ]; then
      echo "WARNING: Railway trusted-live recovery attempt $_railway_attempts_after exited before readiness with status $_railway_recovery_status; relaunching so recoveryd can advance its fallback." >&2
      sleep 1
      continue
    fi

    echo "FATAL: Railway recovery process exited before readiness with status $_railway_recovery_status without advancing its trusted-live attempts; baked recovery failed." >&2
    [ "$_railway_recovery_status" -ne 0 ] || _railway_recovery_status=1
    exit "$_railway_recovery_status"
  done

  exit 0
}

railway_rollback_platform_boot_attempt() {
  _railway_counter_helper="$1"
  _railway_boot_file="$2"
  _railway_boot_id="$3"
  _railway_boot_prior="$4"
  _railway_boot_charged="$5"
  [ -n "$_railway_counter_helper" ] || return 0
  python3 -P "$_railway_counter_helper" rollback \
    "$_railway_boot_file" "$_railway_boot_id" \
    "$_railway_boot_charged" "$_railway_boot_prior" \
    >/dev/null
}

railway_wait_for_essential_child_exit() {
  _railway_gateway_pid="$1"
  _railway_app_pid="$2"
  _railway_recovery_pid="$3"
  _railway_counter_helper="$4"
  _railway_boot_file="$5"
  _railway_boot_id="$6"
  _railway_boot_prior="$7"
  _railway_boot_charged="$8"

  while railway_child_running "$_railway_gateway_pid" &&
        railway_child_running "$_railway_app_pid" &&
        railway_child_running "$_railway_recovery_pid"; do
    sleep 1
  done

  # App failure owns platform rollback decisions. Check it first so a
  # simultaneous recovery/gateway exit cannot disguise a dead app and undo the
  # platform attempt. Non-app failures roll back only this boot's exact CAS.
  if ! railway_child_running "$_railway_app_pid"; then
    wait "$_railway_app_pid"
    _railway_child_status=$?
    echo "FATAL: Railway app process exited with status $_railway_child_status." >&2
  elif ! railway_child_running "$_railway_recovery_pid"; then
    wait "$_railway_recovery_pid"
    _railway_child_status=$?
    echo "FATAL: Railway recovery supervisor exited with status $_railway_child_status." >&2
    if ! railway_rollback_platform_boot_attempt \
        "$_railway_counter_helper" "$_railway_boot_file" "$_railway_boot_id" \
        "$_railway_boot_prior" "$_railway_boot_charged"; then
      echo "WARNING: could not roll back the non-platform boot attempt." >&2
    fi
  else
    wait "$_railway_gateway_pid"
    _railway_child_status=$?
    echo "FATAL: Railway gateway process exited with status $_railway_child_status." >&2
    if ! railway_rollback_platform_boot_attempt \
        "$_railway_counter_helper" "$_railway_boot_file" "$_railway_boot_id" \
        "$_railway_boot_prior" "$_railway_boot_charged"; then
      echo "WARNING: could not roll back the non-platform boot attempt." >&2
    fi
  fi

  # A clean essential-child exit is still a service failure: Railway's
  # ON_FAILURE policy must restart the incoherent process set.
  [ "$_railway_child_status" -ne 0 ] || _railway_child_status=1
  return "$_railway_child_status"
}
