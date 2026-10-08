"""Opt-in OpenTelemetry tracing for one Möbius instance.

Off unless ``<data_dir>/tracing.json`` enables it::

  {"enabled": true, "endpoint": "http://127.0.0.1:4318"}

``endpoint`` is an OTLP/HTTP collector base URL (Jaeger, an OpenTelemetry
Collector, Grafana Alloy, ...). The file is read once at startup, so a change
needs a server restart.

When enabled, this records the shape of work, never its content: request
routes and status codes, SQL statement text (SQLite placeholders, not bound
values), outgoing call hosts, and the duration and outcome of agent
turns, tool calls and app builds. Query strings are stripped from every URL
attribute because Möbius and external APIs carry tokens there; concrete paths
are replaced by the route template (server spans) or dropped (client spans)
because paths can carry secrets too; and error text is reduced to the
exception type because messages can quote data. All of this happens at
export, after every instrumentation has finished with the span. Never attach
prompts, messages, tool input or tool output to a span. Spans never leave the
instance through request headers: no trace context is injected into outgoing
requests or adopted from incoming ones.

The OpenTelemetry packages are an optional local install, not a declared
platform dependency, so every helper here degrades to a no-op without them.
To use tracing, install them into the backend's Python environment::

  pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http \
    opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-httpx \
    opentelemetry-instrumentation-sqlalchemy
"""

from __future__ import annotations

import json
import logging
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

from app.config import get_settings

_log = logging.getLogger(__name__)

_CONFIG_NAME = "tracing.json"
_PATH_ATTRIBUTES = ("http.target", "url.path")
_FULL_URL_ATTRIBUTES = ("http.url", "url.full")
_DROPPED_ATTRIBUTES = ("url.query",)

try:
  from opentelemetry import trace as _otel_trace
  from opentelemetry.trace import Status, StatusCode
except ImportError:  # optional local install
  _otel_trace = None

_tracer = None


def _read_config() -> dict | None:
  path = Path(get_settings().data_dir) / _CONFIG_NAME
  try:
    payload = json.loads(path.read_text(encoding="utf-8"))
  except FileNotFoundError:
    return None
  except (OSError, UnicodeError, json.JSONDecodeError):
    _log.warning("tracing: %s is unreadable; tracing stays off", path)
    return None
  if not isinstance(payload, dict) or payload.get("enabled") is not True:
    return None
  endpoint = payload.get("endpoint")
  if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
    _log.warning("tracing: %s needs an http(s) endpoint; tracing stays off", path)
    return None
  return {"endpoint": endpoint.rstrip("/")}


def _strip_query(value: Any) -> Any:
  if not isinstance(value, str) or ("?" not in value and "#" not in value):
    return value
  if value.startswith("/"):
    return value.split("?", 1)[0].split("#", 1)[0]
  parts = urlsplit(value)
  return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _scrub_attributes(attributes) -> dict:
  """Keep the shape of a URL, never its concrete path or query.

  Real paths can carry secrets (published-site tokens, pairing codes, third
  party resource ids), so a server span keeps only its route template and a
  client span only its scheme and host.
  """
  clean = {
    key: value for key, value in (attributes or {}).items()
    if key not in _DROPPED_ATTRIBUTES
  }
  route = clean.get("http.route")
  route = route if isinstance(route, str) and route else None
  for key in _PATH_ATTRIBUTES:
    if key in clean:
      clean[key] = route or "[unmatched]"
  for key in _FULL_URL_ATTRIBUTES:
    if key in clean:
      parts = urlsplit(str(_strip_query(clean[key])))
      clean[key] = urlunsplit((parts.scheme, parts.netloc, route or "", "", ""))
  return clean


def _scrubbed(span, readable_span, event_type):
  """A copy of a finished span with query strings and error text removed.

  Exception messages, tracebacks and status descriptions can quote request
  data, so an error keeps only its exception type.
  """
  events = [
    event_type(
      event.name,
      {k: v for k, v in (event.attributes or {}).items() if k == "exception.type"},
      event.timestamp,
    )
    for event in span.events
  ]
  status = span.status
  if status.description:
    status = Status(status.status_code)
  return readable_span(
    name=span.name,
    context=span.context,
    parent=span.parent,
    resource=span.resource,
    attributes=_scrub_attributes(span.attributes),
    events=events,
    links=span.links,
    kind=span.kind,
    status=status,
    start_time=span.start_time,
    end_time=span.end_time,
    instrumentation_scope=span.instrumentation_scope,
  )


