"""A prepared PR's screenshots are approved with its text and hosted at Send.

Staging uses the real stage route and repositories from the staging suite.
Publication runs real Git against a local bare repository standing in for the
owner's fork; only the anonymous raw-URL read is faked.
"""

import hashlib
import subprocess
import zlib
from pathlib import Path

import pytest

from app import contribution_screenshots as shots
from app.config import get_settings
from app.contribution_errors import ContributionSubmitError
from app.github_contributions import (
  _publish_reviewed_screenshots,
  publication_approval_sha256,
)
from app.routes import contribution_relay
from tests.test_contribution_staging import staging  # noqa: F401  (fixture)


def _png(seed: int) -> bytes:
  """A tiny valid PNG whose pixel differs per seed."""
  def chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return (
      len(data).to_bytes(4, "big") + body
      + zlib.crc32(body).to_bytes(4, "big")
    )
  header = (1).to_bytes(4, "big") * 2 + bytes([8, 2, 0, 0, 0])
  pixel = zlib.compress(bytes([0, seed % 256, 0, 0]))
  return (
    b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
    + chunk(b"IDAT", pixel) + chunk(b"IEND", b"")
  )


def _write(tmp_path: Path, name: str, data: bytes) -> str:
  path = tmp_path / name
  path.write_bytes(data)
  return str(path)


BODY = (
  "Fixes the header.\n\n| Before | After |\n|---|---|\n"
  "| ![Old header](screenshot:before.png) | ![New header](screenshot:after.png) |\n"
)


def _items(tmp_path: Path, before=1, after=2) -> list[dict]:
  return [
    {"name": "before.png", "path": _write(tmp_path, "b.png", _png(before)),
     "alt": "Old header"},
    {"name": "after.png", "path": _write(tmp_path, "a.png", _png(after)),
     "alt": "New header"},
  ]


def _stage(record_path: Path, items: list[dict], body: str) -> list[dict]:
  entries, data = shots.read(items, body)
  shots.store(record_path, data)
  return entries


def _store(app_id: int, record_id: str = "greet") -> Path:
  return shots.blob_dir(
    Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
    / f"{record_id}.json"
  )


