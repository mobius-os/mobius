"""Parallel commands and live output for current Connect runners.

A current runner runs every command independently — its own id, time limit,
output, and cancellation — and nothing waits in a queue. Output streams to
Möbius in numbered chunks while the command runs; the final result still
carries each stream's head and tail for callers that never read live output.
"""

import asyncio
import io
import sys
import time
import urllib.error

import pytest

from app import connect_runner
from app.routes import connect as connect_routes


@pytest.fixture(autouse=True)
def _clear_connect_state():
  connect_routes._channels.clear()
  connect_routes._commands.clear()
  connect_routes._host_id_by_token_hash.clear()
  connect_routes._finished_output.clear()
  connect_routes._pair_limiter.reset()
  yield
  connect_routes._channels.clear()
  connect_routes._commands.clear()
  connect_routes._host_id_by_token_hash.clear()
  connect_routes._finished_output.clear()
  connect_routes._pair_limiter.reset()


def _paired_host(
  client, auth, *, capabilities=connect_runner.RUNNER_CAPABILITIES,
):
  created = client.post("/api/connect/hosts", headers=auth, json={"name": "Box"})
  pairing = created.json()
  paired = client.post("/api/connect/pair", json={"code": pairing["pairing_code"]})
  assert paired.status_code == 200, paired.text
  host = connect_routes._load_host(pairing["id"])
  host["runner_protocol"] = connect_runner.RUNNER_PROTOCOL_VERSION
  host["runner_release"] = connect_runner.RUNNER_RELEASE
  host["runner_capabilities"] = list(capabilities)
  host["runner_transport"] = "sse"
  connect_routes._save_host(host)
  channel = connect_routes._Channel()
  connect_routes._channels[pairing["id"]] = channel
  return pairing["id"], channel


async def _start_streaming(host_id, channel, request_id, cmd, **extra):
  caller = asyncio.create_task(connect_routes.exec_on_host(
    host_id,
    connect_routes.ExecBody(cmd=cmd, request_id=request_id, stream=True, **extra),
    _owner=object(),
  ))
  event = await asyncio.wait_for(channel.queue.get(), timeout=1)
  assert event["request_id"] == request_id
  connect_routes._mark_command_started(host_id, request_id)
  return await asyncio.wait_for(caller, timeout=1), event


def _chunks(*items):
  return [
    connect_routes.OutputChunk(seq=seq, stream=stream, text=text)
    for seq, stream, text in items
  ]


def _post_output(host_id, request_id, chunks):
  command = connect_routes._find_command(host_id, request_id)
  command.output.append([chunk.model_dump() for chunk in chunks])


@pytest.mark.asyncio
async def test_current_runner_starts_a_second_command_while_the_first_runs(
  client, auth,
):
  host_id, channel = _paired_host(client, auth)

  first, _ = await _start_streaming(host_id, channel, "1" * 16, "docker build .")
  second, _ = await _start_streaming(
    host_id, channel, "2" * 16, "tail -n 20 build.log",
  )

  assert first == {"request_id": "1" * 16, "state": "running"}
  assert second == {"request_id": "2" * 16, "state": "running"}
  public = connect_routes._public_host(connect_routes._load_host(host_id))
  assert public["parallel_commands"] is True
  assert [item["id"] for item in public["active_commands"]] == ["1" * 16, "2" * 16]
  assert [item["label"] for item in public["active_commands"]] == [
    "docker build .", "tail -n 20 build.log",
  ]
  # Durable state keeps both commands but never their text once started.
  persisted = connect_routes._load_host(host_id)["active_commands"]
  assert {record["id"] for record in persisted} == {"1" * 16, "2" * 16}
  assert all("cmd" not in record for record in persisted)

  # Stopping one exact command leaves the other untouched.
  await connect_routes.cancel_host_command(host_id, "1" * 16, _owner=object())
  assert await channel.queue.get() == {"type": "cancel", "request_id": "1" * 16}
  assert connect_routes._find_command(host_id, "2" * 16).state == "running"


