"""Tests for the shared codex login-banner parsers.

Pins the two real banner shapes the device-auth flow sees so the normal
(`routes/auth.py`) and recovery (`recover_oauth.py`) callers can't drift to
different expectations.
"""

from app.codex_login_parse import banner_has_code, parse_login_banner


def test_readiness_predicate_needs_both_word_and_code():
  # Word "code" present but no code-shaped token yet → keep reading.
  assert banner_has_code("Enter the code shown in your browser") is False
  # Code-shaped token but the banner hasn't said "code" yet → keep reading.
  assert banner_has_code("Go to https://example.com  ABCD-1234") is False
  # Both present → ready.
  assert banner_has_code("Your code is ABCD-1234") is True


def test_parses_ansi_wrapped_url_and_code():
  # The CLI underlines the URL with ANSI escapes (\x1b[4m … \x1b[0m).
  banner = (
    "Sign in to Codex\n"
    "Open \x1b[4mhttps://auth.openai.com/device\x1b[0m and enter\n"
    "code: \x1b[1mWXYZ-5678\x1b[0m\n"
  )
  parsed = parse_login_banner(banner)
  assert parsed == {"url": "https://auth.openai.com/device", "code": "WXYZ-5678"}


def test_trims_trailing_sentence_punctuation_from_url():
  banner = "Visit https://auth.openai.com/device. Your code is ABCD-1234."
  parsed = parse_login_banner(banner)
  assert parsed["url"] == "https://auth.openai.com/device"
  assert parsed["code"] == "ABCD-1234"


def test_returns_none_when_url_or_code_missing():
  assert parse_login_banner("no url here, code ABCD-1234") is None
  assert parse_login_banner("https://example.com but no device code") is None
  assert parse_login_banner("") is None