def test_staged_screenshots_are_listed_in_the_approved_plan(staging, tmp_path, client, owner_token):
  response = staging["new_record"](body_draft=BODY, screenshots=_items(tmp_path))
  assert response.status_code == 200, response.text
  entries = response.json()["record"]["plan"]["screenshots"]
  assert [e["name"] for e in entries] == ["before.png", "after.png"]
  assert entries[0]["sha256"] == hashlib.sha256(_png(1)).hexdigest()
  stored = sorted(p.name for p in _store(staging["app_id"]).iterdir())
  assert stored == sorted(f"{e['sha256']}.png" for e in entries)

  served = client.get(
    f"/api/github/contributions/{staging['app_id']}/greet/screenshots/after.png",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert served.status_code == 200
  assert served.headers["content-type"] == "image/png"
  assert served.content == _png(2)


def test_the_approval_digest_changes_when_a_screenshot_changes():
  record = {"id": "r", "plan": {"screenshots": [{"name": "a.png", "sha256": "1" * 64}]}}
  other = {"id": "r", "plan": {"screenshots": [{"name": "a.png", "sha256": "2" * 64}]}}
  assert publication_approval_sha256(record) != publication_approval_sha256(other)


@pytest.mark.parametrize(("body", "expected"), [
  ("Only ![Old header](screenshot:before.png).", "Not shown: after.png"),
  (BODY + "![Extra](screenshot:extra.png)", "not attached: extra.png"),
])
def test_stage_refuses_screenshots_and_references_that_do_not_match(staging, tmp_path, body, expected):
  response = staging["new_record"](body_draft=body, screenshots=_items(tmp_path))
  assert response.status_code == 422
  assert expected in response.text


def test_stage_refuses_a_reference_without_any_attachment(staging):
  response = staging["new_record"](body_draft="See ![x](screenshot:x.png)")
  assert response.status_code == 422
  assert "not attached: x.png" in response.text


def test_stage_refuses_a_file_that_is_not_a_png(staging, tmp_path):
  items = _items(tmp_path)
  items[0]["path"] = _write(tmp_path, "fake.png", b"GIF89a not a png")
  response = staging["new_record"](body_draft=BODY, screenshots=items)
  assert response.status_code == 422
  assert "is not a PNG image" in response.text


def test_changing_a_screenshot_drops_the_verdict_and_prunes_the_old_image(staging, tmp_path):
  record = staging["new_record"](body_draft=BODY, screenshots=_items(tmp_path)).json()["record"]
  assert staging["review"](record).status_code == 200
  kept = staging["stage"](body_draft=BODY).json()["record"]
  assert kept["quality_review"]["state"] == "all_clear"

  changed = staging["stage"](screenshots=_items(tmp_path, after=3)).json()["record"]
  assert "quality_review" not in changed
  stored = {p.name for p in _store(staging["app_id"]).iterdir()}
  assert stored == {f"{e['sha256']}.png" for e in changed["plan"]["screenshots"]}

  cleared = staging["stage"](body_draft="No images.", screenshots=[]).json()["record"]
  assert "screenshots" not in cleared["plan"]
  assert not _store(staging["app_id"]).exists()


def test_the_bot_relay_refuses_screenshots_it_cannot_host():
  with pytest.raises(ContributionSubmitError) as raised:
    contribution_relay._reviewed_public_metadata({
      "plan": {"title": "T", "body_draft": BODY, "screenshots": [{"name": "a.png"}]},
    })
  assert raised.value.code == "relay_screenshots_unsupported"


# --- Publication -------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
  return subprocess.run(
    ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
  ).stdout.strip()


@pytest.fixture
def fork(tmp_path, monkeypatch):
  """A bare 'fork' and a record store with two images."""
  monkeypatch.setattr(shots, "_PROOF_BACKOFF_SECONDS", 0)
  remote = tmp_path / "fork.git"
  _git(tmp_path, "init", "-q", "--bare", str(remote))
  record_path = tmp_path / "records" / "greet.json"
  record_path.parent.mkdir()
  entries = _stage(record_path, _items(tmp_path), BODY)

  def read_public(url: str) -> bytes | None:
    prefix = "https://raw.githubusercontent.com/octocat/project/"
    # Like a real HTTP client, the fragment never reaches the server.
    commit, path = url.split("#", 1)[0].removeprefix(prefix).split("/", 1)
    found = subprocess.run(
      ["git", "show", f"{commit}:{path}"], cwd=remote, capture_output=True,
    )
    return found.stdout if found.returncode == 0 else None

  def publish(entries=entries, **overrides):
    return shots.publish(
      record_path, entries, assets_repo="octocat/project",
      branch="fix/header", author=("octocat", "1+octocat@users.noreply.github.com"),
      remote_url=str(remote), **{"read_public": read_public, **overrides},
    )

  return {"remote": remote, "record_path": record_path, "entries": entries,
          "publish": publish}


def test_send_hosts_the_exact_images_and_pins_the_description_to_them(fork):
  commit = fork["publish"]()
  remote = fork["remote"]
  assert _git(remote, "rev-parse", "refs/heads/mobius-screenshots") == commit
  assert _git(remote, "log", "-1", "--format=%an", commit) == "octocat"
  shown = subprocess.run(
    ["git", "show", f"{commit}:screenshots/fix/header/after.png"],
    cwd=remote, capture_output=True, check=True,
  ).stdout
  assert shown == _png(2)

  body = shots.resolve_body(
    BODY, fork["entries"], assets_repo="octocat/project", commit=commit,
    branch="fix/header",
  )
  assert "screenshot:" not in body
  assert (
    f"(https://raw.githubusercontent.com/octocat/project/{commit}"
    "/screenshots/fix/header/after.png)"
  ) in body


def test_a_retried_send_links_the_same_upload_without_a_new_commit(fork):
  first = fork["publish"]()
  assert fork["publish"](reuse_commit=first) == first
  assert fork["publish"]() == first
  assert _git(fork["remote"], "rev-list", "--count", "refs/heads/mobius-screenshots") == "1"


def _push_to_assets_branch(fork, tmp_path, path: str, text: str) -> str:
  other = tmp_path / "other"
  _git(tmp_path, "clone", "-q", str(fork["remote"]), str(other))
  _git(other, "checkout", "-q", "-b", shots.ASSETS_BRANCH)
  (other / path).parent.mkdir(parents=True, exist_ok=True)
  (other / path).write_text(text)
  _git(other, "add", path)
  _git(other, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "-m", "old")
  _git(other, "push", "-q", "origin", shots.ASSETS_BRANCH)
  return _git(other, "rev-parse", "HEAD")


def test_send_adds_to_an_existing_screenshot_branch_without_rewriting_it(fork, tmp_path):
  earlier = _push_to_assets_branch(
    fork, tmp_path, "screenshots/fix/older/keep.png", "earlier upload\n",
  )
  commit = fork["publish"]()
  assert _git(fork["remote"], "rev-parse", f"{commit}^") == earlier
  assert _git(fork["remote"], "show", f"{commit}:screenshots/fix/older/keep.png") == "earlier upload"


def test_send_never_adds_to_a_branch_that_holds_other_files(fork, tmp_path):
  earlier = _push_to_assets_branch(fork, tmp_path, "README.md", "not ours\n")
  with pytest.raises(ContributionSubmitError) as raised:
    fork["publish"]()
  assert raised.value.code == "screenshot_upload_failed"
  assert _git(fork["remote"], "rev-parse", f"refs/heads/{shots.ASSETS_BRANCH}") == earlier


def test_send_fetches_only_the_tip_trees_of_the_screenshot_branch(fork, tmp_path, monkeypatch):
  _push_to_assets_branch(fork, tmp_path, "screenshots/fix/older/keep.png", "old\n")
  calls = []
  original = shots._git

  def git(repo, *args, **kwargs):
    calls.append(args)
    return original(repo, *args, **kwargs)

  monkeypatch.setattr(shots, "_git", git)
  fork["publish"]()
  fetch = next(args for args in calls if args[0] == "fetch")
  assert "--depth=1" in fetch and "--filter=blob:none" in fetch


def test_send_refuses_an_image_that_changed_after_approval(fork):
  stored = shots.blob_dir(fork["record_path"]) / f"{fork['entries'][0]['sha256']}.png"
  stored.write_bytes(_png(9))
  with pytest.raises(ContributionSubmitError) as raised:
    fork["publish"]()
  assert raised.value.code == "screenshot_changed"
  assert subprocess.run(
    ["git", "rev-parse", "--verify", "--quiet", "refs/heads/mobius-screenshots"],
    cwd=fork["remote"], capture_output=True,
  ).returncode != 0


def test_send_stops_when_github_does_not_serve_the_images_publicly(fork):
  with pytest.raises(ContributionSubmitError) as raised:
    fork["publish"](read_public=lambda _url: None)
  assert raised.value.code == "screenshot_not_public"


def test_an_open_pr_update_cannot_carry_screenshots(fork):
  with pytest.raises(ContributionSubmitError) as raised:
    _publish_reviewed_screenshots(
      {}, fork["record_path"].with_suffix(".diff"), Path("/nonexistent"),
      fork["entries"], BODY, upstream_repo="octo/project", login="octocat",
      branch="fix/header", author=("octocat", "e"), is_update=True,
      record_patch={"head_repository": "octocat/project"},
    )
  assert raised.value.code == "screenshot_update_unsupported"
  assert raised.value.record_patch == {"head_repository": "octocat/project"}


# --- Record fields are re-checked at Send -------------------------------------


def test_a_tampered_reuse_commit_never_reaches_the_public_link(fork):
  # The fragment is not sent, so the injected value still proves real bytes.
  after = [fork["entries"][1]]
  real = fork["publish"](after)
  injected = f"{real}/screenshots/fix/header/after.png#) **Approved** ("
  commit = fork["publish"](after, reuse_commit=injected)
  assert commit == real


@pytest.mark.parametrize("entry", [
  {"name": "../escape.png"},
  {"name": "/tmp/escape.png"},
])
def test_send_refuses_a_screenshot_name_staging_would_not_accept(fork, entry):
  entries = [{**fork["entries"][0], **entry}]
  with pytest.raises(ContributionSubmitError) as raised:
    shots.verify(fork["record_path"], entries)
  assert raised.value.code == "screenshot_changed"


def test_send_refuses_stored_bytes_that_are_not_a_png(fork):
  data = b"not a png at all"
  sha = hashlib.sha256(data).hexdigest()
  (shots.blob_dir(fork["record_path"]) / f"{sha}.png").write_bytes(data)
  with pytest.raises(ContributionSubmitError) as raised:
    shots.verify(fork["record_path"], [{"name": "a.png", "sha256": sha}])
  assert raised.value.code == "screenshot_changed"


def test_restaging_repairs_a_damaged_stored_image(fork, tmp_path):
  stored = shots.blob_dir(fork["record_path"]) / f"{fork['entries'][0]['sha256']}.png"
  stored.write_bytes(b"damaged")
  _stage(fork["record_path"], _items(tmp_path), BODY)
  shots.verify(fork["record_path"], fork["entries"])


def test_a_concurrent_upload_is_kept_and_ours_rebuilt_on_top(fork, tmp_path, monkeypatch):
  other = tmp_path / "racer"
  _git(tmp_path, "clone", "-q", str(fork["remote"]), str(other))
  original = shots._assets_commit
  raced = {"done": False}

  def race_then_build(*args, **kwargs):
    commit = original(*args, **kwargs)
    if not raced["done"]:
      raced["done"] = True
      _git(other, "checkout", "-q", "--orphan", shots.ASSETS_BRANCH)
      (other / "screenshots").mkdir()
      (other / "screenshots" / "theirs.png").write_text("another send\n")
      _git(other, "add", "screenshots")
      _git(other, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "-m", "theirs")
      _git(other, "push", "-q", "origin", shots.ASSETS_BRANCH)
    return commit

  monkeypatch.setattr(shots, "_assets_commit", race_then_build)
  commit = fork["publish"]()
  assert _git(fork["remote"], "show", f"{commit}:screenshots/theirs.png") == "another send"
  assert _git(fork["remote"], "rev-parse", "refs/heads/mobius-screenshots") == commit


# --- Description references ---------------------------------------------------


def test_code_examples_of_the_syntax_are_neither_checked_nor_rewritten():
  body = "Write `![alt](screenshot:NAME)` to attach one.\n\n```\n![x](screenshot:y.png)\n```\n"
  assert shots.references(body) == []
  shots.check_references([], body)
  assert shots.resolve_body(body, [], assets_repo="o/r", commit="a" * 40, branch="b") == body


def test_a_linked_screenshot_pins_both_the_image_and_its_link():
  body = "[![Wide](screenshot:a.png)](screenshot:a.png)"
  entries = [{"name": "a.png"}]
  resolved = shots.resolve_body(body, entries, assets_repo="o/r", commit="a" * 40, branch="b")
  url = shots.raw_url("o/r", "a" * 40, "b", "a.png")
  assert resolved == f"[![Wide]({url})]({url})"


# --- Send ordering and refusals ---------------------------------------------------


def _send_screenshots(fork, **overrides):
  args = dict(
    record={}, diff_path=fork["record_path"].with_suffix(".diff"),
    repo=Path("/nonexistent"), screenshots=fork["entries"], body=BODY,
    upstream_repo="octo/project", login="octocat", branch="fix/header",
    author=("octocat", "e"), is_update=False, record_patch={"k": "v"},
  )
  args.update(overrides)
  return _publish_reviewed_screenshots(
    args.pop("record"), args.pop("diff_path"), args.pop("repo"),
    args.pop("screenshots"), args.pop("body"), **args,
  )


@pytest.mark.parametrize("padding", ["x", "é"])
def test_an_oversized_pinned_body_is_refused_before_any_upload(fork, monkeypatch, padding):
  from app import github_contributions
  monkeypatch.setattr(github_contributions, "_ensure_owner_fork_remote", lambda *_: pytest.fail("forked"))
  with pytest.raises(ContributionSubmitError) as raised:
    # Within the limit as written, over it once both links are pinned.
    _send_screenshots(fork, body=BODY + padding * (65_500 - len(BODY)))
  assert raised.value.code == "screenshot_body_too_large"


def test_a_fork_that_cannot_be_made_stops_send_with_a_clear_reason(fork, monkeypatch):
  from app import github_contributions

  def no_fork(*_):
    raise ContributionSubmitError("forking is disabled for this repository")

  monkeypatch.setattr(github_contributions, "_ensure_owner_fork_remote", no_fork)
  with pytest.raises(ContributionSubmitError) as raised:
    _send_screenshots(fork)
  assert raised.value.code == "screenshot_upload_failed"
  assert "without screenshots" in raised.value.message
  assert raised.value.record_patch == {"k": "v"}


def test_send_pins_the_body_and_remembers_the_upload(fork, monkeypatch):
  from app import github_contributions
  monkeypatch.setattr(github_contributions, "_ensure_owner_fork_remote", lambda *_: "octocat/project")
  monkeypatch.setattr(
    github_contributions.contribution_screenshots, "publish",
    lambda *a, **k: "b" * 40,
  )
  body, patch, assets_repo = _send_screenshots(fork)
  assert assets_repo == "octocat/project"
  assert "screenshot:" not in body
  assert patch == {"k": "v", "last_submit_screenshots": {"repo": "octocat/project", "commit": "b" * 40}}


@pytest.mark.parametrize("fork_name", ["project", "p" * 90])
def test_screenshot_body_limit_counts_characters_before_and_after_upload(fork, monkeypatch, fork_name):
  from app import github_contributions
  assets_repo = f"octocat/{fork_name}"
  monkeypatch.setattr(github_contributions, "_ensure_owner_fork_remote", lambda *_: assets_repo)
  monkeypatch.setattr(shots, "publish", lambda *a, **k: "b" * 40)
  guessed = shots.resolve_body(
    BODY, fork["entries"], assets_repo="octocat/project", commit="0" * 64, branch="fix/header",
  )
  actual = shots.resolve_body(
    BODY, fork["entries"], assets_repo=assets_repo, commit="b" * 40, branch="fix/header",
  )
  # The normal fork fills the early check; a renamed fork fills the final one.
  body = BODY + "é" * (65_536 - max(len(guessed), len(actual)))
  resolved, _, hosted_in = _send_screenshots(fork, body=body)
  assert hosted_in == assets_repo
  assert len(resolved) <= 65_536 < len(resolved.encode("utf-8"))


def test_the_image_route_is_never_cached(staging, tmp_path, client, owner_token):
  staging["new_record"](body_draft=BODY, screenshots=_items(tmp_path))
  served = client.get(
    f"/api/github/contributions/{staging['app_id']}/greet/screenshots/before.png",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert served.headers["cache-control"] == "no-store"
  missing = client.get(
    f"/api/github/contributions/{staging['app_id']}/greet/screenshots/none.png",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert missing.status_code == 404


def test_an_open_pr_update_cannot_stage_screenshots(staging, tmp_path, monkeypatch):
  import json
  from app.routes import github as github_routes
  record = staging["new_record"]().json()["record"]
  path = (
    Path(get_settings().data_dir) / "apps" / str(staging["app_id"])
    / "contributions" / "greet.json"
  )
  path.write_text(json.dumps({
    **record, "status": "open", "number": 7,
    "url": "https://github.com/octo/project/pull/7", "head_repository": "octo/project",
  }))
  monkeypatch.setattr(github_routes, "_autopilot_live_target", lambda *_: {
    "error": None, "head_sha": record["plan"]["head_sha"], "base_branch": "main",
    "base_sha": staging["base"], "title": "T", "body": "B",
  })
  refused = staging["stage"](screenshots=_items(tmp_path))
  assert refused.status_code == 422
  assert refused.json()["detail"]["code"] == "stage_public_text"


def test_a_failed_stage_stores_no_images(staging, tmp_path):
  response = staging["new_record"](
    title=" Padded title ", body_draft=BODY, screenshots=_items(tmp_path),
  )
  assert response.status_code == 409
  assert "title" in response.text
  assert not _store(staging["app_id"]).exists()


def test_pruning_leaves_a_folder_it_did_not_write(tmp_path):
  record_path = tmp_path / "greet.json"
  entries = _stage(record_path, _items(tmp_path), BODY)
  (shots.blob_dir(record_path) / "extra").mkdir()
  shots.prune(record_path, entries[:1])
  assert {p.name for p in shots.blob_dir(record_path).iterdir()} == {
    "extra", f"{entries[0]['sha256']}.png",
  }
  shots.prune(record_path, [])
  assert [p.name for p in shots.blob_dir(record_path).iterdir()] == ["extra"]


@pytest.mark.parametrize("body", [
  "Write ``![a](screenshot:x.png)`` with a ` inside.",
  "Write `![a]\n(screenshot:x.png)` over two lines.",
  "Write `x\n![a](screenshot:x.png)` over two lines.",
])
def test_every_inline_code_span_is_left_alone(body):
  assert shots.references(body) == []


def test_a_blank_line_ends_an_unclosed_code_span():
  assert shots.references("A ` tick.\n\n![a](screenshot:x.png) then `.") == ["x.png"]


# --- A whole Send ---------------------------------------------------------------


@pytest.fixture
def submit(tmp_path, monkeypatch):
  """`_submit_prepared_pr` for a reviewed PR with screenshots on a repository
  the owner can push to; Git and GitHub are faked, the order is recorded."""
  from app import github_contributions
  from tests.test_github_routes import _cp, _write_token

  _write_token(login="octocat")
  repo = Path(get_settings().data_dir) / "contrib" / "shots-send" / "worktree"
  (repo / ".git").mkdir(parents=True, exist_ok=True)
  diff_path = tmp_path / "records" / "shots-send.diff"
  diff_path.parent.mkdir()
  entries = _stage(diff_path.with_suffix(".json"), _items(tmp_path), BODY)
  branch, base, head = "fix/header", "b" * 40, "a" * 40
  events: list = []

  def record(upstream_repo):
    return {
      "id": "shots-send", "type": "pr", "repo": upstream_repo,
      "status": "submitting", "title": "Header", "branch": branch,
      "plan": {
        "action": "pr", "repo": upstream_repo, "title": "Header",
        "body_draft": BODY, "branch": branch, "repo_path": str(repo),
        "base_sha": base, "head_sha": head, "diff_sha256": "f" * 64,
        "screenshots": entries,
      },
    }

  git_ops = "app.github_contribution_git."
  for name, value in {
    "_assert_fresh": lambda *_a, **_k: (base, head, "f" * 64),
    "_assert_clean_worktree": lambda *_a: None,
    "_assert_coauthor_trailer": lambda *_a: None,
    "_assert_head_attribution": lambda *_a, **_k: None,
    "_assert_upstream_push_permission": lambda *_a: None,
    "_authoritative_upstream_branch_sha": lambda *_a, **_k: None,
    "_assert_merges_with_upstream": lambda *_a, **_k: {
      "last_submit_upstream_branch": "main", "last_submit_upstream_sha": base,
    },
  }.items():
    monkeypatch.setattr(git_ops + name, value)
  monkeypatch.setattr(github_contributions.shutil, "which", lambda name: f"/bin/{name}")
  monkeypatch.setattr(github_contributions, "_safe_repo_path", lambda _raw: repo)
  monkeypatch.setattr(
    github_contributions, "_confirm_existing_pr_update",
    lambda _repo, upstream, number, **_k: (f"https://github.com/{upstream}/pull/{number}", "draft"),
  )
  monkeypatch.setattr(
    github_contributions, "_ensure_owner_fork_remote",
    lambda _repo, _upstream, login: events.append("fork") or f"{login}/project",
  )

  def upload(record_path, shown, *, assets_repo, **_k):
    assert record_path == diff_path.with_suffix(".json") and shown == entries
    events.append(("upload", assets_repo))
    return "c" * 40

  monkeypatch.setattr(github_contributions.contribution_screenshots, "publish", upload)
  push = {"error": None}
  monkeypatch.setattr(
    github_contributions, "_push_branch",
    lambda _repo, remote, *_a: events.append(("push", remote)) or push["error"],
  )

  def fake_git(_repo, *args, check=True):
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp(branch + "\n")
    if args == ("rev-parse", "HEAD"):
      return _cp(head + "\n")
    return _cp("")

  def fake_gh(_repo, *args, check=True):
    if args[:1] == ("api",):
      return _cp("true\n")
    if args[:2] == ("pr", "list"):
      return _cp("[]")
    if args[:2] == ("pr", "create"):
      events.append(("create", Path(args[args.index("--body-file") + 1]).read_text()))
      return _cp(f"https://github.com/{args[args.index('-R') + 1]}/pull/9\n")
    return _cp("")

  monkeypatch.setattr(git_ops + "_git", fake_git)
  monkeypatch.setattr(git_ops + "_gh", fake_gh)

  def send(upstream_repo):
    return github_contributions._submit_prepared_pr(record(upstream_repo), diff_path)

  return {"send": send, "events": events, "push": push}


@pytest.mark.parametrize(("upstream_repo", "assets_repo", "forked"), [
  ("octocat/project", "octocat/project", False),
  # A shared repository the owner can push to: code there, images in the fork.
  ("octo-org/project", "octocat/project", True),
])
def test_send_uploads_to_the_owners_repository_before_pushing_the_code(
  submit, upstream_repo, assets_repo, forked,
):
  _url, number, patch = submit["send"](upstream_repo)

  assert number == 9
  events = submit["events"]
  assert ("fork" in events) == forked
  assert events.index(("upload", assets_repo)) < events.index(
    ("push", f"https://github.com/{upstream_repo}.git"),
  )
  created = next(event[1] for event in events if event[0] == "create")
  assert "screenshot:" not in created
  assert shots.raw_url(assets_repo, "c" * 40, "fix/header", "after.png") in created
  assert patch["last_submit_screenshots"] == {"repo": assets_repo, "commit": "c" * 40}


def test_a_failed_push_after_the_upload_keeps_the_upload_for_the_retry(submit):
  submit["push"]["error"] = "rejected: protected branch"
  with pytest.raises(ContributionSubmitError) as raised:
    submit["send"]("octocat/project")
  assert raised.value.record_patch["last_submit_screenshots"] == {
    "repo": "octocat/project", "commit": "c" * 40,
  }


# --- Stacks ---------------------------------------------------------------------


@pytest.fixture
def stack(tmp_path, monkeypatch):
  """Two private layers: the first without screenshots, the second with."""
  from app import github_contributions
  from tests.test_github_routes import _cp, _write_token

  _write_token(login="octocat")
  rows = []
  for position, body in ((1, "Model layer."), (2, BODY)):
    repo = Path(get_settings().data_dir) / "contrib" / f"layer-{position}" / "worktree"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    diff_path = tmp_path / f"layer-{position}.diff"
    plan = {
      "action": "pr", "repo": "mobius-os/project", "title": f"Layer {position}",
      "body_draft": body, "branch": f"stack/s/0{position}-layer",
      "repo_path": str(repo),
    }
    if position == 2:
      plan["screenshots"] = _stage(diff_path.with_suffix(".json"), _items(tmp_path), BODY)
    rows.append({
      "record": {"id": f"layer-{position}", "status": "submitting", "plan": plan},
      "diff_path": str(diff_path),
      "stack": {"base_branch": "main" if position == 1 else "stack/s/01-layer"},
    })
  git_ops = "app.github_contribution_git."
  for name in (
    "_assert_clean_worktree", "_assert_fresh", "_assert_coauthor_trailer",
    "_assert_head_attribution", "_assert_merges_with_upstream",
    "_assert_upstream_push_permission",
  ):
    monkeypatch.setattr(git_ops + name, lambda *_a, **_k: None)
  monkeypatch.setattr(git_ops + "_upstream_default_branch", lambda *_a: "main")
  monkeypatch.setattr(git_ops + "_git", lambda _repo, *args, check=True: _cp("main\n"))
  monkeypatch.setattr(github_contributions.shutil, "which", lambda name: f"/bin/{name}")
  uploads: list = []
  monkeypatch.setattr(
    github_contributions.contribution_screenshots, "publish",
    lambda *_a, **k: uploads.append(k["assets_repo"]) or "c" * 40,
  )

  def preflight():
    github_contributions._preflight_prepared_stack(rows, source_preflight=lambda _r: "")

  return {"rows": rows, "uploads": uploads, "preflight": preflight}


def test_a_stack_hosts_every_layers_screenshots_before_any_push(stack, monkeypatch):
  from app import github_contributions
  monkeypatch.setattr(github_contributions, "_ensure_owner_fork_remote", lambda *_: "octocat/project")
  stack["preflight"]()
  assert stack["uploads"] == ["octocat/project"]


def test_a_stack_whose_later_layer_cannot_fork_stops_before_any_push(stack, monkeypatch):
  from app import github_contributions

  def no_fork(*_):
    raise ContributionSubmitError("forking is disabled for this repository")

  monkeypatch.setattr(github_contributions, "_ensure_owner_fork_remote", no_fork)
  with pytest.raises(ContributionSubmitError) as raised:
    stack["preflight"]()
  assert raised.value.code == "screenshot_upload_failed"


def test_a_stack_with_an_oversized_layer_uploads_nothing(stack, monkeypatch):
  from app import github_contributions
  monkeypatch.setattr(github_contributions, "_ensure_owner_fork_remote", lambda *_: pytest.fail("forked"))
  plan = stack["rows"][1]["record"]["plan"]
  plan["body_draft"] = BODY + "x" * (65_500 - len(BODY))
  with pytest.raises(ContributionSubmitError) as raised:
    stack["preflight"]()
  assert raised.value.code == "screenshot_body_too_large"
  assert stack["uploads"] == []


def test_a_fork_send_resolves_the_fork_once(submit, monkeypatch):
  from app import github_contributions
  git_ops = "app.github_contribution_git."
  monkeypatch.setattr(git_ops + "_has_upstream_push_permission", lambda *_a: False)
  monkeypatch.setattr(git_ops + "_normalize_head_attribution", lambda *_a, **_k: {})
  monkeypatch.setattr(
    github_contributions, "_push_topic_branch",
    lambda *_a: submit["events"].append(("push", "fork")),
  )
  submit["send"]("octo-org/project")
  assert submit["events"].count("fork") == 1


def test_a_fence_closes_only_on_its_own_character_and_length():
  shown = "![a](screenshot:x.png)"
  body = f"````md\n```\n{shown}\n```\n{shown}\n````\n~~~~\n~~~\n{shown}\n~~~~\nThen {shown}"
  assert shots.references(body) == ["x.png"]
  pinned = shots.resolve_body(body, [{"name": "x.png"}], assets_repo="o/r", commit="a" * 40, branch="b")
  assert pinned.count("screenshot:x.png") == 3


def test_a_same_line_triple_backtick_span_is_not_a_fence():
  body = "```example```\n![a](screenshot:x.png)"
  assert shots.references(body) == ["x.png"]


# --- App-writable storage and agent-supplied paths ------------------------------


def test_storing_and_pruning_never_follow_a_linked_folder(tmp_path):
  record_path = tmp_path / "records" / "greet.json"
  record_path.parent.mkdir()
  victim = tmp_path / "victim"
  victim.mkdir()
  (victim / "keep.txt").write_text("not ours\n")
  shots.blob_dir(record_path).symlink_to(victim)

  shots.prune(record_path, [])
  assert (victim / "keep.txt").exists()
  assert not shots.blob_dir(record_path).is_symlink()

  shots.blob_dir(record_path).symlink_to(victim)
  _stage(record_path, _items(tmp_path), BODY)
  assert sorted(p.name for p in victim.iterdir()) == ["keep.txt"]
  assert shots.blob_dir(record_path).is_dir() and not shots.blob_dir(record_path).is_symlink()


def test_a_linked_screenshot_source_is_refused(tmp_path):
  items = _items(tmp_path)
  link = tmp_path / "link.png"
  link.symlink_to(items[0]["path"])
  items[0]["path"] = str(link)
  with pytest.raises(ContributionSubmitError, match="not a link"):
    shots.read(items, BODY)


def test_a_screenshot_source_outside_the_data_and_temporary_folders_is_refused(tmp_path, monkeypatch):
  items = _items(tmp_path)
  monkeypatch.setattr(shots.tempfile, "gettempdir", lambda: str(tmp_path / "elsewhere"))
  with pytest.raises(ContributionSubmitError, match="data or temporary folder"):
    shots.read(items, BODY)


def test_a_malformed_listed_screenshot_is_refused_not_a_crash(fork):
  with pytest.raises(ContributionSubmitError) as raised:
    shots.check_references([{"sha256": "1" * 64}], BODY)
  assert raised.value.status_code == 422
  with pytest.raises(ContributionSubmitError) as raised:
    shots.verify(fork["record_path"], ["after.png"])
  assert raised.value.code == "screenshot_changed"
  with pytest.raises(ContributionSubmitError) as raised:
    _send_screenshots(fork, screenshots=[{"sha256": "1" * 64}])
  assert raised.value.code == "screenshot_changed"
  assert raised.value.record_patch == {"k": "v"}


@pytest.mark.parametrize("padding", ["x", "é"])
def test_a_renamed_fork_whose_links_overflow_the_description_is_refused(fork, monkeypatch, padding):
  from app import github_contributions
  long_fork = "octocat/" + "p" * 90
  monkeypatch.setattr(github_contributions, "_ensure_owner_fork_remote", lambda *_: long_fork)
  monkeypatch.setattr(github_contributions.contribution_screenshots, "publish", lambda *a, **k: "b" * 40)
  # Fits with the guessed fork name, not with the real one.
  guessed = shots.resolve_body(
    BODY, fork["entries"], assets_repo="octocat/project", commit="0" * 64, branch="fix/header",
  )
  body = BODY + padding * (65_536 - len(guessed) - 10)
  with pytest.raises(ContributionSubmitError) as raised:
    _send_screenshots(fork, body=body)
  assert raised.value.code == "screenshot_body_too_large"
  assert raised.value.record_patch == {"k": "v"}


def test_a_shared_repository_without_push_permission_uploads_nothing(submit, monkeypatch):
  def refused(*_a):
    raise ContributionSubmitError("You cannot push to this repository.")

  monkeypatch.setattr("app.github_contribution_git._assert_upstream_push_permission", refused)
  with pytest.raises(ContributionSubmitError, match="cannot push"):
    submit["send"]("octo-org/project")
  assert not any(event[0] == "upload" for event in submit["events"] if isinstance(event, tuple))


def _replace_stored(fork, make) -> None:
  stored = shots.blob_dir(fork["record_path"]) / f"{fork['entries'][0]['sha256']}.png"
  stored.unlink()
  make(stored)


def test_an_oversized_stored_image_is_refused_without_reading_it_whole(fork, monkeypatch):
  _replace_stored(fork, lambda path: path.write_bytes(_png(1) + b"\0" * shots.MAX_BYTES))
  reads = []
  original = shots._read_bounded
  monkeypatch.setattr(shots, "_read_bounded", lambda path: reads.append(len(original(path))) or original(path))
  with pytest.raises(ContributionSubmitError) as raised:
    shots.verify(fork["record_path"], fork["entries"][:1])
  assert raised.value.code == "screenshot_changed"
  assert reads == [shots.MAX_BYTES + 1]


def test_a_stored_image_replaced_by_a_named_pipe_is_refused_without_blocking(fork):
  import os
  _replace_stored(fork, lambda path: os.mkfifo(path))
  with pytest.raises(ContributionSubmitError) as raised:
    shots.verify(fork["record_path"], fork["entries"][:1])
  assert raised.value.code == "screenshot_changed"


def test_a_stored_image_replaced_by_a_link_is_refused(fork, tmp_path):
  real = tmp_path / "real.png"
  real.write_bytes(_png(1))
  _replace_stored(fork, lambda path: path.symlink_to(real))
  with pytest.raises(ContributionSubmitError) as raised:
    shots.verify(fork["record_path"], fork["entries"][:1])
  assert raised.value.code == "screenshot_changed"
