"""Reflection-run reliability: turn countdown + guaranteed-brief fallback.

Three of four prod nights died at max_turns (rc=2, subtype
error_max_turns) with NO brief. The skill's "bail to the brief by turn
40" rule is prose the agent cannot act on — it has no view of its own
turn count — so the runner now (a) counts assistant turns and injects
turn-budget steering messages into the live session as thresholds are
crossed, and (b) spawns one short rescue session when a failed night
left no brief on disk, making "you wake to nothing" structurally
impossible.
"""

from datetime import date

import pytest

import scripts.reflection_runner as dr


# ---------------------------------------------------------------------------
# Turn-countdown thresholds + steering messages (pure functions)
# ---------------------------------------------------------------------------

def test_steering_thresholds_default_budget():
  assert dr.steering_thresholds(60) == (35, 45)


def test_steering_thresholds_scale_with_budget():
  assert dr.steering_thresholds(120) == (70, 90)


def test_steering_thresholds_never_collapse_on_tiny_budgets():
  for max_turns in range(1, 12):
    soft, hard = dr.steering_thresholds(max_turns)
    assert soft >= 1
    assert hard > soft


def test_steering_message_fires_exactly_at_each_threshold():
  fired = [
    (turn, dr.steering_message(turn - 1, turn, 60))
    for turn in range(1, 61)
  ]
  msgs = [(turn, msg) for turn, msg in fired if msg is not None]
  assert [turn for turn, _ in msgs] == [35, 45]
  soft_msg, hard_msg = msgs[0][1], msgs[1][1]
  assert "turn 35 of 60" in soft_msg
  assert "STOP open-ended investigation" in soft_msg
  assert "turn 45 of 60" in hard_msg
  assert "MINIMAL brief" in hard_msg


def test_steering_messages_both_protect_the_inbox_drain():
  # The graph froze for days when the old soft message said "phases
  # 1-5 are over" and the agent skipped consolidation. Both steering
  # messages must now name the inbox drain as a floor deliverable so
  # the runner never authorizes skipping it.
  soft = dr.steering_message(34, 35, 60)
  hard = dr.steering_message(44, 45, 60)
  assert soft is not None and hard is not None
  for msg in (soft, hard):
    assert "inbox" in msg
  # The soft message must not regress to "phases 1-5 are over" — that
  # exact phrasing is what told the agent consolidation was done.
  assert "phases 1-5 are over" not in soft


def test_steering_message_double_cross_returns_only_the_stern_one():
  # One step that jumps over both thresholds must not produce two
  # back-to-back warnings — the stern one wins.
  msg = dr.steering_message(30, 50, 60)
  assert msg is not None
  assert "MINIMAL brief" in msg


def test_steering_message_quiet_off_threshold():
  assert dr.steering_message(35, 36, 60) is None
  assert dr.steering_message(45, 46, 60) is None
  assert dr.steering_message(0, 1, 60) is None


# ---------------------------------------------------------------------------
# Drain loop — injection happens against the live client
# ---------------------------------------------------------------------------

# The drain detects message types by class NAME, so these bare fakes
# are enough — no SDK import needed.
class AssistantMessage:
  pass


class ResultMessage:
  def __init__(self, is_error=False, subtype="success", result=None):
    self.is_error = is_error
    self.subtype = subtype
    self.result = result


class _FakeClient:
  def __init__(self, messages):
    self._messages = messages
    self.queries: list[str] = []

  async def query(self, text):
    self.queries.append(text)

  async def receive_response(self):
    for msg in self._messages:
      yield msg


@pytest.mark.asyncio
async def test_drain_session_injects_steering_at_both_thresholds():
  msgs = [AssistantMessage() for _ in range(46)] + [
    ResultMessage(is_error=True, subtype="error_max_turns")
  ]
  client = _FakeClient(msgs)
  saw_result, result_error, auth_failure = await dr._drain_session(
    client, None, max_turns=60, countdown=True,
  )
  assert saw_result is True
  assert result_error is True
  assert auth_failure is False
  assert len(client.queries) == 2
  assert "turn 35 of 60" in client.queries[0]
  assert "turn 45 of 60" in client.queries[1]


@pytest.mark.asyncio
async def test_drain_session_countdown_off_never_queries():
  msgs = [AssistantMessage() for _ in range(50)] + [ResultMessage()]
  client = _FakeClient(msgs)
  saw_result, result_error, auth_failure = await dr._drain_session(
    client, None, max_turns=60, countdown=False,
  )
  assert (saw_result, result_error, auth_failure) == (True, False, False)
  assert client.queries == []


