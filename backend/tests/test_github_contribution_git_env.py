"""Contribution Git never runs a program the checkout names, and only commands
that talk to GitHub carry its credentials.

Each probe first shows it does run under plain Git, so a pass means the
contribution environment stopped it rather than the probe never firing.
"""
import os
import subprocess
from pathlib import Path

import pytest

from app import github_auth
from app import github_contribution_git as git_ops


def plain_git(repo: Path, *args: str, **kwargs) -> subprocess.CompletedProcess:
  return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True,
                        env=dict(os.environ), timeout=30, **kwargs)


def probe(tmp_path: Path, name: str) -> tuple[Path, Path]:
  """A script that records the environment it ran with."""
  marker = tmp_path / f"{name}-ran"
  script = tmp_path / name
  script.write_text(f"#!/bin/sh\nenv > {marker}\necho probe\n")
  script.chmod(0o755)
  return script, marker


@pytest.fixture
def checkout(tmp_path, monkeypatch):
  auth = tmp_path / "gh-auth"
  auth.mkdir()
  monkeypatch.setattr(github_auth, "get_token", lambda: "probe-token")
  monkeypatch.setattr(github_auth, "GH_AUTH_DIR", auth)
  monkeypatch.setenv("GH_TOKEN", "inherited-token")
  repo = tmp_path / "repo"
  subprocess.run(["git", "init", "-q", str(repo)], check=True)
  plain_git(repo, "-c", "user.name=x", "-c", "user.email=x@x",
            "commit", "-q", "--allow-empty", "-m", "c", check=True)
  return repo


def test_only_network_commands_carry_github_credentials(checkout, monkeypatch):
  seen = {}

  def run(argv, *, env, **_k):
    seen.setdefault(argv[3] if argv[0] == "git" else "gh", env)
    return subprocess.CompletedProcess(argv, 0, "", "")

  monkeypatch.setattr(git_ops, "_run_cmd", run)
  for command in ("status", "rev-parse", "update-ref", "diff", "fetch", "ls-remote"):
    git_ops._git(checkout, command)
  git_ops._git(checkout, "push", "origin", "HEAD:refs/heads/topic")
  git_ops._gh(checkout, "api", "user")
  for local in ("status", "rev-parse", "update-ref", "diff"):
    assert "GH_TOKEN" not in seen[local] and "GH_CONFIG_DIR" not in seen[local], local
  for network in ("fetch", "push", "ls-remote", "gh"):
    assert seen[network]["GH_TOKEN"] == "probe-token", network
    assert seen[network]["GH_CONFIG_DIR"] == str(github_auth.GH_AUTH_DIR)
  assert "GH_TOKEN" not in git_ops._git_env(checkout)


def test_a_checkout_fsmonitor_never_runs(checkout, tmp_path):
  script, marker = probe(tmp_path, "fsmonitor")
  plain_git(checkout, "config", "--local", "core.fsmonitor", str(script), check=True)
  plain_git(checkout, "status", "--porcelain")
  assert marker.exists()
  marker.unlink()

  git_ops._git(checkout, "status", "--porcelain")
  assert not marker.exists()


def install_hook(repo: Path, body: str) -> None:
  hook = repo / ".git" / "hooks" / "pre-push"
  hook.write_text(body)
  hook.chmod(0o755)


def bare_remote(tmp_path: Path) -> Path:
  remote = tmp_path / "remote.git"
  subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
  return remote


def test_the_pre_push_gate_runs_once_without_credentials_then_the_push_lands(
  checkout, tmp_path,
):
  record = tmp_path / "gate"
  install_hook(checkout, f'#!/bin/sh\n{{ echo "$@"; cat; env; }} >> {record}\n')
  remote = bare_remote(tmp_path)
  head = plain_git(checkout, "rev-parse", "HEAD").stdout.strip()

  pushed = git_ops._git(checkout, "push", "--force-with-lease=refs/heads/topic:",
                        str(remote), "HEAD:refs/heads/topic", check=False)

  assert pushed.returncode == 0, pushed.stderr
  seen = record.read_text()
  # Exactly once: the network push itself never runs the hook.
  assert seen.count(f"{remote} {remote}\n") == 1
  assert f"HEAD {head} refs/heads/topic {'0' * 40}\n" in seen
  assert "GH_TOKEN" not in seen and "GH_CONFIG_DIR" not in seen
  assert plain_git(remote, "rev-parse", "refs/heads/topic").stdout.strip() == head


def test_a_refusing_gate_stops_the_push_with_its_output(checkout, tmp_path):
  install_hook(checkout, "#!/bin/sh\necho '[pre-push] verdict=blocked cause=checks checks=x' >&2\nexit 1\n")
  remote = bare_remote(tmp_path)

  refused = git_ops._git(checkout, "push", str(remote), "HEAD:refs/heads/topic", check=False)

  assert refused.returncode != 0
  assert "verdict=blocked" in refused.stderr
  assert plain_git(remote, "rev-parse", "--verify", "--quiet", "refs/heads/topic").returncode != 0


def test_the_platform_gate_refuses_a_reverted_private_path_without_the_token(
  checkout, tmp_path,
):
  gate = Path(__file__).resolve().parents[2] / "scripts" / "githooks" / "pre-push"
  record = tmp_path / "gate-env"
  install_hook(checkout, f'#!/bin/sh\nenv > {record}\nexec {gate} "$@"\n')
  commit = ("-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "-m")
  (checkout / "docs").mkdir()
  (checkout / "docs" / "notes.md").write_text("private\n")
  plain_git(checkout, "add", "docs", check=True)
  plain_git(checkout, *commit, "add notes", check=True)
  plain_git(checkout, "rm", "-q", "-r", "docs", check=True)
  plain_git(checkout, *commit, "remove notes", check=True)
  remote = bare_remote(tmp_path)

  refused = git_ops._git(checkout, "push", str(remote), "HEAD:refs/heads/topic", check=False)

  assert refused.returncode != 0
  assert "verdict=blocked cause=privacy" in refused.stderr
  assert "GH_TOKEN" not in record.read_text()
  assert plain_git(remote, "rev-parse", "--verify", "--quiet", "refs/heads/topic").returncode != 0


@pytest.mark.parametrize("key", ["credential.helper", "core.askPass"])
def test_a_checkout_credential_program_never_sees_the_token(checkout, tmp_path, monkeypatch, key):
  script, marker = probe(tmp_path, "credential")
  value = f"!{script}" if key == "credential.helper" else str(script)
  plain_git(checkout, "config", "--local", key, value, check=True)
  # An inherited askpass must not run either.
  monkeypatch.setenv("GIT_ASKPASS", str(script))
  monkeypatch.setenv("SSH_ASKPASS", str(script))
  monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
  request = "protocol=https\nhost=example.invalid\n\n"
  plain_git(checkout, "credential", "fill", input=request)
  assert marker.exists()
  marker.unlink()

  env = git_ops._git_env(checkout, network=True)
  # The fixed GitHub helper is the only one Git consults.
  helpers = subprocess.run(["git", "-C", str(checkout), "config", "--get-all", "credential.helper"],
                           env=env, text=True, capture_output=True).stdout.splitlines()
  assert helpers[-2:] == ["", "!gh auth git-credential"]
  subprocess.run(["git", "-C", str(checkout), "credential", "fill"], input=request,
                 env=env, text=True, capture_output=True, timeout=30)
  assert not marker.exists()
