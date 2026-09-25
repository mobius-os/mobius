"""Contract tests for the authenticated curl convenience wrapper."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest


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
    b"-q",
    b"--globoff",
    b"-sS",
    b"-H", b"Authorization: Bearer owner-token",
    b"-H", b"Content-Type: application/json",
    b"-X", b"PATCH",
    b"https://mobius.example/api/connect/hosts/h_1",
    b"-d", b'{"name":"Desk"}',
  ]


def test_mapi_disables_curl_url_globbing_after_validating_one_api_target(
  tmp_path: Path,
):
  result = _run_mapi(tmp_path, "/api/{../outside,ready}")

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments[:2] == [b"-q", b"--globoff"]
  assert result.curl_arguments[-1] == b"https://mobius.example/api/{../outside,ready}"


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


def test_mapi_accepts_combined_flags_before_a_header_capture_path(tmp_path: Path):
  result = _run_mapi(
    tmp_path,
    "-sD", "/tmp/response.headers", "-o", "/tmp/response.json", "/api/ready",
  )

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments[-5:] == [
    b"-sD", b"/tmp/response.headers",
    b"-o", b"/tmp/response.json",
    b"https://mobius.example/api/ready",
  ]


def test_mapi_accepts_an_attached_value_in_combined_short_options(tmp_path: Path):
  result = _run_mapi(
    tmp_path,
    "-sD/tmp/response.headers", "-o", "/tmp/response.json", "/api/ready",
  )

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments[-4:] == [
    b"-sD/tmp/response.headers",
    b"-o", b"/tmp/response.json",
    b"https://mobius.example/api/ready",
  ]


def test_mapi_combined_data_flag_still_adds_json_content_type(tmp_path: Path):
  result = _run_mapi(tmp_path, "-sd", '{"ok":true}', "/api/ready")

  assert result.returncode == 0, result.stderr
  assert b"Content-Type: application/json" in result.curl_arguments
  assert result.curl_arguments[-3:] == [
    b"-sd", b'{"ok":true}', b"https://mobius.example/api/ready",
  ]


def test_mapi_allows_url_text_as_a_data_value_without_treating_it_as_a_target(
  tmp_path: Path,
):
  result = _run_mapi(tmp_path, "-sd", "https://example.net/value", "/api/ready")

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments[-3:] == [
    b"-sd", b"https://example.net/value", b"https://mobius.example/api/ready",
  ]


@pytest.mark.parametrize("option", ["--data-ascii", "--json"])
def test_mapi_recognizes_other_curl_body_options(
  tmp_path: Path,
  option: str,
):
  result = _run_mapi(tmp_path, option, '{"ok":true}', "/api/ready")

  assert result.returncode == 0, result.stderr
  assert b"Content-Type: application/json" in result.curl_arguments
  assert result.curl_arguments[-3:] == [
    option.encode(), b'{"ok":true}', b"https://mobius.example/api/ready",
  ]


def test_mapi_combined_header_flag_preserves_explicit_content_type(tmp_path: Path):
  result = _run_mapi(
    tmp_path,
    "-sH", "Content-Type: text/css", "-d", "body", "/api/ready",
  )

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments.count(b"Content-Type: text/css") == 1
  assert b"Content-Type: application/json" not in result.curl_arguments


def test_mapi_rejects_unknown_short_flag_before_curl_can_reparse_a_url(
  tmp_path: Path,
):
  # curl reads `-AH` as `--user-agent H`, leaving the following value as a
  # second target.  Treating its H as `--header` instead would consume that
  # target during validation and then leak the injected owner credential.
  result = _run_mapi(
    tmp_path, "/api/ready", "-AH", "https://example.net/collect",
  )

  assert result.returncode == 2
  assert result.curl_arguments is None
  assert "unsupported curl option '-AH'" in result.stderr


def test_mapi_preserves_an_ordinary_custom_header(tmp_path: Path):
  result = _run_mapi(
    tmp_path,
    "-H", "x-mobius-version: 1", "/api/ready",
  )

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments[-3:] == [
    b"-H", b"x-mobius-version: 1", b"https://mobius.example/api/ready",
  ]


@pytest.mark.parametrize("option", ["--fail", "-sS"])
def test_mapi_preserves_boolean_curl_options(tmp_path: Path, option: str):
  result = _run_mapi(tmp_path, option, "/api/ready")

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments[-2:] == [
    option.encode(), b"https://mobius.example/api/ready",
  ]


@pytest.mark.parametrize("option", ["-G", "--get"])
def test_mapi_supports_query_data_without_adding_a_body_content_type(
  tmp_path: Path,
  option: str,
):
  result = _run_mapi(
    tmp_path,
    option, "/api/store/apps", "--data-urlencode", "managed=true",
  )

  assert result.returncode == 0, result.stderr
  assert b"Content-Type: application/json" not in result.curl_arguments
  assert result.curl_arguments[-4:] == [
    option.encode(), b"https://mobius.example/api/store/apps",
    b"--data-urlencode", b"managed=true",
  ]


def test_mapi_supports_get_in_combined_short_options(tmp_path: Path):
  result = _run_mapi(
    tmp_path,
    "-sG", "/api/store/apps", "--data-urlencode=managed=true",
  )

  assert result.returncode == 0, result.stderr
  assert b"Content-Type: application/json" not in result.curl_arguments
  assert result.curl_arguments[-4:] == [
    b"-sG", b"https://mobius.example/api/store/apps",
    b"--data-urlencode", b"managed=true",
  ]


@pytest.mark.parametrize(
  ("arguments", "forwarded"),
  [
    (("--url-query", "managed=true"), ("--url-query", "managed=true")),
    (("--url-query=managed=true",), ("--url-query", "managed=true")),
  ],
)
def test_mapi_supports_explicit_url_query_data_without_a_body_header(
  tmp_path: Path,
  arguments: tuple[str, ...],
  forwarded: tuple[str, ...],
):
  result = _run_mapi(tmp_path, "/api/store/apps", *arguments)

  assert result.returncode == 0, result.stderr
  assert b"Content-Type: application/json" not in result.curl_arguments
  assert result.curl_arguments[-(len(forwarded) + 1):] == [
    b"https://mobius.example/api/store/apps",
    *(argument.encode() for argument in forwarded),
  ]


@pytest.mark.parametrize(
  "option",
  ["-H", "-sH", "--output", "--url-query"],
)
def test_mapi_reports_a_missing_option_value_before_running_curl(
  tmp_path: Path,
  option: str,
):
  result = _run_mapi(tmp_path, "/api/ready", option)

  assert result.returncode == 2
  assert result.curl_arguments is None
  assert f"curl option '{option}' needs a value" in result.stderr


def test_mapi_rejects_long_options_that_can_reverse_its_safe_defaults(
  tmp_path: Path,
):
  result = _run_mapi(
    tmp_path, "--no-globoff", "/api/{../outside,ready}",
  )

  assert result.returncode == 2
  assert result.curl_arguments is None
  assert "unsupported curl" in result.stderr


def test_mapi_supports_safe_short_aliases_already_allowed_by_long_name(
  tmp_path: Path,
):
  result = _run_mapi(
    tmp_path,
    "-sm", "3", "-sE", "/tmp/client.pem", "/api/ready",
  )

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments[-5:] == [
    b"-sm", b"3", b"-sE", b"/tmp/client.pem",
    b"https://mobius.example/api/ready",
  ]


@pytest.mark.parametrize(
  "arguments",
  [
    ("-sK/tmp/curl.conf", "/api/ready"),
    ("--config=/tmp/curl.conf", "/api/ready"),
    ("-sL", "/api/ready"),
    ("--location-trusted", "/api/ready"),
    ("--connect-to=mobius.example:443:example.net:443", "/api/ready"),
    ("--resolve=mobius.example:443:192.0.2.1", "/api/ready"),
    ("--alt-svc=/tmp/alt-svc.cache", "/api/ready"),
    ("-sxhttp://example.net:8080", "/api/ready"),
    ("--unix-socket=/tmp/other.sock", "/api/ready"),
    ("--request-target=/not-api", "/api/ready"),
  ],
)
def test_mapi_rejects_options_that_can_change_the_authenticated_target(
  tmp_path: Path,
  arguments: tuple[str, ...],
):
  result = _run_mapi(tmp_path, *arguments)

  assert result.returncode == 2
  assert result.curl_arguments is None
  assert "can change the authenticated request target" in result.stderr


@pytest.mark.parametrize(
  "header",
  [
    "Host: example.net",
    "Host;",
    ":authority: example.net",
    ":authority;example.net",
    "X-Safe: yes\r\nHost: example.net",
    "@/tmp/headers",
  ],
)
def test_mapi_rejects_uninspectable_or_host_replacing_headers(
  tmp_path: Path,
  header: str,
):
  result = _run_mapi(tmp_path, "-sH", header, "/api/ready")

  assert result.returncode == 2
  assert result.curl_arguments is None
  assert "Host-replacing header" in result.stderr


@pytest.mark.parametrize(
  "method",
  ["GET /outside HTTP/1.1\r\nX-Ignore:", "GET POST", "GET\nDELETE"],
)
def test_mapi_rejects_request_line_injection(tmp_path: Path, method: str):
  result = _run_mapi(tmp_path, "-X", method, "/api/ready")

  assert result.returncode == 2
  assert result.curl_arguments is None
  assert "request method" in result.stderr


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


def test_mapi_normalizes_joined_long_options_for_older_curl_versions(
  tmp_path: Path,
):
  result = _run_mapi(
    tmp_path,
    "/api/chats",
    '--data={"title":"Notes"}',
    "--header=Content-Type: application/merge-patch+json",
  )

  assert result.returncode == 0, result.stderr
  assert result.curl_arguments == [
    b"-q",
    b"--globoff",
    b"-sS",
    b"-H", b"Authorization: Bearer owner-token",
    b"https://mobius.example/api/chats",
    b"--data", b'{"title":"Notes"}',
    b"--header", b"Content-Type: application/merge-patch+json",
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


@pytest.mark.parametrize(
  "target",
  [
    "/api/../admin",
    "/api/%2e%2e/admin",
    "/api/%2f../admin",
    "/api/%5c../admin",
    "/api/\\../admin",
  ],
)
def test_mapi_rejects_api_paths_that_escape_the_api_prefix(
  tmp_path: Path,
  target: str,
):
  result = _run_mapi(tmp_path, target)

  assert result.returncode == 2
  assert result.curl_arguments is None
  assert "API target" in result.stderr or "within /api" in result.stderr


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


def test_mapi_guidance_describes_the_safe_curl_subset():
  """The constitution owns the mapi contract; no skill may restate a stale one."""
  platform = SCRIPT.parents[2]
  core = (platform / "skill" / "core.md").read_text(encoding="utf-8")
  assert "Supported safe curl options pass through" in core
  for path in [platform / "skill" / "core.md", *(SCRIPT.parent / "seed-skills").glob("*.md")]:
    text = path.read_text(encoding="utf-8")
    assert "Everything else passes straight through to curl" not in text, path.name
