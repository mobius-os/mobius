"""Low-level Git/GitHub validation for reviewed contribution actions."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote

from app import app_git, github_auth
from app.config import get_settings
from app.contribution_errors import ContributionSubmitError, push_gate_refused
from app.github_contribution_contract import (
  BRANCH_NAME as _BRANCH_NAME,
  COAUTHOR_TRAILER as _COAUTHOR_TRAILER,
  GITHUB_LOGIN as _GITHUB_LOGIN,
  GITHUB_REPO as _GITHUB_REPO,
  GIT_SHA as _GIT_SHA,
  PUSH_TIMEOUT_SECONDS as _PUSH_TIMEOUT,
  SUBMIT_TIMEOUT_SECONDS as _SUBMIT_TIMEOUT,
  coauthor_trailer_required,
)
from app.terminal_output import readable_output


_FETCH_ATTEMPTS = 2
_FULL_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _is_transient_transport_error(message: str) -> bool:
  """Classify failures worth one safe retry before any public mutation.

  Rate limiting (429) is deliberately absent. The preflight retry below is
  sleepless, and GitHub answers a throttled caller with Retry-After, so an
  immediate second attempt would spend the only retry on a near-certain
  second rejection and add one more request to the throttle.
  """
  detail = str(message or "").lower()
  transient_markers = (
    "could not resolve host",
    "failed to connect",
    "connection reset",
    "connection timed out",
    "operation timed out",
    "remote end hung up unexpectedly",
    "remote hung up unexpectedly",
    "temporarily unavailable",
    "empty reply from server",
    "early eof",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "the requested url returned error: 500",
    "the requested url returned error: 502",
    "the requested url returned error: 503",
    "the requested url returned error: 504",
  )
  return any(marker in detail for marker in transient_markers)

def _validate_repo_slug(value: object) -> str:
  repo = str(value or "")
  if not _GITHUB_REPO.match(repo):
    raise ContributionSubmitError("The staged GitHub repo is invalid.")
  return repo


def _validate_branch(value: object) -> str:
  branch = str(value or "")
  if (
    not _BRANCH_NAME.match(branch)
    or branch.startswith("-")
    or ".." in branch
    or "//" in branch
    or branch.endswith(("/", ".", ".lock"))
  ):
    raise ContributionSubmitError("The staged branch name is invalid.")
  return branch


# The only inherited variables any contribution command (or the pre-push
# gate, which runs checkout code) sees: where programs live, locale, time,
# temp space, what https needs to reach GitHub, and the repository gate's own
# MOBIUS_PREPUSH_* switches. Everything else, server
# secrets and Git's own GIT_* settings and traces among it, is left out.
_INHERITED_ENV = frozenset({
  "PATH", "LANG", "LANGUAGE", "TZ", "TMPDIR",
  "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO",
  "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
  "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY",
  "https_proxy", "http_proxy", "no_proxy", "all_proxy",
})
# Command-scope config, exactly like ``-c``: it outranks the checkout's own
# config and reaches nested repositories, so the programs local commands
# would most often start (fsmonitor, hooks, ssh, credential helper) do not
# run. The empty helper drops every helper configured so far.
_INERT_CONFIG = (
  ("core.fsmonitor", "false"), ("submodule.recurse", "false"),
  ("core.hooksPath", "/dev/null"), ("core.sshCommand", "false"),
  # A scratch borrows the checkout's objects; without this, its token-bearing
  # fetch would start Git in the checkout to list the alternate's refs.
  ("core.alternateRefsCommand", "true"),
  ("credential.helper", ""),
)
# The one credential source, for commands that talk to GitHub.
_GITHUB_CREDENTIAL_HELPER = ("credential.helper", "!gh auth git-credential")
_NETWORK_GIT = frozenset({("fetch",), ("push",), ("ls-remote",)})
# Contribution remotes are GitHub over https; other transports can run
# programs, so they are refused.
_ALLOWED_PROTOCOLS = "https"
def _git_env(repo: Path, *, network: bool = False) -> dict:
  """The one environment for every contribution Git and gh command.

  Global and system config are ignored, nothing prompts, only https transport
  is allowed, and the commonest programs a checkout's config can name
  (fsmonitor, hooks, credential helper, askpass, ssh, pager, editor) are
  inert. GitHub credentials are present only for ``network`` commands, and
  those never run in an agent-writable checkout: ``_git`` runs them in a
  server-owned scratch repository and ``_gh`` outside any checkout. A local
  command never carries credentials, though it can still run a program the
  checkout names, such as a filter. ``_run_cmd`` also gives every command an
  empty home.
  """
  env = {
    key: value for key, value in os.environ.items()
    if key in _INHERITED_ENV or key.startswith(("LC_", "MOBIUS_PREPUSH_"))
  }
  config = _INERT_CONFIG + ((_GITHUB_CREDENTIAL_HELPER,) if network else ())
  env.update(
    GIT_CEILING_DIRECTORIES=str(repo.resolve().parent),
    GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
    GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never",
    GIT_ASKPASS="/bin/false", SSH_ASKPASS="/bin/false",
    GIT_PAGER="cat", GIT_EDITOR=":", GH_PROMPT_DISABLED="1",
    GIT_ALLOW_PROTOCOL=_ALLOWED_PROTOCOLS, GIT_CONFIG_COUNT=str(len(config)),
  )
  for index, (name, value) in enumerate(config):
    env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"] = name, value
  if network:
    token = github_auth.get_token()
    if token:
      env["GH_TOKEN"] = token
    if github_auth.GH_AUTH_DIR.exists():
      env["GH_CONFIG_DIR"] = str(github_auth.GH_AUTH_DIR)
  return env


@contextmanager
def _scratch_home(env: dict) -> Iterator[dict]:
  """``env`` with a fresh, empty home, removed afterwards.

  The service account's home links gh's token store, so a program a command
  starts must not find it there.
  """
  home = tempfile.mkdtemp(prefix="home-", dir=scratch_root())
  try:
    yield {**env, "HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")}
  finally:
    shutil.rmtree(home, ignore_errors=True)


def _run_cmd(
  argv: list[str], *,
  cwd: Path,
  check: bool = True,
  timeout: int = _SUBMIT_TIMEOUT,
  env: dict | None = None,
  input_text: str | None = None,
) -> subprocess.CompletedProcess:
  with _scratch_home(env or _git_env(cwd)) as run_env:
    proc = subprocess.run(
      argv,
      cwd=str(cwd),
      capture_output=True,
      text=True,
      input=input_text,
      timeout=timeout,
      check=False,
      env=run_env,
    )
  if check and proc.returncode != 0:
    detail = (proc.stderr or proc.stdout or "command failed").strip()
    raise ContributionSubmitError(detail[:600] or "GitHub command failed.")
  return proc


def _git(
  repo: Path,
  *args: str,
  check: bool = True,
) -> subprocess.CompletedProcess:
  timeout = _PUSH_TIMEOUT if args[:1] == ("push",) else _SUBMIT_TIMEOUT
  if args[:1] in _NETWORK_GIT:
    if not isinstance(repo, ServerRepo):
      return _network_git(repo, args, check=check, timeout=timeout)
  return _run_cmd(
    ["git", "-C", str(repo), *args],
    cwd=repo,
    check=check,
    timeout=timeout,
    env=_git_env(repo, network=args[:1] in _NETWORK_GIT),
  )


def _scratch_parent() -> Path:
  return Path(get_settings().data_dir) / "run" / "contribution-git"


def sweep_scratch_root() -> None:
  """Discard scratch directories a crashed process left behind.

  Run at startup, before any contribution command can start.
  """
  shutil.rmtree(_scratch_parent(), ignore_errors=True)


def scratch_root() -> Path:
  """The one private parent of every scratch directory; cleared at startup."""
  root = _scratch_parent()
  root.mkdir(mode=0o700, parents=True, exist_ok=True)
  root.chmod(0o700)
  return root


class ServerRepo(type(Path())):
  """A repository the server created for one operation.

  ``_git`` runs network commands in it directly; anything else is a
  contribution checkout and gets a scratch repository. It lives under the
  private ``scratch_root``, but code running as the same user could still
  reach it.
  """


@contextmanager
def _server_scratch(*, bare: bool = True) -> Iterator[ServerRepo]:
  """A fresh repository the server creates, removed afterwards."""
  root = ServerRepo(tempfile.mkdtemp(prefix="repo-", dir=scratch_root()))
  try:
    _run_cmd(["git", "init", "-q", *(("--bare",) if bare else ()), str(root)],
             cwd=root, env=_git_env(root))
    yield root
  finally:
    shutil.rmtree(root, ignore_errors=True)


def _refs(repo: Path) -> dict[str, str]:
  listed = _git(repo, "for-each-ref", "--format=%(objectname) %(refname)")
  return dict(reversed(line.split(" ", 1)) for line in listed.stdout.splitlines() if line)


# The options contribution fetches and pushes use, all in ``--name[=value]``
# form. Anything else is refused rather than guessed at, so the positional
# arguments (URL, then refspecs) are always read correctly.
_NETWORK_OPTIONS = {
  "fetch": {"--no-tags", "--force", "--quiet", "--depth", "--filter", "--no-recurse-submodules"},
  "push": {"--force-with-lease", "--porcelain"},
  "ls-remote": set(),
}


def _network_args(args: tuple[str, ...]) -> list[str]:
  """The positional arguments of a fetch, push or ls-remote, checked."""
  command, rest = args[0], args[1:]
  positional = []
  for arg in rest:
    if arg.startswith("-"):
      if arg.split("=", 1)[0] not in _NETWORK_OPTIONS[command]:
        raise ContributionSubmitError(f"Contribution Git does not run {command} {arg}.")
    else:
      positional.append(arg)
  # A remote name or a relative path would be resolved through the checkout:
  # callers pass the URL they verified.
  if not positional or not ("/" in positional[0] or ":" in positional[0]):
    raise ContributionSubmitError(f"A contribution {command} must name its URL.")
  if command == "fetch" and (
      len(positional) < 2
      or any(not refspec.partition(":")[2] for refspec in positional[1:])):
    raise ContributionSubmitError("A contribution fetch must name each destination ref.")
  return positional


def _network_git(
  checkout: Path, args: tuple[str, ...], *, check: bool, timeout: int,
) -> subprocess.CompletedProcess:
  """Run one token-bearing fetch, push or ls-remote for ``checkout``.

  It runs in a scratch repository the server creates, reading the checkout's
  objects through an alternates file the server writes. With the alternate's
  ref listing switched off, no process that holds the token reads the
  checkout's config, hooks or attributes. A fetch names each destination
  ref: it starts from the checkout's refs, and a local, credential-free
  fetch hands what it fetched back to the checkout.
  """
  _network_args(args)
  objects = _git(
    checkout, "rev-parse", "--path-format=absolute", "--git-path", "objects",
  ).stdout.strip()

  def failed(proc: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    if check:
      detail = (proc.stderr or proc.stdout or "command failed").strip()
      raise ContributionSubmitError(detail[:600] or "GitHub command failed.")
    return proc

  with _server_scratch() as scratch:
    (scratch / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (scratch / "objects" / "info" / "alternates").write_text(objects + "\n")
    before = {}
    if args[0] == "fetch":
      before = _refs(checkout)
      seeded = _run_cmd(
        ["git", "-C", str(scratch), "update-ref", "--stdin"], cwd=scratch,
        check=False, env=_git_env(scratch),
        input_text="".join(f"create {ref} {sha}\n" for ref, sha in before.items()),
      )
      if seeded.returncode != 0:
        return failed(seeded)
    proc = _run_cmd(
      ["git", "-C", str(scratch), *args], cwd=scratch, check=False,
      timeout=timeout, env=_git_env(scratch, network=True),
    )
    if args[0] == "fetch" and proc.returncode == 0:
      fetched = [ref for ref, sha in _refs(scratch).items() if before.get(ref) != sha]
      if fetched:
        # Local and credential-free: the checkout copies what it fetched.
        copied = _run_cmd(
          ["git", "-C", str(checkout), "fetch", "--quiet", "--no-tags", "--force",
           "--no-write-fetch-head", str(scratch), *(f"+{ref}:{ref}" for ref in fetched)],
          cwd=checkout, check=False, timeout=timeout,
          env={**_git_env(checkout), "GIT_ALLOW_PROTOCOL": "file"},
        )
        if copied.returncode != 0:
          return failed(copied)
  return proc if proc.returncode == 0 else failed(proc)


class PushGateRefused(ContributionSubmitError):
  """The repository's pre-push gate refused, timed out or could not start.

  It ran locally before the push, so nothing reached GitHub.
  """

  def __init__(self, transcript: str):
    refused = push_gate_refused(transcript)
    super().__init__(refused.message, code=refused.code, detail=refused.detail)


def push(
  repo: Path, remote: str, commit: str, ref: str, *, lease: str,
) -> subprocess.CompletedProcess:
  """Push one exact commit to ``ref`` behind the repository's pre-push gate.

  The gate runs once, as its own local step, then the token-bearing push
  sends the same commit id. ``lease`` is the expected remote tip ("" for
  absent). Raises ``PushGateRefused`` when the gate refuses, times out or
  cannot start, and otherwise returns the push's result unchecked.
  """
  _pre_push_gate(repo, remote, commit, ref, lease)
  return _send_push(repo, remote, commit, ref, lease)


def _exact_commit(commit: str) -> str:
  if not _FULL_GIT_OID.match(commit):
    raise ContributionSubmitError("Only an exact commit id can be pushed.")
  return commit


def _send_push(
  repo: Path, remote: str, commit: str, ref: str, lease: str,
) -> subprocess.CompletedProcess:
  """The network push alone, hooks inert; callers run the gate first."""
  return _git(
    repo, "push", f"--force-with-lease={ref}:{lease}", remote, f"{_exact_commit(commit)}:{ref}",
    check=False,
  )


def _executable(path: Path) -> bool:
  return path.is_file() and os.access(path, os.X_OK)


def _pre_push_hook(repo: Path) -> Path | None:
  """The pre-push hook Git would run in ``repo``, or None when there is none."""
  common = Path(_git(
    repo, "rev-parse", "--path-format=absolute", "--git-common-dir",
  ).stdout.strip())
  default = common / "hooks" / "pre-push"
  # Only the checkout's own scopes: command scope carries the inert override.
  listed = _git(repo, "config", "--show-scope", "--get-all", "core.hooksPath", check=False)
  if listed.returncode not in (0, 1):
    raise PushGateRefused("Git could not read where this checkout keeps its hooks.")
  configured = [
    value for scope, _, value in
    (line.partition("\t") for line in listed.stdout.splitlines())
    if scope in ("local", "worktree")
  ]
  if not configured:
    return default if _executable(default) else None
  # Git runs hooks from the worktree root, so a relative path starts there.
  top = _git(repo, "rev-parse", "--show-toplevel", check=False).stdout.strip()
  hooks = Path(configured[-1])
  hook = (hooks if hooks.is_absolute() else Path(top or common) / hooks) / "pre-push"
  if _executable(hook):
    return hook
  if _executable(default):
    raise PushGateRefused(
      "This checkout points its hooks away from the repository's pre-push "
      "checks, so they could not run."
    )
  return None


def _pre_push_gate(
  repo: Path, remote: str, commit: str, ref: str, lease: str,
) -> None:
  """Run the pre-push hook Git would run, as a pre-push hook, without credentials.

  The hook is found as Git finds it: under the checkout's own core.hooksPath
  when set, otherwise in the common hooks directory, where the platform
  installs its gate. A hooksPath without a pre-push hook while the common
  one exists is refused rather than skipping the gate. Like Git, it passes
  the pushed commit on stdin; checks the hook
  makes of HEAD or the working tree see the checkout as it is, as they did
  inside a push. It runs in its own process group, which is killed when it
  ends; a child that starts its own session leaves that group and can
  outlive it, though it never had credentials in its environment.
  """
  try:
    hook = _pre_push_hook(repo)
    if hook is None:
      return
    named = _git(repo, "remote", "get-url", remote, check=False)
  except (subprocess.TimeoutExpired, OSError, ContributionSubmitError) as exc:
    # Still local: nothing was sent, so this is a refusal, never ambiguous.
    if isinstance(exc, PushGateRefused):
      raise
    raise PushGateRefused(f"Git could not prepare the pre-push checks: {exc}") from exc
  url = named.stdout.strip() if named.returncode == 0 else remote
  line = f"{_exact_commit(commit)} {commit} {ref} {lease or '0' * 40}\n"
  with _scratch_home(_git_env(repo)) as env, \
      tempfile.TemporaryFile("w+") as stdin, tempfile.TemporaryFile("w+") as output:
    stdin.write(line)
    stdin.seek(0)
    def start(argv: list[str]) -> subprocess.Popen:
      return subprocess.Popen(
        argv, cwd=str(repo), env=env, stdin=stdin,
        stdout=output, stderr=subprocess.STDOUT, text=True, start_new_session=True,
      )

    try:
      try:
        proc = start([str(hook), remote, url])
      except OSError as exc:
        if exc.errno != errno.ENOEXEC:
          raise
        # Like Git, run a hook without an interpreter line through sh.
        proc = start(["/bin/sh", str(hook), remote, url])
    except OSError as exc:
      raise PushGateRefused(f"The pre-push checks could not start: {exc}") from exc
    try:
      returncode = proc.wait(timeout=_PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
      returncode = None
    finally:
      try:
        os.killpg(proc.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      proc.wait()
    output.seek(0)
    transcript = output.read()
  if returncode is None:
    raise PushGateRefused(
      f"{transcript}\nThe pre-push checks did not finish within {_PUSH_TIMEOUT} seconds."
    )
  if returncode != 0:
    raise PushGateRefused(transcript)


def _gh(cwd: Path, *args: str, check: bool = True,
        input_text: str | None = None) -> subprocess.CompletedProcess:
  """Run gh with the token, never in a contribution checkout.

  Every call names its repository (-R or an API path), so gh runs in an
  empty directory. Only a ``ServerRepo`` is used as gh's directory: the
  in-repo fork command needs one whose origin is upstream. Any other
  ``cwd`` is ignored; it keeps the ``gh(cwd, *args)`` shape the review
  modules share.
  """
  if isinstance(cwd, ServerRepo):
    return _run_cmd(["gh", *args], cwd=cwd, check=check, input_text=input_text,
                    env=_git_env(cwd, network=True))
  with tempfile.TemporaryDirectory(prefix="gh-", dir=scratch_root()) as empty:
    return _run_cmd(["gh", *args], cwd=Path(empty), check=check, input_text=input_text,
                    env=_git_env(Path(empty), network=True))


def _assert_clean_worktree(repo: Path) -> None:
  status = _git(repo, "status", "--porcelain").stdout.strip()
  if status:
    raise ContributionSubmitError(
      "This staged branch has uncommitted source changes. Ask your agent "
      "to prepare the PR again before submitting.",
      code="working_changes",
    )


_GITHUB_WEB_COMMITTER = "noreply@github.com"


def _assert_coauthor_trailer(
  repo: Path, branch: str, record: dict | None = None,
) -> None:
  if not coauthor_trailer_required(record):
    return
  body = _git(repo, "log", "-1", "--format=%B", branch).stdout
  if _COAUTHOR_TRAILER in body:
    return
  committer = _git(repo, "log", "-1", "--format=%ce", branch).stdout.strip()
  if committer == _GITHUB_WEB_COMMITTER:
    # GitHub's own commits (Update branch, web edits) cannot carry the
    # trailer, which otherwise reads as a defect in the agent's commit.
    message = (
      "The newest commit on this pull request was added on GitHub (for "
      "example by Update branch) and doesn't carry the Möbius co-author "
      "line. Nothing was changed. Ask your agent to prepare it again."
    )
  else:
    message = (
      "This staged commit is missing the Möbius Agent co-author trailer. "
      "Leave feedback so your agent can prepare it again."
    )
  raise ContributionSubmitError(message, code="missing_coauthor")


def _connected_git_identity(state: dict, login: str) -> tuple[str, str]:
  if not _GITHUB_LOGIN.match(login):
    raise ContributionSubmitError("Reconnect GitHub before approving this PR.", 401)
  user_id = str(state.get("user_id") or "").strip()
  if user_id and not user_id.isdigit():
    raise ContributionSubmitError("Reconnect GitHub before approving this PR.", 401)
  return login, github_auth.noreply_email(login, user_id)


def _head_commit_metadata(repo: Path, branch: str) -> dict:
  out = _git(
    repo,
    "show", "-s",
    "--format=%H%x00%T%x00%an%x00%ae%x00%cn%x00%ce%x00%aI",
    branch,
  ).stdout.rstrip("\n")
  parts = out.split("\x00")
  if len(parts) != 7:
    raise ContributionSubmitError(
      "Could not inspect the staged commit attribution. Ask your agent "
      "to prepare this PR again."
    )
  return {
    "sha": parts[0],
    "tree": parts[1],
    "author_name": parts[2],
    "author_email": parts[3],
    "committer_name": parts[4],
    "committer_email": parts[5],
    "author_date": parts[6],
  }


def _head_sha_patch(record: dict, old_head: str, new_head: str) -> dict:
  if old_head == new_head:
    return {}
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  return {
    "head_sha": new_head,
    "plan": {
      **plan,
      "head_sha": new_head,
      "attribution_normalized_from": old_head,
    },
  }


def _merge_error_patch(exc: ContributionSubmitError, patch: dict) -> ContributionSubmitError:
  if not patch:
    return exc
  return ContributionSubmitError(
    exc.message,
    exc.status_code,
    record_patch={**patch, **exc.record_patch},
    code=exc.code,
    detail=exc.detail,
  )


def _nothing_published(
  exc: ContributionSubmitError, record_patch: dict,
) -> ContributionSubmitError:
  """Send's wording for a shared check that refused before any public step."""
  return ContributionSubmitError(
    f"{exc.message} Nothing was published.",
    exc.status_code,
    record_patch=record_patch,
    code=exc.code,
    detail=exc.detail,
  )


