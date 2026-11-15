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

pytestmark = pytest.mark.usefixtures("allow_local_test_remotes")


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


def test_only_network_commands_carry_github_credentials(checkout, tmp_path, monkeypatch):
  seen = {}

  def run(argv, *, cwd, env, **_k):
    command = argv[3] if argv[0] == "git" else "gh"
    if command not in ("init", "update-ref", "rev-parse") or str(cwd) == str(checkout):
      seen.setdefault(command, (Path(cwd), env))
    return subprocess.CompletedProcess(argv, 0, "", "")

  monkeypatch.setattr(git_ops, "_run_cmd", run)
  for command in ("status", "rev-parse", "update-ref", "diff"):
    git_ops._git(checkout, command)
  git_ops._git(checkout, "fetch", "https://github.com/owner/repo.git", "+refs/heads/main:refs/x")
  git_ops._git(checkout, "ls-remote", "https://github.com/owner/repo.git")
  git_ops._git(checkout, "push", "https://github.com/owner/repo.git", "HEAD:refs/heads/topic")
  git_ops._gh(checkout, "api", "user")
  for local in ("status", "rev-parse", "update-ref", "diff"):
    _cwd, env = seen[local]
    assert "GH_TOKEN" not in env and "GH_CONFIG_DIR" not in env, local
  for network in ("fetch", "push", "ls-remote", "gh"):
    cwd, env = seen[network]
    assert env["GH_TOKEN"] == "probe-token", network
    assert env["GH_CONFIG_DIR"] == str(github_auth.GH_AUTH_DIR)
    # Never in the agent-writable checkout.
    assert not str(cwd).startswith(str(checkout)), network
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


def test_a_checkout_hooks_path_runs_that_hook_as_git_would(checkout, tmp_path):
  hooks = tmp_path / "elsewhere"
  hooks.mkdir()
  (hooks / "pre-push").write_text("#!/bin/sh\necho '[pre-push] verdict=blocked cause=checks checks=x' >&2\nexit 1\n")
  (hooks / "pre-push").chmod(0o755)
  plain_git(checkout, "config", "--local", "core.hooksPath", str(hooks), check=True)
  remote = bare_remote(tmp_path)
  assert plain_git(checkout, "push", str(remote), "HEAD:refs/heads/probe").returncode != 0

  with pytest.raises(git_ops.PushGateRefused):
    git_ops.push(checkout, str(remote), head_of(checkout), "refs/heads/topic", lease="")
  assert branch_at(remote) is None


def test_a_relative_hooks_path_starts_at_the_worktree_root(checkout, tmp_path):
  marker = tmp_path / "ran"
  (checkout / "hooks").mkdir()
  (checkout / "hooks" / "pre-push").write_text(f"#!/bin/sh\ntouch {marker}\n")
  (checkout / "hooks" / "pre-push").chmod(0o755)
  plain_git(checkout, "config", "--local", "core.hooksPath", "hooks", check=True)

  git_ops.push(checkout, str(bare_remote(tmp_path)), head_of(checkout), "refs/heads/topic", lease="")
  assert marker.exists()


def test_a_hooks_path_that_skips_the_common_gate_is_refused(checkout, tmp_path):
  install_hook(checkout, "#!/bin/sh\nexit 0\n")
  (tmp_path / "empty-hooks").mkdir()
  plain_git(checkout, "config", "--local", "core.hooksPath", str(tmp_path / "empty-hooks"), check=True)
  remote = bare_remote(tmp_path)

  with pytest.raises(git_ops.PushGateRefused) as refused:
    git_ops.push(checkout, str(remote), head_of(checkout), "refs/heads/topic", lease="")
  assert "hooks away" in refused.value.detail
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


# -- token-bearing Git never reads the checkout ---------------------------------

@pytest.fixture
def https_only(monkeypatch):
  monkeypatch.setattr(git_ops, "_ALLOWED_PROTOCOLS", "https")


def source_repo(tmp_path: Path, checkout: Path) -> Path:
  """A stand-in for GitHub one commit ahead of the checkout."""
  source = tmp_path / "source.git"
  subprocess.run(["git", "clone", "-q", "--bare", str(checkout), str(source)], check=True)
  work = tmp_path / "ahead"
  subprocess.run(["git", "clone", "-q", str(source), str(work)], check=True)
  plain_git(work, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q",
            "--allow-empty", "-m", "ahead", check=True)
  plain_git(work, "push", "-q", "origin", "HEAD:refs/heads/ahead", check=True)
  return source