@pytest.mark.asyncio
async def test_single_flight_runner_still_refuses_parallel_work_as_busy(
  client, auth,
):
  # Another copy of the runner may share a release number without the
  # ability; only the announced capability enables parallel work.
  host_id, channel = _paired_host(client, auth, capabilities=())
  await _start_streaming(host_id, channel, "1" * 16, "first")

  with pytest.raises(connect_routes.HTTPException) as refused:
    await connect_routes.exec_on_host(
      host_id,
      connect_routes.ExecBody(cmd="second", request_id="2" * 16, stream=True),
      _owner=object(),
    )
  assert refused.value.status_code == 409
  assert "one command at a time" in refused.value.detail
  assert channel.queue.empty()


def test_exec_time_limit_has_no_upper_ceiling():
  body = connect_routes.ExecBody(cmd="make release", timeout=6 * 3600)
  assert body.timeout == 6 * 3600
  with pytest.raises(ValueError):
    connect_routes.ExecBody(cmd="make release", timeout=0)


@pytest.mark.asyncio
async def test_output_long_poll_delivers_chunks_then_the_final_result(
  client, auth,
):
  host_id, channel = _paired_host(client, auth)
  request_id = "3" * 16
  await _start_streaming(host_id, channel, request_id, "make")

  empty = await connect_routes.read_command_output(
    host_id, request_id, after=0, wait=0, _owner=object(),
  )
  assert empty["chunks"] == [] and empty["state"] == "running"

  waiting = asyncio.create_task(connect_routes.read_command_output(
    host_id, request_id, after=0, wait=5, _owner=object(),
  ))
  await asyncio.sleep(0.02)
  assert not waiting.done()
  _post_output(host_id, request_id, _chunks((0, "stdout", "compiling\n")))
  woken = await asyncio.wait_for(waiting, timeout=1)
  assert [chunk["text"] for chunk in woken["chunks"]] == ["compiling\n"]
  assert woken["next"] == 1

  # A retried delivery of an already-received chunk is ignored.
  _post_output(host_id, request_id, _chunks(
    (0, "stdout", "compiling\n"), (1, "stderr", "warning: slow\n"),
  ))
  later = await connect_routes.read_command_output(
    host_id, request_id, after=1, wait=0, _owner=object(),
  )
  assert [(c["stream"], c["text"]) for c in later["chunks"]] == [
    ("stderr", "warning: slow\n"),
  ]

  connect_routes._runner_result(host_id, connect_routes.ResultBody(
    request_id=request_id, stdout="compiling\n", stderr="warning: slow\n",
    exit_code=0, outcome="completed", output_seq=2,
  ))
  finished = await connect_routes.read_command_output(
    host_id, request_id, after=2, wait=5, _owner=object(),
  )
  assert finished["state"] == "finished"
  assert finished["chunks"] == []
  assert finished["output_seq"] == 2
  assert finished["result"]["exit_code"] == 0

  # A late reader attaching from the start still sees everything.
  replay = await connect_routes.read_command_output(
    host_id, request_id, after=0, wait=0, _owner=object(),
  )
  assert "".join(c["text"] for c in replay["chunks"]) == (
    "compiling\nwarning: slow\n"
  )
  listed = await connect_routes.list_host_commands(host_id, _owner=object())
  assert listed["running"] == []
  assert listed["recent"][0]["id"] == request_id


@pytest.mark.asyncio
async def test_missing_output_stays_visible_as_a_sequence_jump(client, auth):
  host_id, channel = _paired_host(client, auth)
  request_id = "4" * 16
  await _start_streaming(host_id, channel, request_id, "yes")

  # The runner dropped chunks 0-4 while Möbius was unreachable.
  _post_output(host_id, request_id, _chunks((5, "stdout", "resumed\n")))
  view = await connect_routes.read_command_output(
    host_id, request_id, after=0, wait=0, _owner=object(),
  )
  # The reader asked for 0 and received 5: it can tell output was lost.
  assert [chunk["seq"] for chunk in view["chunks"]] == [5]
  assert view["next"] == 6


