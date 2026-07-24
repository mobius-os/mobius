import pytest
from pydantic import ValidationError

from app.config import Settings


SECRET_KEY = "test-secret-key-that-is-at-least-thirty-two-characters"
CLIENT_SECRET = "managed-client-secret-that-is-at-least-thirty-two-characters"


def settings(**values):
  return Settings(secret_key=SECRET_KEY, _env_file=None, **values)


def test_managed_sign_in_is_disabled_for_ordinary_self_hosting():
  config = settings()

  assert config.mobius_sso_enabled is False


def test_managed_sign_in_requires_complete_configuration():
  with pytest.raises(ValidationError, match="must be configured together"):
    settings(
      mobius_sso_issuer="https://www.mobius.you",
      mobius_sso_instance_id="mob_example",
    )


def test_managed_sign_in_accepts_launcher_origin_and_normalizes_it():
  config = settings(
    mobius_sso_issuer="https://www.mobius.you/",
    mobius_sso_instance_id="mob_example",
    mobius_sso_client_secret=CLIENT_SECRET,
  )

  assert config.mobius_sso_enabled is True
  assert config.mobius_sso_issuer == "https://www.mobius.you"


@pytest.mark.parametrize(
  ("issuer", "message"),
  [
    ("http://mobius.you", "must be an HTTPS origin"),
    ("https://mobius.you/path", "must be an HTTPS origin"),
  ],
)
def test_managed_sign_in_rejects_unsafe_issuer(issuer, message):
  with pytest.raises(ValidationError, match=message):
    settings(
      mobius_sso_issuer=issuer,
      mobius_sso_instance_id="mob_example",
      mobius_sso_client_secret=CLIENT_SECRET,
    )


def test_managed_sign_in_rejects_short_instance_secret():
  with pytest.raises(ValidationError, match="must be at least 32 characters"):
    settings(
      mobius_sso_issuer="https://www.mobius.you",
      mobius_sso_instance_id="mob_example",
      mobius_sso_client_secret="too-short",
    )