def _record_patch_with(base: dict, extra: dict) -> dict:
  if not extra:
    return base
  if not base:
    return extra
  return {**base, **extra}


def _normalize_head_attribution(
  repo: Path,
  branch: str,
  *,
  author_name: str,
  author_email: str,
  base_sha: str,
  expected_diff: str,
  record: dict,
  before_amend: Callable[[dict], None] | None = None,
) -> dict:
  before = _head_commit_metadata(repo, branch)
  if head_attribution_matches(
    before, author_name=author_name, author_email=author_email,
  ):
    return {}

  parents = _git(
    repo, "show", "-s", "--format=%P", before["sha"],
  ).stdout.strip().split()
  raw_commit = _git(repo, "cat-file", "-p", before["sha"]).stdout
  _headers, separator, message = raw_commit.partition("\n\n")
  if not separator:
    raise ContributionSubmitError(
      "Could not inspect the staged commit before normalizing attribution."
    )

  identity_env = _git_env(repo)
  identity_env.update({
    "GIT_AUTHOR_NAME": author_name,
    "GIT_AUTHOR_EMAIL": author_email,
    "GIT_AUTHOR_DATE": before["author_date"],
    "GIT_COMMITTER_NAME": author_name,
    "GIT_COMMITTER_EMAIL": author_email,
    "GIT_COMMITTER_DATE": before["author_date"],
  })
  author_ident = _run_cmd(
    ["git", "-C", str(repo), "var", "GIT_AUTHOR_IDENT"],
    cwd=repo,
    env=identity_env,
  ).stdout.strip()
  committer_ident = _run_cmd(
    ["git", "-C", str(repo), "var", "GIT_COMMITTER_IDENT"],
    cwd=repo,
    env=identity_env,
  ).stdout.strip()
  canonical_lines = [f"tree {before['tree']}"]
  canonical_lines.extend(f"parent {parent}" for parent in parents)
  canonical_lines.extend((
    f"author {author_ident}",
    f"committer {committer_ident}",
  ))
  canonical_commit = "\n".join(canonical_lines) + "\n\n" + message
  expected_normalized_head = _run_cmd(
    ["git", "-C", str(repo), "hash-object", "-t", "commit", "--stdin"],
    cwd=repo,
    input_text=canonical_commit,
  ).stdout.strip()
  if not re.fullmatch(r"[0-9a-f]{40,64}", expected_normalized_head):
    raise ContributionSubmitError(
      "Could not derive the deterministic normalized commit identity."
    )

  if before_amend is not None:
    before_amend({
      **before,
      "parents": parents,
      "expected_normalized_head_sha": expected_normalized_head,
    })

  written_head = _run_cmd(
    [
      "git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin",
    ],
    cwd=repo,
    input_text=canonical_commit,
  ).stdout.strip()
  if written_head != expected_normalized_head:
    raise ContributionSubmitError(
      "Writing the normalized commit produced an unexpected identity."
    )
  _git(
    repo,
    "update-ref",
    f"refs/heads/{branch}",
    expected_normalized_head,
    before["sha"],
  )

  after = _head_commit_metadata(repo, branch)
  if after["sha"] != expected_normalized_head:
    raise ContributionSubmitError(
      "Normalizing commit attribution produced an unexpected commit identity. "
      "Ask your agent to prepare this PR again."
    )
  if after["tree"] != before["tree"]:
    raise ContributionSubmitError(
      "Normalizing commit attribution changed the staged source tree. "
      "Ask your agent to prepare this PR again."
    )
  if not head_attribution_matches(
    after, author_name=author_name, author_email=author_email,
  ):
    raise ContributionSubmitError(
      "Could not normalize the staged commit attribution. Ask your agent "
      "to prepare this PR again."
    )
  branch_hash = hashlib.sha256(
    _reviewed_branch_diff(repo, base_sha, after["sha"])
  ).hexdigest()
  if branch_hash != expected_diff:
    raise ContributionSubmitError(
      "Normalizing commit attribution changed the reviewed diff. Ask your "
      "agent to prepare this PR again."
    )
  return _head_sha_patch(record, before["sha"], after["sha"])


