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
  monkeypatch.setattr(git_ops, "_run_cmd",
                      lambda argv, *, env, **_k: seen.setdefault(argv[3] if argv[0] == "git" else "gh", env))
  for command in ("status", "rev-parse", "update-ref", "diff", "fetch", "push", "ls-remote"):
    git_ops._git(checkout, command)
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


def test_a_checkout_pre_push_hook_never_runs_and_the_push_lands(checkout, tmp_path):
  script, marker = probe(tmp_path, "pre-push")
  hook = checkout / ".git" / "hooks" / "pre-push"
  hook.write_bytes(script.read_bytes())
  hook.chmod(0o755)
  remote = tmp_path / "remote.git"
  subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
  plain_git(checkout, "push", "-q", str(remote), "HEAD:refs/heads/probe")
  assert marker.exists()
  marker.unlink()

  pushed = git_ops._git(checkout, "push", str(remote), "HEAD:refs/heads/topic", check=False)
  assert pushed.returncode == 0, pushed.stderr
  assert not marker.exists()


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
