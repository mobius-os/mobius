"""CI/check normalization for prepared and open contribution records."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import get_settings
from app.contribution_records import MAX_RECORD_BYTES

_API_BASE = "https://api.github.com"
_GITHUB_REPO = re.compile(
  r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
)

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

_REPOSITORY_HEAD_FRAGMENT = """
fragment repositoryHead on Repository {
  defaultBranchRef {
    name
    target {
      ... on Commit { oid }
    }
  }
}
""".strip()


def _contributions_dir(app_id: int) -> Path:
  return Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"


def _read_record_tolerant(path: Path) -> dict | None:
  """Reads a small JSON object — a contribution record, or the app's settings
  file — returning None (not raising) on a missing, oversized, or corrupt file so
  one bad record can't abort a whole refresh sweep."""
  try:
    with path.open("rb") as handle:
      raw = handle.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
      return None
    data = json.loads(raw)
  except (OSError, UnicodeDecodeError, ValueError):
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


def _build_repository_heads_query(
  repos: list[str],
) -> tuple[str, dict, dict[str, str]]:
  """Build one variables-only query for distinct repository default heads.

  Repository names are case-insensitive on GitHub. Stable, first-seen
  de-duplication prevents two prepared records for the same target from
  spending two GraphQL fields while retaining the reviewed spelling in the
  returned alias map.
  """
  distinct: list[str] = []
  seen: set[str] = set()
  for repo in repos:
    normalized = str(repo or "").strip()
    if not _GITHUB_REPO.fullmatch(normalized):
      continue
    key = normalized.casefold()
    if key in seen:
      continue
    seen.add(key)
    distinct.append(normalized)

  var_decls: list[str] = []
  selections: list[str] = []
  variables: dict = {}
  aliases: dict[str, str] = {}
  for index, repo in enumerate(distinct):
    alias = f"repo{index}"
    owner, name = repo.split("/", 1)
    var_decls.append(f"${alias}o: String!, ${alias}n: String!")
    variables[f"{alias}o"] = owner
    variables[f"{alias}n"] = name
    aliases[alias] = repo
    selections.append(
      f"  {alias}: repository(owner: ${alias}o, name: ${alias}n) "
      "{ ...repositoryHead }"
    )
  if not selections:
    return "", {}, {}
  query = (
    "query(" + ", ".join(var_decls) + ") {\n"
    + "\n".join(selections)
    + "\n}\n\n"
    + _REPOSITORY_HEAD_FRAGMENT
  )
  return query, variables, aliases


def _repository_default_head(node: object) -> tuple[str, str] | None:
  """Return one repository's (default branch, head SHA), if resolvable."""
  if not isinstance(node, dict):
    return None
  default_ref = node.get("defaultBranchRef")
  if not isinstance(default_ref, dict):
    return None
  target = default_ref.get("target")
  if not isinstance(target, dict):
    return None
  branch = str(default_ref.get("name") or "")
  head_sha = str(target.get("oid") or "")
  if not branch or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
    return None
  return branch, head_sha.lower()


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