def commit_identity_mismatch() -> ContributionSubmitError:
  """The shared review/Send refusal for a direct commit with another identity."""
  return ContributionSubmitError(
    "The reviewed commit's author or committer is not the connected GitHub "
    "account. It is published directly to the target repository, where "
    "reviewed commits are never rewritten, so nothing was published. Ask the "
    "agent to recommit it with the connected identity and stage it again.",
    code="commit_identity_mismatch",
  )


def publishes_to_target_repository(
  upstream_repo: str,
  login: str,
  *,
  direct_base: bool,
  existing_head_repository: str | None,
  push_permission: Callable[[], bool | None],
) -> bool | None:
  """Whether a reviewed PR's branch is pushed to the target repository itself.

  The reviewed commit is then published as-is: rewriting it would break a
  stack's reviewed parent links or an existing PR's public history. Its author
  and committer must already be the connected account. A fork publication
  instead normalizes the one reviewed commit before push. ``None`` means the
  push permission that decides it could not be read.
  """
  if existing_head_repository is not None:
    # A known PR's live head repository fixes the destination regardless of
    # the connected account's generic upstream permission.
    return existing_head_repository.casefold() == upstream_repo.casefold()
  if direct_base or upstream_repo.split("/", 1)[0].casefold() == login.casefold():
    return True
  return push_permission()


