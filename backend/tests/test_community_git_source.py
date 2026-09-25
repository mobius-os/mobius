"""Community Store packages update through their registry-proven Git commit.

A community install names a stable registry URL; the registry says which
publisher commit that URL serves. These tests pin the three contracts that make
Store updates work for such apps: fresh installs clone that commit, installs
imported before the origin was recorded adopt it on their first update check,
and a candidate for another package can never attach its origin.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app import app_git, community_source
from app.community_broker import COMMUNITY_BASE_URL, CommunityBrokerError
from app.config import get_settings
from tests.test_apps_install import (  # noqa: F401
  JSX,
  _fake_async_client,
  _fixture_commit,
  bypass_url_validation,
)


APP_ID = "app_" + "a1" * 16
OTHER_APP_ID = "app_" + "b2" * 16
BASE = f"{COMMUNITY_BASE_URL}/v1/community/source/{APP_ID}/current/"
MANIFEST = {
  "id": "community-pkg",
  "name": "Community package",
  "version": "1.0.0",
  "description": "Published through the community Store",
  "entry": "index.jsx",
  "source_files": ["cards.js"],
  "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
}
CARDS_V1 = "export const cards = ['v1']\n"
JSX_V2 = JSX.replace("ok", "second revision")
CARDS_V2 = "export const cards = ['v2']\n"


def _registry_responses(manifest, jsx, cards):
  return {
    BASE + "mobius.json": (200, json.dumps(manifest).encode()),
    BASE + "index.jsx": (200, jsx.encode()),
    BASE + "cards.js": (200, cards.encode()),
  }


def _publisher_repo(tmp_path: Path) -> tuple[Path, Path, str]:
  """A publisher repository whose first commit is the v1 package."""
  work = tmp_path / "publisher-work"
  bare = tmp_path / "publisher.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
  (work / "mobius.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
  (work / "index.jsx").write_text(JSX, encoding="utf-8")
  (work / "cards.js").write_text(CARDS_V1, encoding="utf-8")
  first = _fixture_commit(work, "v1")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(work), str(bare)],
    check=True, env=app_git._git_env(work),
  )
  return work, bare, first


def _publish_v2(work: Path, bare: Path) -> str:
  (work / "mobius.json").write_text(
    json.dumps({**MANIFEST, "version": "2.0.0"}), encoding="utf-8",
  )
  (work / "index.jsx").write_text(JSX_V2, encoding="utf-8")
  (work / "cards.js").write_text(CARDS_V2, encoding="utf-8")
  commit = _fixture_commit(work, "v2")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True, env=app_git._git_env(work),
  )
  return commit


def _registry_serves(origin: str, commit: str):
  return patch(
    "app.community_source.resolve_git_source",
    new=AsyncMock(return_value=(origin, commit)),
  )


def _install(client, auth, responses):
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    return client.post("/api/apps/install", headers=auth, json={
      "manifest_url": BASE + "mobius.json",
    })


def _legacy_import(client, auth) -> tuple[int, Path]:
  """Model an install made before community packages resolved to Git."""
  with patch(
    "app.community_source.resolve_git_source", new=AsyncMock(return_value=None),
  ):
    installed = _install(
      client, auth, _registry_responses(MANIFEST, JSX, CARDS_V1),
    )
  assert installed.status_code == 201, installed.text
  src = Path(get_settings().data_dir) / "apps" / MANIFEST["id"]
  assert app_git.origin_url(src) is None
  assert "mobius.json" not in app_git.read_ref_tree(src, app_git.UPSTREAM_BRANCH)
  return installed.json()["id"], src


def test_legacy_community_install_gains_git_updates_through_registry_origin(
  client, auth, tmp_path, bypass_url_validation,
):
  app_id, src = _legacy_import(client, auth)
  work, bare, _ = _publisher_repo(tmp_path)
  latest = _publish_v2(work, bare)

  with _registry_serves(bare.as_uri(), latest):
    check = client.get(f"/api/apps/{app_id}/update-check", headers=auth)
    assert check.status_code == 200, check.text
    assert check.json()["update_available"] is True
    assert check.json()["upstream_version"] == "2.0.0"
    assert app_git.origin_url(src) == bare.as_uri()
    # Adopting the origin is remote metadata only; nothing installed moved.
    assert (src / "index.jsx").read_text() == JSX

    preview = client.get(
      f"/api/apps/{app_id}/update-candidate-preview", headers=auth,
    )
    assert preview.status_code == 200, preview.text
    reviewed = preview.json()
    assert reviewed["upstream_commit"] == latest

    applied = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": BASE + "mobius.json",
      "reviewed_source_digest": reviewed["source_digest"],
      "update_app_id": app_id,
      "reviewed_upstream_commit": reviewed["upstream_commit"],
    })
    assert applied.status_code == 201, applied.text
    assert applied.json()["mode"] == "update"
    assert applied.json()["version"] == "2.0.0"
    assert (src / "index.jsx").read_text() == JSX_V2
    assert (src / "cards.js").read_text() == CARDS_V2
    assert app_git.head_sha(src, app_git.UPSTREAM_BRANCH) == latest

    settled = client.get(f"/api/apps/{app_id}/update-check", headers=auth)
    assert settled.status_code == 200, settled.text
    assert settled.json()["update_available"] is False


def test_first_community_update_keeps_owner_edits_made_before_the_origin(
  client, auth, tmp_path, bypass_url_validation,
):
  """Adopting the real lineage three-way merges owner edits, never drops them."""
  app_id, src = _legacy_import(client, auth)
  owner_cards = "export const cards = ['owner edit']\n"
  (src / "cards.js").write_text(owner_cards, encoding="utf-8")
  app_git.commit_local(src, "owner edit before the origin existed")

  work, bare, _ = _publisher_repo(tmp_path)
  (work / "mobius.json").write_text(
    json.dumps({**MANIFEST, "version": "2.0.0"}), encoding="utf-8",
  )
  (work / "index.jsx").write_text(JSX_V2, encoding="utf-8")
  latest = _fixture_commit(work, "v2 entry only")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True, env=app_git._git_env(work),
  )

  with _registry_serves(bare.as_uri(), latest):
    preview = client.get(
      f"/api/apps/{app_id}/update-candidate-preview", headers=auth,
    )
    assert preview.status_code == 200, preview.text
    applied = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": BASE + "mobius.json",
      "reviewed_source_digest": preview.json()["source_digest"],
      "update_app_id": app_id,
      "reviewed_upstream_commit": preview.json()["upstream_commit"],
    })

  assert applied.status_code == 201, applied.text
  assert applied.json()["mode"] == "update"
  assert (src / "index.jsx").read_text() == JSX_V2
  assert (src / "cards.js").read_text() == owner_cards


def test_registry_outage_leaves_a_legacy_install_unknown_and_unchanged(
  client, auth, bypass_url_validation,
):
  app_id, src = _legacy_import(client, auth)
  with patch(
    "app.community_source.resolve_git_source",
    new=AsyncMock(side_effect=community_source.CommunitySourceUnavailable("down")),
  ):
    check = client.get(f"/api/apps/{app_id}/update-check", headers=auth)
    preview = client.get(
      f"/api/apps/{app_id}/update-candidate-preview", headers=auth,
    )

  assert check.status_code == 200, check.text
  assert check.json()["update_available"] is None
  assert preview.status_code == 503, preview.text
  assert app_git.origin_url(src) is None


def test_registry_never_overwrites_an_existing_different_origin(
  client, auth, tmp_path, bypass_url_validation,
):
  app_id, src = _legacy_import(client, auth)
  existing = "https://github.com/someone/else.git"
  app_git._run(src, "remote", "add", "origin", existing)
  _, bare, first = _publisher_repo(tmp_path)

  with _registry_serves(bare.as_uri(), first):
    check = client.get(f"/api/apps/{app_id}/update-check", headers=auth)

  assert check.status_code == 200, check.text
  assert check.json()["update_available"] is None
  assert app_git.origin_url(src) == existing


def test_origin_identity_ignores_github_case_and_git_suffix():
  assert app_git.same_origin(
    "https://github.com/Publisher/App-Pkg.git",
    "https://github.com/publisher/app-pkg/",
  )
  assert not app_git.same_origin(
    "https://github.com/publisher/app-pkg.git",
    "https://github.com/publisher/other.git",
  )
  assert not app_git.same_origin(None, "https://github.com/publisher/app-pkg")


def test_update_check_never_adopts_origin_from_another_community_package(
  client, auth, tmp_path, bypass_url_validation,
):
  app_id, src = _legacy_import(client, auth)
  _, bare, first = _publisher_repo(tmp_path)
  other = (
    f"{COMMUNITY_BASE_URL}/v1/community/source/{OTHER_APP_ID}/current/mobius.json"
  )

  with _registry_serves(bare.as_uri(), first):
    check = client.get(
      f"/api/apps/{app_id}/update-check", headers=auth,
      params={"manifest_url": other},
    )

  assert check.status_code == 200, check.text
  assert check.json()["update_available"] is None
  assert app_git.origin_url(src) is None


def test_fresh_community_install_clones_the_registry_commit(
  client, auth, tmp_path, bypass_url_validation,
):
  work, bare, first = _publisher_repo(tmp_path)
  with _registry_serves(bare.as_uri(), first):
    installed = _install(
      client, auth, _registry_responses(MANIFEST, JSX, CARDS_V1),
    )
  assert installed.status_code == 201, installed.text
  src = Path(get_settings().data_dir) / "apps" / MANIFEST["id"]
  assert app_git.origin_url(src) == bare.as_uri()
  assert app_git.head_sha(src, app_git.UPSTREAM_BRANCH) == first

  latest = _publish_v2(work, bare)
  with _registry_serves(bare.as_uri(), latest):
    check = client.get(
      f"/api/apps/{installed.json()['id']}/update-check", headers=auth,
    )
  assert check.status_code == 200, check.text
  assert check.json()["update_available"] is True


def test_fresh_community_install_fails_closed_when_registry_is_unreachable(
  client, auth, bypass_url_validation,
):
  with patch(
    "app.community_source.resolve_git_source",
    new=AsyncMock(side_effect=community_source.CommunitySourceUnavailable("down")),
  ):
    installed = _install(
      client, auth, _registry_responses(MANIFEST, JSX, CARDS_V1),
    )
  assert installed.status_code == 409, installed.text
  assert installed.json()["detail"]["code"] == "git_install_unavailable"
  assert not (Path(get_settings().data_dir) / "apps" / MANIFEST["id"]).exists()


def _record(**revision):
  return {
    "repository": "Publisher/app-pkg",
    "repository_url": "https://github.com/Publisher/app-pkg",
    "latest_revision": {"id": "rev_latest0000", "commit_sha": "c" * 40},
    "revision": {"id": "rev_pinned0000", "commit_sha": "d" * 40, **revision},
  }


@pytest.mark.parametrize(
  ("selector", "path_suffix", "commit"),
  [
    ("current", "", "c" * 40),
    ("rev_pinned0000", "/revisions/rev_pinned0000", "d" * 40),
  ],
)
def test_registry_url_resolves_to_the_commit_it_serves(
  selector, path_suffix, commit,
):
  url = f"{COMMUNITY_BASE_URL}/v1/community/source/{APP_ID}/{selector}/mobius.json"
  broker = AsyncMock(return_value=(_record(), 200, {}))
  with patch.object(community_source.community_broker, "request", broker):
    resolved = asyncio.run(community_source.resolve_git_source(url))
  assert resolved == ("https://github.com/Publisher/app-pkg.git", commit)
  broker.assert_awaited_once_with(
    "GET", f"/v1/community/apps/{APP_ID}{path_suffix}",
  )


def test_non_registry_hosts_never_consult_the_registry():
  broker = AsyncMock()
  lookalike = f"https://mirror.invalid/v1/community/source/{APP_ID}/current/mobius.json"
  with patch.object(community_source.community_broker, "request", broker):
    assert asyncio.run(community_source.resolve_git_source(lookalike)) is None
  broker.assert_not_awaited()


@pytest.mark.parametrize(
  "broker",
  [
    AsyncMock(side_effect=CommunityBrokerError(503, "offline")),
    AsyncMock(return_value=({
      **_record(),
      "repository_url": "https://github.com/Someone/else",
    }, 200, {})),
    AsyncMock(return_value=({
      **_record(),
      "latest_revision": {"id": "rev_latest0000", "commit_sha": "not-a-sha"},
    }, 200, {})),
  ],
  ids=["registry-offline", "inconsistent-repository", "missing-commit"],
)
def test_unprovable_registry_source_is_unavailable_not_absent(broker):
  with patch.object(community_source.community_broker, "request", broker):
    with pytest.raises(community_source.CommunitySourceUnavailable):
      asyncio.run(community_source.resolve_git_source(BASE + "mobius.json"))
