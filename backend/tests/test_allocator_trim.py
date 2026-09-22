from app import allocator


def test_trim_glibc_records_attempt(monkeypatch):
  class FakeTrim:
    argtypes = None
    restype = None

    def __call__(self, _pad):
      return 1

  class FakeLibc:
    malloc_trim = FakeTrim()

  monkeypatch.setattr(allocator.ctypes, "CDLL", lambda _name: FakeLibc())

  assert allocator.trim_glibc() is True