def head_attribution_matches(
  metadata: dict, *, author_name: str, author_email: str,
) -> bool:
  return (
    metadata["author_name"] == author_name
    and metadata["author_email"] == author_email
    and metadata["committer_name"] == author_name
    and metadata["committer_email"] == author_email
  )


def _assert_head_attribution(
  repo: Path,
  branch: str,
  *,
  author_name: str,
  author_email: str,
) -> None:
  """Require a directly published commit to carry the connected identity.

  Review applies the same rule, so this repeats it only for an account or
  branch that changed after review.
  """
  if not head_attribution_matches(
    _head_commit_metadata(repo, branch),
    author_name=author_name,
    author_email=author_email,
  ):
    raise commit_identity_mismatch()


def _upstream_default_branch(repo: Path, upstream_repo: str) -> str:
  proc = _gh(
    repo,
    "repo", "view", upstream_repo,
    "--json", "defaultBranchRef",
    "--jq", ".defaultBranchRef.name",
    check=False,
  )
  branch = (proc.stdout or "").strip() if proc.returncode == 0 else ""
  if not branch:
    branch = "main"
  return _validate_branch(branch)


def upstream_push_permission(repo: Path, upstream_repo: str) -> bool | None:
  """Read the connected account's push permission; ``None`` if unreadable."""
  proc = _gh(
    repo,
    "api", f"repos/{upstream_repo}",
    "--jq", ".permissions.push",
    check=False,
  )
  value = (proc.stdout or "").strip().lower()
  if proc.returncode != 0 or value not in ("true", "false"):
    return None
  return value == "true"


