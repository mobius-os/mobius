"""GitHub connection routes: device flow, PAT fallback, read surface, submit.

Connect endpoints persist a token via app.github_auth (owner OR a
github_access app — so the Contribute app can drive connect from its
own UI — CSRF-guarded, rate-limited — INV4). github_access is a
connection-management grant, not a read scope: an app with it can
start/complete the connect flow, submit a PAT, and disconnect. A
normal connect still needs the owner to authorize on github.com or
paste their own token, but the grant itself is powerful — see the
get_owner_or_app_with_github_access docstring. The remote read surface
(/api/{path}, /graphql) is read-only by construction (INV2): the REST
passthrough registers GET only, and the GraphQL endpoint rejects any
document containing a mutation or subscription operation. GitHub writes
are limited to the Contribute submit endpoint, which consumes a single
prepared ledger record after the owner presses Send: it claims that
record, rechecks the reviewed branch/diff, pushes to the owner's fork,
and creates the pull request. An app-scoped github_access token may submit
only its own prepared record; it cannot act as a general GitHub write proxy.

The fetch-free /source-status read is the local companion for Contribute's
Sources view. It exposes only sanitized repository identity, refs, diff
magnitudes, and capped relative path names — never source contents, absolute
paths, raw remotes, or credentials — so Contribute does not need the broader
filesystem capability merely to explain where local work sits.

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

from app import fs_locks, github_auth, models, source_status
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal,
  get_principal,
  get_owner_or_app_with_github_access,
  reject_cross_site,
)
from app.push import notify_owner
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

# The classic-token creation URL with the required scope + description
# pre-filled. Fine-grained tokens (github_pat_…) can't push to or open PRs
# on public repos the owner doesn't own, so contributing upstream needs a
# classic token with public_repo — this link creates exactly that.
_CLASSIC_TOKEN_URL = (
  "https://github.com/settings/tokens/new"
  "?scopes=public_repo&description=Mobius%20Contribute"
)
_CLASSIC_WORKFLOW_TOKEN_URL = (
  "https://github.com/settings/tokens/new"
  "?scopes=public_repo,workflow&description=Mobius%20Contribute"
)


class GithubTokenRequest(BaseModel):
  token: str


class GithubConnectStartRequest(BaseModel):
  workflow: bool = False


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
  # A durable repo must live under one of these roots so a restart can find it
  # again. "contrib" is the de-facto staging root the agent prepares work in
  # (often nested, e.g. contrib/<audit>/<slug>); the plural "contributions" is
  # kept alongside it for back-compat with older docs that named that form.
  allowed_roots = (
    data_dir / "contrib",
    data_dir / "apps",
    data_dir / "platform",
    data_dir / "contributions",
  )
  for root in allowed_roots:
    try:
      repo.relative_to(root)
      return repo
    except ValueError:
      continue
  raise ContributionSubmitError(
    "This prepared PR was staged outside Mobius' durable contribution folders. "
    "Ask the agent to prepare it again from /data/contrib, /data/apps, or "
    "/data/platform; nothing was sent to GitHub."
  )


def _cleanup_terminal_staging_checkout(record: dict) -> bool:
  """Remove only disposable contribution clones for terminal records."""
  if record.get("status") not in {"merged", "closed"}:
    return False
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  repo = _safe_repo_path(plan.get("repo_path"))
  data_dir = Path(get_settings().data_dir).resolve()
  roots = (data_dir / "contrib", data_dir / "contributions")
  if not any(repo.is_relative_to(root) for root in roots):
    return False
  if not (repo / ".git").exists():
    return False
  shutil.rmtree(repo)
  return True


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


def _ensure_owner_fork_remote(repo: Path, upstream_repo: str, login: str) -> str:
  """Make local remote `fork` point at the approving owner's fork."""
  existing = _git(repo, "remote", "get-url", "fork", check=False)
  if existing.returncode == 0:
    actual_slug = _github_remote_slug(existing.stdout)
    if actual_slug and actual_slug.split("/", 1)[0].lower() == login.lower():
      return actual_slug
    # The staged contribution checkout is disposable. Replacing a stale
    # remote is safer than pushing reviewed code to an ambient `fork` URL.
    _git(repo, "remote", "remove", "fork", check=False)

  origin = _git(repo, "remote", "get-url", "origin", check=False)
  origin_slug = (
    _github_remote_slug(origin.stdout) if origin.returncode == 0 else None
  )
  if not origin_slug or origin_slug.lower() != upstream_repo.lower():
    _git(
      repo,
      "remote", "set-url" if origin.returncode == 0 else "add",
      "origin", f"https://github.com/{upstream_repo}.git",
    )

  # gh 2.96 rejects --remote with a repository argument; origin selects
  # the upstream repo for the in-repo fork command.
  _gh(repo, "repo", "fork", "--remote", "--remote-name", "fork")
  final = _git(repo, "remote", "get-url", "fork", check=False)
  final_slug = _github_remote_slug(final.stdout) if final.returncode == 0 else None
  if not final_slug or final_slug.split("/", 1)[0].lower() != login.lower():
    raise ContributionSubmitError(
      "Could not verify the fork remote for this GitHub account. "
      "Reconnect GitHub or ask the agent to prepare the contribution again."
    )
  return final_slug


