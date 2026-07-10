"""GitHub connection routes: device flow, PAT fallback, read surface, submit.

Connect endpoints persist a token via app.github_auth (owner OR a
github_access app — so the Contribute app can drive connect from its
own UI — CSRF-guarded, rate-limited — INV4). github_access is a
connection-management grant, not a read scope: an app with it can
start/complete the connect flow, submit a PAT, and disconnect. A
normal connect still needs the owner to authorize on github.com or
paste their own token, but the grant itself is powerful — see the
get_owner_or_app_with_github_access docstring. The read surface
(/api/{path}, /graphql) is read-only by construction (INV2): the REST
passthrough registers GET only, and the GraphQL endpoint rejects any
document containing a mutation or subscription operation. GitHub writes
are limited to the Contribute submit endpoint, which consumes a single
prepared ledger record after the owner presses Send: it claims that
record, rechecks the reviewed branch/diff, pushes to the owner's fork,
and creates the pull request. An app-scoped github_access token may submit
only its own prepared record; it cannot act as a general GitHub write proxy.

The token itself never appears in any response or log line (INV1).
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import fs_locks, github_auth, models
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal,
  get_principal,
  get_owner_or_app_with_github_access,
  reject_cross_site,
)
from app.storage_io import atomic_write

router = APIRouter(prefix="/api/github", tags=["github"])
_limiter = Limiter(key_func=get_remote_address)
log = logging.getLogger("moebius.github")

_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105
_API_BASE = "https://api.github.com"

# Response cap + timeout mirror routes/proxy.py: GitHub payloads the
# dashboard needs are small; anything bigger is truncated, not buffered.
_MAX_BYTES = 2 * 1024 * 1024

# INV2 scanner. The single alternation matters: matching block strings,
# strings, and comments in ONE left-to-right pass means a `"""` inside a
# comment (or a `#` inside a string) can't confuse the scrubber into
# eating — or keeping — the wrong span, which a strip-strings-then-
# comments sequence would allow. Unterminated constructs simply don't
# match, so their content stays visible to the operation scan and an
# ambiguous document is rejected rather than trusted.
_GQL_NOISE = re.compile(
  r'"""(?:[^"]|"(?!""))*"""'  # block strings (may span lines)
  r'|"(?:\\.|[^"\\\n])*"'     # single-line strings with escapes
  r"|#[^\n]*"                 # comments
)
_GQL_WRITE_OP = re.compile(r"\b(?:mutation|subscription)\b", re.IGNORECASE)
_CONTRIBUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_GITHUB_REPO = re.compile(
  r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
)
_BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,160}$")
_GIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_COAUTHOR_TRAILER = (
  "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>"
)
_SUBMIT_TIMEOUT = 90
_PUSH_RETRIES = 3


class GithubTokenRequest(BaseModel):
  token: str


class GraphqlRequest(BaseModel):
  query: str
  variables: dict | None = None


class ContributionSubmitError(Exception):
  """A partner-actionable failure while submitting a prepared contribution."""

  def __init__(
    self,
    message: str,
    status_code: int = 409,
    *,
    record_patch: dict | None = None,
  ):
    super().__init__(message)
    self.message = message
    self.status_code = status_code
    self.record_patch = record_patch or {}


def _now_iso() -> str:
  return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _record_paths(app_id: int, record_id: str) -> tuple[Path, Path]:
  if not _CONTRIBUTION_ID.match(record_id):
    raise HTTPException(status_code=400, detail="Invalid contribution id.")
  base = Path(get_settings().data_dir) / "apps" / str(app_id)
  return (
    base / "contributions" / f"{record_id}.json",
    base / "contributions" / f"{record_id}.diff",
  )


def _read_record(path: Path) -> dict:
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except FileNotFoundError:
    raise HTTPException(status_code=404, detail="Contribution not found.")
  except (OSError, ValueError):
    raise HTTPException(status_code=400, detail="Contribution record is invalid.")
  if not isinstance(data, dict):
    raise HTTPException(status_code=400, detail="Contribution record is invalid.")
  return data


def _write_record(path: Path, record: dict) -> None:
  atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")