def test_fetch_hands_the_fetched_ref_to_the_checkout_and_a_linked_worktree(checkout, tmp_path):
  source = source_repo(tmp_path, checkout)
  ahead = plain_git(source, "rev-parse", "refs/heads/ahead").stdout.strip()
  review = tmp_path / "review"
  plain_git(checkout, "worktree", "add", "-q", str(review), check=True)

  for repo, ref in ((checkout, "refs/mobius-submit/a"), (review, "refs/mobius-submit/b")):
    git_ops._git(repo, "fetch", "--no-tags", "--force", str(source),
                 f"+refs/heads/ahead:{ref}")
    assert plain_git(repo, "rev-parse", ref).stdout.strip() == ahead
    assert plain_git(repo, "cat-file", "-e", f"{ahead}^{{commit}}").returncode == 0


def test_push_from_a_linked_worktree_lands_the_exact_commit(checkout, tmp_path):
  review = tmp_path / "review"
  plain_git(checkout, "worktree", "add", "-q", str(review), check=True)
  plain_git(review, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q",
            "--allow-empty", "-m", "reviewed", check=True)
  remote = bare_remote(tmp_path)

  pushed = git_ops.push(review, str(remote), head_of(review), "refs/heads/topic", lease="")
  assert pushed.returncode == 0, pushed.stderr
  assert branch_at(remote) == head_of(review)


def test_an_alternate_refs_command_never_runs_with_the_token(checkout, tmp_path):
  script, marker = probe(tmp_path, "alternate-refs")
  (checkout / ".git" / "objects" / "info" / "alternates").write_text(
    f"{bare_remote(tmp_path)}/objects\n")
  plain_git(checkout, "config", "--local", "core.alternateRefsCommand", str(script), check=True)
  source = source_repo(tmp_path, checkout)
  plain_git(checkout, "fetch", "-q", str(source))
  assert marker.exists()
  marker.unlink()

  git_ops._git(checkout, "fetch", "--no-tags", str(source), "+refs/heads/ahead:refs/x")
  assert "probe-token" not in (marker.read_text() if marker.exists() else "")


def test_a_racing_config_change_never_reaches_the_token(checkout, tmp_path):
  """The checkout's config is never read by the process that holds the token."""
  import threading

  script, marker = probe(tmp_path, "race")
  (checkout / ".git" / "objects" / "info" / "alternates").write_text(
    f"{bare_remote(tmp_path)}/objects\n")
  source = source_repo(tmp_path, checkout)
  done = threading.Event()

  def flip():
    while not done.is_set():
      plain_git(checkout, "config", "--local", "core.alternateRefsCommand", str(script))
      plain_git(checkout, "config", "--local", "--unset", "core.alternateRefsCommand")

  flipper = threading.Thread(target=flip)
  flipper.start()
  try:
    for attempt in range(20):
      git_ops._git(checkout, "fetch", "--no-tags", "--force", str(source),
                   f"+refs/heads/ahead:refs/race/{attempt}", check=False)
  finally:
    done.set()
    flipper.join()
  assert "probe-token" not in (marker.read_text() if marker.exists() else "")


def test_a_push_signing_program_never_runs_and_the_push_lands(checkout, tmp_path):
  script, marker = probe(tmp_path, "gpg")
  remote = bare_remote(tmp_path)
  plain_git(remote, "config", "receive.certNonceSeed", "probe", check=True)
  plain_git(checkout, "config", "--local", "push.gpgSign", "if-asked", check=True)
  plain_git(checkout, "config", "--local", "gpg.program", str(script), check=True)
  plain_git(checkout, "-c", "user.name=x", "-c", "user.email=x@x",
            "push", "-q", str(remote), "HEAD:refs/heads/probe")
  assert marker.exists()
  marker.unlink()

  pushed = git_ops.push(checkout, str(remote), head_of(checkout), "refs/heads/topic", lease="")
  assert pushed.returncode == 0, pushed.stderr
  assert not marker.exists()
  assert branch_at(remote) == head_of(checkout)


@pytest.mark.parametrize("key, value", [
  ("url.ext::{script}.insteadOf", "{source}"),
  ("remote.origin.uploadpack", "{script}"),
])
def test_fetch_ignores_the_checkouts_transport_settings(checkout, tmp_path, key, value):
  script, marker = probe(tmp_path, "named")
  source = source_repo(tmp_path, checkout)
  plain_git(checkout, "remote", "add", "origin", str(source), check=True)
  plain_git(checkout, "config", "--local", "protocol.ext.allow", "always", check=True)
  plain_git(checkout, "config", "--local", key.format(script=script),
            value.format(script=script, source=source), check=True)
  plain_git(checkout, "fetch", "-q", str(source) if "url." in key else "origin")
  assert marker.exists()
  marker.unlink()

  git_ops._git(checkout, "fetch", "--no-tags", str(source), "+refs/heads/ahead:refs/x")
  assert not marker.exists()