def _inspect_owner_fork_default_branch(
  repo: Path,
  fork_slug: str,
  *,
  upstream_branch: str,
  upstream_sha: str,
) -> dict:
  """Inspect a reusable PR fork without mutating its default branch.

  GitHub rejects an OAuth push that would introduce a new or changed Actions
  workflow to a repository unless the token also has the broad `workflow`
  scope. The same restriction applies to GitHub's merge-upstream endpoint, so
  a public_repo-only connection cannot refresh a stale fork that crossed a
  workflow change. Instead, a strictly-behind fork is handled by preparing the
  reviewed change on its existing tip; the fork's default branch stays put.

  A current fork (or one containing upstream) can receive the reviewed branch
  normally. A diverged default branch is left untouched and stops submission.
  """
  upstream_branch = _validate_branch(upstream_branch)
  if not _GIT_SHA.match(str(upstream_sha or "")):
    raise ContributionSubmitError(
      "Could not resolve the upstream tip before inspecting the PR fork."
    )
  fork_branch = _upstream_default_branch(repo, fork_slug)
  fork_url = f"https://github.com/{fork_slug}.git"
  ref_key = hashlib.sha256(
    f"{fork_slug}\0{fork_branch}\0{time.time_ns()}".encode("utf-8")
  ).hexdigest()[:24]
  fork_ref = f"refs/mobius-submit/fork-{ref_key}"
  fork_heads_prefix = f"refs/mobius-submit/fork-heads-{ref_key}"
  patch = {
    "last_submit_fork_branch": fork_branch,
    "last_submit_upstream_branch": upstream_branch,
  }

  def fetch_fork_tip() -> str:
    fetched = _git(
      repo,
      "fetch", "--no-tags", "--force",
      fork_url,
      f"+refs/heads/{fork_branch}:{fork_ref}",
      check=False,
    )
    if fetched.returncode != 0:
      raise ContributionSubmitError(
        "Could not inspect the GitHub fork before pushing this PR. Try Send "
        "again, or leave feedback if it keeps failing.",
        record_patch=patch,
      ) from None
    fork_sha = _git(
      repo, "rev-parse", "--verify", f"{fork_ref}^{{commit}}",
    ).stdout.strip()
    if not _GIT_SHA.match(fork_sha):
      raise ContributionSubmitError(
        "Could not resolve the GitHub fork's default branch before pushing.",
        record_patch=patch,
      )
    return fork_sha

  def is_ancestor(older: str, newer: str) -> bool:
    result = _git(
      repo, "merge-base", "--is-ancestor", older, newer, check=False,
    )
    if result.returncode not in (0, 1):
      raise ContributionSubmitError(
        "Could not compare the GitHub fork with current upstream.",
        record_patch=patch,
      )
    return result.returncode == 0

  try:
    fork_sha = fetch_fork_tip()
    patch["last_submit_fork_sha"] = fork_sha
    if fork_sha == upstream_sha:
      return {**patch, "last_submit_fork_sync": "current"}
    if is_ancestor(upstream_sha, fork_sha):
      return {**patch, "last_submit_fork_sync": "contains-upstream"}
    if not is_ancestor(fork_sha, upstream_sha):
      raise ContributionSubmitError(
        f"Your PR fork's {fork_branch} branch has diverged from upstream, so "
        "Contribute left it untouched. Review that fork on GitHub or leave "
        "feedback for your agent before trying again.",
        record_patch={**patch, "last_submit_fork_sync": "diverged"},
      )

    # GitHub's own "Update branch" action can merge current upstream into a
    # topic branch while leaving the reusable fork's default branch stale. In
    # that case the workflow-bearing upstream commits already exist in the
    # fork, so a public_repo-only token may safely create another reviewed
    # topic ref without introducing workflow history. Discover that carrier
    # branch before asking for broader workflow access.
    fetched_heads = _git(
      repo,
      "fetch", "--no-tags", "--force",
      fork_url,
      f"+refs/heads/*:{fork_heads_prefix}/*",
      check=False,
    )
    if fetched_heads.returncode == 0:
      refs = _git(
        repo,
        "for-each-ref", "--format=%(refname)%00%(objectname)",
        f"{fork_heads_prefix}/",
      ).stdout.splitlines()
      for row in refs:
        ref, separator, tip = row.partition("\0")
        if not separator or not _GIT_SHA.match(tip):
          continue
        if is_ancestor(upstream_sha, tip):
          carrier = ref.removeprefix(f"{fork_heads_prefix}/")
          return {
            **patch,
            "last_submit_fork_sync": "contains-upstream",
            "last_submit_fork_carrier_branch": carrier,
            "last_submit_fork_carrier_sha": tip,
          }
    return {**patch, "last_submit_fork_sync": "strictly-behind"}
  finally:
    _git(repo, "update-ref", "-d", fork_ref, check=False)
    refs = _git(
      repo,
      "for-each-ref", "--format=%(refname)", f"{fork_heads_prefix}/",
      check=False,
    )
    if refs.returncode == 0:
      for ref in (refs.stdout or "").splitlines():
        _git(repo, "update-ref", "-d", ref, check=False)