def _require_github_access_principal(
  principal: Principal, db: Session
) -> models.Owner:
  if principal.app_id is None:
    return principal.owner
  app = (
    db.query(models.App)
    .filter(models.App.id == principal.app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if not app:
    raise HTTPException(status_code=401, detail="App not found.")
  if bool(app.github_access):
    return principal.owner
  raise HTTPException(
    status_code=403,
    detail=(
      "This app needs permissions.github_access=true in its manifest "
      "to manage and read the GitHub connection on your behalf."
    ),
  )


def _validate_submit_app(
  app_id: int, principal: Principal, db: Session
) -> str | None:
  """Authorize a direct contribution submit and return the app token nonce."""
  _require_github_access_principal(principal, db)
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(
      status_code=403,
      detail="An app can only submit contributions from its own storage.",
    )
  app = (
    db.query(models.App)
    .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")
  return app.token_nonce


def _recheck_submit_app(db: Session, app_id: int, expected_nonce: str | None) -> None:
  row = (
    db.query(models.App)
    .populate_existing()
    .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if row is None or row.token_nonce != expected_nonce:
    raise HTTPException(status_code=404, detail="App not found.")


def _safe_repo_path(raw: object) -> Path:
  if not isinstance(raw, str) or not raw:
    raise ContributionSubmitError(
      "This record needs to be prepared again: it has no durable repo_path."
    )
  try:
    repo = Path(raw).resolve()
  except (OSError, RuntimeError):
    raise ContributionSubmitError("The staged repo path is invalid.")
  data_dir = Path(get_settings().data_dir).resolve()
  allowed_roots = (
    data_dir / "apps",
    data_dir / "platform",
    data_dir / "contributions",
  )
  if repo == allowed_roots[1]:
    return repo
  for root in (allowed_roots[0], allowed_roots[2]):
    try:
      repo.relative_to(root)
      return repo
    except ValueError:
      continue
  raise ContributionSubmitError(
    "This staged repo is outside the contribution source allowlist."
  )


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


def _git_env(repo: Path) -> dict:
  env = dict(os.environ)
  for var in (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
  ):
    env.pop(var, None)
  env["GIT_CEILING_DIRECTORIES"] = str(repo.resolve().parent)
  env["GIT_TERMINAL_PROMPT"] = "0"
  env["GH_PROMPT_DISABLED"] = "1"
  if github_auth.GH_AUTH_DIR.exists():
    env["GH_CONFIG_DIR"] = str(github_auth.GH_AUTH_DIR)
  return env


def _run_cmd(
  argv: list[str], *,
  cwd: Path,
  check: bool = True,
  timeout: int = _SUBMIT_TIMEOUT,
  env: dict | None = None,
) -> subprocess.CompletedProcess:
  proc = subprocess.run(
    argv,
    cwd=str(cwd),
    capture_output=True,
    text=True,
    timeout=timeout,
    check=False,
    env=env or _git_env(cwd),
  )
  if check and proc.returncode != 0:
    detail = (proc.stderr or proc.stdout or "command failed").strip()
    raise ContributionSubmitError(detail[:600] or "GitHub command failed.")
  return proc


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
  return _run_cmd(["git", "-C", str(repo), *args], cwd=repo, check=check)


def _gh(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
  return _run_cmd(["gh", *args], cwd=repo, check=check)


def _assert_clean_worktree(repo: Path) -> None:
  status = _git(repo, "status", "--porcelain").stdout.strip()
  if status:
    raise ContributionSubmitError(
      "This staged branch has uncommitted source changes. Ask your agent "
      "to prepare the PR again before submitting."
    )


def _assert_coauthor_trailer(repo: Path, branch: str) -> None:
  body = _git(repo, "log", "-1", "--format=%B", branch).stdout
  if _COAUTHOR_TRAILER not in body:
    raise ContributionSubmitError(
      "This staged commit is missing the Möbius Agent co-author trailer. "
      "Leave feedback so your agent can prepare it again."
    )


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
) -> dict:
  before = _head_commit_metadata(repo, branch)
  if (
    before["author_name"] == author_name
    and before["author_email"] == author_email
    and before["committer_name"] == author_name
    and before["committer_email"] == author_email
  ):
    return {}

  _git(
    repo,
    "-c", f"user.name={author_name}",
    "-c", f"user.email={author_email}",
    "commit", "--amend", "--no-edit", "--no-gpg-sign",
    "--author", f"{author_name} <{author_email}>",
    "--date", before["author_date"],
  )

  after = _head_commit_metadata(repo, branch)
  if after["tree"] != before["tree"]:
    raise ContributionSubmitError(
      "Normalizing commit attribution changed the staged source tree. "
      "Ask your agent to prepare this PR again."
    )
  if (
    after["author_name"] != author_name
    or after["author_email"] != author_email
    or after["committer_name"] != author_name
    or after["committer_email"] != author_email
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


def _assert_merges_with_upstream(
  repo: Path, upstream_repo: str, branch: str,
) -> dict:
  upstream_branch = _upstream_default_branch(repo, upstream_repo)
  remote_url = f"https://github.com/{upstream_repo}.git"
  ref_key = hashlib.sha256(
    f"{upstream_repo}\0{branch}\0{time.time_ns()}".encode("utf-8")
  ).hexdigest()[:24]
  upstream_ref = f"refs/mobius-submit/upstream-{ref_key}"
  preflight_patch = {"last_submit_upstream_branch": upstream_branch}
  try:
    fetched = _git(
      repo,
      "fetch", "--no-tags", "--force",
      remote_url,
      f"+refs/heads/{upstream_branch}:{upstream_ref}",
      check=False,
    )
    if fetched.returncode != 0:
      raise ContributionSubmitError(
        (
          "Could not verify that this PR merges with the upstream branch. "
          "Leave feedback so your agent can refresh it."
        ),
        record_patch=preflight_patch,
      ) from None
    upstream_sha = _git(
      repo, "rev-parse", "--verify", f"{upstream_ref}^{{commit}}",
    ).stdout.strip()
    if not _GIT_SHA.match(upstream_sha):
      raise ContributionSubmitError(
        "Could not resolve the upstream branch for this PR. Leave feedback "
        "so your agent can refresh it.",
        record_patch=preflight_patch,
      )
    preflight_patch["last_submit_upstream_sha"] = upstream_sha
    merged = _git(
      repo, "merge-tree", "--write-tree", upstream_sha, branch, check=False,
    )
    if merged.returncode != 0:
      raise ContributionSubmitError(
        (
          f"This PR no longer merges cleanly with upstream {upstream_branch}. "
          "Leave feedback so your agent can refresh the branch before it is "
          "pushed."
        ),
        record_patch=preflight_patch,
      )
    return preflight_patch
  finally:
    _git(repo, "update-ref", "-d", upstream_ref, check=False)


def _resolve_reviewed_commit(repo: Path, value: object, label: str) -> str:
  raw = str(value or "").strip()
  if not raw:
    raise ContributionSubmitError(
      f"This record needs to be prepared again: it has no reviewed {label}."
    )
  if not _GIT_SHA.match(raw):
    raise ContributionSubmitError(f"The reviewed {label} is invalid.")
  try:
    resolved = _git(
      repo, "rev-parse", "--verify", f"{raw}^{{commit}}"
    ).stdout.strip()
  except ContributionSubmitError:
    raise ContributionSubmitError(
      f"The reviewed {label} is not present in the staged repo."
    )
  if not _GIT_SHA.match(resolved):
    raise ContributionSubmitError(f"The reviewed {label} resolved incorrectly.")
  return resolved


def _reviewed_branch_diff(repo: Path, base_sha: str, head_sha: str) -> bytes:
  proc = _git(
    repo,
    "-c", "core.quotePath=false",
    "diff",
    "--no-ext-diff",
    "--no-color",
    "--binary",
    "--full-index",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    f"{base_sha}..{head_sha}",
  )
  return proc.stdout.encode("utf-8")


def _assert_fresh(
  record: dict, diff_path: Path, repo: Path, branch: str,
) -> tuple[str, str, str]:
  plan = record.get("plan") or {}
  expected_base = _resolve_reviewed_commit(repo, plan.get("base_sha"), "base sha")
  expected_head = _resolve_reviewed_commit(
    repo, plan.get("head_sha") or record.get("head_sha"), "head sha"
  )
  actual_head = _git(repo, "rev-parse", branch).stdout.strip()
  if actual_head != expected_head:
    raise ContributionSubmitError(
      "This branch changed after review. Ask your agent to refresh the "
      "Contribute card before submitting."
    )
  expected_diff = str(plan.get("diff_sha256") or "").strip()
  if not expected_diff:
    raise ContributionSubmitError(
      "This record needs to be prepared again: it has no reviewed diff hash."
    )
  try:
    diff_bytes = diff_path.read_bytes()
  except OSError:
    raise ContributionSubmitError(
      "The reviewed diff is missing. Ask your agent to prepare this again."
    )
  stored_hash = hashlib.sha256(diff_bytes).hexdigest()
  if stored_hash != expected_diff:
    raise ContributionSubmitError(
      "The reviewed diff changed. Ask your agent to refresh the "
      "Contribute card before submitting."
    )
  branch_hash = hashlib.sha256(
    _reviewed_branch_diff(repo, expected_base, expected_head)
  ).hexdigest()
  if branch_hash != expected_diff:
    raise ContributionSubmitError(
      "The reviewed diff does not match the branch that would be pushed. "
      "Ask your agent to prepare this PR again."
    )
  return expected_base, expected_head, expected_diff


def _claim_record(
  *, app_id: int, record_id: str, db: Session, expected_nonce: str | None
) -> tuple[dict, Path, Path]:
  record_path, diff_path = _record_paths(app_id, record_id)
  _recheck_submit_app(db, app_id, expected_nonce)
  record = _read_record(record_path)
  if record.get("status") != "prepared":
    raise HTTPException(
      status_code=409,
      detail="This contribution is no longer waiting for approval.",
    )
  plan = record.get("plan")
  if not isinstance(plan, dict):
    raise HTTPException(
      status_code=409,
      detail="This older contribution needs agent review before it can submit.",
    )
  if plan.get("action") != "pr" or record.get("type") != "pr":
    raise HTTPException(
      status_code=400,
      detail="Direct approval currently supports pull requests.",
    )
  now = _now_iso()
  claimed = {
    **record,
    "status": "submitting",
    "submitter": "contribute-button",
    "submit_started_at": now,
    "updated_at": now,
  }
  _write_record(record_path, claimed)
  return claimed, record_path, diff_path


def _mark_submit_failure(
  *,
  app_id: int,
  record_path: Path,
  message: str,
  record_patch: dict | None = None,
) -> dict | None:
  try:
    record = _read_record(record_path)
  except HTTPException:
    return None
  if record.get("status") != "submitting":
    return record
  next_record = {
    **record,
    **(record_patch or {}),
    "status": "prepared",
    "last_submit_error": message,
    "updated_at": _now_iso(),
  }
  _write_record(record_path, next_record)
  return next_record


def _mark_submit_success(
  *,
  record_path: Path,
  record: dict,
  pr_url: str,
  number: int | None,
  record_patch: dict | None = None,
) -> dict:
  now = _now_iso()
  next_record = {
    **record,
    **(record_patch or {}),
    "status": "open",
    "url": pr_url,
    "updated_at": now,
    "submitted_at": now,
  }
  if number is not None:
    next_record["number"] = number
  next_record.pop("last_submit_error", None)
  _write_record(record_path, next_record)
  return next_record


def _parse_pr_number(url: str) -> int | None:
  m = re.search(r"/pull/(\d+)(?:$|[/?#])", url)
  return int(m.group(1)) if m else None


def _find_existing_pr(repo: Path, upstream_repo: str, login: str, branch: str) -> str | None:
  proc = _gh(
    repo,
    "pr", "list",
    "-R", upstream_repo,
    "--head", f"{login}:{branch}",
    "--state", "open",
    "--json", "url",
    "--limit", "1",
    check=False,
  )
  if proc.returncode != 0:
    return None
  try:
    rows = json.loads(proc.stdout or "[]")
  except ValueError:
    return None
  if isinstance(rows, list) and rows:
    url = rows[0].get("url") if isinstance(rows[0], dict) else None
    if isinstance(url, str) and url.startswith("https://github.com/"):
      return url
  return None


def _github_remote_slug(remote_url: str) -> str | None:
  """Return owner/repo for GitHub remotes we can verify."""
  raw = str(remote_url or "").strip()
  if raw.startswith("git@github.com:"):
    path = raw.removeprefix("git@github.com:")
  else:
    parsed = urlparse(raw)
    if (parsed.hostname or "").lower() != "github.com":
      return None
    path = parsed.path.lstrip("/")
  path = path.removesuffix(".git").strip("/")
  parts = path.split("/")
  if len(parts) != 2 or not parts[0] or not parts[1]:
    return None
  return f"{parts[0]}/{parts[1]}"


def _ensure_owner_fork_remote(repo: Path, upstream_repo: str, login: str) -> None:
  """Make local remote `fork` point at the approving owner's fork."""
  repo_name = upstream_repo.split("/", 1)[1]
  expected_slug = f"{login}/{repo_name}"
  existing = _git(repo, "remote", "get-url", "fork", check=False)
  if existing.returncode == 0:
    actual_slug = _github_remote_slug(existing.stdout)
    if actual_slug and actual_slug.lower() == expected_slug.lower():
      return
    # The staged contribution checkout is disposable. Replacing a stale
    # remote is safer than pushing reviewed code to an ambient `fork` URL.
    _git(repo, "remote", "remove", "fork", check=False)

  _gh(
    repo,
    "repo", "fork", upstream_repo,
    "--remote", "--remote-name", "fork",
  )
  final = _git(repo, "remote", "get-url", "fork", check=False)
  final_slug = _github_remote_slug(final.stdout) if final.returncode == 0 else None
  if not final_slug or final_slug.lower() != expected_slug.lower():
    raise ContributionSubmitError(
      "Could not verify the fork remote for this GitHub account. "
      "Reconnect GitHub or ask the agent to prepare the contribution again."
    )


def _submit_prepared_pr(record: dict, diff_path: Path) -> tuple[str, int | None, dict]:
  if not shutil.which("git") or not shutil.which("gh"):
    raise ContributionSubmitError(
      "This platform needs git and gh installed before it can submit PRs.",
      status_code=409,
    )
  token = github_auth.get_token()
  state = github_auth.read_state() or {}
  login = str(state.get("login") or "")
  if not token or not login:
    raise ContributionSubmitError("Connect GitHub before approving this PR.", 401)
  author_name, author_email = _connected_git_identity(state, login)

  plan = record.get("plan") or {}
  upstream_repo = _validate_repo_slug(plan.get("repo") or record.get("repo"))
  branch = _validate_branch(plan.get("branch") or record.get("branch"))
  repo = _safe_repo_path(plan.get("repo_path"))
  if not (repo / ".git").exists():
    raise ContributionSubmitError("The staged repo is not a git checkout.")

  title = str(plan.get("title") or record.get("title") or "").strip()
  body = str(plan.get("body_draft") or "").strip()
  if not title:
    raise ContributionSubmitError("This prepared PR is missing a title.")
  if not body:
    raise ContributionSubmitError("This prepared PR is missing its reviewed body.")

  checkout_back = None
  try:
    current_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    checkout_back = (
      _git(repo, "rev-parse", "HEAD").stdout.strip()
      if current_branch == "HEAD"
      else current_branch
    )
    _git(repo, "check-ref-format", "--branch", branch)
    _assert_clean_worktree(repo)
    _git(repo, "checkout", "-q", branch)
    _assert_clean_worktree(repo)
    expected_base, _, expected_diff = _assert_fresh(record, diff_path, repo, branch)
    _assert_coauthor_trailer(repo, branch)
    record_patch = _normalize_head_attribution(
      repo,
      branch,
      author_name=author_name,
      author_email=author_email,
      base_sha=expected_base,
      expected_diff=expected_diff,
      record=record,
    )
    _assert_clean_worktree(repo)

    try:
      merge_patch = _assert_merges_with_upstream(repo, upstream_repo, branch)
      record_patch = _record_patch_with(record_patch, merge_patch)
    except ContributionSubmitError as exc:
      raise _merge_error_patch(exc, record_patch) from exc

    try:
      _ensure_owner_fork_remote(repo, upstream_repo, login)
    except ContributionSubmitError as exc:
      raise _merge_error_patch(exc, record_patch) from exc

    last_push_error = None
    for _ in range(_PUSH_RETRIES):
      proc = _git(
        repo,
        "push", "fork", f"HEAD:refs/heads/{branch}",
        check=False,
      )
      if proc.returncode == 0:
        last_push_error = None
        break
      last_push_error = (proc.stderr or proc.stdout or "").strip()
      time.sleep(2)
    if last_push_error:
      raise ContributionSubmitError(
        last_push_error[:600] or "Git push failed.",
        record_patch=record_patch,
      )
    pushed_branch_url = (
      f"https://github.com/{login}/{upstream_repo.split('/', 1)[1]}"
      f"/tree/{quote(branch, safe='/')}"
    )
    pushed_patch = {
      **record_patch,
      "last_submit_stage": "pushed",
      "last_pushed_branch": f"{login}:{branch}",
      "last_pushed_branch_url": pushed_branch_url,
    }

    existing = _find_existing_pr(repo, upstream_repo, login, branch)
    if existing:
      return existing, _parse_pr_number(existing), record_patch

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
      f.write(body)
      body_file = f.name
    try:
      try:
        create_args = [
          "pr", "create",
          "-R", upstream_repo,
          "-H", f"{login}:{branch}",
          "--title", title,
          "--body-file", body_file,
        ]
        pr = _gh(
          repo,
          *create_args,
        )
      except ContributionSubmitError as exc:
        raise ContributionSubmitError(
          f"{exc.message} The branch was pushed to {pushed_branch_url}.",
          exc.status_code,
          record_patch=pushed_patch,
        )
    finally:
      try:
        os.unlink(body_file)
      except OSError:
        pass
    url = (pr.stdout or "").strip().splitlines()[-1].strip()
    if not url.startswith("https://github.com/"):
      raise ContributionSubmitError(
        f"GitHub did not return a pull request URL. The branch was pushed "
        f"to {pushed_branch_url}.",
        record_patch=pushed_patch,
      )
    return url, _parse_pr_number(url), record_patch
  finally:
    if checkout_back:
      _git(repo, "checkout", "-q", checkout_back, check=False)


async def _github_user(token: str) -> tuple[int, str, int | None, list[str]]:
  """GET /user with `token`; returns (status, login, user_id, scopes).

  scopes come from the X-OAuth-Scopes response header — the only place
  GitHub reports a classic token's grants. login/user_id are "" / None
  on a non-200.
  """
  async with httpx.AsyncClient(timeout=15.0) as client:
    r = await client.get(
      f"{_API_BASE}/user",
      headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "mobius",
      },
    )
  scopes = [
    s.strip()
    for s in (r.headers.get("x-oauth-scopes") or "").split(",")
    if s.strip()
  ]
  if r.status_code != 200:
    return r.status_code, "", None, scopes
  data = r.json()
  return r.status_code, data.get("login") or "", data.get("id"), scopes


@router.post("/connect/start", dependencies=[Depends(reject_cross_site)])
@_limiter.limit("3/minute")
async def connect_start(
  request: Request,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Starts the GitHub device flow; returns the code the owner enters."""
  client_id = get_settings().github_oauth_client_id
  if not client_id:
    raise HTTPException(
      status_code=409,
      detail=(
        "Device flow is not configured on this instance "
        "(GITHUB_OAUTH_CLIENT_ID is unset). Connect with a classic "
        "personal access token instead."
      ),
    )
  try:
    async with httpx.AsyncClient(timeout=15.0) as client:
      r = await client.post(
        _DEVICE_CODE_URL,
        data={"client_id": client_id, "scope": "public_repo"},
        headers={"Accept": "application/json"},
      )
  except httpx.HTTPError:
    raise HTTPException(status_code=502, detail="Could not reach GitHub.")
  try:
    payload = r.json()
  except ValueError:
    payload = {}
  if payload.get("error") == "device_flow_disabled":
    raise HTTPException(
      status_code=409,
      detail=(
        "The configured GitHub OAuth app has the device flow disabled. "
        "Connect with a classic personal access token instead."
      ),
    )
  if r.status_code != 200 or "device_code" not in payload:
    log.error("GitHub device/code failed (%d)", r.status_code)
    raise HTTPException(
      status_code=502, detail="GitHub device flow could not be started.",
    )
  now = time.time()
  interval = int(payload.get("interval", 5))
  expires_in = int(payload.get("expires_in", 900))
  github_auth.set_device_flow({
    "device_code": payload["device_code"],
    "interval": interval,
    "next_poll_at": now + interval,
  })
  return {
    "user_code": payload["user_code"],
    "verification_uri": payload["verification_uri"],
    "expires_in": expires_in,
    "interval": interval,
  }


@router.post("/connect/poll", dependencies=[Depends(reject_cross_site)])
@_limiter.limit("30/minute")
async def connect_poll(
  request: Request,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Polls the in-flight device flow once.

  Statuses: none (no flow), pending (keep polling), failed (flow
  cleared; `reason` says why), complete (credentials stored). Polls
  arriving before GitHub's requested interval are answered pending
  WITHOUT an upstream call — the server enforces the pacing so an
  eager frontend can't trip GitHub's slow_down escalation.
  """
  flow = github_auth.get_device_flow()
  if not flow:
    return {"status": "none"}
  now = time.time()
  if now < flow["next_poll_at"]:
    return {"status": "pending"}
  try:
    async with httpx.AsyncClient(timeout=15.0) as client:
      r = await client.post(
        _ACCESS_TOKEN_URL,
        data={
          "client_id": get_settings().github_oauth_client_id,
          "device_code": flow["device_code"],
          "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
        headers={"Accept": "application/json"},
      )
  except httpx.HTTPError:
    raise HTTPException(status_code=502, detail="Could not reach GitHub.")
  try:
    payload = r.json()
  except ValueError:
    payload = {}
  error = payload.get("error")
  if error == "authorization_pending":
    flow["next_poll_at"] = now + flow["interval"]
    return {"status": "pending"}
  if error == "slow_down":
    # GitHub sends the new minimum interval; honor it, never shrink,
    # and always back off at least 5s beyond the previous pace.
    flow["interval"] = max(
      int(payload.get("interval", 0)), flow["interval"] + 5,
    )
    flow["next_poll_at"] = now + flow["interval"]
    return {"status": "pending"}
  if error:
    # expired_token / access_denied / anything unexpected: the flow is
    # dead either way — clear it so the frontend can offer a restart.
    github_auth.set_device_flow(None)
    return {"status": "failed", "reason": error}
  token = payload.get("access_token")
  if not token:
    github_auth.set_device_flow(None)
    return {"status": "failed", "reason": "no_access_token"}
  status, login, user_id, scopes = await _github_user(token)
  if status != 200 or not login:
    github_auth.set_device_flow(None)
    return {"status": "failed", "reason": "user_lookup_failed"}
  github_auth.write_credentials(
    token=token, login=login, user_id=user_id, scopes=scopes,
    source="device",
  )
  github_auth.set_device_flow(None)
  return {"status": "complete", "login": login}


@router.post("/connect/token", dependencies=[Depends(reject_cross_site)])
@_limiter.limit("5/minute")
async def connect_token(
  request: Request,
  body: GithubTokenRequest,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Connects GitHub with a pasted classic personal access token."""
  token = body.token.strip()
  if token.startswith("github_pat_"):
    raise HTTPException(
      status_code=400,
      detail=(
        "That is a fine-grained personal access token — GitHub does "
        "not let those write to public repos you don't own, so it "
        "can't open pull requests upstream. Use a classic token with "
        "the public_repo scope (or the device flow)."
      ),
    )
  if not token:
    raise HTTPException(status_code=400, detail="Token is empty.")
  status, login, user_id, scopes = await _github_user(token)
  if status != 200 or not login:
    raise HTTPException(
      status_code=400, detail="GitHub rejected the token.",
    )
  if "repo" not in scopes and "public_repo" not in scopes:
    granted = ", ".join(scopes) if scopes else "none"
    raise HTTPException(
      status_code=400,
      detail=(
        "The token lacks the public_repo (or repo) scope needed to "
        f"contribute — its scopes are: {granted}."
      ),
    )
  github_auth.write_credentials(
    token=token, login=login, user_id=user_id, scopes=scopes, source="pat",
  )
  return {"login": login}


@router.get("/status")
async def github_status(
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Connection metadata for the Contribute app's UI. Never the token
  (INV1).

  Gated on github_access like the rest of the surface: status still discloses
  the owner's GitHub login + scope list, so an app without the grant shouldn't
  read it. The owner (Settings) always passes; the Contribute app holds the
  grant. (A malicious same-origin app can already read the owner JWT and call
  the granted endpoints directly — this is least-privilege consistency, not a
  new boundary.)"""
  state = github_auth.read_state() or {}
  connected = bool(state.get("token"))
  return {
    "connected": connected,
    "login": state.get("login") if connected else None,
    "scopes": (state.get("scopes") or []) if connected else [],
    "token_source": state.get("token_source") if connected else None,
    "device_flow_available": bool(get_settings().github_oauth_client_id),
    "gh_version": github_auth.gh_version(),
  }


@router.delete("/connect", dependencies=[Depends(reject_cross_site)])
@_limiter.limit("5/minute")
def github_disconnect(
  request: Request,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Disconnects GitHub — removes the stored credentials."""
  github_auth.clear_credentials()
  return {"ok": True}


@router.post(
  "/contributions/{app_id}/{record_id}/submit",
  dependencies=[Depends(reject_cross_site)],
)
@_limiter.limit("10/minute")
async def submit_contribution(
  request: Request,
  app_id: int,
  record_id: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Submit one prepared Contribute record as a pull request.

  This is the owner-confirmed button path. The Contribute app stores a prepared
  record + reviewed diff; this endpoint claims that record, rechecks freshness,
  pushes the already-prepared branch to the owner's fork, creates the PR, then
  writes the GitHub URL back to the ledger. It does not expose the GitHub token
  to the app, and an app-scoped token can only submit a record from that app's
  own storage after the same server-side freshness checks pass.
  """
  expected_nonce = _validate_submit_app(app_id, principal, db)
  async with fs_locks.app_storage_lock(app_id):
    claimed, record_path, diff_path = _claim_record(
      app_id=app_id,
      record_id=record_id,
      db=db,
      expected_nonce=expected_nonce,
    )

  try:
    plan = claimed.get("plan") or {}
    repo_path = _safe_repo_path(plan.get("repo_path"))
    async with fs_locks.source_dir_lock(str(repo_path)):
      pr_url, number, record_patch = await asyncio.to_thread(
        _submit_prepared_pr, claimed, diff_path,
      )
  except ContributionSubmitError as exc:
    async with fs_locks.app_storage_lock(app_id):
      _recheck_submit_app(db, app_id, expected_nonce)
      record = _mark_submit_failure(
        app_id=app_id,
        record_path=record_path,
        message=exc.message,
        record_patch=exc.record_patch,
      )
    raise HTTPException(
      status_code=exc.status_code,
      detail={"message": exc.message, "record": record},
    )
  except Exception as exc:
    log.exception("Contribution submit failed for %s/%s", app_id, record_id)
    message = "Could not submit this PR. Leave feedback so your agent can retry."
    async with fs_locks.app_storage_lock(app_id):
      _recheck_submit_app(db, app_id, expected_nonce)
      record = _mark_submit_failure(
        app_id=app_id, record_path=record_path, message=message,
      )
    raise HTTPException(
      status_code=500,
      detail={"message": message, "record": record},
    ) from exc

  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    current = _read_record(record_path)
    if current.get("status") != "submitting":
      raise HTTPException(
        status_code=409,
        detail="This contribution changed while the PR was being created.",
      )
    submitted = _mark_submit_success(
      record_path=record_path,
      record=current,
      pr_url=pr_url,
      number=number,
      record_patch=record_patch,
    )
  return {"record": submitted, "url": pr_url, "number": number}


async def _forward_capped(
  client: httpx.AsyncClient, req: httpx.Request
) -> Response:
  """Sends `req` streaming and reads at most _MAX_BYTES (the
  routes/proxy.py idiom — the cap bounds memory BEFORE the body is
  buffered). Surfaces X-RateLimit-Remaining so callers can self-pace.
  Failure details stay generic: the request carries the GitHub token
  in its Authorization header and must never be echoed (INV1)."""
  try:
    r = await client.send(req, stream=True)
  except httpx.HTTPError:
    raise HTTPException(status_code=502, detail="GitHub request failed.")
  try:
    buf = bytearray()
    async for chunk in r.aiter_bytes():
      room = _MAX_BYTES - len(buf)
      buf.extend(chunk[:room])
      if len(buf) >= _MAX_BYTES:
        break
    headers = {}
    remaining = r.headers.get("x-ratelimit-remaining")
    if remaining is not None:
      headers["X-RateLimit-Remaining"] = remaining
    return Response(
      content=bytes(buf),
      status_code=r.status_code,
      media_type=r.headers.get("content-type", "application/json"),
      headers=headers,
    )
  finally:
    await r.aclose()


@router.get("/api/{path:path}")
@_limiter.limit("120/minute")
async def github_rest(
  request: Request,
  path: str,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Authenticated GET passthrough to api.github.com (INV2: only GET
  is registered, so the surface is read-only by construction)."""
  token = github_auth.get_token()
  if not token:
    raise HTTPException(status_code=401, detail="GitHub not connected.")
  # urljoin resolves any ../, //host, or absolute-URL smuggling in the
  # captured path; the result must still land on api.github.com.
  target = urljoin(_API_BASE + "/", path)
  parsed = urlparse(target)
  if parsed.scheme != "https" or parsed.netloc != "api.github.com":
    raise HTTPException(
      status_code=400, detail="Path resolves outside api.github.com.",
    )
  if request.url.query:
    target = f"{target}?{request.url.query}"
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    req = client.build_request("GET", target, headers={
      "Authorization": f"Bearer {token}",
      "Accept": (
        request.headers.get("accept") or "application/vnd.github+json"
      ),
      "User-Agent": "mobius",
    })
    return await _forward_capped(client, req)


@router.post("/graphql")
@_limiter.limit("60/minute")
async def github_graphql(
  request: Request,
  body: GraphqlRequest,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Read-only GraphQL passthrough to api.github.com/graphql.

  INV2: the document is scrubbed of strings + comments, then rejected
  if a mutation/subscription keyword remains. The word inside a string
  literal is data, not an operation, and passes; a keyword the scrubber
  can't prove inert is rejected.
  """
  token = github_auth.get_token()
  if not token:
    raise HTTPException(status_code=401, detail="GitHub not connected.")
  scrubbed = _GQL_NOISE.sub(" ", body.query)
  if _GQL_WRITE_OP.search(scrubbed):
    raise HTTPException(
      status_code=400,
      detail=(
        "This surface is read-only: mutations and subscriptions are "
        "not allowed. GitHub writes go through the agent with your "
        "explicit approval."
      ),
    )
  payload: dict = {"query": body.query}
  if body.variables is not None:
    payload["variables"] = body.variables
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    req = client.build_request(
      "POST", f"{_API_BASE}/graphql", json=payload, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "mobius",
      },
    )
    return await _forward_capped(client, req)