def configure(app, engine, *, exporter=None) -> bool:
  """Install exporters and auto-instrumentation when tracing.json enables it.

  ``exporter`` replaces the OTLP exporter (tests pass an in-memory one, which
  is exported synchronously).
  """
  global _tracer
  config = _read_config()
  if config is None:
    return False
  if _otel_trace is None:
    _log.warning("tracing: enabled in config but OpenTelemetry is not installed")
    return False
  try:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.context import Context
    from opentelemetry.propagate import get_global_textmap, set_global_textmap
    from opentelemetry.propagators.textmap import TextMapPropagator
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
    from opentelemetry.sdk.trace.export import (
      BatchSpanProcessor, SimpleSpanProcessor, SpanExporter,
    )
  except ImportError as exc:
    _log.warning("tracing: OpenTelemetry install is incomplete (%s)", exc)
    return False

  class _LocalOnlyPropagation(TextMapPropagator):
    """Never inject trace headers; never adopt a caller's trace context."""

    def extract(self, carrier, context=None, getter=None):
      return context if context is not None else Context()

    def inject(self, carrier, context=None, setter=None):
      return None

    @property
    def fields(self):
      return set()

  class _ScrubbingExporter(SpanExporter):
    """Scrub every span on its way out, whenever its attributes were set."""

    def __init__(self, inner):
      self._inner = inner

    def export(self, spans):
      return self._inner.export([_scrubbed(span, ReadableSpan, Event) for span in spans])

    def shutdown(self):
      self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
      return self._inner.force_flush(timeout_millis)

  try:
    # All or nothing: a failed step unwinds every step before it, so a
    # half-configured setup never exports spans or keeps the propagator.
    with ExitStack() as rollback:
      provider = TracerProvider(resource=Resource.create({"service.name": "mobius"}))
      rollback.callback(provider.shutdown)
      if exporter is None:
        provider.add_span_processor(BatchSpanProcessor(_ScrubbingExporter(
          OTLPSpanExporter(endpoint=f"{config['endpoint']}/v1/traces"),
        )))
      else:
        provider.add_span_processor(SimpleSpanProcessor(_ScrubbingExporter(exporter)))
      # Spans stay inside this instance: never add a traceparent header to
      # outgoing requests (proxy fetches reach arbitrary third parties).
      rollback.callback(set_global_textmap, get_global_textmap())
      set_global_textmap(_LocalOnlyPropagation())
      SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=provider)
      rollback.callback(SQLAlchemyInstrumentor().uninstrument)
      HTTPXClientInstrumentor().instrument(tracer_provider=provider)
      rollback.callback(HTTPXClientInstrumentor().uninstrument)

      # Last, because it cannot be undone safely: uninstrument_app builds the
      # middleware stack, after which main.py could no longer add middleware.
      # ASGI send/receive spans are one per streamed chunk; on a live chat
      # stream they bury the request span under thousands of children.
      FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, exclude_spans=["send", "receive"],
      )
      rollback.pop_all()
  except Exception:
    # Optional diagnostics must never stop the server from booting, e.g. when
    # a locally installed OpenTelemetry drifts from the platform's FastAPI.
    _log.exception("tracing: setup failed; tracing stays off")
    return False
  _tracer = provider.get_tracer("mobius")
  _log.info("tracing: exporting to %s", config["endpoint"])
  return True


def _attributes(attributes: dict | None) -> dict:
  return {
    key: value for key, value in (attributes or {}).items()
    if isinstance(value, (str, bool, int, float)) and value != ""
  }


@contextmanager
def span(name: str, attributes: dict | None = None) -> Iterator[Any]:
  """Time a block as the current span; a no-op unless tracing is on."""
  if _tracer is None:
    yield None
    return
  with _tracer.start_as_current_span(name, attributes=_attributes(attributes)) as current:
    yield current


def start_span(name: str, attributes: dict | None = None) -> Any:
  """Start a span that ends elsewhere (for example on a later event).

  It is a child of the current span but is not made current, so unrelated
  work in between is not attributed to it. Close it with ``end_span``.
  """
  if _tracer is None:
    return None
  return _tracer.start_span(
    name, attributes=_attributes(attributes),
  )


def end_span(handle: Any, *, error: str | None = None) -> None:
  if handle is None:
    return
  if error:
    handle.set_status(Status(StatusCode.ERROR, error))
  handle.end()


def annotate(handle: Any, attributes: dict | None) -> None:
  """Add attributes to a span, or to the current span when ``handle`` is None."""
  if _tracer is None:
    return
  target = handle if handle is not None else _otel_trace.get_current_span()
  for key, value in _attributes(attributes).items():
    target.set_attribute(key, value)