def _build_fork_compatible_topic_commit(
  repo: Path,
  *,
  branch: str,
  fork_sha: str,
  upstream_sha: str,
  diff_path: Path,
  expected_diff: str,
  author_name: str,
  author_email: str,
) -> str:
  """Re-parent an exact reviewed change onto a strictly-behind fork tip.

  The fork default branch is never changed. The temporary topic commit is
  accepted only when merging it into current upstream produces the exact
  reviewed source diff byte-for-byte. This avoids OAuth's workflow restriction
  without weakening review or silently changing the contribution.
  """
  message = _git(repo, "log", "-1", "--format=%B", branch).stdout
  if _COAUTHOR_TRAILER not in message:
    raise ContributionSubmitError(
      "This staged commit is missing the Möbius Agent co-author trailer. "
      "Leave feedback so your agent can prepare it again."
    )

  message_path = None
  detached = False
  try:
    with tempfile.NamedTemporaryFile(
      "w", encoding="utf-8", delete=False,
    ) as message_file:
      message_file.write(message)
      message_path = message_file.name

    _git(repo, "checkout", "-q", "--detach", fork_sha)
    detached = True
    applied = _git(
      repo,
      "apply", "--index", "--3way", "--binary", str(diff_path),
      check=False,
    )
    if applied.returncode != 0:
      raise ContributionSubmitError(
        "The reviewed change cannot be placed safely on this stale PR fork. "
        "Leave feedback so your agent can refresh the contribution."
      )

    workflows = _git(
      repo, "diff", "--cached", "--name-only", "--", ".github/workflows",
    ).stdout.strip()
    if workflows:
      raise ContributionSubmitError(
        "This reviewed contribution changes a GitHub Actions workflow. "
        "Reconnect GitHub with a classic token granting public_repo and "
        "workflow, then try Send again."
      )

    _git(
      repo,
      "-c", f"user.name={author_name}",
      "-c", f"user.email={author_email}",
      "commit", "--no-gpg-sign", "-F", message_path,
    )
    push_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if not _GIT_SHA.match(push_sha):
      raise ContributionSubmitError(
        "Could not prepare the reviewed branch for this stale PR fork."
      )

    merged = _git(
      repo, "merge-tree", "--write-tree", upstream_sha, push_sha,
      check=False,
    )
    merged_tree = (merged.stdout or "").strip().splitlines()[0:1]
    if (
      merged.returncode != 0
      or not merged_tree
      or not _GIT_SHA.match(merged_tree[0])
    ):
      raise ContributionSubmitError(
        "The stale-fork branch no longer merges cleanly with upstream. Leave "
        "feedback so your agent can refresh the contribution."
      )
    merged_hash = hashlib.sha256(
      _reviewed_branch_diff(repo, upstream_sha, merged_tree[0])
    ).hexdigest()
    if merged_hash != expected_diff:
      raise ContributionSubmitError(
        "Adapting this branch to the stale PR fork would change the reviewed "
        "result, so Contribute stopped before pushing anything."
      )
    return push_sha
  finally:
    if message_path:
      try:
        os.unlink(message_path)
      except OSError:
        pass
    if detached:
      _git(repo, "reset", "--hard", fork_sha, check=False)
      _git(repo, "checkout", "-q", branch, check=False)


