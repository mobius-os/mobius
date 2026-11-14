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
  git_ops._git(checkout, "push", "origin", "HEAD:refs/heads/topic")  # the network step
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


def head_of(repo: Path) -> str:
  return plain_git(repo, "rev-parse", "HEAD").stdout.strip()


def branch_at(remote: Path, ref: str = "refs/heads/topic") -> str | None:
  found = plain_git(remote, "rev-parse", "--verify", "--quiet", ref)
  return found.stdout.strip() if found.returncode == 0 else None


def test_the_pre_push_gate_runs_once_without_credentials_then_the_push_lands(
  checkout, tmp_path,
):
  record = tmp_path / "gate"
  install_hook(checkout, f'#!/bin/sh\n{{ echo "$@"; cat; env; }} >> {record}\n')
  remote = bare_remote(tmp_path)
  head = head_of(checkout)

  pushed = git_ops.push(checkout, str(remote), head, "refs/heads/topic", lease="")

  assert pushed.returncode == 0, pushed.stderr
  seen = record.read_text()
  # Exactly once: the network push itself never runs the hook.
  assert seen.count(f"{remote} {remote}\n") == 1
  assert f"{head} {head} refs/heads/topic {'0' * 40}\n" in seen
  assert "GH_TOKEN" not in seen and "GH_CONFIG_DIR" not in seen
  assert branch_at(remote) == head


def test_a_commit_made_during_the_gate_is_not_what_gets_pushed(checkout, tmp_path):
  install_hook(checkout, "#!/bin/sh\n"
               "git -c user.name=x -c user.email=x@x commit -q --allow-empty -m unreviewed\n")
  remote = bare_remote(tmp_path)
  reviewed = head_of(checkout)

  assert git_ops.push(checkout, str(remote), reviewed, "refs/heads/topic", lease="").returncode == 0

  assert head_of(checkout) != reviewed  # the gate did commit
  assert branch_at(remote) == reviewed


def test_a_refusing_gate_stops_the_push_with_its_output(checkout, tmp_path):
  install_hook(checkout, "#!/bin/sh\necho '[pre-push] verdict=blocked cause=checks checks=x' >&2\nexit 1\n")
  remote = bare_remote(tmp_path)

  with pytest.raises(git_ops.PushGateRefused) as refused:
    git_ops.push(checkout, str(remote), head_of(checkout), "refs/heads/topic", lease="")

  assert "verdict=blocked" in refused.value.detail
  assert branch_at(remote) is None


def test_a_gate_that_does_not_finish_is_refused_not_ambiguous(checkout, tmp_path, monkeypatch):
  install_hook(checkout, "#!/bin/sh\nsleep 30\n")
  monkeypatch.setattr(git_ops, "_PUSH_TIMEOUT", 1)
  remote = bare_remote(tmp_path)

  with pytest.raises(git_ops.PushGateRefused):
    git_ops.push(checkout, str(remote), head_of(checkout), "refs/heads/topic", lease="")
  assert branch_at(remote) is None


def test_the_gates_process_group_is_killed_when_it_ends(checkout, tmp_path):
  pid_file = tmp_path / "child"
  install_hook(checkout, f"#!/bin/sh\nsleep 60 &\necho $! > {pid_file}\n")

  git_ops.push(checkout, str(bare_remote(tmp_path)), head_of(checkout), "refs/heads/topic", lease="")

  stat = Path(f"/proc/{pid_file.read_text().strip()}/stat")
  assert not stat.exists() or stat.read_text().split(") ", 1)[1].startswith("Z")


def test_a_hook_without_an_interpreter_line_runs_through_sh_like_git(checkout, tmp_path):
  marker = tmp_path / "ran"
  install_hook(checkout, f"touch {marker}\n")
  remote = bare_remote(tmp_path)

  assert git_ops.push(checkout, str(remote), head_of(checkout), "refs/heads/topic", lease="").returncode == 0
  assert marker.exists()


def test_a_gate_that_cannot_start_is_refused_not_ambiguous(checkout, tmp_path):
  install_hook(checkout, "#!/nonexistent/interpreter\n")
  remote = bare_remote(tmp_path)

  with pytest.raises(git_ops.PushGateRefused):
    git_ops.push(checkout, str(remote), head_of(checkout), "refs/heads/topic", lease="")
  assert branch_at(remote) is None


def test_the_gate_cannot_find_gh_credentials_in_the_home(checkout, tmp_path, monkeypatch):
  home = tmp_path / "home"
  (home / ".config" / "gh").mkdir(parents=True)
  (home / ".config" / "gh" / "hosts.yml").write_text(
    "github.com:\n    oauth_token: home-token\n    user: probe\n")
  monkeypatch.setenv("HOME", str(home))
  monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
  monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
  record = tmp_path / "found"
  reader = tmp_path / "reader"
  reader.write_text(f"#!/bin/sh\n{{ cat \"$HOME/.config/gh/hosts.yml\"; gh auth token; }} > {record} 2>&1\nexit 0\n")
  reader.chmod(0o755)
  subprocess.run([str(reader)], check=False)
  assert "home-token" in record.read_text()
  install_hook(checkout, reader.read_text())

  git_ops.push(checkout, str(bare_remote(tmp_path)), head_of(checkout), "refs/heads/topic", lease="")

  assert "home-token" not in record.read_text()


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

  with pytest.raises(git_ops.PushGateRefused) as refused:
    git_ops.push(checkout, str(remote), head_of(checkout), "refs/heads/topic", lease="")

  assert "verdict=blocked cause=privacy" in refused.value.detail
  assert "GH_TOKEN" not in record.read_text()
  assert branch_at(remote) is None