def test_live_output_log_is_memory_bounded_but_keeps_the_newest(monkeypatch):
  monkeypatch.setattr(connect_routes, "_MAX_LIVE_OUTPUT_CHARS", 10)
  log = connect_routes._OutputLog()
  log.append([
    {"seq": 0, "stream": "stdout", "text": "aaaaaa"},
    {"seq": 1, "stream": "stdout", "text": "bbbbbb"},
  ])
  view = log.read(0)
  assert [(chunk["seq"], chunk["text"]) for chunk in view["chunks"]] == [
    (1, "bbbbbb"),
  ]


@pytest.mark.asyncio
async def test_streaming_retry_after_completion_points_at_the_result(
  client, auth,
):
  host_id, channel = _paired_host(client, auth)
  request_id = "5" * 16
  await _start_streaming(host_id, channel, request_id, "true")
  connect_routes._finish_command(host_id, request_id, {
    "stdout": "", "stderr": "", "exit_code": 0, "outcome": "completed",
  })

  retry = await connect_routes.exec_on_host(
    host_id,
    connect_routes.ExecBody(cmd="true", request_id=request_id, stream=True),
    _owner=object(),
  )
  assert retry == {"request_id": request_id, "state": "finished"}
  assert channel.queue.empty()


@pytest.mark.asyncio
async def test_reconnect_reconciles_every_command_the_runner_reports(
  client, auth,
):
  host_id, channel = _paired_host(client, auth)
  await _start_streaming(host_id, channel, "6" * 16, "still running")
  await _start_streaming(host_id, channel, "7" * 16, "runner lost this")

  replacement = connect_routes._Channel()
  connect_routes._replace_channel(host_id, replacement)
  await connect_routes._reconcile_runner(host_id, replacement, {
    "active_request_ids": ["6" * 16, "8" * 16],
    "pending_result_ids": [],
  })

  assert connect_routes._find_command(host_id, "6" * 16).state == "running"
  assert connect_routes._find_command(host_id, "7" * 16) is None
  recent = connect_routes._load_host(host_id)["recent_commands"]
  assert recent["7" * 16]["result"]["outcome"] == "lost"
  # Work Möbius no longer tracks has no caller, so the runner is told to stop.
  assert await replacement.queue.get() == {"type": "cancel", "request_id": "8" * 16}


@pytest.mark.asyncio
async def test_single_legacy_active_command_record_is_restored(client, auth):
  host_id, _channel = _paired_host(client, auth)
  host = connect_routes._load_host(host_id)
  host.pop("active_commands", None)
  host["active_command"] = {
    "id": "9" * 16, "timeout": 60, "created_at": time.time(),
    "started_at": time.time(), "state": "running", "fingerprint": "x",
  }
  connect_routes._save_host(host)

  assert list(connect_routes._host_commands(host_id)) == ["9" * 16]
  connect_routes._persist_commands(host_id)
  saved = connect_routes._load_host(host_id)
  assert "active_command" not in saved
  assert [record["id"] for record in saved["active_commands"]] == ["9" * 16]


def test_command_label_is_the_first_line_and_never_persists():
  script = "\nset -euo pipefail\ncd /srv/app\ndocker compose up -d\n"
  assert connect_routes._command_label(None, script) == "set -euo pipefail …"
  assert connect_routes._command_label("make test", None) == "make test"
  assert connect_routes._command_label("x" * 500, None).endswith("…")


# --------------------------------------------------------------------------- #
# Runner side
# --------------------------------------------------------------------------- #
def test_runner_final_view_matches_the_capped_full_text():
  text = "".join(f"line {index}\n" for index in range(40_000))
  tail = connect_runner._HeadTail()
  for start in range(0, len(text), 7_777):
    tail.append(text[start:start + 7_777])
  assert tail.value() == connect_runner._cap_output(text)

  small = connect_runner._HeadTail()
  small.append("ok\n")
  assert small.value() == ("ok\n", False)


def test_runner_retries_undelivered_chunks_with_the_same_sequence():
  output = connect_runner._CommandOutput()
  output.append("stdout", "one ")
  output.append("stdout", "two ")
  first = output.take_batch()
  assert [(c["seq"], c["text"]) for c in first] == [(0, "one "), (1, "two ")]
  output.append("stderr", "three")
  # The first delivery failed; a retry resends the same numbers plus new text.
  retry = output.take_batch()
  assert [c["seq"] for c in retry] == [0, 1, 2]
  output.acknowledge(2)
  assert output.take_batch() == []