def test_network_git_never_reads_a_remote_name_or_a_bare_fetch(checkout, tmp_path):
  """Remote settings live in the checkout's config, so callers pass URLs and
  name every destination ref; a remote name or a FETCH_HEAD-only fetch is refused."""
  script, marker = probe(tmp_path, "receive-pack")
  remote = bare_remote(tmp_path)
  plain_git(checkout, "remote", "add", "rp", str(remote), check=True)
  plain_git(checkout, "config", "--local", "remote.rp.receivepack", str(script), check=True)

  with pytest.raises(git_ops.ContributionSubmitError, match="must name its URL"):
    git_ops.push(checkout, "rp", head_of(checkout), "refs/heads/topic", lease="")
  with pytest.raises(git_ops.ContributionSubmitError, match="destination ref"):
    git_ops._git(checkout, "fetch", str(remote), "refs/heads/main")
  assert not marker.exists()


def test_the_verified_fork_url_is_pushed_even_if_the_gate_rewrites_the_remote(
  checkout, tmp_path, monkeypatch,
):
  from app import github_contributions

  plain_git(checkout, "remote", "add", "fork", "https://github.com/octocat/repo.git", check=True)
  install_hook(checkout, "#!/bin/sh\ngit remote set-url fork https://github.com/evil/repo.git\n")
  sent = []
  monkeypatch.setattr(git_ops, "_send_push", lambda repo, remote, *rest: (
    sent.append(remote) or subprocess.CompletedProcess([], 0, "", "")))

  assert github_contributions._push_topic_branch(
    checkout, "topic", head_of(checkout), fork_slug="octocat/repo",
  ) is None
  assert "evil" in plain_git(checkout, "remote", "get-url", "fork").stdout  # the gate did rewrite it
  assert sent == ["https://github.com/octocat/repo.git"]


def test_commands_see_only_allowlisted_environment(checkout, tmp_path, monkeypatch):
  monkeypatch.setenv("SECRET_KEY", "server-secret")
  monkeypatch.setenv("GIT_TRACE_CURL", "1")
  monkeypatch.setenv("DATABASE_URL", "postgres://secret")
  record = tmp_path / "gate-env"
  install_hook(checkout, f"#!/bin/sh\nenv > {record}\n")
  git_ops.push(checkout, str(bare_remote(tmp_path)), head_of(checkout), "refs/heads/topic", lease="")
  gate = record.read_text()
  for name in ("SECRET_KEY", "GIT_TRACE_CURL", "DATABASE_URL", "GH_TOKEN"):
    assert f"{name}=" not in gate, name

  own = {
    "GIT_CEILING_DIRECTORIES", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
    "GIT_TERMINAL_PROMPT", "GCM_INTERACTIVE", "GIT_ASKPASS", "SSH_ASKPASS",
    "GIT_PAGER", "GIT_EDITOR", "GH_PROMPT_DISABLED", "GIT_ALLOW_PROTOCOL",
  }

  def allowed(name: str) -> bool:
    return (name in git_ops._INHERITED_ENV or name in own or name.startswith(("LC_", "GIT_CONFIG_")))

  local = git_ops._git_env(checkout)
  network = git_ops._git_env(checkout, network=True)
  assert all(allowed(name) for name in local), sorted(local)
  assert {name for name in network if not allowed(name)} == {"GH_TOKEN", "GH_CONFIG_DIR"}
  assert network["GH_TOKEN"] == "probe-token"


def test_gh_runs_outside_any_checkout(checkout, tmp_path, monkeypatch):
  bin_dir = tmp_path / "bin"
  bin_dir.mkdir()
  seen = tmp_path / "gh-cwd"
  fake = bin_dir / "gh"
  fake.write_text(f"#!/bin/sh\n{{ pwd; git rev-parse --git-dir 2>&1; }} > {seen}\nexit 0\n")
  fake.chmod(0o755)
  monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

  git_ops._gh(checkout, "pr", "view", "1", "-R", "owner/repo")

  where, repo_check = seen.read_text().splitlines()[:2]
  assert not where.startswith(str(checkout))
  assert "not a git repository" in repo_check


def test_an_inherited_git_config_file_is_dropped(monkeypatch, tmp_path):
  monkeypatch.setenv("GIT_CONFIG", str(tmp_path / "elsewhere"))
  assert "GIT_CONFIG" not in git_ops._git_env(tmp_path)


def test_only_https_transport_is_allowed(checkout, tmp_path, https_only):
  fetched = git_ops._git(checkout, "fetch", str(bare_remote(tmp_path)), "+refs/heads/main:refs/x",
                         check=False)
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
