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
import base64
import codecs
import http.client
from collections import deque
from contextlib import contextmanager
import io
import ipaddress
import json
import locale
import os
import platform
import shlex
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl
except ImportError:  # Windows uses msvcrt below.
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX uses fcntl above.
    msvcrt = None
try:
    import winreg
except ImportError:  # Only Windows has the registry.
    winreg = None

CONFIG_DIR = os.path.expanduser("~/.mobius-connect")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
RUNNER_PATH = os.path.join(CONFIG_DIR, "runner.py")
LOG_PATH = os.path.join(CONFIG_DIR, "service.log")
PID_PATH = os.path.join(CONFIG_DIR, "runner.pid")
LAUNCHD_LABEL = "sh.mobius.connect"
LAUNCHD_PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCHD_LABEL)
SYSTEMD_UNIT = os.path.expanduser("~/.config/systemd/user/mobius-connect.service")
# Windows starts the runner at sign-in from the per-user Run key: no admin
# rights, no Task Scheduler policy, and removed cleanly on uninstall.
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_RUN_VALUE = "MobiusConnect"
RUNNER_PROTOCOL_VERSION = 4
# Increment this for every shipped runner change that an existing installation
# should receive. Protocol only describes wire compatibility; compatible
# releases can keep using the same protocol while still offering an update.
RUNNER_RELEASE = 8
# What this runner can do, announced on every stream. Möbius gates behavior on
# these names, never on release numbers: independently maintained copies of
# this runner can reach the same release number with different abilities.
RUNNER_CAPABILITIES = ("parallel", "command_file", "inventory_body")
# A live stream receives a server heartbeat every 15 seconds. Some hosting
# proxies keep the client TCP socket open after the backend behind it restarts,
# leaving the runner blocked forever on a stream the new backend no longer
# owns. Bound each read to several heartbeat intervals so that transport can
# reconnect without making one late heartbeat look like an outage.
STREAM_HEARTBEAT_SECONDS = 15
STREAM_READ_TIMEOUT_SECONDS = STREAM_HEARTBEAT_SECONDS * 4
# A healthy stream is deliberately rotated by the server before common proxy
# response caps. Reconnect that handoff immediately: sleeping here creates a
# visible offline flash even though neither endpoint failed. Streams that die
# before one heartbeat interval still take the ordinary retry backoff so a
# broken intermediary cannot create a tight reconnect loop.
STREAM_HEALTHY_SECONDS = STREAM_HEARTBEAT_SECONDS
# Set by a Mobius that supervises this runner (a Mobius-to-Mobius connection).
# Such a runner is updated by its supervisor, not by an install command.
SUPERVISOR_ENV = "CONNECT_RUNNER_SUPERVISOR"
RUNNER_USER_AGENT = (
    "mobius-connect/%s (protocol/%s; +https://github.com/mobius-os/mobius)"
    % (RUNNER_RELEASE, RUNNER_PROTOCOL_VERSION)
)
_POWERSHELL_STDIN_BOOTSTRAP = (
    "$encoded=[Console]::In.ReadToEnd();"
    "$script=[Text.Encoding]::UTF8.GetString("
    "[Convert]::FromBase64String($encoded));"
    "& ([ScriptBlock]::Create($script))"
)


