#!/bin/sh
# Configure the externally-owned root capability before the entrypoint drops
# privileges. This file is baked root-owned; the live platform never sources it.

configure_agent_sudo() {
  _sudo_mode="${1:-0}"
  _sudo_dir="${2:-/etc/sudoers.d}"
  _visudo="${3:-/usr/sbin/visudo}"
  rm -f "$_sudo_dir/mobius-apt" "$_sudo_dir/mobius-agent"
  case "$_sudo_mode" in
    0)
      return 0
      ;;
    1)
      printf 'mobius ALL=(root) NOPASSWD: ALL\n' > "$_sudo_dir/mobius-agent" || return 1
      chmod 440 "$_sudo_dir/mobius-agent" || return 1
      "$_visudo" -cf "$_sudo_dir/mobius-agent" >/dev/null || {
        rm -f "$_sudo_dir/mobius-agent"
        return 1
      }
      return 0
      ;;
    *)
      echo "FATAL: MOBIUS_AGENT_SUDO must be 0 or 1." >&2
      return 64
      ;;
  esac
}
