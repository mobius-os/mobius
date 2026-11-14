"""Shared check that the test app serves the SPA shell."""


def assert_shell_served(client, path="/"):
  """Conftest provisions the shell; a missing route is a regression, not a skip."""
  r = client.get(path)
  assert r.status_code == 200, r.text
  assert "text/html" in r.headers.get("content-type", ""), r.headers
