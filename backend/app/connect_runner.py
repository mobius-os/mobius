#!/usr/bin/env python3
"""Mobius Connect runner.

Dials OUT to your Mobius instance and lets it run commands on this machine.
It only makes OUTBOUND HTTPS requests (no open ports), and it runs commands as
YOU, in your own environment -- the same trust model as running a coding CLI
locally. Disconnecting in the Connect app stops this runner and revokes it.

Pair AND install as a background service that survives reboots (recommended):
    curl -fsSL "https://YOUR-INSTANCE/api/connect/runner" | python3 - \
        --pair ABCD-EFGH --url "https://YOUR-INSTANCE" --install

Just try it in this terminal (stops on Ctrl-C):
    curl -fsSL "https://YOUR-INSTANCE/api/connect/runner" | python3 - \
        --pair ABCD-EFGH --url "https://YOUR-INSTANCE"

Manage the installed service:
    python3 ~/.mobius-connect/runner.py --uninstall
"""
import argparse
from collections import deque
import json
import os
import platform
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_DIR = os.path.expanduser("~/.mobius-connect")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
RUNNER_PATH = os.path.join(CONFIG_DIR, "runner.py")
LOG_PATH = os.path.join(CONFIG_DIR, "service.log")
PID_PATH = os.path.join(CONFIG_DIR, "runner.pid")
LAUNCHD_LABEL = "sh.mobius.connect"
LAUNCHD_PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCHD_LABEL)
SYSTEMD_UNIT = os.path.expanduser("~/.config/systemd/user/mobius-connect.service")


def _load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    os.chmod(CONFIG_PATH, 0o600)


def _post(url, payload, token=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pair(base, code):
    out = _post(base + "/api/connect/pair", {"code": code})
    cfg = {"url": base, "host_id": out["host_id"], "token": out["token"]}
    _save_config(cfg)
    print("Paired as '%s'." % out.get("name", "machine"))
    return cfg


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _run_required(cmd, action):
    result = _run(cmd)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError("%s failed: %s" % (action, detail))
    return result


def _self_download(base):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with urllib.request.urlopen(base + "/api/connect/runner", timeout=30) as resp:
        src = resp.read()
    with open(RUNNER_PATH, "wb") as fh:
        fh.write(src)
    os.chmod(RUNNER_PATH, 0o755)


def _install_launchd(py):
    os.makedirs(os.path.dirname(LAUNCHD_PLIST), exist_ok=True)
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        "  <key>Label</key><string>%s</string>\n"
        "  <key>ProgramArguments</key><array>"
        "<string>%s</string><string>%s</string></array>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><dict>"
        "<key>SuccessfulExit</key><false/></dict>\n"
        "  <key>StandardOutPath</key><string>%s</string>\n"
        "  <key>StandardErrorPath</key><string>%s</string>\n"
        "</dict></plist>\n"
    ) % (LAUNCHD_LABEL, py, RUNNER_PATH, LOG_PATH, LOG_PATH)
    with open(LAUNCHD_PLIST, "w", encoding="utf-8") as fh:
        fh.write(plist)
    _run(["launchctl", "unload", LAUNCHD_PLIST])
    r = _run(["launchctl", "load", "-w", LAUNCHD_PLIST])
    if r.returncode != 0:
        print("launchctl load failed: %s" % r.stderr.strip())
        return False
    print("Installed as a launchd service (starts at login, restarts if it crashes).")
    print("  Logs:      tail -f %s" % LOG_PATH)
    print("  Uninstall: python3 %s --uninstall" % RUNNER_PATH)
    return True


def _install_systemd(py):
    os.makedirs(os.path.dirname(SYSTEMD_UNIT), exist_ok=True)
    unit = (
        "[Unit]\n"
        "Description=Mobius Connect runner\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "ExecStart=%s %s\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ) % (py, RUNNER_PATH)
    with open(SYSTEMD_UNIT, "w", encoding="utf-8") as fh:
        fh.write(unit)
    _run(["systemctl", "--user", "daemon-reload"])
    r = _run(["systemctl", "--user", "enable", "mobius-connect.service"])
    if r.returncode == 0:
        r = _run(["systemctl", "--user", "restart", "mobius-connect.service"])
    if r.returncode != 0:
        print("systemctl --user failed: %s" % r.stderr.strip())
        return _install_background(py)
    linger = _run(["loginctl", "enable-linger", os.environ.get("USER", "")])
    if linger.returncode != 0:
        print("note: could not enable linger; the service may pause when you log "
              "out (%s)" % linger.stderr.strip())
    print("Installed as a systemd --user service (starts at boot, restarts if it crashes).")
    print("  Status:    systemctl --user status mobius-connect")
    print("  Logs:      journalctl --user -u mobius-connect -f")
    print("  Uninstall: python3 %s --uninstall" % RUNNER_PATH)
    return True