@pytest.mark.asyncio
async def test_drain_session_swallows_steering_injection_failure():
  """A dead stdin must not abort the drain — the fallback layer still
  guarantees the brief, so steering is strictly best-effort."""
  class _BrokenStdin(_FakeClient):
    async def query(self, text):
      raise RuntimeError("stdin closed")

  msgs = [AssistantMessage() for _ in range(36)] + [ResultMessage()]
  client = _BrokenStdin(msgs)
  saw_result, result_error, auth_failure = await dr._drain_session(
    client, None, max_turns=60, countdown=True,
  )
  assert (saw_result, result_error, auth_failure) == (True, False, False)


# ---------------------------------------------------------------------------
# Guaranteed-brief fallback — trigger condition
# ---------------------------------------------------------------------------

def test_fallback_needed_matrix(tmp_path):
  brief = tmp_path / "2026-06-11.html"
  # A clean night never needs rescue, brief or not.
  assert dr.fallback_needed(0, brief) is False
  assert dr.fallback_needed(0, None) is False
  # A failed night with no brief on disk does.
  assert dr.fallback_needed(2, brief) is True
  assert dr.fallback_needed(1, brief) is True
  # A failed night whose brief already shipped does not.
  brief.write_text("<html>brief</html>")
  assert dr.fallback_needed(2, brief) is False
  # An unresolvable brief path counts as missing.
  assert dr.fallback_needed(2, None) is True


