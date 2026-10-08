"""Opt-in tracing records the shape of work, never secrets or content."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import tracing
from app.config import get_settings


def _otel_sdk():
  """The OpenTelemetry SDK, which only a local opt-in install provides."""
  return pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")


@pytest.fixture
def tracing_config():
  path = Path(get_settings().data_dir) / "tracing.json"
  path.write_text(json.dumps({"enabled": True, "endpoint": "http://127.0.0.1:4318"}))
  yield path
  path.unlink(missing_ok=True)


def test_tracing_stays_off_without_config(monkeypatch):
  monkeypatch.setattr(tracing, "_tracer", None)
  assert tracing.configure(FastAPI(), create_engine("sqlite://")) is False
  with tracing.span("anything") as handle:
    assert handle is None
  assert tracing.start_span("anything") is None


def test_disabled_config_keeps_tracing_off(tracing_config, monkeypatch):
  monkeypatch.setattr(tracing, "_tracer", None)
  tracing_config.write_text(json.dumps({"enabled": False, "endpoint": "http://x"}))
  assert tracing.configure(FastAPI(), create_engine("sqlite://")) is False


def test_enabled_config_without_opentelemetry_is_a_no_op(tracing_config, monkeypatch):
  """OpenTelemetry is not a platform dependency; enabling it without the
  optional install must leave every helper inert."""
  monkeypatch.setattr(tracing, "_tracer", None)
  monkeypatch.setattr(tracing, "_otel_trace", None)
  assert tracing.configure(FastAPI(), create_engine("sqlite://")) is False
  with tracing.span("anything") as handle:
    assert handle is None
  assert tracing.start_span("anything") is None
  tracing.annotate(None, {"mobius.anything": 1})
  tracing.end_span(None)


@pytest.mark.parametrize("raw, expected", [
  ("/api/chat/stream?token=secret", "/api/chat/stream"),
  ("https://api.example.com/v1/x?key=secret#frag", "https://api.example.com/v1/x"),
  ("/api/apps/", "/api/apps/"),
])
def test_query_strings_are_stripped(raw, expected):
  assert tracing._strip_query(raw) == expected


def test_server_spans_keep_the_route_template_not_the_concrete_path():
  clean = tracing._scrub_attributes({
    "http.route": "/api/published-sites/{token}/data",
    "http.target": "/api/published-sites/s3cr3t-token/data?x=1",
    "http.url": "https://me.example/api/published-sites/s3cr3t-token/data?x=1",
    "url.query": "x=1",
  })
  assert clean["http.target"] == "/api/published-sites/{token}/data"
  assert clean["http.url"] == "https://me.example/api/published-sites/{token}/data"
  assert "url.query" not in clean
  assert "s3cr3t" not in str(clean)


def test_client_and_unmatched_spans_keep_only_scheme_and_host():
  client = tracing._scrub_attributes({
    "url.full": "https://api.anthropic.com/v1/orgs/org-secret/usage?k=1",
  })
  assert client["url.full"] == "https://api.anthropic.com"
  unmatched = tracing._scrub_attributes({"http.target": "/i/pairing-code-123"})
  assert unmatched["http.target"] == "[unmatched]"


def test_enabled_tracing_records_routes_queries_and_tool_calls(
  tracing_config, monkeypatch,
):
  otel_sdk = _otel_sdk()
  from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
  from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
  from app.chat_event_sink import ChatEventSink

  monkeypatch.setattr(tracing, "_tracer", None)
  exporter = otel_sdk.InMemorySpanExporter()
  app = FastAPI()
  engine = create_engine("sqlite://")

  @app.get("/api/things/{thing_id}")
  def read_thing(thing_id: int):
    with engine.connect() as conn:
      conn.execute(text("SELECT :value"), {"value": "private-value"})
    return {"id": thing_id}

  @app.get("/api/broken")
  def broken():
    raise ValueError("secret-in-error")

  try:
    assert tracing.configure(app, engine, exporter=exporter) is True
    response = TestClient(app).get("/api/things/7?token=secret-token")
    assert response.status_code == 200
    assert TestClient(app, raise_server_exceptions=False).get("/api/broken").status_code == 500

    sink = SimpleNamespace(chat_id="chat-1", _tool_spans={})
    with tracing.span("agent.turn", {"mobius.chat_id": "chat-1"}):
      for event in (
        {"type": "tool_start", "tool": "Bash", "tool_use_id": "t1", "input": "rm -rf secret"},
        {"type": "tool_output", "tool_use_id": "t1", "content": "boom", "output_exit_code": 1},
        {"type": "tool_end", "tool_use_id": "t1"},
        {"type": "tool_start", "tool": "Read", "tool_use_id": "t2"},
      ):
        ChatEventSink._trace_tool_event(sink, event)
      ChatEventSink._end_open_tool_spans(sink)
  finally:
    HTTPXClientInstrumentor().uninstrument()
    SQLAlchemyInstrumentor().uninstrument()

  spans = {span.name: span for span in exporter.get_finished_spans()}
  recorded = json.dumps([dict(span.attributes or {}) for span in spans.values()])
  assert "secret-token" not in recorded
  assert "private-value" not in recorded
  assert "rm -rf" not in recorded and "boom" not in recorded
  for span in spans.values():
    assert "secret" not in str(span.events) + str(span.status.description)
  broken = spans["GET /api/broken"]
  assert not broken.status.is_ok
  assert any(e.attributes.get("exception.type") == "ValueError" for e in broken.events)

  assert "GET /api/things/{thing_id}" in spans
  assert spans["GET /api/things/{thing_id}"].attributes["http.target"] == "/api/things/{thing_id}"

  from opentelemetry.propagate import get_global_textmap
  assert get_global_textmap().fields == set(), "no traceparent on outgoing requests"
  assert any(span.attributes.get("db.system") == "sqlite" for span in spans.values())

  turn = spans["agent.turn"]
  bash = spans["tool Bash"]
  read = spans["tool Read"]
  assert bash.parent.span_id == turn.context.span_id
  assert bash.attributes["mobius.tool.failed"] is True
  assert not read.status.is_ok
  assert sink._tool_spans == {}


def test_tracing_setup_failure_never_blocks_server_boot(tracing_config, monkeypatch):
  """A locally installed OpenTelemetry that drifts from FastAPI must not crash import."""
  otel_sdk = _otel_sdk()
  from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

  monkeypatch.setattr(tracing, "_tracer", None)

  def drifted(*_args, **_kwargs):
    raise TypeError("unexpected keyword argument 'exclude_spans'")

  monkeypatch.setattr(FastAPIInstrumentor, "instrument_app", drifted)
  exporter = otel_sdk.InMemorySpanExporter()
  assert tracing.configure(FastAPI(), create_engine("sqlite://"), exporter=exporter) is False
  assert tracing._tracer is None
  with tracing.span("anything") as handle:
    assert handle is None



@pytest.mark.parametrize("failing_step", ["httpx", "fastapi"])
def test_late_tracing_setup_failure_unwinds_every_earlier_step(
  tracing_config, monkeypatch, failing_step,
):
  """A failure in a late setup step must leave tracing fully off: no request
  or SQL spans reach the exporter, the propagator is restored, and the server
  can keep assembling its app."""
  otel_sdk = _otel_sdk()
  from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
  from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
  from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
  from opentelemetry.propagate import get_global_textmap
  from starlette.middleware.gzip import GZipMiddleware

  monkeypatch.setattr(tracing, "_tracer", None)

  def drifted(*_args, **_kwargs):
    raise TypeError(f"{failing_step} instrumentation drifted")

  if failing_step == "httpx":
    monkeypatch.setattr(HTTPXClientInstrumentor, "instrument", drifted)
  else:
    monkeypatch.setattr(FastAPIInstrumentor, "instrument_app", drifted)
  original_textmap = get_global_textmap()
  exporter = otel_sdk.InMemorySpanExporter()
  app = FastAPI()
  engine = create_engine("sqlite://")

  @app.get("/api/things/{thing_id}")
  def read_thing(thing_id: int):
    with engine.connect() as conn:
      conn.execute(text("SELECT 1"))
    return {"id": thing_id}

  try:
    assert tracing.configure(app, engine, exporter=exporter) is False
    assert tracing._tracer is None
    assert get_global_textmap() is original_textmap
    assert not SQLAlchemyInstrumentor().is_instrumented_by_opentelemetry
    assert not HTTPXClientInstrumentor().is_instrumented_by_opentelemetry
    # main.py adds its middleware after configure(); that must still work.
    app.add_middleware(GZipMiddleware)
    assert TestClient(app).get("/api/things/7").status_code == 200
  finally:
    HTTPXClientInstrumentor().uninstrument()
    SQLAlchemyInstrumentor().uninstrument()
  assert exporter.get_finished_spans() == ()
