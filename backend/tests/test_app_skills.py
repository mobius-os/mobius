"""Manifest `skills` — validation + the post-commit shared-skill sync phase.

Same harness as test_apps_install.py: a mocked httpx.AsyncClient (no real
network), the cron-scaffold bypass, and — for the clone-path tests — a local
bare repo standing in for the GitHub catalog remote. The sync phase's
never-lose-work contract is the focus: an agent-edited (or unrecorded) skill
file must be git-snapshotted into the /data repo before being overwritten,
and left untouched when the snapshot cannot be guaranteed.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app import app_git
from app.config import get_settings

# Importing fixtures registers them for this module too (autouse included):
# the cron-scaffold bypass and resolver stub must cover these tests exactly
# as they cover test_apps_install's.
from tests.test_apps_install import (  # noqa: F401
  JSX,
  JSX_MULTI,
  _bypass_cron_scaffold,
  _fake_async_client,
  _fixture_commit,
  _stub_resolver_run_chat,
  bypass_url_validation,
)

BASE = "https://skills.test/repo/"
SKILL_V1 = "# contributing v1\nStudy existing work first.\n"
SKILL_V2 = "# contributing v2\nStage the plan, then the green light.\n"
SKILL_AGENT = "# contributing v1\nMy own hard-won addendum.\n"


def _skill_manifest(**over):
  m = {
    "id": "skilled",
    "name": "Skilled",
    "version": "1.0.0",
    "description": "Ships a shared skill",
    "entry": "index.jsx",
    "source_files": ["contributing.md"],
    "skills": ["contributing.md"],
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  m.update(over)
  return m


def _install(client, auth, manifest, files, base=BASE):
  """POST /install with every `files` entry served at base+<rel>."""
  responses = {base + "mobius.json": (200, json.dumps(manifest).encode())}
  for rel, body in files.items():
    responses[base + rel] = (
      200, body if isinstance(body, bytes) else body.encode(),
    )
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    return client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })


def _skills_dir() -> Path:
  return Path(get_settings().data_dir) / "shared" / "skills"


def _sidecar() -> dict:
  return json.loads(
    (_skills_dir() / ".app-skills.json").read_text(encoding="utf-8")
  )


def _sha(text: str) -> str:
  return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def data_git_repo():
  """A git repo at DATA_DIR standing in for the /data safety-net repo.

  Torn down after the test: DATA_DIR is one module-level tempdir shared by
  the whole suite, so a persistent .git would flip every later skill sync
  into the snapshot path.
  """
  data_dir = Path(get_settings().data_dir)
  subprocess.run(["git", "init", "-q", "-b", "main", str(data_dir)], check=True)
  # An initial commit so HEAD is born — partial (`--only`) commits are not
  # allowed on an unborn branch, and prod's entrypoint commits "init" too.
  subprocess.run(
    ["git", "-C", str(data_dir),
     "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
     "commit", "-q", "--allow-empty", "-m", "init"],
    check=True,
  )
  yield data_dir
  shutil.rmtree(data_dir / ".git", ignore_errors=True)


# --- validation -----------------------------------------------------------


def _expect_400(client, auth, manifest, needle):
  r = client.post("/api/apps/install", headers=auth, json={
    "manifest": manifest,
    "raw_base": "https://raw.githubusercontent.com/x/app/main/",
  })
  assert r.status_code == 400, r.text
  assert needle in r.json()["detail"]


def test_skills_must_be_an_array(client, auth):
  _expect_400(
    client, auth,
    _skill_manifest(skills="contributing.md"),
    "must be an array",
  )


def test_skills_count_capped(client, auth):
  names = [f"s{i}.md" for i in range(6)]
  _expect_400(
    client, auth,
    _skill_manifest(skills=names, source_files=names),
    "too many skills",
  )


@pytest.mark.parametrize("bad", [
  "notes.txt",          # not .md
  ".md",                # extension only
  42,                   # not a string
  "docs/contributing.md",  # directory
  "..\\contributing.md",   # backslash / traversal
  ".hidden.md",         # dotfile — skills-dir dotfiles are installer-owned
])
def test_skills_entry_shape_rejected(client, auth, bad):
  _expect_400(
    client, auth,
    _skill_manifest(skills=[bad]),
    "skills[0]",
  )


def test_skills_must_be_root_source_files(client, auth):
  # Not listed in source_files at all.
  _expect_400(
    client, auth,
    _skill_manifest(source_files=None),
    "source_files",
  )
  # Listed, but nested — only ROOT basenames qualify (the sync phase reads
  # source_dir/<basename>).
  _expect_400(
    client, auth,
    _skill_manifest(source_files=["docs/contributing.md"]),
    "source_files",
  )


# --- sync phase: the P2 matrix ---------------------------------------------


def test_install_materializes_skill_with_record(
  client, auth, bypass_url_validation,
):
  """Absent target → written 0o664, sidecar records owner + sha."""
  r = _install(client, auth, _skill_manifest(), {
    "index.jsx": JSX, "contributing.md": SKILL_V1,
  })
  assert r.status_code == 201, r.text
  assert not any("skill" in w for w in r.json()["warnings"])
  target = _skills_dir() / "contributing.md"
  assert target.read_text() == SKILL_V1
  assert stat.S_IMODE(target.stat().st_mode) == 0o664
  rec = _sidecar()["contributing.md"]
  assert rec["app_id"] == r.json()["id"]
  assert rec["slug"] == "skilled"
  assert rec["sha256"] == _sha(SKILL_V1)
  assert rec["installed_at"]
  assert rec["manifest_url"]


def test_update_overwrites_unmodified_skill_silently(
  client, auth, bypass_url_validation,
):
  """sha == recorded → routine overwrite + re-record, no snapshot warning."""
  r1 = _install(client, auth, _skill_manifest(), {
    "index.jsx": JSX, "contributing.md": SKILL_V1,
  })
  assert r1.status_code == 201, r1.text
  r2 = _install(client, auth, _skill_manifest(version="2.0.0"), {
    "index.jsx": JSX, "contributing.md": SKILL_V2,
  })
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert not any("skill" in w for w in r2.json()["warnings"])
  assert (_skills_dir() / "contributing.md").read_text() == SKILL_V2
  assert _sidecar()["contributing.md"]["sha256"] == _sha(SKILL_V2)


def test_modified_skill_is_snapshotted_then_overwritten(
  client, auth, bypass_url_validation, data_git_repo,
):
  """Agent-edited bytes land in a /data git commit BEFORE the overwrite."""
  r1 = _install(client, auth, _skill_manifest(), {
    "index.jsx": JSX, "contributing.md": SKILL_V1,
  })
  assert r1.status_code == 201, r1.text
  (_skills_dir() / "contributing.md").write_text(SKILL_AGENT)

  r2 = _install(client, auth, _skill_manifest(version="2.0.0"), {
    "index.jsx": JSX, "contributing.md": SKILL_V2,
  })
  assert r2.status_code == 201, r2.text
  assert any(
    "contributing.md: snapshotted then updated" in w
    for w in r2.json()["warnings"]
  ), r2.json()["warnings"]
  assert (_skills_dir() / "contributing.md").read_text() == SKILL_V2
  assert _sidecar()["contributing.md"]["sha256"] == _sha(SKILL_V2)
  # The snapshot commit exists, is scoped to the one file, and preserves the
  # agent's exact bytes.
  subjects = subprocess.run(
    ["git", "-C", str(data_git_repo), "log", "--format=%s",
     "--", "shared/skills/contributing.md"],
    capture_output=True, text=True, check=True,
  ).stdout
  assert (
    "pre-install snapshot of contributing.md (app skilled v2.0.0)"
    in subjects
  )
  snapshot = subprocess.run(
    ["git", "-C", str(data_git_repo), "show",
     "HEAD:shared/skills/contributing.md"],
    capture_output=True, text=True, check=True,
  ).stdout
  assert snapshot == SKILL_AGENT


def test_snapshot_failure_leaves_modified_skill_untouched(
  client, auth, bypass_url_validation, data_git_repo,
):
  """Snapshot blocked (index.lock) → NO overwrite, NO re-record, warning."""
  r1 = _install(client, auth, _skill_manifest(), {
    "index.jsx": JSX, "contributing.md": SKILL_V1,
  })
  assert r1.status_code == 201, r1.text
  (_skills_dir() / "contributing.md").write_text(SKILL_AGENT)
  (data_git_repo / ".git" / "index.lock").touch()

  r2 = _install(client, auth, _skill_manifest(version="2.0.0"), {
    "index.jsx": JSX, "contributing.md": SKILL_V2,
  })
  assert r2.status_code == 201, r2.text
  assert any(
    "contributing.md: left unchanged (snapshot failed" in w
    for w in r2.json()["warnings"]
  ), r2.json()["warnings"]
  assert (_skills_dir() / "contributing.md").read_text() == SKILL_AGENT
  assert _sidecar()["contributing.md"]["sha256"] == _sha(SKILL_V1)


def test_foreign_live_owner_is_never_clobbered(
  client, auth, bypass_url_validation,
):
  """A second live app declaring the same skill file is skipped + warned."""
  r1 = _install(client, auth, _skill_manifest(), {
    "index.jsx": JSX, "contributing.md": SKILL_V1,
  })
  assert r1.status_code == 201, r1.text
  r2 = _install(
    client, auth,
    _skill_manifest(id="other", name="Other"),
    {"index.jsx": JSX, "contributing.md": "# impostor\n"},
    base="https://other.test/repo/",
  )
  assert r2.status_code == 201, r2.text
  assert any(
    "contributing.md: owned by app skilled" in w
    for w in r2.json()["warnings"]
  ), r2.json()["warnings"]
  assert (_skills_dir() / "contributing.md").read_text() == SKILL_V1
  assert _sidecar()["contributing.md"]["app_id"] == r1.json()["id"]


def test_conflict_update_skips_skill_sync(
  client, auth, bypass_url_validation,
):
  """mode=conflict returns before the sync phase — old-code-served means
  old-skill-kept (never install new-version instructions for old code)."""
  m = _skill_manifest()
  r1 = _install(client, auth, m, {
    "index.jsx": JSX_MULTI, "contributing.md": SKILL_V1,
  })
  assert r1.status_code == 201, r1.text
  app_dir = Path(get_settings().data_dir) / "apps" / "skilled"
  (app_dir / "index.jsx").write_text(
    JSX_MULTI.replace("ORIGINAL TITLE", "AGENT TITLE"), encoding="utf-8",
  )

  r2 = _install(client, auth, {**m, "version": "2.0.0"}, {
    "index.jsx": JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE"),
    "contributing.md": SKILL_V2,
  })
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "conflict"
  assert (_skills_dir() / "contributing.md").read_text() == SKILL_V1
  assert _sidecar()["contributing.md"]["sha256"] == _sha(SKILL_V1)


def test_uninstall_deactivates_skill_and_recover_restores_exact_bytes(
  client, auth, bypass_url_validation,
):
  r1 = _install(client, auth, _skill_manifest(), {
    "index.jsx": JSX, "contributing.md": SKILL_V1,
  })
  assert r1.status_code == 201, r1.text
  # Preserve owner/agent edits exactly across the inactive interval.
  (_skills_dir() / "contributing.md").write_text(SKILL_AGENT)
  r2 = client.delete(f"/api/apps/{r1.json()['id']}", headers=auth)
  assert r2.status_code == 204
  assert not (_skills_dir() / "contributing.md").exists()
  inactive = (
    _skills_dir() / ".inactive" / str(r1.json()["id"]) / "contributing.md"
  )
  assert inactive.read_text() == SKILL_AGENT
  inactive_record = _sidecar()["contributing.md"]
  assert inactive_record["active"] is False
  assert inactive_record["sha256"] == _sha(SKILL_V1)
  assert inactive_record["inactive_sha256"] == _sha(SKILL_AGENT)

  recovered = client.post(
    f"/api/apps/{r1.json()['id']}/recover", headers=auth,
  )
  assert recovered.status_code == 200, recovered.text
  assert (_skills_dir() / "contributing.md").read_text() == SKILL_AGENT
  assert not inactive.exists()
  restored_record = _sidecar()["contributing.md"]
  assert restored_record["active"] is True
  assert restored_record["sha256"] == _sha(SKILL_V1)
  assert "inactive_sha256" not in restored_record


def test_tombstoned_skill_basename_remains_reserved(
  client, auth, bypass_url_validation,
):
  owner = _install(client, auth, _skill_manifest(), {
    "index.jsx": JSX, "contributing.md": SKILL_V1,
  })
  assert owner.status_code == 201
  assert client.delete(
    f"/api/apps/{owner.json()['id']}", headers=auth,
  ).status_code == 204

  other = _install(
    client, auth,
    _skill_manifest(id="other", name="Other"),
    {"index.jsx": JSX, "contributing.md": "# impostor\n"},
    base="https://other.test/repo/",
  )
  assert other.status_code == 201, other.text
  assert any(
    "contributing.md: owned by app skilled" in warning
    for warning in other.json()["warnings"]
  )
  assert not (_skills_dir() / "contributing.md").exists()


def test_update_dropping_skill_retires_and_releases_record(
  client, auth, bypass_url_validation,
):
  first = _install(client, auth, _skill_manifest(), {
    "index.jsx": JSX, "contributing.md": SKILL_V1,
  })
  assert first.status_code == 201
  update = _install(
    client,
    auth,
    _skill_manifest(version="2.0.0", skills=[], source_files=[]),
    {"index.jsx": JSX},
  )
  assert update.status_code == 201, update.text
  assert not (_skills_dir() / "contributing.md").exists()
  assert "contributing.md" not in _sidecar()
  retired = (
    _skills_dir() / ".inactive" / str(first.json()["id"])
    / "retired" / "contributing.md"
  )
  assert retired.read_text() == SKILL_V1


def test_oversized_skill_is_skipped_with_warning(
  client, auth, bypass_url_validation,
):
  big = "x" * (256 * 1024 + 1)
  r = _install(client, auth, _skill_manifest(), {
    "index.jsx": JSX, "contributing.md": big,
  })
  assert r.status_code == 201, r.text
  assert any(
    "contributing.md: exceeds" in w for w in r.json()["warnings"]
  ), r.json()["warnings"]
  assert not (_skills_dir() / "contributing.md").exists()
  assert not (_skills_dir() / ".app-skills.json").exists()


# --- clone path: skills read from the FINAL on-disk tree --------------------


def _make_repo(tmp_path, files: dict[str, str], exec_names=()):
  """A work tree + bare remote standing in for a catalog GitHub repo."""
  work = tmp_path / "repo-work"
  bare = tmp_path / "repo.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
  for rel, text in files.items():
    p = work / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    if rel in exec_names:
      p.chmod(0o755)
  _fixture_commit(work, "v1")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(work), str(bare)],
    check=True, env=app_git._git_env(work),
  )
  return work, bare


def _push_repo(
  work: Path, bare: Path, files: dict[str, str], msg: str = "update",
) -> str:
  """Advances the fixture repo — test_apps_install's _push_clone_fixture,
  generalized to arbitrary file sets."""
  for rel, text in files.items():
    p = work / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
  head = _fixture_commit(work, msg)
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True, env=app_git._git_env(work),
  )
  return head


def _install_clone(client, auth, base, manifest, responses, bare):
  with patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), "main"),
  ), patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    return client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })


def test_clone_install_reads_skill_from_repo_not_http(
  client, auth, tmp_path, bypass_url_validation,
):
  """On the clone path the repo's bytes are canonical: the skill lands from
  the checked-out tree, not from the (discarded) HTTP source_files fetch."""
  base = "https://raw.githubusercontent.com/acme/app-skilled/main/"
  _, bare = _make_repo(tmp_path, {
    "index.jsx": JSX, "contributing.md": "REPO SKILL\n",
  })
  m = _skill_manifest()
  r = _install_clone(client, auth, base, m, {
    base + "mobius.json": (200, json.dumps(m).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "contributing.md": (200, b"HTTP SKILL\n"),
  }, bare)
  assert r.status_code == 201, r.text
  assert (_skills_dir() / "contributing.md").read_text() == "REPO SKILL\n"
  assert _sidecar()["contributing.md"]["sha256"] == _sha("REPO SKILL\n")


def test_clone_install_missing_skill_file_warns(
  client, auth, tmp_path, bypass_url_validation,
):
  """A repo tree that lacks the declared skill warns instead of silently
  falling back to the HTTP bytes (validation checked the manifest's claim,
  not the repo's contents)."""
  base = "https://raw.githubusercontent.com/acme/app-noskill/main/"
  _, bare = _make_repo(tmp_path, {"index.jsx": JSX})
  m = _skill_manifest(id="noskill", name="No Skill")
  r = _install_clone(client, auth, base, m, {
    base + "mobius.json": (200, json.dumps(m).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "contributing.md": (200, b"HTTP SKILL\n"),
  }, bare)
  assert r.status_code == 201, r.text
  assert any(
    "contributing.md: missing from installed source tree" in w
    for w in r.json()["warnings"]
  ), r.json()["warnings"]
  assert not (_skills_dir() / "contributing.md").exists()


# --- clone path: canonical entry + the dead-cron warning --------------------


def test_clone_install_rejects_non_index_entry(
  client, auth, tmp_path, bypass_url_validation,
):
  """Clone-eligible packages still use the platform's canonical index.jsx."""
  base = "https://raw.githubusercontent.com/acme/app-entry/main/"
  _, bare = _make_repo(tmp_path, {"app.jsx": JSX})
  m = {
    "id": "custom-entry",
    "name": "Custom Entry",
    "version": "1.0.0",
    "description": "Non-root entry",
    "entry": "app.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  r = _install_clone(client, auth, base, m, {
    base + "mobius.json": (200, json.dumps(m).encode()),
    base + "app.jsx": (200, JSX.encode()),
  }, bare)
  assert r.status_code == 400, r.text
  assert "index.jsx" in r.json()["detail"]


JOB_SH = "#!/bin/sh\necho ok\n"


def _job_manifest(app_id: str):
  return {
    "id": app_id,
    "name": app_id,
    "version": "1.0.0",
    "description": "Scheduled clone",
    "entry": "index.jsx",
    "schedule": {"default": "0 10 * * *", "job": "job.sh"},
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }


def test_cloned_job_without_exec_bit_warns(
  client, auth, tmp_path, bypass_url_validation,
):
  """A repo carrying job.sh as 100644 lands non-executable (the installer
  must not chmod tracked clone files) and the install says so."""
  base = "https://raw.githubusercontent.com/acme/app-cronjob/main/"
  _, bare = _make_repo(tmp_path, {"index.jsx": JSX, "job.sh": JOB_SH})
  m = _job_manifest("cronjob")
  r = _install_clone(client, auth, base, m, {
    base + "mobius.json": (200, json.dumps(m).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "job.sh": (200, JOB_SH.encode()),
  }, bare)
  assert r.status_code == 201, r.text
  job = Path(get_settings().data_dir) / "apps" / "cronjob" / "job.sh"
  assert job.exists()
  assert not os.access(job, os.X_OK)
  assert any(
    "job.sh is not executable" in w for w in r.json()["warnings"]
  ), r.json()["warnings"]


def test_cloned_job_with_exec_bit_is_executable(
  client, auth, tmp_path, bypass_url_validation,
):
  """The committed +x bit survives the clone — no warning, job runnable."""
  base = "https://raw.githubusercontent.com/acme/app-cronjob-x/main/"
  _, bare = _make_repo(
    tmp_path, {"index.jsx": JSX, "job.sh": JOB_SH}, exec_names={"job.sh"},
  )
  m = _job_manifest("cronjob-x")
  r = _install_clone(client, auth, base, m, {
    base + "mobius.json": (200, json.dumps(m).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "job.sh": (200, JOB_SH.encode()),
  }, bare)
  assert r.status_code == 201, r.text
  job = Path(get_settings().data_dir) / "apps" / "cronjob-x" / "job.sh"
  assert os.access(job, os.X_OK)
  assert not any(
    "not executable" in w for w in r.json()["warnings"]
  ), r.json()["warnings"]
