"""Contract tests for the authenticated curl convenience wrapper."""

import json
import os
import re
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mapi"


def _run_mapi(
  tmp_path: Path,
  *arguments: str,
  stdin_text: str | None = None,
) -> subprocess.CompletedProcess:
  bin_dir = tmp_path / "bin"
  bin_dir.mkdir()
  capture = tmp_path / "curl-arguments"
  stdin_capture = tmp_path / "curl-stdin"
  fake_curl = bin_dir / "curl"
  fake_curl.write_text(
    "#!/usr/bin/env bash\n"
    "printf '%s\\0' \"$@\" > \"$MAPI_CAPTURE\"\n"
    "for argument in \"$@\"; do\n"
    "  if [[ \"$argument\" == '@-' ]]; then\n"
    "    cat > \"$MAPI_STDIN_CAPTURE\"\n"
    "    break\n"
    "  fi\n"
    "done\n",
    encoding="utf-8",
  )
  fake_curl.chmod(0o755)
  env = {
    **os.environ,
    "AGENT_TOKEN": "owner-token",
    "API_BASE_URL": "https://mobius.example/",
    "MAPI_CAPTURE": str(capture),
    "MAPI_STDIN_CAPTURE": str(stdin_capture),
    "PATH": f"{bin_dir}:{os.environ['PATH']}",
  }
  result = subprocess.run(
    [str(SCRIPT), *arguments],
    env=env,
    input=stdin_text,
    capture_output=True,
    text=True,
    check=False,
  )
  result.curl_arguments = (
    capture.read_bytes().split(b"\0")[:-1] if capture.exists() else None
  )
  result.curl_stdin = stdin_capture.read_bytes() if stdin_capture.exists() else None
  return result


def test_mapi_resolves_api_paths_and_adds_json_for_data(tmp_path: Path):
  result = _run_mapi(
    tmp_path,
    "-X", "PATCH", "/api/connect/hosts/h_1", "-d", '{"name":"Desk"}',
  )

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments == [
    b"-sS",
    b"-H", b"Authorization: Bearer owner-token",
    b"-H", b"Content-Type: application/json",
    b"-X", b"PATCH",
    b"https://mobius.example/api/connect/hosts/h_1",
    b"-d", b'{"name":"Desk"}',
  ]


def test_mapi_preserves_an_explicit_content_type(tmp_path: Path):
  result = _run_mapi(
    tmp_path,
    "/api/storage/shared/theme.css",
    "--data-binary", "@theme.css",
    "-H", "Content-Type: text/css",
  )

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments.count(b"Content-Type: text/css") == 1
  assert b"Content-Type: application/json" not in result.curl_arguments


def test_mapi_accepts_a_response_header_capture_path(tmp_path: Path):
  result = _run_mapi(
    tmp_path,
    "-D", "/tmp/response.headers", "-o", "/tmp/response.json", "/api/ready",
  )

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments[-5:] == [
    b"-D", b"/tmp/response.headers",
    b"-o", b"/tmp/response.json",
    b"https://mobius.example/api/ready",
  ]


def test_mapi_streams_literal_json_stdin_without_shell_reencoding(tmp_path: Path):
  payload = '{"text":"$HOME `whoami` \\\"quoted\\\" — line\\nnext"}\n'
  result = _run_mapi(
    tmp_path,
    "-X", "POST", "/api/notifications/send", "--data-binary", "@-",
    stdin_text=payload,
  )

  assert result.returncode == 0, result.stderr
  assert result.curl_stdin == payload.encode("utf-8")
  assert b"Content-Type: application/json" in result.curl_arguments
  assert result.curl_arguments[-2:] == [b"--data-binary", b"@-"]


def test_mapi_streams_raw_css_with_its_explicit_content_type(tmp_path: Path):
  css = ':root { --label: "price: $5"; }\n.note::after { content: "`ok`"; }\n'
  result = _run_mapi(
    tmp_path,
    "-X", "PUT", "/api/storage/shared/theme.css",
    "-H", "Content-Type: text/css", "--data-binary", "@-",
    stdin_text=css,
  )

  assert result.returncode == 0, result.stderr
  assert result.curl_stdin == css.encode("utf-8")
  assert result.curl_arguments.count(b"Content-Type: text/css") == 1
  assert b"Content-Type: application/json" not in result.curl_arguments


def test_mapi_recognizes_joined_data_and_header_options(tmp_path: Path):
  result = _run_mapi(
    tmp_path,
    "/api/chats",
    '--data={"title":"Notes"}',
    "--header=Content-Type: application/merge-patch+json",
  )

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments == [
    b"-sS",
    b"-H", b"Authorization: Bearer owner-token",
    b"https://mobius.example/api/chats",
    b'--data={"title":"Notes"}',
    b"--header=Content-Type: application/merge-patch+json",
  ]


def test_mapi_refuses_external_targets_without_exposing_owner_auth(tmp_path: Path):
  result = _run_mapi(tmp_path, "https://example.net/collect")

  assert result.returncode == 2
  assert result.curl_arguments is None
  assert "refusing to forward owner auth" in result.stderr


def test_mapi_rejects_curl_url_indirection(tmp_path: Path):
  result = _run_mapi(tmp_path, "--url=https://example.net/collect")

  assert result.returncode == 2
  assert result.curl_arguments is None
  assert "refusing to forward owner auth" in result.stderr


def test_mapi_refuses_bare_hosts_even_after_a_valid_api_target(tmp_path: Path):
  result = _run_mapi(tmp_path, "/api/ready", "example.net/collect")

  assert result.returncode == 2
  assert result.curl_arguments is None
  assert "only Möbius /api paths are allowed" in result.stderr


def test_seed_guidance_uses_literal_payload_boundaries():
  seed = SCRIPT.parent / "seed-skills"
  notifications = (seed / "notifications.md").read_text(encoding="utf-8")
  theming = (seed / "theming.md").read_text(encoding="utf-8")

  assert "--data-binary @- <<JSON" in notifications
  assert "--data-binary @- <<'JSON'" in notifications
  assert "real JSON encoder" in notifications
  assert "'\"$CHAT_ID\"'" not in notifications
  examples = re.findall(
    r"<<'?JSON'?\n(.*?)\nJSON", notifications, flags=re.DOTALL,
  )
  assert len(examples) == 3
  for example in examples:
    assert isinstance(json.loads(example.replace("$CHAT_ID", "chat-123")), dict)

  assert "Content-Type: text/css" in theming
  assert "--data-binary @- <<'CSS'" in theming
  assert "quotes, newlines, and $ stay literal" in theming
  assert "{\"content\": \"<css here>\"}" not in theming
