"""Contribution Git carries the GitHub token, so it never runs a prompt program."""
import subprocess

from app import github_contribution_git as git_ops


def test_contribution_git_never_runs_an_askpass_program(tmp_path, monkeypatch):
  marker = tmp_path / "askpass-ran"
  askpass = tmp_path / "askpass"
  askpass.write_text(f"#!/bin/sh\nenv > {marker}\necho probe\n")
  askpass.chmod(0o755)
  repo = tmp_path / "repo"
  subprocess.run(["git", "init", "-q", str(repo)], check=True)
  # A checkout's own config and the inherited environment both name one.
  subprocess.run(["git", "-C", str(repo), "config", "--local", "core.askPass", str(askpass)],
                 check=True)
  monkeypatch.setenv("GIT_ASKPASS", str(askpass))
  monkeypatch.setenv("SSH_ASKPASS", str(askpass))
  monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
  monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
  monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

  env = git_ops._git_env(repo)
  # With no credential helper, Git asks for the username and password.
  result = subprocess.run(
    ["git", "-C", str(repo), "credential", "fill"],
    input="protocol=https\nhost=example.invalid\n\n",
    env=env, capture_output=True, text=True, timeout=30, check=False,
  )

  assert result.returncode != 0
  assert not marker.exists()
  assert env["GIT_TERMINAL_PROMPT"] == "0"
