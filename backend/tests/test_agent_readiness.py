"""Contract tests for ``GET /api/ready/agent``."""

from __future__ import annotations

from app import agent_readiness as ar
from app import main as main_module
from app import providers as providers_module
from app.agent_readiness import agent_readiness
from app.startup import DatabaseBootResult


def _status(disk="normal", memory="normal"):
  return {
    "pressure": {
      "disk": {"state": disk},
      "memory": {"state": memory},
    },
  }


def _readiness(
  *,
  disk="normal",
  memory="normal",
  writable=None,
  writer=(True, None),
  providers=None,
  provenance="current",
  boot_degraded=None,
):
  if providers is None:
    providers = {"codex": None, "claude": None}
  return agent_readiness(
    "/data",
    boot_degraded=boot_degraded,
    status_reader=lambda _data_dir: _status(disk, memory),
    writer_readiness_reader=lambda: writer,
    provider_report_reader=lambda _data_dir: dict(providers),
    provenance_reader=lambda _data_dir: {"state": provenance},
    writable_probe=lambda _data_dir: writable,
  )


def test_all_green_is_ready():
  assert _readiness() == {
    "ready": True,
    "reason_code": None,
    "reason_codes": [],
    "warning_codes": [],
  }


def test_blocking_conditions_accumulate_in_stable_order():
  result = _readiness(
    disk="critical",
    memory="critical",
    writable="read only",
    writer=(False, "writer is fatal"),
    boot_degraded={"reason": "schema_mismatch"},
  )

  assert result["ready"] is False
  assert result["reason_code"] == ar.CODE_DATABASE_DEGRADED
  assert result["reason_codes"] == [
    ar.CODE_DATABASE_DEGRADED,
    ar.CODE_WRITER_UNAVAILABLE,
    ar.CODE_DATA_DISK_CRITICAL,
    ar.CODE_MEMORY_CRITICAL,
    ar.CODE_DATA_DIR_UNWRITABLE,
  ]


def test_non_database_boot_failure_has_an_honest_code():
  result = _readiness(boot_degraded={"reason": "router_import_failure"})
  assert result["reason_code"] == ar.CODE_BOOT_DEGRADED


def test_database_initialization_failure_has_the_database_code():
  result = _readiness(
    boot_degraded={"reason": "database_initialization_failed"},
  )
  assert result["reason_code"] == ar.CODE_DATABASE_DEGRADED


def test_constrained_disk_blocks_but_unknown_disk_warns():
  constrained = _readiness(disk="constrained")
  unknown = _readiness(disk="unknown")

  assert constrained["reason_code"] == ar.CODE_DATA_DISK_CONSTRAINED
  assert unknown["ready"] is True
  assert unknown["warning_codes"] == [ar.CODE_DATA_DISK_UNKNOWN]


def test_constrained_memory_warns_without_blocking():
  result = _readiness(memory="constrained")
  assert result["ready"] is True
  assert result["warning_codes"] == [ar.CODE_MEMORY_CONSTRAINED]


def test_provider_connection_state_is_advisory():
  none_connected = _readiness(
    providers={"codex": "missing", "claude": "expired"},
  )
  one_connected = _readiness(
    providers={"codex": None, "claude": "expired"},
  )

  assert none_connected["ready"] is True
  assert none_connected["warning_codes"] == [ar.CODE_NO_AUTHENTICATED_PROVIDER]
  assert one_connected["warning_codes"] == [ar.CODE_PROVIDER_UNAVAILABLE]


def test_provider_report_checks_connected_providers_without_leaking_errors(
  monkeypatch,
):
  class _Provider:
    def __init__(self, result=None, error=None):
      self.result = result
      self.error = error

    def check_auth(self, _data_dir):
      if self.error:
        raise self.error
      return self.result

  monkeypatch.setattr(providers_module, "CONNECTED_DEFAULT_ORDER", ("a", "b"))
  monkeypatch.setattr(providers_module, "PROVIDERS", {
    "a": _Provider(),
    "b": _Provider(error=RuntimeError("private detail")),
  })

  report = providers_module.provider_auth_report("/data")

  assert report == {
    "a": None,
    "b": "auth preflight error: RuntimeError",
  }
  assert "private detail" not in repr(report)


