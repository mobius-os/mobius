#!/usr/bin/env python3
"""Exercise the installer's actual units on a disposable GitHub-hosted runner.

The worker is a deterministic stand-in; systemd, the watcher, and timer are
real. No Docker commands, application data, or existing Mobius units are used;
the generated services retain their dependency on the runner's Docker daemon.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import time


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15)


def main():
    if os.geteuid() != 0 or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("run only as root on a disposable GitHub-hosted runner")
    prefix = f"mobius-recovery-test-{os.getpid()}"
    names = [f"{prefix}.service", f"{prefix}.path",
             f"{prefix}-reconcile.service", f"{prefix}-reconcile.timer"]
    installed = []
    with tempfile.TemporaryDirectory(prefix="recovery-units-") as directory:
        root = Path(directory)
        worker = root / "worker"
        data = root / "data"
        inbox = data / prefix / "inbox"
        inbox.mkdir(parents=True)
        transaction = root / "transaction"
        transaction.touch()
        worker.write_text(f'''#!/usr/bin/python3
import pathlib, sys
root = pathlib.Path({str(root)!r})
request = pathlib.Path({str(inbox / "request.json")!r})
with (root / "calls").open("a") as log:
    log.write(sys.argv[1] + "\\n")
if sys.argv[1] == "run":
    if not (root / "transaction").exists() and request.exists():
        request.unlink()
        (root / "dispatched").touch()
elif (root / "allow-recovery").exists():
    (root / "transaction").unlink(missing_ok=True)
    (root / "reconciled").touch()
''')
        worker.chmod(0o755)
        source = (Path(__file__).parent / "install-rebuild-helper.sh").read_text()
        start = source.index("cat >/etc/systemd/system/mobius-rebuild.service")
        end = source.index("chmod 0644 /etc/systemd/system/mobius-rebuild.service", start)
        units = source[start:end].replace("/usr/local/libexec/mobius-rebuild-host", str(worker))
        units = units.replace("/etc/systemd/system/", f"{root}/").replace("mobius-rebuild", prefix)
        # Scale only the timer interval; the Python contract test checks the
        # installed production budget, and systemd's dispatch semantics remain real.
        # Four seconds also stays below the normal 5-starts/10-seconds service
        # limit; a one-second test timer would itself exhaust that limit.
        units = units.replace("OnBootSec=30", "OnBootSec=4").replace("OnUnitInactiveSec=30", "OnUnitInactiveSec=4")
        subprocess.run(["bash", "-eu", "-c", units], check=True, timeout=10,
                       env={**os.environ, "DATA_SOURCE": str(data)})
        try:
            for name in names:
                destination = Path("/run/systemd/system") / name
                if destination.exists() or destination.is_symlink():
                    raise RuntimeError(f"refusing to replace existing unit {name}")
                destination.symlink_to(root / name)
                installed.append(destination)
            command("systemctl", "daemon-reload")
            command("systemctl", "start", f"{prefix}.path", f"{prefix}-reconcile.timer")
            (inbox / "request.json").write_text("queued behind recovery")
            time.sleep(8)
            assert not (root / "dispatched").exists(), "request bypassed recovery ownership"
            assert (inbox / "request.json").exists(), "queued request was lost"
            assert command("systemctl", "is-active", f"{prefix}.path").stdout.strip() == "active"
            assert len((root / "calls").read_text().splitlines()) < 20, "watcher is spinning"
            (root / "allow-recovery").touch()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not (root / "dispatched").exists():
                time.sleep(0.2)
            assert (root / "reconciled").exists(), "timer did not retry reconciliation"
            assert (root / "dispatched").exists(), "timer did not dispatch the pending request"
            assert not transaction.exists()
            assert command("systemctl", "is-active", f"{prefix}.path").stdout.strip() == "active"
            print("recovery units: pending request survives recovery and dispatches without watcher failure")
        finally:
            owned = [path.name for path in installed]
            try:
                if owned:
                    subprocess.run(["systemctl", "stop", *owned], check=False, timeout=30,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            finally:
                for path in installed:
                    path.unlink()
                command("systemctl", "daemon-reload")
                if owned:
                    subprocess.run(["systemctl", "reset-failed", *owned], check=False, timeout=10,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
