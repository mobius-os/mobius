"""A developer's instance can start on its staging launcher; nobody else changes.

The identity broker owns the rule (``select_launcher_side``) and the server
follows its answer (``app.launcher_side``).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import httpx
import pytest

from app import launcher_side

BROKER_PATH = Path(__file__).parents[1] / "runtime" / "identity_broker.py"
SPEC = importlib.util.spec_from_file_location("launcher_side_broker", BROKER_PATH)
assert SPEC and SPEC.loader
broker_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(broker_module)

STAGING = "https://staging.example.test"
SECRET = "s" * 48
DEFAULT_ENV = {
  "MOBIUS_SSO_ISSUER": "https://www.mobius.you",
  "MOBIUS_SSO_CLIENT_SECRET": "p" * 48,
  "MOBIUS_SSO_INSTANCE_ID": "mob_example",
  "MOBIUS_IDENTITY_BOOTSTRAP": "default-receipt",
}
STAGING_ENV = {
  "MOBIUS_STAGING_ISSUER": STAGING,
  "MOBIUS_STAGING_CLIENT_SECRET": SECRET,
  "MOBIUS_STAGING_BOOTSTRAP": "staging-receipt",
}


def _choose_staging(data_dir: Path) -> None:
  (data_dir / "launcher-side").write_text("staging\n", encoding="utf-8")


def test_an_instance_without_staging_variables_is_left_exactly_as_it_was(tmp_path):
  _choose_staging(tmp_path)
  environ = dict(DEFAULT_ENV)

  decision = broker_module.select_launcher_side(environ, tmp_path)

  assert decision == {"available": False, "side": "production", "launcher": None}
  assert environ == DEFAULT_ENV


def test_staging_variables_alone_do_not_move_an_instance_off_its_launcher(tmp_path):
  environ = {**DEFAULT_ENV, **STAGING_ENV}

  decision = broker_module.select_launcher_side(environ, tmp_path)

  assert decision == {"available": True, "side": "production", "launcher": None}
  assert environ == DEFAULT_ENV


def test_a_chosen_staging_side_replaces_the_launcher_for_this_boot(tmp_path):
  _choose_staging(tmp_path)
  environ = {**DEFAULT_ENV, **STAGING_ENV}

  decision = broker_module.select_launcher_side(environ, tmp_path)

  assert decision == {"available": True, "side": "staging", "launcher": STAGING}
  assert environ == {
    "MOBIUS_SSO_ISSUER": STAGING,
    "MOBIUS_IDENTITY_ISSUER": STAGING,
    "MOBIUS_SSO_CLIENT_SECRET": SECRET,
    "MOBIUS_SSO_INSTANCE_ID": "mob_example",
    "MOBIUS_IDENTITY_BOOTSTRAP": "staging-receipt",
  }


def test_the_default_enrollment_receipt_is_never_offered_to_staging(tmp_path):
  _choose_staging(tmp_path)
  environ = {**DEFAULT_ENV, **STAGING_ENV}
  del environ["MOBIUS_STAGING_BOOTSTRAP"]

  broker_module.select_launcher_side(environ, tmp_path)

  assert "MOBIUS_IDENTITY_BOOTSTRAP" not in environ


@pytest.mark.parametrize("broken", [
  {"MOBIUS_STAGING_ISSUER": "http://staging.example.test"},
  {"MOBIUS_STAGING_ISSUER": "https://staging.example.test/path"},
  {"MOBIUS_STAGING_ISSUER": "https://user@staging.example.test"},
  {"MOBIUS_STAGING_ISSUER": ""},
  {"MOBIUS_STAGING_ISSUER": "https://[bad"},
  {"MOBIUS_STAGING_ISSUER": "https://staging.example.test:notaport"},
  # The default launcher under a second name would share its link files.
  {"MOBIUS_STAGING_ISSUER": "https://WWW.mobius.you/"},
  {"MOBIUS_STAGING_CLIENT_SECRET": "too-short"},
  # Without an instance id the broker and the settings both refuse to start.
  {"MOBIUS_SSO_INSTANCE_ID": ""},
  {"MOBIUS_SSO_INSTANCE_ID": "not-an-instance"},
])
def test_a_broken_staging_configuration_falls_back_to_the_default_launcher(
  tmp_path, broken,
):
  _choose_staging(tmp_path)
  environ = {**DEFAULT_ENV, **STAGING_ENV, **broken}

  decision = broker_module.select_launcher_side(environ, tmp_path)

  assert decision == {"available": False, "side": "production", "launcher": None}
  assert environ == {**DEFAULT_ENV, **{k: v for k, v in broken.items() if k in DEFAULT_ENV}}


def test_the_staging_address_is_reported_in_the_form_the_settings_accept(tmp_path):
  _choose_staging(tmp_path)
  environ = {**DEFAULT_ENV, **STAGING_ENV, "MOBIUS_STAGING_ISSUER": "HTTPS://Staging.Example.Test/"}

  assert broker_module.select_launcher_side(environ, tmp_path)["launcher"] == STAGING


@pytest.mark.parametrize("content", [b"", b"production\n", b"STAGING", b"\xff\xfe"])
def test_only_the_exact_word_chooses_staging(tmp_path, content):
  (tmp_path / "launcher-side").write_bytes(content)
  environ = {**DEFAULT_ENV, **STAGING_ENV}

  assert broker_module.select_launcher_side(environ, tmp_path)["side"] == "production"
  assert environ == DEFAULT_ENV


def test_an_unavailable_staging_side_never_reads_the_choice_file(tmp_path, monkeypatch):
  def unread(_data_dir):
    raise AssertionError("read the launcher-side file")

  monkeypatch.setattr(broker_module, "_chosen_side", unread)

  assert broker_module.select_launcher_side(dict(DEFAULT_ENV), tmp_path)["available"] is False


def test_a_fifo_in_place_of_the_choice_file_cannot_stall_the_broker(tmp_path):
  os.mkfifo(tmp_path / "launcher-side")
  environ = {**DEFAULT_ENV, **STAGING_ENV}

  assert broker_module.select_launcher_side(environ, tmp_path)["side"] == "production"


def test_a_link_in_place_of_the_choice_file_is_not_followed(tmp_path):
  target = tmp_path / "elsewhere"
  target.write_text("staging\n", encoding="utf-8")
  (tmp_path / "launcher-side").symlink_to(target)
  environ = {**DEFAULT_ENV, **STAGING_ENV}

  assert broker_module.select_launcher_side(environ, tmp_path)["side"] == "production"


def test_a_device_in_place_of_the_choice_file_is_not_read(tmp_path):
  (tmp_path / "launcher-side").symlink_to("/dev/zero")
  environ = {**DEFAULT_ENV, **STAGING_ENV}

  assert broker_module.select_launcher_side(environ, tmp_path)["side"] == "production"


def test_an_oversized_choice_file_is_not_read_as_a_choice(tmp_path):
  (tmp_path / "launcher-side").write_text("staging\n" + " " * 4096, encoding="utf-8")
  environ = {**DEFAULT_ENV, **STAGING_ENV}

  assert broker_module.select_launcher_side(environ, tmp_path)["side"] == "production"


class _NoUpstreamRoutes:
  """A broker with nothing to forward, as for any path it does not know."""

  def proxy(self, *_args, **_kwargs):
    raise FileNotFoundError("broker route not found")


def test_the_broker_reports_its_decision_only_on_the_private_socket(monkeypatch):
  decision = {"available": True, "side": "staging", "launcher": STAGING}
  monkeypatch.setattr(broker_module, "LAUNCHER_SIDE", decision)
  socket_dir = Path(tempfile.mkdtemp(prefix="mls-"))
  unix = broker_module._UnixServer(str(socket_dir / "b.sock"), broker_module._Handler)
  tcp = broker_module._TcpServer(("127.0.0.1", 0), broker_module._Handler)
  unix.broker = tcp.broker = _NoUpstreamRoutes()
  unix.is_unix, tcp.is_unix = True, False
  threads = [
    threading.Thread(target=server.serve_forever, daemon=True)
    for server in (unix, tcp)
  ]
  for thread in threads:
    thread.start()
  try:
    transport = httpx.HTTPTransport(uds=str(socket_dir / "b.sock"))
    with httpx.Client(transport=transport, base_url="http://broker") as client:
      assert client.get("/identity/launcher-side").json() == decision
    port = tcp.server_address[1]
    loopback = httpx.get(f"http://127.0.0.1:{port}/identity/launcher-side")
    assert loopback.status_code == 404
  finally:
    for server in (unix, tcp):
      server.shutdown()
      server.server_close()
    shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture
def launcher_env(monkeypatch, tmp_path):
  """A broker whose socket exists and answers as each test says."""
  monkeypatch.setattr(launcher_side.time, "sleep", lambda _seconds: None)
  monkeypatch.setattr(launcher_side, "broker_socket_path", lambda: str(tmp_path))
  return monkeypatch


def _answer(monkeypatch, decision: dict) -> None:
  monkeypatch.setattr(launcher_side, "_broker_decision", lambda: decision)


def test_the_server_follows_a_staging_decision(launcher_env):
  environ = {"MOBIUS_SSO_ISSUER": "https://www.mobius.you"}
  _answer(launcher_env, {"available": True, "side": "staging", "launcher": STAGING})

  side = launcher_side._follow_broker(environ)

  assert side == launcher_side.LauncherSide(available=True, side="staging")
  assert environ["MOBIUS_SSO_ISSUER"] == STAGING
  assert environ["MOBIUS_IDENTITY_ISSUER"] == STAGING


@pytest.mark.parametrize("decision", [
  {},
  {"available": False, "side": "production", "launcher": None},
  {"available": True, "side": "production", "launcher": None},
  {"available": True, "side": "staging", "launcher": ""},
  {"available": True, "side": "staging"},
])
def test_the_server_keeps_its_launcher_without_a_complete_staging_decision(
  launcher_env, decision,
):
  environ = {"MOBIUS_SSO_ISSUER": "https://www.mobius.you"}
  _answer(launcher_env, decision)

  side = launcher_side._follow_broker(environ)

  assert side.side == "production"
  assert side.available is (decision.get("available") is True)
  assert environ == {"MOBIUS_SSO_ISSUER": "https://www.mobius.you"}


def test_the_settings_load_with_the_staging_address_the_broker_chose(launcher_env):
  import os
  from app.config import Settings
  # Recorded first, so the staging address does not outlive this test.
  for name in ("MOBIUS_IDENTITY_ISSUER", "MOBIUS_SSO_ISSUER"):
    launcher_env.setenv(name, "")
  launcher_env.setenv("MOBIUS_SSO_INSTANCE_ID", "mob_example")
  _answer(launcher_env, {"available": True, "side": "staging", "launcher": STAGING})

  launcher_side._follow_broker(os.environ)

  assert Settings().mobius_sso_issuer == STAGING


def test_a_missed_answer_is_asked_again_only_on_a_developer_instance(launcher_env):
  answers = iter([{}, {}, {"available": True, "side": "staging", "launcher": STAGING}])
  launcher_env.setattr(launcher_side, "_broker_decision", lambda: next(answers, {}))

  # An ordinary instance asks once and moves on.
  assert launcher_side._follow_broker({}).side == "production"

  assert launcher_side._follow_broker({"MOBIUS_STAGING_ISSUER": STAGING}).side == "staging"


def test_a_boot_step_with_no_broker_yet_does_not_wait_for_one(launcher_env, tmp_path):
  sleeps = []
  launcher_env.setattr(launcher_side.time, "sleep", sleeps.append)
  launcher_env.setattr(launcher_side, "broker_socket_path", lambda: str(tmp_path / "absent.sock"))
  _answer(launcher_env, {})

  side = launcher_side._follow_broker({"MOBIUS_STAGING_ISSUER": STAGING})

  assert side.side == "production"
  assert sleeps == []


def test_the_staging_credential_never_stays_in_the_server_environment(launcher_env):
  environ = {name: "value" for name in launcher_side.STAGING_ENV}
  _answer(launcher_env, {})

  launcher_side._follow_broker(environ)

  assert environ == {}


def test_the_settings_follow_the_side_the_broker_decided_first(monkeypatch):
  from app import config
  followed = []
  monkeypatch.setattr(launcher_side, "boot_side", lambda: followed.append(1))
  config.get_settings.cache_clear()
  try:
    config.get_settings()
  finally:
    config.get_settings.cache_clear()

  assert followed == [1]


def test_the_store_address_follows_the_side_even_when_it_is_imported_first(monkeypatch):
  # Recorded first, so the staging address does not outlive this test.
  for name in ("MOBIUS_IDENTITY_ISSUER", "MOBIUS_SSO_ISSUER", "MOBIUS_COMMUNITY_REGISTRY_URL"):
    monkeypatch.setenv(name, "")
  monkeypatch.setattr(launcher_side, "_boot_side", None)
  _answer(monkeypatch, {"available": True, "side": "staging", "launcher": STAGING})
  path = Path(__file__).parents[1] / "app" / "community_broker.py"
  spec = importlib.util.spec_from_file_location("community_broker_import_probe", path)
  probe = importlib.util.module_from_spec(spec)
  monkeypatch.setitem(sys.modules, spec.name, probe)

  spec.loader.exec_module(probe)

  assert probe.COMMUNITY_BASE_URL == STAGING


def test_no_reachable_broker_means_no_decision(monkeypatch, tmp_path):
  monkeypatch.setenv("MOBIUS_IDENTITY_BROKER_SOCKET", str(tmp_path / "absent.sock"))

  assert launcher_side._broker_decision() == {}


def test_choosing_production_is_the_absence_of_a_choice(tmp_path):
  launcher_side.choose_side("staging", tmp_path)
  assert (tmp_path / "launcher-side").read_text(encoding="utf-8") == "staging\n"

  launcher_side.choose_side("production", tmp_path)
  assert not (tmp_path / "launcher-side").exists()
  launcher_side.choose_side("production", tmp_path)


def _boot(monkeypatch, *, available: bool, side: str = "production") -> None:
  monkeypatch.setattr(
    launcher_side, "_boot_side",
    launcher_side.LauncherSide(available=available, side=side),
  )


def test_status_reports_no_staging_side_on_an_ordinary_instance(
  client, auth, monkeypatch,
):
  _boot(monkeypatch, available=False)

  response = client.get("/api/platform/launcher-side", headers=auth)

  assert response.json() == {"available": False, "side": "production"}


def test_choosing_a_side_is_refused_without_a_staging_launcher(
  client, auth, monkeypatch, tmp_path,
):
  _boot(monkeypatch, available=False)
  restarts = []
  monkeypatch.setattr("app.routes.platform.restart_this_worker", lambda: restarts.append(1))
  monkeypatch.setattr("app.routes.platform.validate_restart_source", lambda: None)

  response = client.post(
    "/api/platform/launcher-side", json={"side": "staging"}, headers=auth,
  )

  assert response.status_code == 409
  assert restarts == []


def test_choosing_a_side_records_it_and_restarts(client, auth, monkeypatch, tmp_path):
  from app.config import get_settings
  _boot(monkeypatch, available=True)
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  restarts = []
  monkeypatch.setattr("app.routes.platform.restart_this_worker", lambda: restarts.append(1))
  monkeypatch.setattr("app.routes.platform.validate_restart_source", lambda: None)

  response = client.post(
    "/api/platform/launcher-side", json={"side": "staging"}, headers=auth,
  )

  assert response.json() == {"status": "restarting"}
  assert (tmp_path / "launcher-side").read_text(encoding="utf-8") == "staging\n"
  assert restarts == [1]


def test_a_leftover_staging_choice_can_be_cleared_without_staging_variables(
  client, auth, monkeypatch, tmp_path,
):
  from app.config import get_settings
  _boot(monkeypatch, available=False)
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  (tmp_path / "launcher-side").write_text("staging\n", encoding="utf-8")
  monkeypatch.setattr("app.routes.platform.restart_this_worker", lambda: None)
  monkeypatch.setattr("app.routes.platform.validate_restart_source", lambda: None)

  response = client.post(
    "/api/platform/launcher-side", json={"side": "production"}, headers=auth,
  )

  assert response.status_code == 200
  assert not (tmp_path / "launcher-side").exists()


def test_a_refused_restart_leaves_no_choice_behind(client, auth, monkeypatch, tmp_path):
  from app.config import get_settings
  from app.restart_util import RestartSourceInvalid
  _boot(monkeypatch, available=True)
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))

  def refuse():
    raise RestartSourceInvalid("Restart stopped", {"reason": "example"})

  monkeypatch.setattr("app.routes.platform.validate_restart_source", refuse)

  response = client.post(
    "/api/platform/launcher-side", json={"side": "staging"}, headers=auth,
  )

  assert response.status_code == 409
  assert not (tmp_path / "launcher-side").exists()


def test_an_unknown_side_is_rejected(client, auth, monkeypatch):
  _boot(monkeypatch, available=True)

  response = client.post(
    "/api/platform/launcher-side", json={"side": "elsewhere"}, headers=auth,
  )

  assert response.status_code == 422