def _has_upstream_push_permission(repo: Path, upstream_repo: str) -> bool:
  return upstream_push_permission(repo, upstream_repo) is True


def _assert_upstream_push_permission(repo: Path, upstream_repo: str) -> None:
  """True GitHub stacks need their base branches in the upstream repository."""
  if not _has_upstream_push_permission(repo, upstream_repo):
    raise ContributionSubmitError(
      "GitHub only allows a PR to target a branch in its base repository. "
      "This account cannot publish the upstream stack branches, so nothing "
      "was sent. Submit these as independent PRs or use an account with "
      "upstream push access."
    )


def _upstream_branch_sha(
  repo: Path, upstream_repo: str, branch: str,
) -> str | None:
  """Read one upstream branch tip, returning ``None`` on an invalid response."""
  branch = _validate_branch(branch)
  proc = _gh(
    repo,
    "api",
    f"repos/{upstream_repo}/git/ref/heads/{quote(branch, safe='')}",
    "--jq", ".object.sha",
    check=False,
  )
  actual_sha = (proc.stdout or "").strip() if proc.returncode == 0 else ""
  return actual_sha if _GIT_SHA.match(actual_sha) else None


def _authoritative_upstream_branch_sha(
  repo: Path, upstream_repo: str, branch: str,
) -> str | None:
  """Read one branch tip, distinguishing absence from an unreadable remote.

  Publication uses the result as a force-with-lease precondition.  Treating a
  transport/auth/parser failure as an absent branch would turn that lease into
  a blind create, so only GitHub's explicit 404 maps to ``None``.
  """
  upstream_repo = _validate_repo_slug(upstream_repo)
  branch = _validate_branch(branch)
  proc = _gh(
    repo,
    "api",
    f"repos/{upstream_repo}/git/ref/heads/{quote(branch, safe='')}",
    "--jq", ".object.sha",
    check=False,
  )
  detail = (proc.stderr or proc.stdout or "").strip()
  if proc.returncode != 0:
    if "404" in detail and "not found" in detail.lower():
      return None
    raise ContributionSubmitError(
      "Could not verify the current GitHub branch before pushing. Nothing "
      "was sent; try again once GitHub is reachable.",
      detail=readable_output(detail),
    )
  actual_sha = (proc.stdout or "").strip()
  if not _FULL_GIT_OID.fullmatch(actual_sha):
    raise ContributionSubmitError(
      "GitHub returned an invalid branch commit. Nothing was pushed."
    )
  return actual_sha


def _assert_upstream_branch_at(
  repo: Path,
  upstream_repo: str,
  branch: str,
  expected_sha: str,
) -> None:
  """Require an already-public stack parent to remain at its reviewed tip."""
  branch = _validate_branch(branch)
  if not _GIT_SHA.match(str(expected_sha or "")):
    raise ContributionSubmitError(
      "An existing PR stack parent has no valid reviewed commit. Leave "
      "feedback so your agent can prepare the remaining layers again."
    )
  actual_sha = _upstream_branch_sha(repo, upstream_repo, branch)
  if actual_sha is None:
    raise ContributionSubmitError(
      f"The existing stack base {branch} is no longer available upstream. "
      "Nothing was sent; leave feedback so your agent can refresh the "
      "remaining layers."
    )
  if actual_sha != expected_sha:
    raise ContributionSubmitError(
      f"The existing stack base {branch} changed after review. Nothing was "
      "sent; leave feedback so your agent can refresh the remaining layers."
    )