def test_todays_brief_path_resolves_from_staged_app_id(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(dr, "DATA_DIR", tmp_path)
  inputs = tmp_path / "apps" / "reflection" / "inputs"
  inputs.mkdir(parents=True)
  (inputs / "app_id").write_text("46\n")
  expected = (
    tmp_path / "apps" / "46" / "reports"
    / f"{date.today().isoformat()}.html"
  )
  assert dr.todays_brief_path() == expected


def test_todays_brief_path_none_when_unstaged_or_blank(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(dr, "DATA_DIR", tmp_path)
  assert dr.todays_brief_path() is None
  inputs = tmp_path / "apps" / "reflection" / "inputs"
  inputs.mkdir(parents=True)
  (inputs / "app_id").write_text("  \n")
  assert dr.todays_brief_path() is None


def test_fallback_goal_points_at_artifacts_and_notes_cutoff(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(dr, "DATA_DIR", tmp_path)
  goal = dr.build_fallback_goal()
  today = date.today().isoformat()
  assert f"runs/{today}" in goal
  assert "git -C" in goal
  assert f"reports/{today}.html" in goal
  assert "CUT OFF" in goal
  # The brief is the rescue's ONLY deliverable; the conversation about it is
  # opened by the partner on tap, so the rescue must NOT create a chat.
  assert "morning chat" not in goal
  assert "do not create a chat" in goal.lower()
  assert "Do NOT restart" in goal


# ---------------------------------------------------------------------------
# Guaranteed-brief fallback — orchestration
# ---------------------------------------------------------------------------

def _stage_app_id(tmp_path, app_id="46"):
  inputs = tmp_path / "apps" / "reflection" / "inputs"
  inputs.mkdir(parents=True, exist_ok=True)
  (inputs / "app_id").write_text(app_id)


@pytest.mark.asyncio
async def test_fallback_runs_short_plain_session_when_brief_missing(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(dr, "DATA_DIR", tmp_path)
  _stage_app_id(tmp_path)

  calls = []

  async def fake_claude_session(**kwargs):
    calls.append(kwargs)
    return 0

  monkeypatch.setattr(dr, "_run_claude_session", fake_claude_session)
  await dr._maybe_write_fallback_brief(
    2, provider="claude", skill_text="skill", env={}, model=None,
    effort=None, log_fh=None,
  )
  assert len(calls) == 1
  call = calls[0]
  assert call["max_turns"] == dr.FALLBACK_MAX_TURNS
  # Recursion guard: the rescue session runs plain — no countdown, and
  # nothing in the helper re-invokes the fallback path.
  assert call["countdown"] is False
  assert "CUT OFF" in call["goal"]
  assert call["skill_text"] == "skill"


@pytest.mark.asyncio
async def test_fallback_skipped_when_brief_already_on_disk(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(dr, "DATA_DIR", tmp_path)
  _stage_app_id(tmp_path)
  reports = tmp_path / "apps" / "46" / "reports"
  reports.mkdir(parents=True)
  (reports / f"{date.today().isoformat()}.html").write_text("<html/>")

  async def explode(**kwargs):
    raise AssertionError("rescue must not run when the brief exists")

  monkeypatch.setattr(dr, "_run_claude_session", explode)
  monkeypatch.setattr(dr, "_run_codex_session", explode)
  await dr._maybe_write_fallback_brief(
    2, provider="claude", skill_text="s", env={}, model=None,
    effort=None, log_fh=None,
  )


@pytest.mark.asyncio
async def test_fallback_skipped_on_clean_night(tmp_path, monkeypatch):
  monkeypatch.setattr(dr, "DATA_DIR", tmp_path)

  async def explode(**kwargs):
    raise AssertionError("rescue must not run on rc=0")

  monkeypatch.setattr(dr, "_run_claude_session", explode)
  monkeypatch.setattr(dr, "_run_codex_session", explode)
  await dr._maybe_write_fallback_brief(
    0, provider="claude", skill_text="s", env={}, model=None,
    effort=None, log_fh=None,
  )


@pytest.mark.asyncio
async def test_fallback_runs_even_when_app_id_unstaged(
  tmp_path, monkeypatch,
):
  """An unresolvable brief path means we can't PROVE a brief exists —
  rescue anyway; the agent resolves the app id itself."""
  monkeypatch.setattr(dr, "DATA_DIR", tmp_path)

  calls = []

  async def fake_claude_session(**kwargs):
    calls.append(kwargs)
    return 0

  monkeypatch.setattr(dr, "_run_claude_session", fake_claude_session)
  await dr._maybe_write_fallback_brief(
    1, provider="claude", skill_text="s", env={}, model=None,
    effort=None, log_fh=None,
  )
  assert len(calls) == 1


@pytest.mark.asyncio
async def test_fallback_uses_the_codex_session_for_codex_nights(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(dr, "DATA_DIR", tmp_path)
  _stage_app_id(tmp_path)

  codex_calls = []

  async def fake_codex_session(**kwargs):
    codex_calls.append(kwargs)
    return 0

  async def explode(**kwargs):
    raise AssertionError("claude session must not run for codex nights")

  monkeypatch.setattr(dr, "_run_codex_session", fake_codex_session)
  monkeypatch.setattr(dr, "_run_claude_session", explode)
  await dr._maybe_write_fallback_brief(
    2, provider="codex", skill_text="s", env={}, model=None,
    effort=None, log_fh=None,
  )
  assert len(codex_calls) == 1
  assert "CUT OFF" in codex_calls[0]["goal"]


@pytest.mark.asyncio
async def test_fallback_crash_is_swallowed(tmp_path, monkeypatch):
  """The rescue must never turn a recorded failure into a crash."""
  monkeypatch.setattr(dr, "DATA_DIR", tmp_path)
  _stage_app_id(tmp_path)

  async def boom(**kwargs):
    raise RuntimeError("SDK exploded")

  monkeypatch.setattr(dr, "_run_claude_session", boom)
  await dr._maybe_write_fallback_brief(
    2, provider="claude", skill_text="s", env={}, model=None,
    effort=None, log_fh=None,
  )


# ---------------------------------------------------------------------------
# Auth-failure handling — a 401 must not burn a doomed CLI rescue
# ---------------------------------------------------------------------------

def test_is_auth_failure_matches_known_401_phrasings():
  assert dr._is_auth_failure("API Error: 401 Unauthorized")
  assert dr._is_auth_failure("Invalid authentication credentials")
  assert dr._is_auth_failure("Failed to authenticate with the API")
  assert dr._is_auth_failure("authentication_error: bad token")
  assert dr._is_auth_failure("OAuth token has expired")
  # Case-insensitive.
  assert dr._is_auth_failure("FAILED TO AUTHENTICATE")
  # A non-auth error and the empty cases do NOT match.
  assert not dr._is_auth_failure("error_max_turns")
  assert not dr._is_auth_failure("")
  assert not dr._is_auth_failure(None)


@pytest.mark.asyncio
async def test_drain_session_flags_auth_failure_on_401():
  # The CLI mislabels a 401 as subtype="success" while is_error=True —
  # the error STRING is the only honest signal, so the drain must read
  # it and set the auth_failure flag.
  msgs = [AssistantMessage()] + [
    ResultMessage(
      is_error=True,
      subtype="success",
      result="API Error: 401 Invalid authentication credentials",
    )
  ]
  client = _FakeClient(msgs)
  saw_result, result_error, auth_failure = await dr._drain_session(
    client, None, max_turns=60, countdown=False,
  )
  assert (saw_result, result_error, auth_failure) == (True, True, True)


@pytest.mark.asyncio
async def test_drain_session_non_auth_error_does_not_flag_auth():
  msgs = [AssistantMessage()] + [
    ResultMessage(is_error=True, subtype="error_max_turns", result=None)
  ]
  client = _FakeClient(msgs)
  saw_result, result_error, auth_failure = await dr._drain_session(
    client, None, max_turns=60, countdown=False,
  )
  assert (saw_result, result_error, auth_failure) == (True, True, False)


def test_write_static_auth_failure_brief_lands_valid_html(tmp_path):
  brief = tmp_path / "apps" / "46" / "reports" / "2026-06-22.html"
  assert dr.write_static_auth_failure_brief(brief) is True
  assert brief.is_file()
  html = brief.read_text(encoding="utf-8")
  assert html.startswith("<!DOCTYPE html>")
  assert "</html>" in html
  assert "failed to authenticate" in html
  assert "resume tomorrow" in html


@pytest.mark.asyncio
async def test_run_claude_session_returns_auth_rc_on_401(monkeypatch):
  """A 401 result must surface as AUTH_FAILURE_RC, not the generic 2.

  This is the core of the fix: the rc carries the auth distinction out
  to the guaranteed-brief layer so it can skip the doomed CLI rescue.
  """
  class _FakeAuthClient:
    def __init__(self, options):
      self.queries = []

    async def connect(self):
      pass

    async def query(self, text):
      self.queries.append(text)

    async def receive_response(self):
      yield AssistantMessage()
      yield ResultMessage(
        is_error=True,
        subtype="success",
        result="API Error: 401 Invalid authentication credentials",
      )

    async def disconnect(self):
      pass

  # Stub the SDK so the import inside _run_claude_session resolves to
  # our fakes — no real claude-agent-sdk needed.
  import sys
  import types

  fake_sdk = types.ModuleType("claude_agent_sdk")
  fake_sdk.ClaudeAgentOptions = lambda **kw: kw
  fake_sdk.ClaudeSDKClient = _FakeAuthClient
  monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

  rc = await dr._run_claude_session(
    goal="g", skill_text="s", env={}, model=None, effort=None,
    max_turns=60, log_fh=None, countdown=False,
  )
  assert rc == dr.AUTH_FAILURE_RC


@pytest.mark.asyncio
async def test_fallback_writes_static_brief_without_cli_on_auth_rc(
  tmp_path, monkeypatch,
):
  """On the auth rc, the runner writes the static brief ITSELF.

  No second CLI session may be spawned (it would just 401 again), and
  the brief must land on disk so the partner wakes to something.
  """
  monkeypatch.setattr(dr, "DATA_DIR", tmp_path)
  _stage_app_id(tmp_path)

  async def explode(**kwargs):
    raise AssertionError("auth-failure rescue must NOT spawn a CLI session")

  monkeypatch.setattr(dr, "_run_claude_session", explode)
  monkeypatch.setattr(dr, "_run_codex_session", explode)

  await dr._maybe_write_fallback_brief(
    dr.AUTH_FAILURE_RC, provider="claude", skill_text="s", env={},
    model=None, effort=None, log_fh=None,
  )

  brief = (
    tmp_path / "apps" / "46" / "reports"
    / f"{date.today().isoformat()}.html"
  )
  assert brief.is_file()
  html = brief.read_text(encoding="utf-8")
  assert "failed to authenticate" in html
  assert html.strip().endswith("</html>")


@pytest.mark.asyncio
async def test_fallback_auth_rc_unstaged_app_id_falls_back_to_cli(
  tmp_path, monkeypatch,
):
  """When the brief path can't be resolved on an auth night, there's
  nowhere to write the static brief — fall through to the normal CLI
  rescue as a last resort rather than silently skipping the night."""
  monkeypatch.setattr(dr, "DATA_DIR", tmp_path)  # app_id unstaged

  calls = []

  async def fake_claude_session(**kwargs):
    calls.append(kwargs)
    return 0

  monkeypatch.setattr(dr, "_run_claude_session", fake_claude_session)
  await dr._maybe_write_fallback_brief(
    dr.AUTH_FAILURE_RC, provider="claude", skill_text="s", env={},
    model=None, effort=None, log_fh=None,
  )
  assert len(calls) == 1


# ---------------------------------------------------------------------------
# Brief-chat contract (seed skill)
# ---------------------------------------------------------------------------

def test_seed_skill_does_not_create_a_morning_chat():
  """The nightly run must NOT pre-create a chat about the brief.

  The conversation about a brief is now opened by the partner on tap
  in the Reflection app — which POSTs /api/app-chats with the brief's
  `report_date`, and the backend injects the brief into the new chat's
  first turn via the app-context seam. So the nightly skill's job ends
  at the brief: it must NOT mint an app token, POST /api/app-chats,
  write a chat-link meta.json, or send an opener. The skill must say so
  explicitly, and the old create recipes must be gone.
  """
  from pathlib import Path

  seed = (
    Path(dr.__file__).resolve().parent / "seed-skills" / "reflection.md"
  ).read_text(encoding="utf-8")
  # The skill must instruct the agent NOT to create the chat.
  assert "Do NOT create a morning chat" in seed
  # The nightly run no longer runs the create recipes.
  assert "/api/auth/app-token" not in seed
  assert 'POST "$API_BASE_URL/api/app-chats"' not in seed
  assert "$MORNING_CHAT" not in seed
  # The owner-create recipe must not be present either.
  assert 'POST "$API_BASE_URL/api/chats"' not in seed