def test_provenance_state_is_advisory():
  assert _readiness(provenance="stale")["warning_codes"] == [
    ar.CODE_PROTECTED_RUNTIME_STALE,
  ]
  assert _readiness(provenance="unavailable")["warning_codes"] == [
    ar.CODE_PROTECTED_RUNTIME_UNAVAILABLE,
  ]


def test_public_contract_never_contains_operator_details():
  result = agent_readiness(
    "/data",
    status_reader=lambda _data_dir: _status("critical", "normal"),
    writer_readiness_reader=lambda: (False, "private writer detail"),
    provider_report_reader=lambda _data_dir: {
      "codex": "private credential detail",
    },
    provenance_reader=lambda _data_dir: {
      "state": "stale",
      "mismatched_paths": ["private-runtime-path.py"],
    },
    writable_probe=lambda _data_dir: "private filesystem detail",
  )

  serialized = repr(result)
  assert set(result) == {
    "ready", "reason_code", "reason_codes", "warning_codes",
  }
  assert "private" not in serialized


def test_default_write_canary_roundtrips_and_cleans_up(tmp_path):
  assert ar._default_writable_probe(tmp_path) is None
  assert list((tmp_path / "run").iterdir()) == []


def test_write_canary_reports_cleanup_failure(tmp_path, monkeypatch):
  def fail_unlink(_path):
    raise OSError(30, "Read-only file system")

  monkeypatch.setattr(ar.os, "unlink", fail_unlink)

  assert ar._default_writable_probe(tmp_path) == "OSError: Read-only file system"


def test_short_write_is_not_reported_as_writable(tmp_path, monkeypatch):
  monkeypatch.setattr(ar.os, "write", lambda _fd, _payload: 1)
  assert ar._default_writable_probe(tmp_path) == "short write"


def test_write_canary_is_coalesced_within_short_ttl(tmp_path):
  calls = []
  clock = iter((100.0, 101.0, 116.0))

  def probe(data_dir):
    calls.append(data_dir)
    return None

  ar._WRITABLE_PROBE_CACHE.clear()
  for _ in range(3):
    assert ar._cached_writable_probe(
      tmp_path,
      probe=probe,
      now=lambda: next(clock),
    ) is None
  assert calls == [tmp_path, tmp_path]


def _patch_route_dependencies(monkeypatch, *, disk="normal"):
  monkeypatch.setattr(
    providers_module,
    "provider_auth_report",
    lambda _data_dir: {"claude": None},
  )
  monkeypatch.setattr(
    ar,
    "resource_status",
    lambda _data_dir: _status(disk, "normal"),
  )
  monkeypatch.setattr(ar, "_cached_writable_probe", lambda _data_dir: None)
  monkeypatch.setattr(
    ar,
    "_default_provenance",
    lambda _data_dir: {"state": "current"},
  )


def test_route_ready_200_has_code_only_public_contract(client, monkeypatch):
  _patch_route_dependencies(monkeypatch)
  response = client.get("/api/ready/agent")
  assert response.status_code == 200
  assert response.headers["cache-control"] == "no-store"
  assert response.json() == {
    "ready": True,
    "reason_code": None,
    "reason_codes": [],
    "warning_codes": [],
  }


def test_route_not_ready_503_does_not_change_basic_readiness(client, monkeypatch):
  _patch_route_dependencies(monkeypatch, disk="critical")
  response = client.get("/api/ready/agent")
  assert response.status_code == 503
  assert response.json()["reason_code"] == ar.CODE_DATA_DISK_CRITICAL
  assert client.get("/api/health").status_code == 200
  assert client.get("/api/ready").status_code == 200


def test_route_reports_database_degradation(client, monkeypatch):
  _patch_route_dependencies(monkeypatch)
  main_module._set_database_boot_state(
    DatabaseBootResult(schema_gaps=("apps.paused_capabilities",)),
  )
  try:
    response = client.get("/api/ready/agent")
    assert response.status_code == 503
    assert response.json()["reason_code"] == ar.CODE_DATABASE_DEGRADED
  finally:
    main_module._set_database_boot_state(DatabaseBootResult())


def test_route_reports_router_degradation(client, monkeypatch):
  _patch_route_dependencies(monkeypatch)
  monkeypatch.setattr(
    main_module,
    "router_import_failures",
    lambda: ("app.routes.broken",),
  )

  response = client.get("/api/ready/agent")

  assert response.status_code == 503
  assert response.json()["reason_code"] == ar.CODE_BOOT_DEGRADED