def test_runner_drops_oldest_undelivered_output_at_its_memory_bound(monkeypatch):
  monkeypatch.setattr(connect_runner, "_MAX_PENDING_OUTPUT_CHARS", 10)
  output = connect_runner._CommandOutput()
  for text in ("aaaa", "bbbb", "cccc"):
    output.append("stdout", text)
  batch = output.take_batch()
  assert [(c["seq"], c["text"]) for c in batch] == [(1, "bbbb"), (2, "cccc")]
  # The capped final view is independent of live delivery.
  assert output.final_streams() == ("aaaabbbbcccc", "", False)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell contract")
def test_runner_streams_output_before_reporting_the_result(monkeypatch):
  posts = []

  def post(url, payload, token=None, timeout=30):
    posts.append((url.rsplit("/", 1)[-1], payload))
    return {"ok": True}

  monkeypatch.setattr(connect_runner, "_post", post)
  runner = connect_runner._CommandRunner("https://mobius.test", "token")
  runner.live_output = True  # the server's hello
  request_id = "a" * 16
  runner.start({
    "request_id": request_id,
    "cmd": "printf 'first\\n'; sleep 1.2; printf 'oops\\n' >&2; printf 'last\\n'",
    "timeout": 30,
    "not_after": time.time() + 5,
  })

  deadline = time.monotonic() + 5
  while time.monotonic() < deadline and not any(
    kind == "result" for kind, _payload in posts
  ):
    time.sleep(0.05)

  kinds = [kind for kind, _payload in posts]
  assert kinds[0] == "state"
  assert kinds[-1] == "result"
  output_posts = [payload for kind, payload in posts if kind == "output"]
  # "first" arrived while the command was still sleeping.
  assert len(output_posts) >= 2
  assert output_posts[0]["chunks"][0]["text"] == "first\n"
  chunks = [chunk for payload in output_posts for chunk in payload["chunks"]]
  assert [chunk["seq"] for chunk in chunks] == list(range(len(chunks)))
  assert "".join(c["text"] for c in chunks if c["stream"] == "stdout") == (
    "first\nlast\n"
  )
  assert "".join(c["text"] for c in chunks if c["stream"] == "stderr") == "oops\n"
  result = posts[-1][1]
  assert result["stdout"] == "first\nlast\n"
  assert result["stderr"] == "oops\n"
  assert result["output_seq"] == len(chunks)
  assert runner.active == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell contract")
def test_runner_sends_no_live_output_to_a_server_that_did_not_offer_it(
  monkeypatch,
):
  posts = []

  def post(url, payload, token=None, timeout=30):
    posts.append((url.rsplit("/", 1)[-1], payload))
    return {"ok": True}

  monkeypatch.setattr(connect_runner, "_post", post)
  runner = connect_runner._CommandRunner("https://older.test", "token")
  runner.start({
    "request_id": "b" * 16,
    "cmd": "printf 'one\\n'; sleep 0.7; printf 'two\\n'",
    "timeout": 30,
    "not_after": time.time() + 5,
  })
  deadline = time.monotonic() + 5
  while time.monotonic() < deadline and not any(k == "result" for k, _ in posts):
    time.sleep(0.05)

  assert [kind for kind, _ in posts] == ["state", "result"]
  assert posts[-1][1]["stdout"] == "one\ntwo\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process contract")
def test_heavy_output_cannot_starve_the_time_limit(monkeypatch):
  results = []

  def slow_post(url, payload, token=None, timeout=30):
    if url.endswith("/output"):
      time.sleep(0.05)
    if url.endswith("/result"):
      results.append(payload)
    return {"ok": True}

  monkeypatch.setattr(connect_runner, "_post", slow_post)
  runner = connect_runner._CommandRunner("https://mobius.test", "token")
  runner.live_output = True
  runner.start({
    "request_id": "c" * 16,
    "cmd": "yes",
    "timeout": 1,
    "not_after": time.time() + 5,
  })
  deadline = time.monotonic() + 15
  while time.monotonic() < deadline and not results:
    time.sleep(0.1)

  # Output arrives faster than it uploads, yet the limit still ends it.
  assert results and results[0]["outcome"] == "timed_out"
  assert results[0]["exit_code"] == 124
  assert runner.active == {}