def _assert_unprotected_landing_target(
  repo: Path, upstream_repo: str, branch: str,
) -> None:
  """Atomic stack landing is deliberately limited to unprotected app refs.

  The platform repository and any app that has opted into branch protection
  keep GitHub's ordinary merge/queue path.  Admin bypass is not treated as
  permission to skip those repository-owned invariants.
  """
  encoded = quote(_validate_branch(branch), safe="")
  protection = _gh(
    repo,
    "api", f"repos/{upstream_repo}/branches/{encoded}/protection",
    check=False,
  )
  protection_detail = (protection.stderr or protection.stdout or "").lower()
  if protection.returncode == 0:
    raise ContributionSubmitError(
      f"{branch} is protected, so Contribute will not bypass its merge rules. "
      "Use GitHub's normal merge or merge queue for this stack."
    )
  if "404" not in protection_detail and "not protected" not in protection_detail:
    raise ContributionSubmitError(
      f"Contribute could not verify that {branch} is safe for atomic landing. "
      "Nothing was changed; try again after GitHub is reachable."
    )

  rules = _gh(
    repo,
    "api", f"repos/{upstream_repo}/rules/branches/{encoded}",
    check=False,
  )
  if rules.returncode != 0:
    raise ContributionSubmitError(
      f"Contribute could not inspect the active rules for {branch}. Nothing "
      "was changed; use GitHub's normal merge flow."
    )
  try:
    active_rules = json.loads(rules.stdout or "[]")
  except ValueError:
    active_rules = None
  if not isinstance(active_rules, list):
    raise ContributionSubmitError(
      f"Contribute could not understand the active rules for {branch}. "
      "Nothing was changed; use GitHub's normal merge flow."
    )
  if active_rules:
    raise ContributionSubmitError(
      f"{branch} has repository rules, so Contribute will not bypass them. "
      "Use GitHub's normal merge or merge queue for this stack."
    )


def _assert_pr_checks_green(
  repo: Path,
  *,
  upstream_repo: str,
  record: dict,
  base_branch: str,
  head_branch: str,
) -> None:
  number = record.get("number")
  if not isinstance(number, int) or number <= 0:
    raise ContributionSubmitError(
      "A pull request in this stack has no verified GitHub number. Refresh "
      "Contribute before landing it."
    )
  proc = _gh(
    repo,
    "pr", "view", str(number),
    "-R", upstream_repo,
    "--json",
    "state,isDraft,baseRefName,headRefName,headRepositoryOwner,statusCheckRollup,url",
    check=False,
  )
  if proc.returncode != 0:
    raise ContributionSubmitError(
      f"Contribute could not verify pull request #{number}. Nothing was "
      "changed; refresh and try again."
    )
  try:
    data = json.loads(proc.stdout or "{}")
  except ValueError:
    data = None
  if not isinstance(data, dict):
    raise ContributionSubmitError(
      f"GitHub returned an invalid result for pull request #{number}."
    )
  owner = data.get("headRepositoryOwner")
  owner_login = owner.get("login") if isinstance(owner, dict) else ""
  upstream_owner = upstream_repo.split("/", 1)[0]
  if (
    data.get("state") != "OPEN"
    or bool(data.get("isDraft"))
    or data.get("baseRefName") != base_branch
    or data.get("headRefName") != head_branch
    or str(owner_login).lower() != upstream_owner.lower()
  ):
    raise ContributionSubmitError(
      f"Pull request #{number} no longer matches the reviewed stack. Nothing "
      "was changed; refresh Contribute before landing it."
    )
  expected_url = str(record.get("url") or "")
  if expected_url and data.get("url") != expected_url:
    raise ContributionSubmitError(
      f"Pull request #{number} no longer matches its Contribute record."
    )

  checks = data.get("statusCheckRollup")
  if not isinstance(checks, list) or not checks:
    raise ContributionSubmitError(
      f"Pull request #{number} has no completed CI checks yet. Nothing was "
      "changed; wait for CI and try again."
    )
  unfinished = []
  failed = []
  for check in checks:
    if not isinstance(check, dict):
      failed.append("unknown check")
      continue
    name = str(check.get("name") or check.get("context") or "check")
    if check.get("__typename") == "StatusContext":
      state = str(check.get("state") or "").upper()
      if state == "SUCCESS":
        continue
      if state in {"PENDING", "EXPECTED"}:
        unfinished.append(name)
      else:
        failed.append(name)
      continue
    status = str(check.get("status") or "").upper()
    conclusion = str(check.get("conclusion") or "").upper()
    if status != "COMPLETED" or not conclusion:
      unfinished.append(name)
    elif conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
      failed.append(name)
  if failed:
    raise ContributionSubmitError(
      f"Pull request #{number} has failing CI ({', '.join(failed[:3])}). "
      "Nothing was changed."
    )
  if unfinished:
    raise ContributionSubmitError(
      f"Pull request #{number} still has CI running ({', '.join(unfinished[:3])}). "
      "Nothing was changed; try again when it is green."
    )


def fetch_upstream_head(
  repo: Path, upstream_repo: str, upstream_branch: str,
) -> str:
  """Fetch one upstream branch tip into ``repo``'s objects and return its sha.

  The tip is fetched through a unique temporary ref that is removed again, so
  concurrent callers never race on a shared remote-tracking ref. Transient
  transport failures get one fresh attempt; failures carry
  ``upstream_fetch_unavailable`` (transient) or ``upstream_fetch_failed``.
  """
  remote_url = f"https://github.com/{upstream_repo}.git"
  ref_key = hashlib.sha256(
    f"{upstream_repo}\0{upstream_branch}\0{time.time_ns()}".encode("utf-8")
  ).hexdigest()[:24]
  upstream_ref = f"refs/mobius-submit/upstream-{ref_key}"
  try:
    fetch_detail = ""
    fetched = None
    fetch_was_transient = False
    for attempt in range(_FETCH_ATTEMPTS):
      fetched = None
      try:
        fetched = _git(
          repo,
          "fetch", "--no-tags", "--force",
          remote_url,
          f"+refs/heads/{upstream_branch}:{upstream_ref}",
          check=False,
        )
      except subprocess.TimeoutExpired as exc:
        fetch_detail = readable_output(
          exc.stderr or exc.stdout or
          f"Git fetch timed out after {exc.timeout} seconds."
        )
        fetch_was_transient = True
        if attempt + 1 < _FETCH_ATTEMPTS:
          continue
        break
      if fetched.returncode == 0:
        break
      observed_detail = readable_output(fetched.stderr or fetched.stdout or "")
      if observed_detail:
        fetch_detail = observed_detail
      fetch_was_transient = _is_transient_transport_error(fetch_detail)
      if not fetch_was_transient:
        break
      # Starting a fresh git process is the recovery boundary. There is no
      # sleep here: callers remain bounded, and deterministic rejections never
      # consume a second attempt.
      if attempt + 1 >= _FETCH_ATTEMPTS:
        break
    if fetched is None or fetched.returncode != 0:
      raise ContributionSubmitError(
        f"GitHub could not provide upstream {upstream_branch}.",
        code=(
          "upstream_fetch_unavailable" if fetch_was_transient
          else "upstream_fetch_failed"
        ),
        detail=fetch_detail or "Git fetch failed without diagnostic output.",
      )
    upstream_sha = _git(
      repo, "rev-parse", "--verify", f"{upstream_ref}^{{commit}}",
    ).stdout.strip()
    if not _GIT_SHA.match(upstream_sha):
      raise ContributionSubmitError(
        f"Could not resolve upstream {upstream_branch}.",
        code="upstream_unresolved",
      )
    return upstream_sha
  finally:
    _git(repo, "update-ref", "-d", upstream_ref, check=False)