def _validated_base_url(raw):
    """Return a credential-safe Mobius origin or reject it.

    Connect sends a long-lived host bearer on every request. Plain HTTP is
    therefore acceptable only on the local loopback used for development;
    remote instances must use HTTPS. Credentials and query/fragment material
    do not belong in the persisted base URL.
    """
    value = str(raw or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Connect requires a valid Mobius HTTPS URL.")
    try:
        hostname = parsed.hostname or ""
        # Accessing port also validates malformed/non-numeric port text.
        parsed.port
    except ValueError as exc:
        raise ValueError("Connect requires a valid Mobius HTTPS URL.") from exc
    if not hostname:
        raise ValueError("Connect requires a valid Mobius HTTPS URL.")
    if parsed.scheme == "http":
        local = hostname.casefold() == "localhost"
        if not local:
            try:
                local = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                local = False
        if not local:
            raise ValueError(
                "Connect refuses plaintext HTTP for a remote instance; use HTTPS."
            )
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Fail closed instead of forwarding pairing codes or host bearers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_url(request, *, timeout, context=None):
    # urllib otherwise identifies itself as Python-urllib/<version>, which
    # browser-integrity filters commonly reject.  Keep every Connect request
    # identifiable at this one transport boundary, including the self-update
    # path that supplies a URL string rather than a pre-built Request.
    if not isinstance(request, urllib.request.Request):
        request = urllib.request.Request(request)
    if not request.has_header("User-agent"):
        request.add_header("User-Agent", RUNNER_USER_AGENT)
    handlers = [_NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    try:
        return urllib.request.build_opener(*handlers).open(request, timeout=timeout)
    except http.client.HTTPException as exc:
        raise urllib.error.URLError(exc) from exc


def _load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, CONFIG_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# One machine can be paired to several Mobius instances at once. The config
# holds a list of connections; each is an independent {url, host_id, token}
# credential served by one shared runner process. A pre-multi-connection config
# stored a single connection at the top level, so it is migrated on read.
_config_lock = threading.Lock()


@contextmanager
def _config_mutation_lock():
    """Serialize a config read-modify-write across runner processes."""
    lock_path = CONFIG_PATH + ".lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with _config_lock, os.fdopen(lock_fd, "r+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write("\x00")
                lock_file.flush()
            lock_file.seek(0)
            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            raise RuntimeError("Connect cannot lock its configuration safely.")
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            else:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _connections(cfg=None):
    """Normalize either config shape into a list of connection dicts."""
    if cfg is None:
        cfg = _load_config()
    if not isinstance(cfg, dict):
        return []
    raw = cfg.get("connections")
    if not isinstance(raw, list):
        # Legacy single-connection config kept url/host_id/token at the top.
        raw = [cfg] if cfg.get("token") and cfg.get("url") else []
    conns = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if not item.get("token") or not item.get("url"):
            continue
        conns.append({
            "url": item["url"],
            "host_id": item.get("host_id"),
            "token": item["token"],
            "name": item.get("name"),
        })
    return conns


def _same_connection(a, b):
    return a.get("url") == b.get("url") and a.get("host_id") == b.get("host_id")


def _add_connection(conn):
    """Add or refresh one connection without dropping the others."""
    with _config_mutation_lock():
        conns = _connections()
        conns = [c for c in conns if not _same_connection(c, conn)]
        conns.append(conn)
        _save_config({"connections": conns})
    return conn


def _remove_connection(url, host_id):
    """Drop one connection; return how many remain."""
    target = {"url": url, "host_id": host_id}
    with _config_mutation_lock():
        conns = [c for c in _connections() if not _same_connection(c, target)]
        _save_config({"connections": conns})
    return len(conns)


def _post(url, payload, token=None, timeout=30, *, context=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _open_url(req, timeout=timeout, context=context) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except http.client.HTTPException as exc:
        raise urllib.error.URLError(exc) from exc


def _open_stream(base, token, metadata, active_ids, pending_ids, context):
    """Open a bounded-URL stream; fall back only for a legacy GET server."""
    endpoint = base + "/api/connect/stream?" + urllib.parse.urlencode(metadata)
    body = json.dumps({
        "active_request_ids": active_ids,
        "pending_result_ids": pending_ids,
    }).encode("utf-8")
    request = urllib.request.Request(endpoint, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "text/event-stream")
    try:
        return _open_url(
            request, timeout=STREAM_READ_TIMEOUT_SECONDS, context=context,
        )
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 405):
            raise
    # Older external Mobius copies know only GET and the legacy query shape.
    legacy = list(metadata)
    legacy.extend(("active_request_id", item) for item in active_ids)
    legacy.extend(("pending_result_id", item) for item in pending_ids)
    request = urllib.request.Request(
        base + "/api/connect/stream?" + urllib.parse.urlencode(legacy),
    )
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "text/event-stream")
    return _open_url(
        request, timeout=STREAM_READ_TIMEOUT_SECONDS, context=context,
    )


def _pair(base, code):
    base = _validated_base_url(base)
    out = _post(base + "/api/connect/pair", {"code": code})
    conn = {
        "url": base,
        "host_id": out["host_id"],
        "token": out["token"],
        "name": out.get("name"),
    }
    # Additive: pairing to another instance keeps the machine's existing
    # connections, so one runner can serve several Mobius instances at once.
    _add_connection(conn)
    print("Granted command access to %s." % base)
    return conn


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
    try:
        with _open_url(base + "/api/connect/runner", timeout=30) as resp:
            src = resp.read()
    except http.client.HTTPException as exc:
        raise urllib.error.URLError(exc) from exc
    # Never replace a working service with an empty, truncated, or non-Python
    # response from a proxy. Compile before the atomic replacement.
    if not src or not src.startswith(b"#!/usr/bin/env python3"):
        raise ValueError("Connect runner download was not a Python runner")
    compile(src, RUNNER_PATH, "exec")
    fd, temporary = tempfile.mkstemp(prefix=".runner-", dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(src)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temporary, 0o755)
        os.replace(temporary, RUNNER_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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


def _start_detached(py):
    """Start the runner as a background process that outlives this terminal.

    Its PID file entry is also the handoff: a previous background runner sees
    that it no longer owns the file and exits (see `_serve_all`), so reinstalling
    or updating never leaves two runners, and nothing signals a PID that may
    have been reused by an unrelated process since it was written."""
    kwargs = {"start_new_session": True}
    if os.name == "nt":
        # start_new_session means nothing on Windows; without leaving the
        # console, closing the PowerShell window would end the runner too.
        kwargs = {"creationflags": (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )}
    with open(LOG_PATH, "ab") as log:
        proc = subprocess.Popen(
            [py, RUNNER_PATH, "--background-service"], stdout=log, stderr=log,
            stdin=subprocess.DEVNULL, **kwargs
        )
    with open(PID_PATH, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))


def _install_background(py):
    # Last resort (no launchd/systemd): detached background process. Survives
    # closing the terminal, but NOT a reboot.
    _start_detached(py)
    print("Started in the background (survives closing this terminal, NOT a reboot).")
    print("  Logs: tail -f %s" % LOG_PATH)
    print("  Uninstall: python3 %s --uninstall" % RUNNER_PATH)
    return True


def _windowless_python(py):
    # pythonw.exe runs the same interpreter without opening a console window.
    folder, name = os.path.split(py)
    if name.lower() == "python.exe":
        candidate = os.path.join(folder, "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return py


def _windows_run_command(py):
    return subprocess.list2cmdline([py, RUNNER_PATH, "--background-service"])


def _install_windows(py):
    py = _windowless_python(py)
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
            winreg.SetValueEx(
                key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, _windows_run_command(py),
            )
    except OSError as exc:
        print("Could not register Connect to start at sign-in: %s" % exc)
        return _install_background(py)
    _start_detached(py)
    print("Installed: running in the background now, and starts again whenever you sign in to Windows.")
    print("  Logs:      Get-Content -Wait %s" % LOG_PATH)
    print("  Uninstall: python %s --uninstall" % RUNNER_PATH)
    return True


def _remove_windows_startup():
    """Remove the sign-in registration; False when there was none."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
    except FileNotFoundError:
        return False
    return True


def _install_service(base):
    _self_download(base)
    py = sys.executable or "python3"
    system = platform.system()
    if system == "Darwin":
        return _install_launchd(py)
    if system == "Linux":
        return _install_systemd(py)
    if system == "Windows" and winreg is not None:
        return _install_windows(py)
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
    elif system == "Windows" and winreg is not None and _remove_windows_startup():
        print("Removed the start-at-sign-in registration.")

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
    elif stop_running and os.name != "nt":
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


def _script_argv(shell, *, is_windows=None):
    """Select one interpreter without placing script text on a command line."""
    windows = os.name == "nt" if is_windows is None else is_windows
    if windows:
        executable = shell or "powershell.exe"
        allowed = {
            "powershell": "powershell.exe",
            "powershell.exe": "powershell.exe",
            "pwsh": "pwsh",
            "pwsh.exe": "pwsh.exe",
        }
        executable = allowed.get(executable.casefold())
        if executable is None:
            raise ValueError(
                "Windows script mode supports powershell or pwsh"
            )
        return [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _POWERSHELL_STDIN_BOOTSTRAP,
        ]
    return [shell or "sh"]


def _script_input(script, *, is_windows=None):
    """Encode Windows stdin as ASCII so console code pages cannot alter it."""
    windows = os.name == "nt" if is_windows is None else is_windows
    if windows:
        return base64.b64encode(script.encode("utf-8")).decode("ascii")
    return script


def _spawn_command(cmd, cwd, *, script=None, shell=None, before_spawn=None):
    """Start one command with binary pipes so output can stream as it arrives.

    Returns the process and the private script file an oversized inline
    command runs from (None otherwise); the caller removes that file when the
    command is over.
    """
    popen_args = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": (cwd or None),
    }
    path = None
    if script is not None:
        if cmd is not None:
            raise ValueError("runner received both cmd and script")
        if "\x00" in script:
            raise ValueError("a script cannot contain a NUL byte")
        popen_args["stdin"] = subprocess.PIPE
        command = _script_argv(shell)
    else:
        if "\x00" in cmd:
            raise ValueError("a command cannot contain a NUL byte")
        if os.name != "nt" and len(cmd.encode("utf-8")) > 32_000:
            # POSIX execve has an OS-dependent per-argument limit. Execute a
            # private script file instead of changing inline-command stdin.
            fd, path = tempfile.mkstemp(prefix="mobius-connect-command-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(cmd.encode("utf-8"))
                # Source in the same -c shell, not `sh path`: the latter
                # changes $0 and positional parameters. Clear the one
                # bootstrap argument before user code runs.
                command = ["/bin/sh", "-c", "set --; . " + shlex.quote(path),
                           "/bin/sh"]
            except BaseException:
                os.unlink(path)
                raise
        else:
            popen_args["shell"] = True
            command = cmd
    if os.name == "nt":
        popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_args["start_new_session"] = True
    try:
        # Preparation above can include writing a large private command file.
        # Recheck eligibility only after that work, immediately before Popen.
        if before_spawn is not None:
            before_spawn()
        proc = subprocess.Popen(command, **popen_args)
    except BaseException:
        if path is not None:
            os.unlink(path)
        raise
    return proc, path


class _StartRefused(Exception):
    """A reserved command became ineligible before its process launch."""

    def __init__(self, outcome):
        self.outcome = outcome


# The final result keeps a head+tail view that matches what the server can
# return. Sending more only spends bandwidth before the server discards it, and
# sufficiently large output can be rejected before it reaches that truncation
# boundary. Both the first error and the final summary usually matter.
_MAX_RESULT_STREAM = 60_000
# Live output is delivered in numbered chunks about twice a second. Unsent
# chunks are spooled to an anonymous temporary file so an outage cannot drop
# their sequence history merely because a process keeps printing.
_OUTPUT_FLUSH_SECONDS = 0.5
# Output is delivered from each command's own watch loop, so a slow Möbius
# must not hold up that loop's time-limit and cancel checks for long.
_OUTPUT_POST_TIMEOUT_SECONDS = 5
_OUTPUT_BATCH_BYTES = 512_000
# After a timeout or cancel kills the process group, a descendant that escaped
# the group could keep the pipes open. Stop waiting for it after this long.
_PIPE_DRAIN_AFTER_KILL_SECONDS = 5


def _truncated_view(head_source, tail_source):
    marker = "\n…[output truncated by runner]…\n"
    kept = _MAX_RESULT_STREAM - len(marker)
    head = (kept + 1) // 2
    tail = kept // 2
    return head_source[:head] + marker + tail_source[-tail:]


def _cap_output(text):
    text = text or ""
    if len(text) <= _MAX_RESULT_STREAM:
        return text, False
    return _truncated_view(text, text), True


class _HeadTail:
    """Accumulate one stream while retaining only what `_cap_output` keeps."""

    def __init__(self):
        self.head = ""
        self.tail = ""
        self.total = 0

    def append(self, text):
        self.total += len(text)
        if len(self.head) < _MAX_RESULT_STREAM:
            self.head += text[:_MAX_RESULT_STREAM - len(self.head)]
        self.tail = (self.tail + text)[-_MAX_RESULT_STREAM:]

    def value(self):
        if self.total <= _MAX_RESULT_STREAM:
            return self.head, False
        return _truncated_view(self.head, self.tail), True


_SPOOL_HEADER = struct.Struct(">QI")  # sequence, JSON byte length


class _CommandOutput:
    """One command's undelivered live chunks plus its capped final view.

    Each read becomes one chunk with an absolute sequence number, so a retried
    delivery is idempotent on the server.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.spool = tempfile.TemporaryFile(mode="w+b")
        self.read_offset = 0
        self.write_offset = 0
        self.next_seq = 0
        self.output_error = None
        self.final = {"stdout": _HeadTail(), "stderr": _HeadTail()}

    def append(self, stream, text):
        if not text:
            return
        with self.lock:
            # Delivery can finish after the bounded drain of an escaped child.
            # Its late reader must not reopen or write to released scratch.
            if self.spool.closed:
                return
            self.final[stream].append(text)
            if self.output_error is not None:
                return
            # JSON's default ASCII escaping can use twelve bytes per Unicode
            # scalar; keep even that worst case within one batch.
            for start in range(0, len(text), 30_000):
                chunk = {"seq": self.next_seq, "stream": stream,
                         "text": text[start:start + 30_000]}
                data = json.dumps(chunk).encode("utf-8")
                try:
                    self.spool.seek(0, os.SEEK_END)
                    self.spool.write(_SPOOL_HEADER.pack(self.next_seq, len(data)))
                    self.spool.write(data)
                    self.spool.flush()
                except OSError as exc:
                    self.output_error = "live output spool failed: %s" % exc
                    return
                self.write_offset = self.spool.tell()
                self.next_seq += 1

    def _header_at(self, offset):
        self.spool.seek(offset)
        header = self.spool.read(_SPOOL_HEADER.size)
        if len(header) != _SPOOL_HEADER.size:
            raise OSError("short output spool header")
        seq, length = _SPOOL_HEADER.unpack(header)
        if not 0 < length <= _OUTPUT_BATCH_BYTES or offset + _SPOOL_HEADER.size + length > self.write_offset:
            raise OSError("invalid output spool record length")
        return seq, length

    def take_batch(self):
        with self.lock:
            batch = []
            size = 8192  # envelope, up to 4096 separators, and margin
            offset = self.read_offset
            while offset < self.write_offset and len(batch) < 4096:
                try:
                    _seq, length = self._header_at(offset)
                    if batch and size + length + 2 > _OUTPUT_BATCH_BYTES:
                        break
                    data = self.spool.read(length)
                    if len(data) != length:
                        raise OSError("short output spool read")
                    batch.append(json.loads(data))
                except (OSError, ValueError) as exc:
                    self.output_error = "live output spool failed: %s" % exc
                    return []
                size += length + 2
                offset += _SPOOL_HEADER.size + length
            return batch

    def acknowledge(self, through_seq):
        with self.lock:
            while self.read_offset < self.write_offset:
                seq, length = self._header_at(self.read_offset)
                if seq > through_seq:
                    break
                self.read_offset += _SPOOL_HEADER.size + length
            # Acknowledged text already belongs to the server's durable ledger.
            if self.read_offset == self.write_offset:
                try:
                    self.spool.seek(0)
                    self.spool.truncate()
                except OSError:
                    # Reclaiming scratch is optional: the output is already
                    # acknowledged, so cleanup failure is not capture loss.
                    pass
                else:
                    self.read_offset = self.write_offset = 0

    def discard_pending(self):
        with self.lock:
            self.read_offset = self.write_offset

    def has_pending(self):
        with self.lock:
            return self.read_offset < self.write_offset

    def close(self):
        with self.lock:
            self.read_offset = self.write_offset
            try:
                self.spool.close()
            except OSError as exc:
                # Cleanup cannot overturn an acknowledged result.
                print("failed to close output scratch: %s" % exc)

    def final_streams(self):
        with self.lock:
            stdout, stdout_truncated = self.final["stdout"].value()
            stderr, stderr_truncated = self.final["stderr"].value()
            return stdout, stderr, stdout_truncated or stderr_truncated


def _stream_decoder():
    # Match text-mode subprocess decoding: the locale's encoding plus universal
    # newlines, applied incrementally so a split character or CRLF is intact.
    encoding = locale.getpreferredencoding(False) or "utf-8"
    try:
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    except LookupError:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    return io.IncrementalNewlineDecoder(decoder, translate=True)


def _pump_stream(pipe, stream, output):
    decoder = _stream_decoder()
    try:
        while True:
            data = pipe.read1(65536)
            if not data:
                break
            output.append(stream, decoder.decode(data))
    except (OSError, ValueError):
        pass
    finally:
        try:
            output.append(stream, decoder.decode(b"", final=True))
        finally:
            pipe.close()


def _feed_stdin(pipe, text):
    try:
        pipe.write(text.encode("ascii" if os.name == "nt" else "utf-8"))
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _supervise_process(proc, output, *, timeout, stdin_text=None,
                       stop_reason=lambda: None, on_tick=lambda: None):
    """Collect output, enforce the time limit, and return why it ended.

    Returns ``"timed_out"``, the caller's stop reason, or ``None`` when the
    command finished by itself. The command is over only when its shell has
    exited AND every process holding its pipes has closed them.
    """
    readers = [
        threading.Thread(
            target=_pump_stream, args=(proc.stdout, "stdout", output),
            daemon=True, name="mobius-connect-stdout",
        ),
        threading.Thread(
            target=_pump_stream, args=(proc.stderr, "stderr", output),
            daemon=True, name="mobius-connect-stderr",
        ),
    ]
    for reader in readers:
        reader.start()
    if stdin_text is not None:
        threading.Thread(
            target=_feed_stdin, args=(proc.stdin, stdin_text),
            daemon=True, name="mobius-connect-stdin",
        ).start()
    deadline = time.monotonic() + timeout
    ended_by = None
    killed_at = None
    while proc.poll() is None or any(reader.is_alive() for reader in readers):
        now = time.monotonic()
        if ended_by is None:
            reason = stop_reason()
            if reason is not None:
                ended_by = reason
            elif now >= deadline:
                ended_by = "timed_out"
            if ended_by is not None:
                _terminate_process_tree(proc)
                killed_at = time.monotonic()
        if (
            killed_at is not None
            and time.monotonic() - killed_at >= _PIPE_DRAIN_AFTER_KILL_SECONDS
        ):
            break
        wait_for = _OUTPUT_FLUSH_SECONDS
        if ended_by is None:
            wait_for = max(0.01, min(wait_for, deadline - now))
        alive = [reader for reader in readers if reader.is_alive()]
        if alive:
            alive[0].join(wait_for)
        else:
            try:
                proc.wait(timeout=wait_for)
            except subprocess.TimeoutExpired:
                pass
        on_tick()
    if proc.poll() is None:
        proc.wait()
    return ended_by if ended_by is not None else stop_reason()


class _Command:
    """One accepted command, from its reservation until its result is delivered."""

    def __init__(self, request_id, timeout):
        self.request_id = request_id
        self.timeout = timeout
        self.proc = None
        # The private script file an oversized inline command runs from.
        self.command_file = None
        self.stdin_text = None
        # Live output; once finished, only output that still has chunks to
        # deliver before the result is kept here.
        self.output = None
        # Why the runner is stopping it ("canceled" or "disconnect").
        self.stop_reason = None
        # The final result message, retained until the server accepts it.
        self.result = None


class _CommandRunner:
    """Own this connection's commands and retain their final results.

    Commands run independently: each has its own request id, time limit,
    output, and cancellation. Nothing queues — a command starts at once or
    reports why it could not.
    """

    def __init__(self, base, token, *, context=None):
        self.base = base
        self.token = token
        # This connection's verified TLS context. A fresh default context
        # reloads the system trust store, so command POSTs share this one.
        self.context = context
        self.lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.active = {}
        # Finished commands whose result the server has not accepted yet.
        self.outbox = deque()
        self.reconcile_requested = False
        # Turned on by the server's stream hello and kept across reconnects,
        # so output buffered during an outage is still delivered. Older
        # servers send no hello and receive only final results.
        self.live_output = False
        # A rotating stream can deliver the same event from the retiring and
        # replacement connection. Request ids are idempotency keys: once this
        # process accepts one, never spawn it a second time.
        self.recent_request_ids = {}  # id -> accept-until timestamp
        self.result_wakeup = threading.Event()
        self.result_worker = None

    def pending_messages(self):
        with self.lock:
            return [command.result for command in self.outbox]

    def _acknowledge(self, command):
        with self.lock:
            self.outbox.remove(command)
        if command.output is not None:
            command.output.close()

    def snapshot(self):
        with self.lock:
            active_ids = sorted(self.active)
            pending = [command.request_id for command in self.outbox]
        return active_ids, pending

    def take_reconcile_request(self):
        """Consume a request to reconnect after discarding a terminal result."""
        with self.lock:
            requested = self.reconcile_requested
            self.reconcile_requested = False
        return requested

    def flush_pending_results(self):
        """Retry retained results without holding the subprocess-state lock."""
        with self.flush_lock:
            with self.lock:
                pending = list(self.outbox)
            for command in pending:
                message = command.result
                output = command.output
                if output is not None:
                    try:
                        if not self.flush_output(command, drain=True):
                            if output.output_error is None:
                                return False
                    except Exception as exc:
                        output.output_error = "live output drain failed: %s" % exc
                    if output.output_error is not None:
                        # Never advertise output the server did not receive.
                        message.pop("output_seq", None)
                        message["output_error"] = output.output_error[:1024]
                        output.discard_pending()
                        command.output = None
                        output.close()
                try:
                    _post(
                        self.base + "/api/connect/result", message,
                        token=self.token, context=self.context,
                    )
                except urllib.error.HTTPError as exc:
                    # A 4xx means the server refuses this exact payload, so an
                    # identical retry can never succeed. Retrying it forever
                    # would pin a finished command and wedge the runner.
                    # Drop it and let the server's reconcile finalize the
                    # command as lost. 408/425/429 are transient, so retry.
                    if 400 <= exc.code < 500 and exc.code not in (408, 425, 429):
                        print(
                            "dropping unreportable result for %s: %s"
                            % (command.request_id, exc)
                        )
                        self._acknowledge(command)
                        # The server still owns the corresponding command.
                        # Reconnect without this pending id so its existing
                        # reconciliation path can finalize that command as
                        # lost now, rather than waiting for stream rotation.
                        with self.lock:
                            self.reconcile_requested = True
                        continue
                    print("failed to report result: %s" % exc)
                    return False
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    print("failed to report result: %s" % exc)
                    return False
                self._acknowledge(command)
        return True

    def flush_output(self, command, drain=False):
        """Deliver pending live chunks: one batch per call unless draining.

        One batch per watch-loop tick keeps a fast-printing command from
        starving its own time-limit and cancel checks. False means retry later.
        """
        output = command.output
        while True:
            if output.output_error is not None:
                return False
            if not self.live_output:
                output.discard_pending()
                return True
            batch = output.take_batch()
            if not batch:
                return True
            try:
                response = _post(self.base + "/api/connect/output", {
                    "request_id": command.request_id,
                    "chunks": batch,
                }, token=self.token, timeout=_OUTPUT_POST_TIMEOUT_SECONDS,
                    context=self.context)
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code not in (408, 425, 429):
                    # Rejection is not acknowledgement. Keep the known
                    # execution outcome, but never advertise complete output.
                    output.output_error = "live output upload rejected: HTTP %s" % exc.code
                return False
            except (urllib.error.URLError, OSError, ValueError):
                return False
            # The server's durable cursor, not our attempted last chunk, is
            # authoritative when a previous response was lost after commit.
            next_seq = response.get("next") if isinstance(response, dict) else None
            if not isinstance(next_seq, int) or next_seq < 0:
                return False
            if next_seq <= batch[0]["seq"]:
                return False
            try:
                output.acknowledge(next_seq - 1)
            except OSError as exc:
                output.output_error = "live output spool failed: %s" % exc
                return False
            if not drain:
                return True

    def _post_result(
        self, command, stdout, stderr, exit_code, outcome,
        truncated=False, output_seq=None, output_error=None,
    ):
        stdout, stdout_truncated = _cap_output(stdout)
        stderr, stderr_truncated = _cap_output(stderr)
        message = {
            "type": "result",
            "request_id": command.request_id,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "timed_out": outcome in ("timed_out", "expired"),
            "outcome": outcome,
            "truncated": truncated or stdout_truncated or stderr_truncated,
        }
        if output_seq is not None:
            message["output_seq"] = output_seq
        if output_error:
            message["output_error"] = output_error[:1024]
        # Retain before attempting the network request. A concurrent stream
        # rotation can now always announce this pending id, so the server will
        # never mistake the just-finished command for lost work and replay it.
        # Releasing the active command and retaining its result must be one
        # atomic state transition. Otherwise a reconnect can observe neither
        # and tell the server that successfully completed work was lost.
        output = command.output
        with self.lock:
            if output is not None and (
                output.output_error is not None or not output.has_pending()
            ):
                # Nothing left to deliver before the result.
                command.output = None
            command.result = message
            self.outbox.append(command)
            if self.active.get(command.request_id) is command:
                del self.active[command.request_id]
        if output is not None and command.output is None:
            output.close()
        self._wake_result_worker()

    def _wake_result_worker(self):
        with self.lock:
            if self.result_worker is None:
                worker = threading.Thread(
                    target=self._result_loop, daemon=True,
                    name="mobius-connect-results",
                )
                try:
                    worker.start()
                except RuntimeError as exc:
                    print("result worker unavailable; retaining result: %s" % exc)
                    return
                self.result_worker = worker
        self.result_wakeup.set()

    def _result_loop(self):
        while True:
            self.result_wakeup.wait(timeout=2)
            self.result_wakeup.clear()
            self.flush_pending_results()
            with self.lock:
                if not self.outbox:
                    self.result_worker = None
                    return

    def _post_started(self, request_id):
        try:
            _post(self.base + "/api/connect/state", {
                "request_id": request_id, "state": "started",
            }, token=self.token, context=self.context)
        except urllib.error.HTTPError as exc:
            # A runner can be upgraded before its server. Protocol v1 has no
            # start endpoint but still accepts this runner's final result.
            if exc.code not in (404, 405):
                raise

    def start(self, evt):
        request_id = str(evt.get("request_id") or "")
        not_after = float(evt.get("not_after") or 0)
        now = time.time()
        with self.lock:
            for old_id, expiry in list(self.recent_request_ids.items()):
                if expiry <= now:
                    del self.recent_request_ids[old_id]
            if (request_id in self.active
                    or any(c.request_id == request_id for c in self.outbox)
                    or request_id in self.recent_request_ids):
                return
            self.recent_request_ids[request_id] = max(not_after, now + 3600)
            command = _Command(request_id, max(1, int(evt.get("timeout", 60))))
            self.active[request_id] = command
        # Its own thread from here on: the stream reader never waits for the
        # start acknowledgement, the launch, or the command itself.
        try:
            threading.Thread(
                target=self._run, args=(evt, command, not_after),
                daemon=True, name="mobius-connect-command",
            ).start()
        except RuntimeError as exc:
            self._post_result(command, "", "runner error: %s" % exc, 1, "lost")

    def _run(self, evt, command, not_after):
        if not_after and time.time() > not_after:
            self._post_result(command, "", "command expired before it could start",
                              124, "expired")
            return

        try:
            command.output = _CommandOutput()
            self._post_started(command.request_id)
            script = evt.get("script")
            if script is not None:
                command.stdin_text = _script_input(script)

            def before_spawn():
                # Do not hold the lifecycle lock across file preparation or
                # Popen: another request's deadline and cancel must progress.
                with self.lock:
                    if command.stop_reason is not None:
                        raise _StartRefused("canceled")
                    if not_after and time.time() > not_after:
                        raise _StartRefused("expired")

            if script is not None:
                proc, command_file = _spawn_command(
                    None, evt.get("cwd"), script=script,
                    shell=evt.get("shell"), before_spawn=before_spawn,
                )
            else:
                proc, command_file = _spawn_command(
                    evt.get("cmd", ""), evt.get("cwd"),
                    before_spawn=before_spawn,
                )
            with self.lock:
                # A cancel during Popen is retained in stop_reason; _wait
                # observes it as soon as the new process is available.
                command.proc = proc
                command.command_file = command_file
        except _StartRefused as refusal:
            expired = refusal.outcome == "expired"
            self._post_result(
                command, "", "command expired before it could start" if expired
                else "command canceled before it could start",
                124 if expired else 130, refusal.outcome,
            )
            return
        except Exception as exc:  # noqa: BLE001 - report the spawn boundary
            self._post_result(command, "", "runner error: %s" % exc, 1, "completed")
            return
        self._wait(command)

    def _wait(self, command):
        proc = command.proc
        output = command.output

        def stop_reason():
            with self.lock:
                return command.stop_reason

        try:
            ended_by = _supervise_process(
                proc,
                output,
                timeout=command.timeout,
                stdin_text=command.stdin_text,
                stop_reason=stop_reason,
                on_tick=lambda: self.flush_output(command),
            )
            error = None
        except Exception as exc:  # noqa: BLE001 - preserve a final result
            try:
                _terminate_process_tree(proc)
            except Exception as kill_exc:
                exc = RuntimeError("%s; cleanup failed: %s" % (exc, kill_exc))
            ended_by = stop_reason()
            error = "runner error: %s" % exc
        # Retain the final result first. The independent result worker drains
        # any remaining chunks before uploading it; a slow or failed drain
        # must never leave the command in active state indefinitely.
        stdout, stderr, truncated = output.final_streams()
        if error is not None:
            stderr = (stderr + "\n" if stderr else "") + error
        if ended_by == "timed_out":
            outcome, exit_code = "timed_out", 124
            stderr = stderr or "command timed out after %ss" % command.timeout
        elif ended_by is not None:
            outcome, exit_code = "canceled", 130
            stderr = stderr or "command canceled"
        else:
            outcome, exit_code = "completed", proc.returncode
        try:
            self._post_result(
                command, stdout, stderr, exit_code, outcome,
                truncated=truncated,
                output_seq=None if output.output_error else output.next_seq,
                output_error=output.output_error,
            )
        finally:
            if command.command_file is not None:
                try:
                    os.unlink(command.command_file)
                except FileNotFoundError:
                    pass

    def cancel(self, request_id, reason="canceled"):
        """Stop one command, or every command when request_id is None."""
        with self.lock:
            if request_id is None:
                commands = list(self.active.values())
            else:
                command = self.active.get(request_id)
                commands = [command] if command is not None else []
            for command in commands:
                if command.stop_reason is None:
                    command.stop_reason = reason
        # The watch loop observes the marked reason within one tick and kills
        # the process tree. Never make the control reader wait for that kill.
        return bool(commands)


def _handle_disconnect(conn, base, token, commands, request_id):
    """Stop this instance's commands, forget its connection, and confirm it."""
    commands.cancel(None, "disconnect")
    # Drop only this instance's connection. The shared runner keeps serving
    # any others; it uninstalls the whole service only when the last
    # connection is gone.
    try:
        remaining = _remove_connection(conn.get("url"), conn.get("host_id"))
        if remaining == 0:
            _uninstall_service(stop_running=False)
            stdout = "Connect daemon removed."
        else:
            stdout = "Disconnected from %s." % base
        payload = {
            "request_id": request_id, "stdout": stdout,
            "stderr": "", "exit_code": 0,
        }
    except Exception as exc:  # local cleanup failure
        payload = {
            "request_id": request_id, "stdout": "",
            "stderr": str(exc), "exit_code": 1,
        }
    try:
        _post(
            base + "/api/connect/result", payload,
            token=token, context=commands.context,
        )
    except urllib.error.URLError as exc:
        print("failed to report disconnect: %s" % exc)


def _serve_connection(conn, stop_event=None):
    base = _validated_base_url(conn["url"])
    token = conn["token"]
    ctx = ssl.create_default_context()
    plat = "%s %s" % (platform.system(), platform.release())
    commands = _CommandRunner(base, token, context=ctx)
    backoff = 1
    # Only a stream opened during the current attempt can establish health. Do
    # not let a healthy prior stream make a new handshake failure look
    # healthy when the transport raises before the next response opens.
    stream_opened_at = None

    def healthy():
        """The current stream stayed open past one heartbeat interval."""
        return (
            stream_opened_at is not None
            and time.monotonic() - stream_opened_at >= STREAM_HEALTHY_SECONDS
        )

    print("Connecting to %s ..." % base)
    while True:
        stream_opened_at = None
        # A connection removed from config (or a shutting-down supervisor) sets
        # this event; stop retrying and let this thread exit.
        if stop_event is not None and stop_event.is_set():
            return
        try:
            # Result and output uploads run separately from stream setup and
            # reading. Pending ids in the hello protect their retry lifecycle.
            if commands.pending_messages():
                commands._wake_result_worker()
            # A result discarded before opening the stream has already made
            # this upcoming hello the required reconciliation boundary.
            commands.take_reconcile_request()
            active_ids, pending_ids = commands.snapshot()
            query = [
                ("protocol", str(RUNNER_PROTOCOL_VERSION)),
                ("release", str(RUNNER_RELEASE)),
                ("platform", plat),
            ]
            query.extend(
                ("capability", capability)
                for capability in RUNNER_CAPABILITIES
            )
            if os.environ.get(SUPERVISOR_ENV) == "mobius":
                query.append(("managed", "mobius"))
            with _open_stream(
                base, token, query, active_ids, pending_ids, ctx,
            ) as stream:
                # Start the health window only after the response is open.
                # A slow DNS/TLS/HTTP handshake followed by an immediate EOF
                # is still an early failure and must retain retry backoff.
                stream_opened_at = time.monotonic()
                print("Connected. This machine is now reachable from Mobius.")
                if commands.take_reconcile_request():
                    print("reconnecting to reconcile a rejected result")
                    continue
                for raw in stream:
                    # Heartbeat comments make this retry path run even while
                    # the host has no new commands.
                    if commands.take_reconcile_request():
                        print("reconnecting to reconcile a rejected result")
                        break
                    if stop_event is not None and stop_event.is_set():
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line.startswith("data:"):
                        continue
                    try:
                        evt = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if evt.get("type") == "hello":
                        commands.live_output = bool(evt.get("live_output"))
                    elif evt.get("type") == "cancel":
                        commands.cancel(evt.get("request_id"))
                    elif evt.get("type") == "disconnect":
                        _handle_disconnect(
                            conn, base, token, commands, evt.get("request_id"),
                        )
                        return
                    elif evt.get("type") == "exec":
                        print("Starting command %s" % evt.get("request_id", ""))
                        commands.start(evt)
            if stop_event is not None and stop_event.is_set():
                return
            # Streams intentionally end before a hosting proxy's response cap.
            # A command belongs to this runner, not the stream, so clean
            # rotation is the same recovery path as any network loss.
            problem = None
        except KeyboardInterrupt:
            print("\nStopped.")
            return
        except urllib.error.HTTPError as exc:
            # 401/403 is transient during a deploy/restart, or a genuinely
            # revoked token. Either way keep retrying with backoff: a blip
            # self-heals and a real revocation simply idles here until the
            # owner re-pairs or removes this connection -- never a silent
            # permanent exit that abandons one instance while the others stay up.
            problem = "HTTP %s" % exc.code
        except socket.timeout:
            problem = "connection stalled"
        except (urllib.error.URLError, http.client.HTTPException) as exc:
            problem = "connection lost (%s)" % exc
        if healthy():
            # The stream outlived its health window, so whatever ended it is a
            # new loss: clear earlier failures and recover with a short retry.
            backoff = 1
            if problem is None:
                print("stream rotated; reconnecting")
                continue
        print("%s; retrying in %ss" % (problem or "stream ended early", backoff))
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)


def _serve_connection_safe(conn, stop_event=None):
    """Run one connection's loop so its failure never stops the others."""
    try:
        _serve_connection(conn, stop_event)
    except Exception as exc:  # noqa: BLE001 - one connection must not crash all
        print("connection to %s stopped: %s" % (conn.get("url"), exc))


def _serve_all(background_service=False):
    """Serve every paired connection from this one runner process.

    Supervises each connection independently: one live thread per configured
    connection, respawned if it exits, and signalled to stop and dropped when
    it is removed from config. A single connection dropping can never leave
    that instance dark while the others keep running; the process exits only
    when the last connection is gone or on an explicit stop."""
    if not _connections():
        print("Not paired. Run with --pair CODE --url URL first.")
        sys.exit(2)
    workers = {}  # key -> (thread, stop_event)
    try:
        while True:
            if background_service and _background_pid() not in (None, os.getpid()):
                print("A newer background runner took over; exiting.")
                break
            conns = _connections()
            if not conns:
                break
            live_keys = set()
            for conn in conns:
                key = conn.get("host_id") or conn.get("url")
                live_keys.add(key)
                worker = workers.get(key)
                if worker is None or not worker[0].is_alive():
                    stop_event = threading.Event()
                    thread = threading.Thread(
                        target=_serve_connection_safe,
                        args=(conn, stop_event), daemon=True,
                        name="mobius-connect-%s" % (key or "unknown"),
                    )
                    thread.start()
                    workers[key] = (thread, stop_event)
            for key in [k for k in workers if k not in live_keys]:
                thread, stop_event = workers[key]
                # Signal a connection removed from config to stop, then drop it
                # once its worker has actually exited.
                stop_event.set()
                if not thread.is_alive():
                    workers.pop(key, None)
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopped.")


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
        # Uninstalling the machine's runner revokes every connection it holds.
        for conn in _connections():
            _revoke_server(conn)
        _uninstall_service()
        return

    conns = _connections()
    try:
        if args.pair:
            base = args.url or (conns[0]["url"] if conns else "")
            if not base:
                print("Missing --url")
                sys.exit(2)
            install_base = _pair(base, args.pair.strip())["url"]
        else:
            if not conns:
                print("Not paired. Run with --pair CODE --url URL first.")
                sys.exit(2)
            # An update can be started by any paired Möbius. Honor the
            # explicitly selected source instead of always downloading from
            # the first saved connection, which may be an older instance.
            install_base = _validated_base_url(args.url or conns[0]["url"])
    except ValueError as exc:
        print(str(exc))
        sys.exit(2)

    if args.install:
        # One shared service serves all connections; installing it again after
        # pairing another instance simply refreshes the runner and restarts it.
        _install_service(install_base)
        return

    if args.background_service:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if sys.stdout is None or sys.stderr is None:
            # Started at Windows sign-in by pythonw.exe, which has no console:
            # keep the same log the installer points to.
            log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stdout or log
            sys.stderr = sys.stderr or log
        with open(PID_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    try:
        _serve_all(background_service=args.background_service)
    finally:
        # A runner that handed over leaves its successor's PID file in place.
        if args.background_service and _background_pid() == os.getpid():
            _remove_file(PID_PATH)


if __name__ == "__main__":
    main()