def test_hello_enables_live_output_and_survives_reconnects(monkeypatch):
  events = iter([
    [b'data: {"type":"hello","live_output":true}\n\n', b": ping\n\n"],
    [b'data: {"type":"disconnect","request_id":"z"}\n\n'],
  ])
  seen = []

  class Stream:
    def __init__(self):
      self.lines = next(events)

    def __enter__(self):
      return self

    def __exit__(self, *_args):
      return False

    def __iter__(self):
      for line in self.lines:
        seen.append(runners[0].live_output)
        yield line

  runners = []
  original = connect_runner._CommandRunner

  def make_runner(*args, **kwargs):
    runners.append(original(*args, **kwargs))
    return runners[-1]

  monkeypatch.setattr(connect_runner, "_CommandRunner", make_runner)
  monkeypatch.setattr(connect_runner, "_open_url", lambda request, **kw: Stream())
  monkeypatch.setattr(connect_runner, "_remove_connection", lambda url, host_id: 1)
  monkeypatch.setattr(connect_runner, "_post", lambda *args, **kwargs: {"ok": True})
  monkeypatch.setattr(connect_runner.time, "sleep", lambda _s: None)

  connect_runner._serve_connection(
    {"url": "https://mobius.test", "host_id": "h_u", "token": "t"},
  )
  # Off until the server's hello, then kept across the reconnect so output
  # buffered while disconnected is still delivered.
  assert seen == [False, True, True]


def test_time_limit_accepts_long_work_but_rejects_absurd_values():
  assert connect_routes.ExecBody(cmd="make", timeout=30 * 24 * 3600).timeout
  with pytest.raises(ValueError):
    connect_routes.ExecBody(cmd="make", timeout=10 ** 400)


@pytest.mark.asyncio
async def test_older_app_clients_still_see_and_stop_one_command(client, auth):
  host_id, channel = _paired_host(client, auth)
  await _start_streaming(host_id, channel, "d" * 16, "first")
  await _start_streaming(host_id, channel, "e" * 16, "second")

  public = connect_routes._public_host(connect_routes._load_host(host_id))
  assert public["active_command"]["id"] == "d" * 16
  assert len(public["active_commands"]) == 2


def test_result_finished_before_upgrade_still_answers_a_retry(client, auth):
  host_id, _channel = _paired_host(client, auth)
  host = connect_routes._load_host(host_id)
  host.pop("recent_commands", None)
  host["last_command"] = {
    "id": "f" * 16, "fingerprint": "abc", "finished_at": time.time(),
    "result": {"request_id": "f" * 16, "exit_code": 0},
  }
  connect_routes._save_host(host)

  connect_routes._prune_recent_commands(connect_routes._load_host(host_id))

  saved = connect_routes._load_host(host_id)
  assert "last_command" not in saved
  assert saved["recent_commands"]["f" * 16]["fingerprint"] == "abc"


def test_cached_runner_identity_still_honours_revocation(client, auth):
  from starlette.requests import Request
  created = client.post("/api/connect/hosts", headers=auth, json={"name": "Box"})
  code = created.json()["pairing_code"]
  token = client.post("/api/connect/pair", json={"code": code}).json()["token"]

  def request():
    return Request({
      "type": "http", "method": "POST", "path": "/api/connect/output",
      "headers": [(b"authorization", f"Bearer {token}".encode())],
    })

  host = connect_routes._auth_host(request())
  assert connect_routes._auth_host(request())["id"] == host["id"]
  host["token_sha256"] = None
  connect_routes._save_host(host)
  with pytest.raises(connect_routes.HTTPException) as revoked:
    connect_routes._auth_host(request())
  assert revoked.value.status_code == 401