# -- checkout config while the token is present -------------------------------

@pytest.fixture
def https_only(monkeypatch):
  monkeypatch.setattr(git_ops, "_ALLOWED_PROTOCOLS", "https")


def refused_names(checkout: Path) -> str:
  with pytest.raises(git_ops.UnapprovedCheckoutConfig) as refused:
    git_ops._git(checkout, "fetch", "https://github.com/owner/repo.git")
  return refused.value.message


def test_a_contribute_checkout_passes_the_config_allowlist(checkout, tmp_path):
  for key, value in (
    ("remote.origin.url", "https://github.com/owner/repo.git"),
    ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
    ("remote.origin.gh-resolved", "base"),
    ("branch.fix/v1.2.remote", "origin"), ("branch.fix/v1.2.merge", "refs/heads/fix/v1.2"),
    ("branch.fix/v1.2.pushRemote", "fork"),
    ("user.name", "x"), ("user.email", "x@x"),
    ("core.hooksPath", str(checkout / ".git" / "hooks")),
    ("extensions.worktreeConfig", "true"),
  ):
    plain_git(checkout, "config", "--local", key, value, check=True)
  review = tmp_path / "review"
  plain_git(checkout, "worktree", "add", "-q", str(review), check=True)
  plain_git(review, "config", "--worktree", "core.sparseCheckout", "true", check=True)
  plain_git(review, "config", "--worktree", "core.sparseCheckoutCone", "true", check=True)

  for repo in (checkout, review):
    assert git_ops.unapproved_config(repo, git_ops._CONTRIBUTION_CHECKOUT_CONFIG) is None


def test_an_alternate_refs_command_never_runs_during_fetch(checkout, tmp_path):
  script, marker = probe(tmp_path, "alternate-refs")
  other = bare_remote(tmp_path)
  (checkout / ".git" / "objects" / "info" / "alternates").write_text(f"{other}/objects\n")
  plain_git(checkout, "config", "--local", "core.alternateRefsCommand", str(script), check=True)
  source = tmp_path / "source.git"
  subprocess.run(["git", "clone", "-q", "--bare", str(checkout), str(source)], check=True)
  plain_git(checkout, "fetch", "-q", str(source))
  assert marker.exists()
  marker.unlink()

  assert "core.alternaterefscommand" in refused_names(checkout)
  assert not marker.exists()


def test_a_push_signing_program_never_runs_during_push(checkout, tmp_path):
  script, marker = probe(tmp_path, "gpg")
  remote = bare_remote(tmp_path)
  plain_git(remote, "config", "receive.certNonceSeed", "probe", check=True)
  plain_git(checkout, "config", "--local", "push.gpgSign", "if-asked", check=True)
  plain_git(checkout, "config", "--local", "gpg.program", str(script), check=True)
  plain_git(checkout, "-c", "user.name=x", "-c", "user.email=x@x",
            "push", "-q", str(remote), "HEAD:refs/heads/probe")
  assert marker.exists()
  marker.unlink()

  with pytest.raises(git_ops.UnapprovedCheckoutConfig) as refused:
    git_ops.push(checkout, str(remote), head_of(checkout), "refs/heads/topic", lease="")
  assert "push.gpgsign" in refused.value.message
  assert not marker.exists()
  assert branch_at(remote) is None


@pytest.mark.parametrize("key, value", [
  ("url.ext::{script}.insteadOf", "https://github.com/owner/repo.git"),
  ("remote.origin.uploadpack", "{script}"),
  ("remote.origin.receivepack", "{script}"),
  # Git cannot even read config that includes a non-config file: refused too.
  ("include.path", "{script}"),
])
def test_other_program_naming_settings_are_refused_before_any_network_command(
  checkout, tmp_path, key, value,
):
  script, _marker = probe(tmp_path, "named")
  plain_git(checkout, "config", "--local", key.format(script=script), value.format(script=script),
            check=True)
  message = refused_names(checkout)
  assert key.split(".")[0] in message or "unreadable" in message


def test_gh_in_a_checkout_follows_the_same_config_rule(checkout, tmp_path):
  plain_git(checkout, "config", "--local", "core.alternateRefsCommand", "true", check=True)
  with pytest.raises(git_ops.UnapprovedCheckoutConfig):
    git_ops._gh(checkout, "repo", "view")


def test_only_https_transport_is_allowed(checkout, tmp_path, https_only):
  fetched = git_ops._git(checkout, "fetch", str(bare_remote(tmp_path)), check=False)
  assert fetched.returncode != 0
  assert "transport 'file' not allowed" in fetched.stderr


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