def _install_background(py):
    # Last resort (no launchd/systemd): detached background process. Survives
    # closing the terminal, but NOT a reboot.
    with open(LOG_PATH, "ab") as log:
        proc = subprocess.Popen(
            [py, RUNNER_PATH, "--background-service"], stdout=log, stderr=log,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    with open(PID_PATH, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    print("Started in the background (survives closing this terminal, NOT a reboot).")
    print("  Logs: tail -f %s" % LOG_PATH)
    print("  Uninstall: python3 %s --uninstall" % RUNNER_PATH)
    return True


def _install_service(cfg):
    _self_download(cfg["url"])
    py = sys.executable or "python3"
    system = platform.system()
    if system == "Darwin":
        return _install_launchd(py)
    if system == "Linux":
        return _install_systemd(py)
    return _install_background(py)


def _remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _background_pid():
    try:
        with open(PID_PATH, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _revoke_server(cfg):
    if not cfg.get("url") or not cfg.get("token"):
        return
    try:
        _post(cfg["url"].rstrip("/") + "/api/connect/disconnect", {},
              token=cfg["token"])
    except (urllib.error.URLError, ValueError) as exc:
        print("Could not reach Mobius to remove the saved connection: %s" % exc)


def _uninstall_service(stop_running=True):
    system = platform.system()
    stop_cmd = None
    if system == "Darwin" and os.path.exists(LAUNCHD_PLIST):
        domain = "gui/%s/%s" % (os.getuid(), LAUNCHD_LABEL)
        _run_required(["launchctl", "disable", domain], "launchd disable")
        _remove_file(LAUNCHD_PLIST)
        if stop_running:
            stop_cmd = ["launchctl", "bootout", domain]
        print("Removed launchd service registration.")
    elif system == "Linux" and os.path.exists(SYSTEMD_UNIT):
        _run_required(
            ["systemctl", "--user", "disable", "mobius-connect.service"],
            "systemd disable",
        )
        _remove_file(SYSTEMD_UNIT)
        _run_required(
            ["systemctl", "--user", "daemon-reload"], "systemd reload",
        )
        if stop_running:
            stop_cmd = ["systemctl", "--user", "stop", "mobius-connect.service"]
        print("Removed systemd service registration.")

    pid = _background_pid()
    _remove_file(CONFIG_PATH)
    _remove_file(PID_PATH)
    _remove_file(RUNNER_PATH)
    print("Removed Connect credentials and runner.")

    if stop_cmd:
        _run_required(stop_cmd, "daemon stop")
    elif stop_running and pid and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    elif stop_running:
        # Compatibility for fallback installs made before PID tracking.
        _run(["pkill", "-f", RUNNER_PATH])


def _terminate_process_tree(proc):
    """Stop the shell and every process the remote command started."""
    if os.name == "nt":
        if proc.poll() is None:
            # CREATE_NEW_PROCESS_GROUP gives taskkill a tree root to terminate.
            _run(["taskkill", "/PID", str(proc.pid), "/T", "/F"])
    else:
        # The shell may already have exited while one of its descendants keeps
        # the captured pipes open. Its process group still identifies that tree.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _spawn_command(cmd, cwd):
    popen_args = {
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "cwd": (cwd or None),
    }
    if os.name == "nt":
        popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_args["start_new_session"] = True
    return subprocess.Popen(cmd, **popen_args)


def _run_command(cmd, cwd, timeout):
    proc = None
    try:
        proc = _spawn_command(cmd, cwd)
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout, stderr, proc.returncode, False
    except subprocess.TimeoutExpired:
        _terminate_process_tree(proc)
        return "", "command timed out after %ss" % timeout, 124, True
    except KeyboardInterrupt:
        if proc is not None:
            _terminate_process_tree(proc)
        raise
    except Exception as exc:  # noqa: BLE001 - report any spawn failure back
        if proc is not None:
            _terminate_process_tree(proc)
        return "", "runner error: %s" % exc, 1, False


class _CommandRunner:
    """Own one subprocess and retain lifecycle messages across reconnects."""

    def __init__(self, base, token):
        self.base = base
        self.token = token
        self.lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.active = None
        self.outbox = deque()
        # A rotating stream can deliver the same event from the retiring and
        # replacement connection. Request ids are idempotency keys: once this
        # process accepts one, never spawn it a second time.
        self.seen_request_ids = deque(maxlen=64)

    def pending_messages(self):
        with self.lock:
            return list(self.outbox)

    def acknowledge_message(self, message):
        with self.lock:
            for index, pending in enumerate(self.outbox):
                if pending is message:
                    del self.outbox[index]
                    return

    def snapshot(self):
        with self.lock:
            active_id = self.active["request_id"] if self.active else None
            pending = [
                message.get("request_id") for message in self.outbox
                if message.get("type") == "result" and message.get("request_id")
            ]
        return active_id, pending

    def flush_pending_results(self):
        """Retry retained results without holding the subprocess-state lock."""
        with self.flush_lock:
            for message in self.pending_messages():
                try:
                    _post(
                        self.base + "/api/connect/result", message,
                        token=self.token,
                    )
                except (urllib.error.URLError, urllib.error.HTTPError) as exc:
                    print("failed to report result: %s" % exc)
                    return False
                self.acknowledge_message(message)
        return True

    def _post_result(
        self, request_id, stdout, stderr, exit_code, outcome, record=None,
    ):
        message = {
            "type": "result",
            "request_id": request_id,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "timed_out": outcome in ("timed_out", "expired"),
            "outcome": outcome,
        }
        # Retain before attempting the network request. A concurrent stream
        # rotation can now always announce this pending id, so the server will
        # never mistake the just-finished command for lost work and replay it.
        # Releasing the active slot and retaining its result must be one
        # atomic state transition. Otherwise a reconnect can observe neither
        # and tell the server that successfully completed work was lost.
        with self.lock:
            self.outbox.append(message)
            if record is not None and self.active is record:
                self.active = None
        self.flush_pending_results()

    def _post_started(self, request_id):
        try:
            _post(self.base + "/api/connect/state", {
                "request_id": request_id, "state": "started",
            }, token=self.token)
        except urllib.error.HTTPError as exc:
            # A runner can be upgraded before its server. Protocol v1 has no
            # start endpoint but still accepts this runner's final result.
            if exc.code not in (404, 405):
                raise

    def start(self, evt):
        request_id = str(evt.get("request_id") or "")
        with self.lock:
            if request_id in self.seen_request_ids:
                return
            if self.active is not None:
                refused = True
            else:
                refused = False
                self.seen_request_ids.append(request_id)
        not_after = float(evt.get("not_after") or 0)
        if not_after and time.time() > not_after:
            self._post_result(
                request_id, "", "command expired before it could start", 124,
                "expired",
            )
            return

        record = {
            "request_id": request_id,
            "proc": None,
            "reason": None,
            "timeout": max(1, int(evt.get("timeout", 60))),
        }
        if refused:
            self._post_result(
                request_id, "", "runner refused a parallel command", 125,
                "expired",
            )
            return
        with self.lock:
            self.active = record

        try:
            self._post_started(request_id)
            # The state acknowledgement itself crosses the network and can
            # outlive the server's dispatch window. Check the absolute
            # deadline again at the last possible point before spawning so an
            # expired request can never become delayed work on this machine.
            if not_after and time.time() > not_after:
                self._post_result(
                    request_id, "", "command expired before it could start",
                    124, "expired", record=record,
                )
                return
            proc = _spawn_command(evt.get("cmd", ""), evt.get("cwd"))
            record["proc"] = proc
            with self.lock:
                reason = record["reason"]
            if reason is not None:
                _terminate_process_tree(proc)
        except Exception as exc:  # noqa: BLE001 - report the spawn boundary
            self._post_result(
                request_id, "", "runner error: %s" % exc, 1, "completed",
                record=record,
            )
            return

        threading.Thread(
            target=self._wait, args=(record,), daemon=True,
            name="mobius-connect-command",
        ).start()

    def _wait(self, record):
        proc = record["proc"]
        stdout = ""
        stderr = ""
        try:
            stdout, stderr = proc.communicate(timeout=record["timeout"])
        except subprocess.TimeoutExpired:
            with self.lock:
                if record["reason"] is None:
                    record["reason"] = "timed_out"
            _terminate_process_tree(proc)
            stdout, stderr = proc.communicate()
        except Exception as exc:  # noqa: BLE001 - preserve a final result
            stderr = "runner error: %s" % exc

        with self.lock:
            reason = record["reason"]
        if reason == "timed_out":
            outcome, exit_code = "timed_out", 124
            stderr = stderr or "command timed out after %ss" % record["timeout"]
        elif reason is not None:
            outcome, exit_code = "canceled", 130
            stderr = stderr or "command canceled"
        else:
            outcome, exit_code = "completed", proc.returncode
        self._post_result(
            record["request_id"], stdout, stderr, exit_code, outcome,
            record=record,
        )

    def cancel(self, request_id, reason="canceled"):
        with self.lock:
            record = self.active
            if record is None or (
                request_id is not None and record["request_id"] != request_id
            ):
                return False
            if record["reason"] is None:
                record["reason"] = reason
            proc = record["proc"]
        if proc is not None:
            _terminate_process_tree(proc)
        return True


def _serve(cfg):
    base = cfg["url"].rstrip("/")
    token = cfg["token"]
    ctx = ssl.create_default_context()
    plat = "%s %s" % (platform.system(), platform.release())
    commands = _CommandRunner(base, token)
    backoff = 1
    print("Connecting to %s ..." % base)
    while True:
        try:
            # Results are ordinary HTTPS requests. Attempt them before the
            # next stream hello so its pending list reflects what still needs
            # recovery, then again after connecting to close any race.
            commands.flush_pending_results()
            active_id, pending_ids = commands.snapshot()
            query = [
                ("protocol", "3"),
                ("platform", plat),
            ]
            if active_id:
                query.append(("active_request_id", active_id))
            query.extend(
                ("pending_result_id", request_id)
                for request_id in pending_ids
            )
            stream_url = base + "/api/connect/stream?" + urllib.parse.urlencode(
                query,
            )
            req = urllib.request.Request(stream_url)
            req.add_header("Authorization", "Bearer " + token)
            req.add_header("Accept", "text/event-stream")
            with urllib.request.urlopen(
                req, timeout=None, context=ctx,
            ) as stream:
                print("Connected. This machine is now reachable from Mobius.")
                backoff = 1
                commands.flush_pending_results()
                for raw in stream:
                    # Heartbeat comments make this retry path run even while
                    # the host has no new commands.
                    commands.flush_pending_results()
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line.startswith("data:"):
                        continue
                    try:
                        evt = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if evt.get("type") == "cancel":
                        commands.cancel(evt.get("request_id"))
                        continue
                    if evt.get("type") == "disconnect":
                        commands.cancel(None, "disconnect")
                        try:
                            _uninstall_service(stop_running=False)
                            payload = {
                                "request_id": evt.get("request_id"),
                                "stdout": "Connect daemon removed.",
                                "stderr": "", "exit_code": 0,
                            }
                        except Exception as exc:  # local cleanup failure
                            payload = {
                                "request_id": evt.get("request_id"),
                                "stdout": "", "stderr": str(exc),
                                "exit_code": 1,
                            }
                        try:
                            _post(
                                base + "/api/connect/result", payload,
                                token=token,
                            )
                        except (
                            urllib.error.URLError,
                            urllib.error.HTTPError,
                        ) as exc:
                            print("failed to report disconnect: %s" % exc)
                        return
                    if evt.get("type") != "exec":
                        continue
                    print("$ " + evt.get("cmd", ""))
                    commands.start(evt)
            # Protocol v3 streams intentionally end before a hosting proxy's
            # response cap. A command belongs to this runner, not the stream,
            # so clean rotation is the same recovery path as any network loss.
            print("stream rotated; reconnecting")
        except KeyboardInterrupt:
            print("\nStopped.")
            return
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                print("Token rejected (removed from Mobius?). Re-pair to reconnect.")
                return
            print("HTTP %s; retrying in %ss" % (exc.code, backoff))
        except urllib.error.URLError as exc:
            print("connection lost (%s); retrying in %ss" % (exc.reason, backoff))
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)


def main():
    ap = argparse.ArgumentParser(description="Mobius Connect runner")
    ap.add_argument("--pair", help="one-time pairing code from the Connect app")
    ap.add_argument("--url", help="your Mobius instance URL")
    ap.add_argument("--install", action="store_true",
                    help="install as a service that starts on boot")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove the installed service")
    ap.add_argument("--foreground", action="store_true",
                    help="run in this terminal (Ctrl-C stops it)")
    ap.add_argument("--background-service", action="store_true",
                    help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.uninstall:
        cfg = _load_config()
        _revoke_server(cfg)
        _uninstall_service()
        return

    cfg = _load_config()
    if args.pair:
        base = (args.url or cfg.get("url") or "").rstrip("/")
        if not base:
            print("Missing --url")
            sys.exit(2)
        cfg = _pair(base, args.pair.strip())
    if not cfg.get("token"):
        print("Not paired. Run with --pair CODE --url URL first.")
        sys.exit(2)

    if args.install:
        _install_service(cfg)
        return

    if args.background_service:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(PID_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    try:
        _serve(cfg)
    finally:
        if args.background_service:
            _remove_file(PID_PATH)


if __name__ == "__main__":
    main()