@dataclass(frozen=True)
class UpstreamMerge:
  """The exact upstream tip a branch was checked against, and whether it merges."""

  branch: str
  sha: str
  conflicts: bool

  @property
  def record_patch(self) -> dict:
    return {
      "last_submit_upstream_branch": self.branch,
      "last_submit_upstream_sha": self.sha,
    }


def inspect_upstream_merge(
  repo: Path, upstream_repo: str, branch: str,
) -> UpstreamMerge:
  """Fetch today's upstream default tip and report whether ``branch`` merges.

  A conflict is a fact about two exact commits, returned rather than raised.
  Failing to learn that fact (transport, unresolved ref, auth, or a
  ``merge-tree`` that could not run) still raises with its own code, so it is
  never mislabelled as a conflict.
  """
  upstream_branch = _upstream_default_branch(repo, upstream_repo)
  preflight_patch = {"last_submit_upstream_branch": upstream_branch}
  try:
    upstream_sha = fetch_upstream_head(repo, upstream_repo, upstream_branch)
  except ContributionSubmitError as exc:
    if exc.code == "upstream_unresolved":
      raise ContributionSubmitError(
        "Could not resolve the upstream branch for this PR. Leave feedback "
        "so your agent can refresh it.",
        record_patch=preflight_patch,
      ) from None
    raise ContributionSubmitError(
      (
        "GitHub was temporarily unreachable while Contribute checked "
        f"upstream {upstream_branch}. Nothing was published. Try Send "
        "again; leave feedback only if it keeps failing."
        if exc.code == "upstream_fetch_unavailable" else
        f"GitHub could not provide upstream {upstream_branch} while "
        "Contribute checked mergeability. Nothing was published. Leave "
        "feedback so your agent can inspect it."
      ),
      record_patch=preflight_patch,
      code=exc.code,
      detail=exc.detail,
    ) from None
  try:
    return merge_with_upstream(repo, upstream_branch, upstream_sha, branch)
  except ContributionSubmitError as exc:
    raise _nothing_published(exc, exc.record_patch) from None


def merge_with_upstream(
  repo: Path, upstream_branch: str, upstream_sha: str, branch: str,
) -> UpstreamMerge:
  """Report whether ``branch`` merges with one exact, already-local upstream tip.

  Raises ``upstream_merge_unavailable`` when the merge cannot be judged, for
  example when either commit is not available locally. Callers that refuse to
  publish on that say so themselves.
  """
  def unknown(detail: str) -> ContributionSubmitError:
    return ContributionSubmitError(
      f"Contribute could not check whether this PR merges with upstream "
      f"{upstream_branch}. Leave feedback so your agent can inspect it.",
      record_patch=UpstreamMerge(upstream_branch, upstream_sha, False).record_patch,
      code="upstream_merge_unavailable",
      detail=readable_output(detail),
    )

  # ``merge-tree`` exits 1 both for a conflict and for a commit it cannot
  # find, so both commits must exist before its exit code means anything.
  for rev in (upstream_sha, branch):
    if _git(
      repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}",
      check=False,
    ).returncode != 0:
      raise unknown(f"Commit {rev} is not available locally.")
  # ``--no-messages`` leaves only the tree id and the conflicted-file list,
  # whose paths git quotes by default. A repository that turns
  # ``core.quotePath`` off can still print a path that is not UTF-8, which
  # leaves the verdict unknown rather than crashing.
  try:
    merged = _git(
      repo, "merge-tree", "--write-tree", "--no-messages", upstream_sha, branch,
      check=False,
    )
  except UnicodeDecodeError:
    raise unknown("git merge-tree printed a path that is not UTF-8.") from None
  # ``merge-tree --write-tree`` exits 0 when clean and 1 when conflicted.
  # Histories with no common ancestor cannot merge at all (git refuses them
  # fatally), which is a deterministic fact about the two commits, proven
  # structurally rather than from localized stderr. Anything else (a fatal
  # 128, a usage error, unreadable objects) is an unknown merge verdict, not a
  # conflict, and never a licence to publish.
  if merged.returncode in (0, 1):
    return UpstreamMerge(upstream_branch, upstream_sha, merged.returncode == 1)
  if _git(repo, "merge-base", upstream_sha, branch, check=False).returncode == 1:
    return UpstreamMerge(upstream_branch, upstream_sha, True)
  raise unknown(
    merged.stderr or merged.stdout or
    f"git merge-tree exited {merged.returncode} without diagnostic output."
  )


def _upstream_conflict_error(merge: UpstreamMerge) -> ContributionSubmitError:
  return ContributionSubmitError(
    (
      f"This PR no longer merges cleanly with upstream {merge.branch}. "
      "Leave feedback so your agent can refresh the branch before it is "
      "pushed."
    ),
    record_patch=merge.record_patch,
    code="upstream_conflict",
  )


def _assert_merges_with_upstream(
  repo: Path, upstream_repo: str, branch: str,
) -> dict:
  """Refuse a branch that conflicts with today's upstream (stacks, updates)."""
  merge = inspect_upstream_merge(repo, upstream_repo, branch)
  if merge.conflicts:
    raise _upstream_conflict_error(merge)
  return merge.record_patch


def opens_new_standalone_pr(
  plan: dict, *, existing_pr_number: int | None = None,
  direct_base: str | None = None,
) -> bool:
  """Whether publishing ``plan`` opens a new PR against the default branch.

  Only such a PR may open over an upstream overlap, and only it is shown by
  GitHub as ``merge-base..head``. A stack layer or an existing PR's update
  must still merge cleanly before anything is pushed.
  """
  return (
    existing_pr_number is None
    and direct_base is None
    and not isinstance(plan.get("stack"), dict)
    and plan.get("action") == "pr"
  )


