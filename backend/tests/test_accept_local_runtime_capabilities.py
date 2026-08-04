"""Explicit local capability acceptance is bound, confined, and atomic."""

import json
from pathlib import Path

import pytest

from app import models, timeutil
from app.app_capabilities import contract_and_digest
from app.database import SessionLocal
from scripts import accept_local_runtime_capabilities as command
from test_app_fixtures import create_local_app


def _store_app(client, auth, db):
  created = create_local_app(
    client,
    auth,
    name="Capability Manager",
    capabilities={"device.asset-cache": {"version": 1}},
  )
  app = db.query(models.App).filter(models.App.id == created["id"]).one()
  manifest = json.loads((Path(app.source_dir) / "mobius.json").read_text())
  app.capability_contract = contract_and_digest(manifest)[0]
  app.manifest_url = "https://store.example/capability-manager/mobius.json"
  db.commit()
  db.refresh(app)
  manifest["capabilities"] = {
    "media.speech": {
      "version": 1,
      "reason": "Read reports with the selected local voice.",
    },
  }
  (Path(app.source_dir) / "mobius.json").write_text(json.dumps(manifest))
  return app


def _run(args, capsys):
  command.main(args)
  return json.loads(capsys.readouterr().out)


def test_review_and_accept_round_trip_preserves_store_contract(client, auth, db, capsys):
  app = _store_app(client, auth, db)
  before = app.capability_contract

  report = _run(["--app-id", str(app.id)], capsys)
  assert report["status"] == "review"
  assert "media.speech" in report["candidate_runtime"]

  accepted = _run([
    "--app-id", str(app.id), "--accept-digest", report["accept_digest"],
  ], capsys)
  assert accepted["status"] == "accepted"
  db.expire_all()
  stored = db.get(models.App, app.id).capability_contract
  assert stored["runtime"] == report["candidate_runtime"]
  assert {k: v for k, v in stored.items() if k != "runtime"} == {
    k: v for k, v in before.items() if k != "runtime"
  }


def test_digest_mismatch_is_a_concise_error_and_does_not_write(
  client, auth, db, capsys,
):
  app = _store_app(client, auth, db)
  before = app.capability_contract
  with pytest.raises(SystemExit) as exc:
    command.main([
      "--app-id", str(app.id), "--accept-digest", "0" * 64,
    ])
  assert exc.value.code == 2
  assert "review again" in capsys.readouterr().err
  db.expire_all()
  assert db.get(models.App, app.id).capability_contract == before


def test_command_rejects_non_store_apps_and_source_paths_outside_apps_root(
  client, auth, db, capsys, tmp_path,
):
  app = _store_app(client, auth, db)
  app.manifest_url = None
  db.commit()
  with pytest.raises(SystemExit):
    command.main(["--app-id", str(app.id)])
  assert "only for Store-installed apps" in capsys.readouterr().err

  app.manifest_url = "https://store.example/app/mobius.json"
  app.source_dir = str(tmp_path)
  db.commit()
  with pytest.raises(SystemExit):
    command.main(["--app-id", str(app.id)])
  assert "outside the reviewed apps root" in capsys.readouterr().err


def test_manifest_symlink_cannot_escape_the_app_source(client, auth, db, capsys, tmp_path):
  app = _store_app(client, auth, db)
  manifest_path = Path(app.source_dir) / "mobius.json"
  outside = tmp_path / "mobius.json"
  outside.write_text(manifest_path.read_text())
  manifest_path.unlink()
  manifest_path.symlink_to(outside)

  with pytest.raises(SystemExit):
    command.main(["--app-id", str(app.id)])
  assert "regular file inside the app source" in capsys.readouterr().err


def test_source_directory_symlink_cannot_alias_a_sibling_app(
  client, auth, db, capsys,
):
  app = _store_app(client, auth, db)
  source = Path(app.source_dir)
  sibling = source.with_name(f"{source.name}-sibling")
  source.rename(sibling)
  source.symlink_to(sibling, target_is_directory=True)
  try:
    with pytest.raises(SystemExit):
      command.main(["--app-id", str(app.id)])
    assert "direct, regular directory under the apps root" in capsys.readouterr().err
  finally:
    source.unlink()
    sibling.rename(source)


def test_pinned_source_directory_cannot_escape_during_parent_swap(
  client, auth, db, capsys, tmp_path, monkeypatch,
):
  app = _store_app(client, auth, db)
  source = Path(app.source_dir)
  pinned = source.with_name(f"{source.name}-pinned")
  outside = tmp_path / "outside"
  outside.mkdir()
  outside_manifest = json.loads((source / "mobius.json").read_text())
  outside_manifest["capabilities"] = {
    "media.microphone.capture": {"version": 1, "reason": "must not escape"},
  }
  (outside / "mobius.json").write_text(json.dumps(outside_manifest))

  real_open = command.os.open
  swapped = False

  def swap_before_manifest(path, flags, *args, **kwargs):
    nonlocal swapped
    if path == "mobius.json" and not swapped:
      swapped = True
      source.rename(pinned)
      source.symlink_to(outside, target_is_directory=True)
    return real_open(path, flags, *args, **kwargs)

  monkeypatch.setattr(command.os, "open", swap_before_manifest)
  try:
    report = _run(["--app-id", str(app.id)], capsys)
    assert swapped is True
    assert "media.speech" in report["candidate_runtime"]
    assert "media.microphone.capture" not in report["candidate_runtime"]
  finally:
    if source.is_symlink():
      source.unlink()
    if pinned.exists():
      pinned.rename(source)


def test_unexpected_failure_rolls_back_the_command_session(
  client, auth, db, monkeypatch,
):
  app = _store_app(client, auth, db)
  original_name = app.name
  digest = command.review(app)[1]["accept_digest"]

  def fail_after_mutation(session, loaded, candidate, report):
    loaded.name = "must roll back"
    session.flush()
    raise RuntimeError("injected failure")

  monkeypatch.setattr(command, "_accept", fail_after_mutation)
  with pytest.raises(RuntimeError, match="injected failure"):
    command.main([
      "--app-id", str(app.id), "--accept-digest", digest,
    ])
  db.expire_all()
  assert db.get(models.App, app.id).name == original_name


def test_concurrent_contract_change_wins_and_acceptance_is_rejected(
  client, auth, db, capsys, monkeypatch,
):
  app = _store_app(client, auth, db)
  report = _run(["--app-id", str(app.id)], capsys)
  original_review = command.review
  concurrent_contract = {**app.capability_contract, "concurrent_fact": "preserve-me"}

  def review_then_concurrent_write(loaded):
    result = original_review(loaded)
    writer = SessionLocal()
    try:
      changed = writer.get(models.App, app.id)
      changed.capability_contract = concurrent_contract
      changed.updated_at = timeutil.now_naive_utc()
      writer.commit()
    finally:
      writer.close()
    return result

  monkeypatch.setattr(command, "review", review_then_concurrent_write)
  with pytest.raises(SystemExit):
    command.main([
      "--app-id", str(app.id), "--accept-digest", report["accept_digest"],
    ])
  assert "changed while capabilities were being accepted" in capsys.readouterr().err
  db.expire_all()
  assert db.get(models.App, app.id).capability_contract == concurrent_contract
