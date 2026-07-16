"""Allocator tuning stays early, bounded, and portable."""

from app import allocator


class _FakeMallopt:
  def __init__(self, result=1):
    self.calls = []
    self.result = result
    self.argtypes = None
    self.restype = None

  def __call__(self, option, value):
    self.calls.append((option, value))
    return self.result


class _FakeLibc:
  def __init__(self, mallopt):
    self.mallopt = mallopt


def test_limit_glibc_arenas_caps_the_process_before_threads(monkeypatch):
  monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
  mallopt = _FakeMallopt()
  monkeypatch.setattr(
    allocator.ctypes, "CDLL", lambda _name: _FakeLibc(mallopt),
  )

  assert allocator.limit_glibc_arenas(2) is True
  assert mallopt.calls == [(allocator._M_ARENA_MAX, 2)]
  assert mallopt.argtypes == (allocator.ctypes.c_int, allocator.ctypes.c_int)
  assert mallopt.restype is allocator.ctypes.c_int
  assert allocator.allocator_status() == {
    "arena_cap": 2, "applied": True, "source": "mallopt",
  }


def test_limit_glibc_arenas_is_a_noop_without_glibc(monkeypatch):
  monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
  monkeypatch.setattr(allocator.ctypes, "CDLL", lambda _name: object())

  assert allocator.limit_glibc_arenas() is False
  assert allocator.allocator_status()["source"] == "unsupported"


def test_limit_glibc_arenas_rejects_invalid_caps(monkeypatch):
  monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
  def unexpected_load(_name):
    raise AssertionError("libc must not be loaded for an invalid cap")

  monkeypatch.setattr(allocator.ctypes, "CDLL", unexpected_load)
  assert allocator.limit_glibc_arenas(0) is False
  assert allocator.allocator_status()["source"] == "invalid"
  assert allocator.limit_glibc_arenas(True) is False


def test_limit_glibc_arenas_preserves_operator_setting(monkeypatch):
  monkeypatch.setenv("MALLOC_ARENA_MAX", "4")

  def unexpected_load(_name):
    raise AssertionError("an operator setting must remain authoritative")

  monkeypatch.setattr(allocator.ctypes, "CDLL", unexpected_load)
  assert allocator.limit_glibc_arenas(2) is False
  assert allocator.allocator_status() == {
    "arena_cap": 4, "applied": True, "source": "environment",
  }


def test_invalid_operator_setting_is_observable_not_claimed_as_applied(monkeypatch):
  monkeypatch.setenv("MALLOC_ARENA_MAX", "not-a-number")

  assert allocator.limit_glibc_arenas(2) is False
  assert allocator.allocator_status() == {
    "arena_cap": None,
    "applied": False,
    "source": "environment_invalid",
  }