def reviewed_base_is_sole_merge_base(
  run: Callable[..., subprocess.CompletedProcess],
  upstream_sha: str, head: str, base_sha: str,
) -> bool:
  """Whether GitHub would show a new PR as exactly its reviewed diff.

  GitHub shows a new PR as ``merge-base..head``. That is byte-for-byte the
  reviewed ``base..head`` only when the reviewed base is the SOLE merge base
  of upstream and head. Ancestry alone is not enough: upstream may already
  hold some of the head's own commits (moving the merge base and shrinking
  the diff), or the histories may criss-cross. ``run`` executes one git
  command without raising, so Send and the read-only status card share this
  one proof. A git failure is an unknown answer and raises.
  """
  bases = run("merge-base", "--all", upstream_sha, head)
  if bases.returncode not in (0, 1):
    raise ContributionSubmitError(
      "Could not compare this PR's reviewed base with upstream.",
      code="upstream_merge_unavailable",
      detail=readable_output(bases.stderr or bases.stdout or ""),
    )
  return bases.returncode == 0 and (bases.stdout or "").split() == [
    str(base_sha).lower()
  ]


def upstream_merge_for_new_pr(
  repo: Path, upstream_repo: str, branch: str, base_sha: str, head_sha: str,
) -> dict:
  """Preflight a NEW standalone PR: publish only the exact reviewed diff.

  The PR may open even while it conflicts with newer upstream, because the
  owner approved this exact change and resolving the overlap is its next
  update. The returned patch then carries ``last_submit_upstream_conflict``,
  which Send records only once the PR is open, so Contribute and agents see
  that it needs upstream merged in (GitHub runs no PR workflows while it
  conflicts). Clean or not, a reviewed base that is not the sole merge base
  refuses. Stacks and existing-PR updates keep ``_assert_merges_with_upstream``.
  """
  merge = inspect_upstream_merge(repo, upstream_repo, branch)
  try:
    exact = reviewed_base_is_sole_merge_base(
      lambda *args: _git(repo, *args, check=False), merge.sha, branch, base_sha,
    )
  except ContributionSubmitError as exc:
    raise _nothing_published(exc, merge.record_patch) from None
  if not exact:
    raise _upstream_conflict_error(merge) if merge.conflicts else (
      ContributionSubmitError(
        f"This PR's reviewed base is not its only merge base with upstream "
        f"{merge.branch}, so GitHub would not show the reviewed diff. Nothing "
        "was published. Leave feedback so your agent can refresh the branch "
        "before it is pushed.",
        record_patch=merge.record_patch,
        code="upstream_base_moved",
      )
    )
  return {**merge.record_patch, **upstream_overlap_patch(merge, head_sha)}


def upstream_overlap_patch(merge: UpstreamMerge, head_sha: str) -> dict:
  """The ``last_submit_upstream_conflict`` a new PR opened over ``merge`` records."""
  if not merge.conflicts:
    return {}
  return {
    "last_submit_upstream_conflict": {
      "upstream_branch": merge.branch,
      "upstream_sha": merge.sha,
      "head_sha": str(head_sha).lower(),
      "next_pr_action": "merge_upstream_into_pr",
    },
  }


def _canonical_reviewed_oid(value: object, label: str) -> str:
  """Require the exact canonical object id the review actually named."""
  raw = str(value or "")
  if not raw:
    raise ContributionSubmitError(
      f"This record needs to be prepared again: it has no reviewed {label}."
    )
  if not _FULL_GIT_OID.fullmatch(raw):
    raise ContributionSubmitError(
      f"The reviewed {label} is not a full canonical Git object id. Ask the "
      "agent to refresh and review this contribution again."
    )
  return raw


def _resolve_reviewed_commit(repo: Path, value: object, label: str) -> str:
  raw = _canonical_reviewed_oid(value, label)
  try:
    resolved = _git(
      repo, "rev-parse", "--verify", f"{raw}^{{commit}}"
    ).stdout.strip()
  except ContributionSubmitError:
    raise ContributionSubmitError(
      f"The reviewed {label} is not present in the staged repo."
    )
  if not _FULL_GIT_OID.fullmatch(resolved) or resolved != raw:
    raise ContributionSubmitError(f"The reviewed {label} resolved incorrectly.")
  return resolved


def _reviewed_branch_diff(repo: Path, base_sha: str, head_sha: str) -> bytes:
  """The byte-exact canonical diff that staging, review status and Send hash.

  One definition only: decoding it as text would rewrite CR line endings and
  fail on non-UTF-8 source, so Send would refuse a correctly staged record.
  """
  diff = app_git._canonical_diff(repo, base_sha, head_sha)
  if diff is None:
    raise ContributionSubmitError(
      "Git could not compute the reviewed branch diff."
    )
  return diff


def _assert_fresh(
  record: dict, diff_path: Path, repo: Path, branch: str,
) -> tuple[str, str, str]:
  plan = record.get("plan") or {}
  expected_base = _resolve_reviewed_commit(repo, plan.get("base_sha"), "base sha")
  expected_head = _resolve_reviewed_commit(
    repo, plan.get("head_sha") or record.get("head_sha"), "head sha"
  )
  ancestry = _git(
    repo,
    "merge-base",
    "--is-ancestor",
    expected_base,
    expected_head,
    check=False,
  )
  if ancestry.returncode != 0:
    raise ContributionSubmitError(
      "The reviewed branch is no longer based on its recorded parent. Ask "
      "your agent to prepare this contribution again.",
      code="invalid_ancestry",
    )
  actual_head = _git(repo, "rev-parse", branch).stdout.strip()
  if actual_head != expected_head:
    raise ContributionSubmitError(
      "This branch changed after review. Ask your agent to refresh the "
      "Contribute card before submitting.",
      code="branch_moved",
    )
  expected_diff = str(plan.get("diff_sha256") or "").strip()
  if not expected_diff:
    raise ContributionSubmitError(
      "This record needs to be prepared again: it has no reviewed diff hash.",
      code="missing_diff_hash",
    )
  try:
    diff_bytes = diff_path.read_bytes()
  except OSError:
    raise ContributionSubmitError(
      "The reviewed diff is missing. Ask your agent to prepare this again.",
      code="missing_diff",
    )
  stored_hash = hashlib.sha256(diff_bytes).hexdigest()
  if stored_hash != expected_diff:
    raise ContributionSubmitError(
      "The reviewed diff changed. Ask your agent to refresh the "
      "Contribute card before submitting.",
      code="review_changed",
    )
  branch_hash = hashlib.sha256(
    _reviewed_branch_diff(repo, expected_base, expected_head)
  ).hexdigest()
  if branch_hash != expected_diff:
    raise reviewed_diff_mismatch()
  return expected_base, expected_head, expected_diff


def reviewed_diff_mismatch() -> ContributionSubmitError:
  """The stored review is not the branch's canonical diff, so nothing it proves
  (freshness, source provenance) can hold until it is prepared again."""
  return ContributionSubmitError(
    "The reviewed diff does not match the branch that would be pushed. "
    "Ask your agent to prepare this PR again.",
    code="diff_mismatch",
  )