def _sync_owner_fork_with_workflow_scope(
  repo: Path,
  fork_slug: str,
  *,
  upstream_branch: str,
  upstream_sha: str,
) -> dict:
  """Fast-forward a proven-behind fork when the owner granted workflow scope."""
  synced = _gh(
    repo,
    "api", "--method", "POST",
    f"repos/{fork_slug}/merge-upstream",
    "-f", f"branch={_validate_branch(upstream_branch)}",
    check=False,
  )
  if synced.returncode != 0:
    detail = (synced.stderr or synced.stdout or "").strip()
    raise ContributionSubmitError(
      detail[:400] or "GitHub could not bring the PR fork up to date."
    )

  verified = _inspect_owner_fork_default_branch(
    repo,
    fork_slug,
    upstream_branch=upstream_branch,
    upstream_sha=upstream_sha,
  )
  if verified.get("last_submit_fork_sync") not in {
    "current", "contains-upstream",
  }:
    raise ContributionSubmitError(
      "GitHub did not finish refreshing the PR fork, so Contribute stopped "
      "before pushing the reviewed branch.",
      record_patch=verified,
    )
  return {**verified, "last_submit_fork_sync": "fast-forwarded"}


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
      fork_slug = _ensure_owner_fork_remote(repo, upstream_repo, login)
    except ContributionSubmitError as exc:
      raise _merge_error_patch(exc, record_patch) from exc
    record_patch = _record_patch_with(record_patch, {"head_repository": fork_slug})

    try:
      fork_sync_patch = _inspect_owner_fork_default_branch(
        repo,
        fork_slug,
        upstream_branch=str(merge_patch["last_submit_upstream_branch"]),
        upstream_sha=str(merge_patch["last_submit_upstream_sha"]),
      )
      record_patch = _record_patch_with(record_patch, fork_sync_patch)
    except ContributionSubmitError as exc:
      raise _merge_error_patch(exc, record_patch) from exc

    push_source = "HEAD"
    if fork_sync_patch.get("last_submit_fork_sync") == "strictly-behind":
      try:
        push_source = _build_fork_compatible_topic_commit(
          repo,
          branch=branch,
          fork_sha=str(fork_sync_patch["last_submit_fork_sha"]),
          upstream_sha=str(merge_patch["last_submit_upstream_sha"]),
          diff_path=diff_path,
          expected_diff=expected_diff,
          author_name=author_name,
          author_email=author_email,
        )
        record_patch = _record_patch_with(record_patch, {
          "last_submit_fork_sync": "stale-base-compatible",
          "last_submit_push_sha": push_source,
        })
      except ContributionSubmitError as exc:
        granted_scopes = set(state.get("scopes") or [])
        if "workflow" not in granted_scopes:
          raise ContributionSubmitError(
            "This reviewed change depends on newer code in a stale PR fork. "
            "In Contribute, enable optional workflow access, then try Send "
            "again; Contribute will fast-forward only that fork's default "
            "branch before pushing the reviewed topic branch.",
            record_patch=_record_patch_with(record_patch, {
              "last_submit_requires_workflow_scope": True,
              "last_submit_compatible_error": exc.message,
            }),
          ) from exc
        try:
          synced_patch = _sync_owner_fork_with_workflow_scope(
            repo,
            fork_slug,
            upstream_branch=str(merge_patch["last_submit_upstream_branch"]),
            upstream_sha=str(merge_patch["last_submit_upstream_sha"]),
          )
          record_patch = _record_patch_with(record_patch, synced_patch)
          push_source = "HEAD"
        except ContributionSubmitError as sync_exc:
          raise _merge_error_patch(sync_exc, record_patch) from sync_exc

    last_push_error = None
    for _ in range(_PUSH_RETRIES):
      proc = _git(
        repo, "push", "fork", f"{push_source}:refs/heads/{branch}",
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
      f"https://github.com/{fork_slug}/tree/{quote(branch, safe='/')}"
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
  body: GithubConnectStartRequest | None = None,
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
    scopes = "public_repo workflow" if body and body.workflow else "public_repo"
    async with httpx.AsyncClient(timeout=15.0) as client:
      r = await client.post(
        _DEVICE_CODE_URL,
        data={"client_id": client_id, "scope": scopes},
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
    "requested_scopes": scopes.split(),
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
        "That's a fine-grained personal access token (github_pat_…). "
        "Fine-grained tokens can only reach repositories you own or are "
        "explicitly granted, so they can't push to or open pull requests "
        "on the upstream public repos Contribute targets. Create a classic "
        "token with the public_repo scope instead — this link pre-fills it: "
        f"{_CLASSIC_TOKEN_URL} (or use the device flow)."
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
    "classic_token_url": _CLASSIC_TOKEN_URL,
    "classic_workflow_token_url": _CLASSIC_WORKFLOW_TOKEN_URL,
    "gh_version": github_auth.gh_version(),
  }


@router.get("/source-status")
async def github_source_status(
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
  db: Session = Depends(get_db),
):
  """Fetch-free local source map for the Contribute app.

  Returns refs, diff magnitudes, and working-tree metadata for the platform and
  every live app source repository.  It deliberately does not fetch remotes,
  expose source contents/absolute paths, or grant Contribute the much broader
  filesystem capability.  App reads take the same per-source lock as the
  watcher and installer, so a commit/update cannot split one status snapshot.
  """
  rows = (
    db.query(models.App)
    .filter(
      models.App.deleted_at.is_(None),
      models.App.source_dir.isnot(None),
    )
    .order_by(models.App.name.asc())
    .all()
  )
  apps = [{
    "id": row.id,
    "name": row.name,
    "slug": row.slug,
    "version": row.version,
    "manifest_url": row.manifest_url,
    "source_dir": row.source_dir,
  } for row in rows]

  # The repository scan may wait on the same source lock held by an app
  # compile/update. Do not keep a database connection checked out across that
  # wait: the compiler also needs the pool before it can release its lock, so
  # overlapping map refreshes would otherwise create a lock-order deadlock.
  # FastAPI's dependency finalizer will close again; SQLAlchemy close is safe
  # and idempotent.
  db.close()

  platform = await asyncio.to_thread(source_status.build_platform_status)
  semaphore = asyncio.Semaphore(4)

  async def inspect(app: dict) -> dict | None:
    async with semaphore:
      async with fs_locks.source_dir_lock(app["source_dir"]):
        try:
          return await asyncio.to_thread(source_status.build_app_status, app)
        except Exception:
          # One damaged checkout must not blank the complete repository map.
          # The omitted app can recover on the next refresh after its source is
          # repaired, while every healthy source remains useful now.
          log.warning(
            "Could not inspect source status for app %s",
            app.get("id"),
            exc_info=True,
          )
          return None

  inspected = await asyncio.gather(*(inspect(app) for app in apps))
  projects = [item for item in inspected if item is not None]
  projects.sort(key=lambda item: item["name"].casefold())
  return {
    "schema": 1,
    "generated_at": _now_iso(),
    "fetch_free": True,
    "platform": platform,
    "apps": projects,
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


@router.post(
  "/contributions/{app_id}/{record_id}/cleanup-staging",
  dependencies=[Depends(reject_cross_site)],
)
async def cleanup_contribution_staging(
  request: Request,
  app_id: int,
  record_id: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Delete a terminal contribution's disposable local clone only."""
  expected_nonce = _validate_submit_app(app_id, principal, db)
  async with fs_locks.app_storage_lock(app_id):
    _recheck_submit_app(db, app_id, expected_nonce)
    record_path, _ = _record_paths(app_id, record_id)
    record = _read_record(record_path)
  plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
  repo = _safe_repo_path(plan.get("repo_path"))
  async with fs_locks.source_dir_lock(str(repo)):
    cleaned = await asyncio.to_thread(_cleanup_terminal_staging_checkout, record)
  return {"cleaned": cleaned}


# --- contribution CI feedback (checks refresh + classification) -------
#
# THE `checks` CONTRACT (feature 196). Written onto a contribution record
# under the top-level `checks` key, ORTHOGONAL to the lifecycle `status`
# enum — a record can be `open` with failing `checks`, and refreshing
# checks never advances the lifecycle. The Contribute app UI reads this
# shape, so it is a stable contract; extend it additively.
#
#   "checks": {
#     "state":       overall statusCheckRollup state — one of
#                    "SUCCESS" | "FAILURE" | "PENDING" | "ERROR" |
#                    "EXPECTED" | null (null = no checks reported yet),
#     "head_sha":    the PR head commit these results were observed at,
#     "pr_state":    "OPEN" | "MERGED" | "CLOSED" (PR lifecycle on GitHub,
#                    NOT the ledger status),
#     "base_ref":    upstream base branch the PR targets (e.g. "main"),
#     "jobs": [ {
#         "name":          check/context name (e.g. "e2e"),
#         "conclusion":    "SUCCESS" | "FAILURE" | "TIMED_OUT" | ... | null
#                          (null = still running),
#         "status":        CheckRun status ("COMPLETED"/"IN_PROGRESS"/…) or
#                          null for legacy StatusContexts,
#         "url":           details URL for the run/context,
#         "classification": present ONLY on failing jobs — "inherited"
#                          (same-named check also red on upstream base),
#                          "suspect-pr-caused" (green on base, red here), or
#                          "unknown" (base data unavailable),
#     } ],
#     "observed_at": ISO-8601 timestamp of this refresh,
#     "notified_sha": last head SHA a failure notification fired for; the
#                     dedupe key so one red result notifies exactly once.
#   }

# A PR whose checks we still track. Merged/closed PRs and non-PR records
# are skipped — a merged PR's red check is moot.
_ACTIVE_PR_STATUSES = frozenset({"open", "draft"})

# Check conclusions that count as red. GraphQL reports these uppercase; the
# REST check-runs API reports them lowercase, so `_is_failing` uppercases
# before comparing and both sources land here. CANCELLED is deliberately
# excluded: a cancelled run is inconclusive, not a failure.
_FAILING_CONCLUSIONS = frozenset({
  "FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED",
})

_CLASSIFICATION_PHRASE = {
  "inherited": "inherited (also red on upstream main)",
  "suspect-pr-caused": "suspect (PR-caused)",
  "unknown": "unclassified",
}

# One batched GraphQL round-trip fetches statusCheckRollup for every tracked
# PR head. Aliases (pr0, pr1, …) and per-alias variables ($pr0o/$pr0n/$pr0p)
# keep repo owner/name/number out of the query string (no injection) while
# following github.py's existing variables-not-interpolation idiom.
_PR_CHECKS_FRAGMENT = """
fragment prChecks on PullRequest {
  number
  state
  isDraft
  baseRefName
  url
  commits(last: 1) {
    nodes {
      commit {
        oid
        statusCheckRollup {
          state
          contexts(first: 100) {
            nodes {
              __typename
              ... on CheckRun { name conclusion status detailsUrl }
              ... on StatusContext { context state targetUrl }
            }
          }
        }
      }
    }
  }
}
""".strip()


def _contributions_dir(app_id: int) -> Path:
  return Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"


def _read_record_tolerant(path: Path) -> dict | None:
  """Reads a contribution record, returning None (not raising) on a missing
  or corrupt file so one bad record can't abort a whole refresh sweep."""
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return None
  return data if isinstance(data, dict) else None


def _is_failing(conclusion: object) -> bool:
  return str(conclusion or "").upper() in _FAILING_CONCLUSIONS


def _pr_ref(record: dict) -> tuple[str, int] | None:
  """Returns (upstream_repo, pr_number) for a trackable PR record, else None."""
  repo = record.get("repo") or (record.get("plan") or {}).get("repo")
  if not isinstance(repo, str) or not _GITHUB_REPO.match(repo):
    return None
  try:
    number = int(record.get("number"))
  except (TypeError, ValueError):
    return None
  if number <= 0:
    return None
  return repo, number


def _active_pr_records(app_id: int) -> list[tuple[str, Path, str, int]]:
  """Returns (record_id, path, upstream_repo, pr_number) for every open/draft
  PR record with a durable PR number, sorted by record id for stable aliasing."""
  out: list[tuple[str, Path, str, int]] = []
  base = _contributions_dir(app_id)
  if not base.is_dir():
    return out
  for path in sorted(base.glob("*.json")):
    record = _read_record_tolerant(path)
    if record is None:
      continue
    if record.get("status") not in _ACTIVE_PR_STATUSES:
      continue
    if record.get("type") != "pr":
      continue
    ref = _pr_ref(record)
    if ref is None:
      continue
    out.append((path.stem, path, ref[0], ref[1]))
  return out


def _build_pr_checks_query(
  refs: list[tuple[str, str, str, int]],
) -> tuple[str, dict]:
  """Builds the batched checks query from (alias, owner, name, number) refs.

  Pure so it is unit-testable without the network. Each ref becomes an
  aliased `repository(...) { pullRequest(...) { ...prChecks } }` selection
  driven by its own String!/Int! variables.
  """
  var_decls: list[str] = []
  selections: list[str] = []
  variables: dict = {}
  for alias, owner, name, number in refs:
    var_decls.append(f"${alias}o: String!, ${alias}n: String!, ${alias}p: Int!")
    variables[f"{alias}o"] = owner
    variables[f"{alias}n"] = name
    variables[f"{alias}p"] = number
    selections.append(
      f"  {alias}: repository(owner: ${alias}o, name: ${alias}n) "
      f"{{ pullRequest(number: ${alias}p) {{ ...prChecks }} }}"
    )
  query = (
    "query(" + ", ".join(var_decls) + ") {\n"
    + "\n".join(selections)
    + "\n}\n\n"
    + _PR_CHECKS_FRAGMENT
  )
  return query, variables


def _normalize_context(ctx: dict) -> dict | None:
  """Flattens a statusCheckRollup context (CheckRun or legacy StatusContext)
  into the uniform job shape the `checks` contract stores."""
  kind = ctx.get("__typename")
  if kind == "CheckRun":
    return {
      "name": ctx.get("name") or "",
      "conclusion": ctx.get("conclusion"),
      "status": ctx.get("status"),
      "url": ctx.get("detailsUrl"),
    }
  if kind == "StatusContext":
    # A commit status has no separate conclusion; its state IS the outcome.
    return {
      "name": ctx.get("context") or "",
      "conclusion": ctx.get("state"),
      "status": None,
      "url": ctx.get("targetUrl"),
    }
  return None


def _parse_rollup(pr_node: object) -> dict | None:
  """Parses one `pullRequest` GraphQL node into the fields the `checks`
  field is built from, or None when the PR could not be resolved."""
  if not isinstance(pr_node, dict):
    return None
  nodes = ((pr_node.get("commits") or {}).get("nodes")) or []
  commit = nodes[-1].get("commit") if nodes and isinstance(nodes[-1], dict) else None
  commit = commit if isinstance(commit, dict) else {}
  rollup = commit.get("statusCheckRollup")
  rollup = rollup if isinstance(rollup, dict) else {}
  contexts = ((rollup.get("contexts") or {}).get("nodes")) or []
  jobs: list[dict] = []
  for ctx in contexts:
    norm = _normalize_context(ctx) if isinstance(ctx, dict) else None
    if norm and norm["name"]:
      jobs.append(norm)
  return {
    "pr_state": pr_node.get("state"),
    "is_draft": bool(pr_node.get("isDraft")),
    "base_ref": pr_node.get("baseRefName"),
    "pr_url": pr_node.get("url"),
    "head_sha": commit.get("oid"),
    "rollup_state": rollup.get("state"),
    "jobs": jobs,
  }


def _classify_jobs(jobs: list[dict], base_failing_names: set | None) -> None:
  """Annotates each FAILING job in place with a `classification`.

  `base_failing_names` is the set of check names red on the upstream base
  branch, or None when that data could not be fetched. A failing check whose
  name is also red on base is `inherited`; green on base is `suspect-pr-caused`;
  no base data at all is `unknown`. Passing (or still-running) jobs carry no
  classification.
  """
  for job in jobs:
    if not _is_failing(job.get("conclusion")):
      job.pop("classification", None)
      continue
    if base_failing_names is None:
      job["classification"] = "unknown"
    elif job.get("name") in base_failing_names:
      job["classification"] = "inherited"
    else:
      job["classification"] = "suspect-pr-caused"


def _build_checks_field(
  parsed: dict,
  base_failing_names: set | None,
  observed_at: str,
  prev_notified_sha: str | None,
) -> dict:
  """Assembles the persisted `checks` object from a parsed rollup. Carries a
  prior `notified_sha` forward so an unchanged head keeps its dedupe key."""
  jobs = [dict(j) for j in parsed["jobs"]]
  _classify_jobs(jobs, base_failing_names)
  checks: dict = {
    "state": parsed.get("rollup_state"),
    "head_sha": parsed.get("head_sha"),
    "pr_state": parsed.get("pr_state"),
    "base_ref": parsed.get("base_ref"),
    "jobs": jobs,
    "observed_at": observed_at,
  }
  if prev_notified_sha:
    checks["notified_sha"] = prev_notified_sha
  return checks


def _should_notify_failure(
  parsed: dict, checks: dict, prev_notified_sha: str | None,
) -> bool:
  """A failure notification fires only for an OPEN PR whose head is newly red
  (a head SHA we have not already notified for)."""
  head = parsed.get("head_sha")
  return bool(
    parsed.get("pr_state") == "OPEN"
    and head
    and head != prev_notified_sha
    and any(_is_failing(j.get("conclusion")) for j in checks["jobs"])
  )


def _checks_failure_notification(record: dict, checks: dict) -> dict:
  """Builds the owner/agent notification payload for a newly-red PR.

  Self-contained by design: a memory-less follow-up session must be able to
  act from repo + PR number + head SHA + each failing job's name, URL, and
  inherited-vs-suspect verdict alone.
  """
  repo = record.get("repo") or (record.get("plan") or {}).get("repo") or ""
  number = record.get("number")
  head = checks.get("head_sha") or ""
  url = record.get("url") or (
    f"https://github.com/{repo}/pull/{number}" if repo and number else ""
  )
  failing = [j for j in checks["jobs"] if _is_failing(j.get("conclusion"))]
  lines = [f"{repo}#{number} at {head[:7]} — {len(failing)} check(s) red."]
  for job in failing:
    phrase = _CLASSIFICATION_PHRASE.get(job.get("classification"), "unclassified")
    detail = f"{job.get('name')} — {phrase}"
    if job.get("url"):
      detail += f": {job['url']}"
    lines.append(detail)
  return {
    "title": f"PR checks failing: {repo}#{number}",
    "body": "\n".join(lines),
    "target": url or None,
    "actions": [{"action": "open-pr", "title": "Open PR", "target": url}]
    if url else None,
  }


async def _github_graphql_json(token: str, query: str, variables: dict) -> dict | None:
  """Server-side GraphQL call for the refresh sweep. Returns the `data`
  object, or None on any transport/HTTP/parse failure (a refresh degrades
  gracefully rather than 500ing). The token stays in the Authorization
  header and never reaches a response or log line (INV1)."""
  async with httpx.AsyncClient(follow_redirects=False, timeout=20) as client:
    try:
      r = await client.post(
        f"{_API_BASE}/graphql",
        json={"query": query, "variables": variables},
        headers={
          "Authorization": f"Bearer {token}",
          "Accept": "application/json",
          "User-Agent": "mobius",
        },
      )
    except httpx.HTTPError:
      return None
  if r.status_code != 200:
    return None
  try:
    body = r.json()
  except ValueError:
    return None
  data = body.get("data") if isinstance(body, dict) else None
  return data if isinstance(data, dict) else None


async def _fetch_base_failing_names(
  token: str, repo: str, base_ref: str,
) -> set | None:
  """Returns the set of check names currently red on the upstream base
  branch (one REST call), or None if the data is unavailable — the signal
  `_classify_jobs` uses to mark a failing check inherited vs suspect."""
  path = f"repos/{repo}/commits/{quote(base_ref, safe='')}/check-runs?per_page=100"
  async with httpx.AsyncClient(follow_redirects=False, timeout=20) as client:
    try:
      r = await client.get(
        f"{_API_BASE}/{path}",
        headers={
          "Authorization": f"Bearer {token}",
          "Accept": "application/vnd.github+json",
          "User-Agent": "mobius",
        },
      )
    except httpx.HTTPError:
      return None
  if r.status_code != 200:
    return None
  try:
    body = r.json()
  except ValueError:
    return None
  runs = body.get("check_runs") if isinstance(body, dict) else None
  if not isinstance(runs, list):
    return None
  return {
    run.get("name")
    for run in runs
    if isinstance(run, dict) and run.get("name") and _is_failing(run.get("conclusion"))
  }


@router.post(
  "/contributions/{app_id}/refresh",
  dependencies=[Depends(reject_cross_site)],
)
@_limiter.limit("20/minute")
async def refresh_contribution_checks(
  request: Request,
  app_id: int,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Refresh CI check results for the app's tracked pull requests.

  Both the Contribute app's live refresh and the hourly cron job hit this:
  it batches one statusCheckRollup GraphQL query across every open/draft PR
  record, classifies each failing job against the upstream base branch, writes
  the `checks` object onto each record (orthogonal to the lifecycle status),
  and fires ONE owner notification per newly-red PR head. The GitHub token is
  read server-side and never returned to the caller.
  """
  _validate_submit_app(app_id, principal, db)
  token = github_auth.get_token()
  if not token:
    raise HTTPException(status_code=401, detail="GitHub not connected.")

  records = _active_pr_records(app_id)
  if not records:
    return {"refreshed": [], "notified": 0}

  refs = [
    (f"pr{i}", repo.split("/", 1)[0], repo.split("/", 1)[1], number)
    for i, (_, _, repo, number) in enumerate(records)
  ]
  query, variables = _build_pr_checks_query(refs)
  data = await _github_graphql_json(token, query, variables)

  # Parse first, then fetch base check-runs only for repos that actually have
  # a failing job — cached per (repo, base_ref) so N red PRs on one repo cost
  # one REST call, not N. All network happens BEFORE the storage lock.
  parsed_by_index: dict[int, dict | None] = {}
  base_cache: dict[tuple[str, str], set | None] = {}
  for i, (_, _, repo, _number) in enumerate(records):
    node = (data or {}).get(f"pr{i}")
    pr_node = node.get("pullRequest") if isinstance(node, dict) else None
    parsed = _parse_rollup(pr_node)
    parsed_by_index[i] = parsed
    if parsed is None:
      continue
    base_ref = parsed.get("base_ref")
    if base_ref and any(_is_failing(j.get("conclusion")) for j in parsed["jobs"]):
      key = (repo, base_ref)
      if key not in base_cache:
        base_cache[key] = await _fetch_base_failing_names(token, repo, base_ref)

  observed_at = _now_iso()
  results: list[dict] = []
  pending_notifications: list[dict] = []
  async with fs_locks.app_storage_lock(app_id):
    for i, (record_id, path, repo, _number) in enumerate(records):
      parsed = parsed_by_index[i]
      if parsed is None:
        continue
      # Re-read under the lock: submit (or a sibling refresh) may have rewritten
      # the record since the pre-network read.
      record = _read_record_tolerant(path)
      if record is None:
        continue
      prev_checks = record.get("checks")
      prev_notified = (
        prev_checks.get("notified_sha") if isinstance(prev_checks, dict) else None
      )
      base_failing = base_cache.get((repo, parsed.get("base_ref")))
      checks = _build_checks_field(parsed, base_failing, observed_at, prev_notified)
      notify = _should_notify_failure(parsed, checks, prev_notified)
      if notify:
        checks["notified_sha"] = parsed["head_sha"]
      record["checks"] = checks
      _write_record(path, record)
      results.append({"id": record_id, "checks": checks})
      if notify:
        pending_notifications.append(_checks_failure_notification(record, checks))

  # Notifications fire after the storage lock releases — notify_owner owns its
  # own DB commit and Web Push delivery, mirroring the merged-PR notify path.
  for payload in pending_notifications:
    notify_owner(
      db,
      principal.owner.id,
      title=payload["title"],
      body=payload["body"],
      source_type="app",
      source_id=str(app_id),
      target=payload["target"],
      actions=payload["actions"],
    )

  return {"refreshed": results, "notified": len(pending_notifications)}


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
