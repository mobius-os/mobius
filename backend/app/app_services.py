"""Bounded request/response execution for app-owned server policy.

The platform owns authentication, accepted-runtime identity, process limits,
and revocation. The app owns the request paths and domain behavior behind one
small JSON protocol. This is deliberately not a framework or an import hook:
every request runs in its own process from one reviewed Python entrypoint and
receives one JSON reply. An entry that declares ``MOBIUS_PRELOAD = True`` has
its module setup run once and each request forked from it (``service_preload``);
every other entry, and any request no preloaded host can take, is spawned.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import contextlib
import io
import itertools
import json
import logging
import os
import re
import signal
import sys
import weakref
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException

from app import app_python_env, auth, models, service_preload, settled, tracing
from app.applied_app_runtime import AppliedRuntimeUnavailable, hold_runtime, runtime_root
from app.browser_access import BrowserLineage, require_live
from app.config import get_settings
from app.manifest_contract import (
  SERVICE_REQUEST_MAX_BYTES,
  accepted_service_max_bytes, accepted_service_route_limits, service_route_key,
)


log = logging.getLogger(__name__)
MAX_REQUEST_BYTES = SERVICE_REQUEST_MAX_BYTES
MAX_RESPONSE_BYTES = SERVICE_REQUEST_MAX_BYTES
MAX_ERROR_BYTES = 16 * 1024
SERVICE_TIMEOUT_SECONDS = 15
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}$")
# The only headers an app service may add. A service answers on the shell
# origin, so anything else (Set-Cookie, Clear-Site-Data, NEL/Report-To,
# Service-Worker-Allowed, framing, transport) would act on the owner's whole
# session or on the platform's own response contract, and is dropped. The
# platform sets the content type from `media_type`.
_SERVICE_RESPONSE_HEADERS = frozenset({
  "cache-control", "content-disposition", "content-language", "etag",
  "last-modified", "vary",
})
# Cache-Control directives that let a shared cache store a response to an
# authorized request (RFC 9111 section 3.5), unless private/no-store also apply.
_SHARED_CACHE_DIRECTIVES = frozenset({
  "public", "s-maxage", "must-revalidate", "proxy-revalidate",
})
_global_slots = {
  "private": asyncio.Semaphore(8),
  "public": asyncio.Semaphore(8),
  "tools": asyncio.Semaphore(8),
}
_app_slots: weakref.WeakValueDictionary[tuple[int, str], asyncio.Semaphore] = (
  weakref.WeakValueDictionary()
)


class ServiceBoundaryError(HTTPException):
  """A platform-boundary failure with a stable trace category.

  The category, never the detail text, reaches tracing: details can quote
  runtime paths or header names and may be reworded without changing meaning.
  """

  def __init__(
    self, category: str, status_code: int, detail: str, *, headers: dict[str, str] | None = None,
  ):
    super().__init__(status_code, detail, headers=headers)
    self.category = category


# This is an exchange allocation budget, not another transfer grant. It is
# shared by every lane and held through the execution queue and response
# handoff. Exhaustion never buffers another body byte and never starts a process.
#
# Structural work units bound the one uninterruptible stdlib call per decoded
# document, json.loads (rendering and request encoding yield between parts), so
# larger transfer grants buy media bytes, not denser JSON. A unit is a
# structural mark ('"', '{', '[', ',', ':' outside strings), 24 other characters
# outside strings, or the measured extra cost of one number token (see
# _json_number_work). Depth stays well inside every consumer: FastAPI's
# response validation of a tool result fails near 255 levels, and the
# pure-Python encoder needs one frame per level.
MAX_JSON_STRUCTURE = 1 << 19
_JSON_BARE_CHARS_PER_UNIT = 24
MAX_JSON_DEPTH = 128
MAX_JSON_DECODE_COST = 320 * 1024 * 1024
MAX_ADMITTED_EXCHANGE_COST = 512 * 1024 * 1024
MAX_PUBLIC_INGRESS_COST = 192 * 1024 * 1024
_admitted_exchange_cost = 0
_public_ingress_cost = 0
# Marks outside strings. A value can only start after '[', ',' or ':' (or
# open the document), and json.loads stops at the first syntax error, so a
# number is examined only there: a float, an int past _JSON_LINEAR_DIGITS,
# or any token (a lone sign included) cut by the chunk end, which carry work
# beyond their characters. Short ints and any other text are skipped by the
# native search, never one Python step each. A '\\' outside a string is
# invalid JSON; it is matched only so the scan never misreads it, and costs a
# unit (``stray``).
_JSON_VALUE_START = (
  r'[ \t\n\r]*+(?:(?P<number>-?[0-9]++(?:[.eE][0-9.eE+-]*+|(?<=[0-9]{19})|\Z)|-\Z)'
  r'|(?P<pending>\Z))?'
)
_JSON_RESOURCE_MARKS = re.compile(r'["\\{}\]]|[\[,:]' + _JSON_VALUE_START)
_JSON_FIRST_VALUE = re.compile(_JSON_VALUE_START)
_JSON_NUMBER_TAIL = re.compile(r'[0-9.eE+-]*')
# Disjoint alternatives consume string content in native, bounded chunks,
# including escaped quotes/backslashes. This only finds the end of a string;
# stdlib JSON still validates escapes and syntax. Never loop per escape in Python.
_JSON_STRING_CONTENT = re.compile(r'(?:[^"\\]+|\\[\s\S])*')
_JSON_NON_ASCII_TEXT = re.compile(r'[^\x00-\x7f]')
_JSON_WIDE_TEXT = re.compile(r'[^\x00-\xff]')
_JSON_ASTRAL_TEXT = re.compile(r'[^\x00-\uffff]')
_JSON_BMP_TEXT = re.compile(r'[\u0100-\uffff]')
# Small enough that one chunk of maximally dense marks and numbers scans in
# a few milliseconds between cooperative yields.
_JSON_SCAN_BYTES = 16 * 1024
# Upper bounds on what stdlib JSON allocates, for 64-bit CPython 3.12. Every
# block may waste 23 bytes (pymalloc rounds to 16; malloc adds a chunk header).
# A phase's allocations are summed, including transients it frees itself.
# One document's fixed scanner, decoder and parser state (match objects, memo
# dict, buffer headers, a big int's conversion buffers), measured under 8.3 KiB.
_JSON_DOCUMENT_COST = 16 * 1024
# Decoding the next scan chunk costs its byte slice and an incremental
# decoder buffer sized by those bytes that may widen twice (1 + 2 + 4 bytes
# each): at most this many bytes per chunk byte.
_JSON_CHUNK_TRANSIENT_PER_BYTE = 8
# A list object (56 + GC rounding, 64) plus the constant part of an append
# reallocation (old and new item arrays coexist; 6 spare slots; two blocks).
_JSON_LIST_COST = 64 + 8 * 5 + 2 * 23
# One element ('[' or ','): its pointer in the old array and in the 9/8 larger
# new array, plus a bare number object (int up to 30 bits or float, 32).
_JSON_ELEMENT_COST = 8 + 9 + 32
# A dict object (64) plus old and new key tables' headers and blocks.
_JSON_DICT_COST = 64 + 2 * 32 + 2 * 23
# One member (':'). A resize from u members coexists the full old table
# (1.5u four-byte indices, u 16-byte entries: 22u) with a new table of fewer
# than 6u indices and 4u entries (88u). The parser's key memo dict grows the
# same way per distinct key. Plus a bare number value (32).
_JSON_MEMBER_COST = 2 * (22 + 88) + 32
# A str object header (56 non-ASCII), terminator (up to 4) and block waste.
_JSON_STRING_COST = 56 + 4 + 23
# A character outside strings: a big int stores under half a byte per digit,
# and parsing a number briefly holds a copy of its text.
_JSON_BARE_CHAR_COST = 2
# Each object costs at most sys.getsizeof plus its blocks' waste: one block, or
# two for a container's separately allocated items/keys.
_RETAINED_BLOCK_SLACK = 23
_RETAINED_ENVELOPE_COST = 4096
# While walking: one pending iterator (a dict's items view, its iterator and
# chain, or a list iterator) and list slots per open container, and a margin
# for one number's text or the next container's iterator (measured under
# 300 bytes).
_RETAINED_WALK_LEVEL_COST = 512
_RETAINED_WALK_HEADROOM = 2048
# The pure-Python encoder's fixed state, and one suspended generator per
# nesting level on the current path (measured under 3.8 KiB and 700 bytes).
_JSON_ENCODER_COST = 4096
_JSON_ENCODER_LEVEL_COST = 768
# A cooperative yield after this many encoded parts or visited values keeps a
# Python walk of a dense document from monopolizing the event loop.
_COOPERATIVE_STEP = 4 * 1024
# Escaping and UTF-8 sizing one _JSON_SCAN_BYTES slice of a long string costs
# about as much CPU as this many visited values (measured near 100 us).
_SIZING_SLICE_STEPS = 256
_EXHAUSTED = object()
# Request stdin is written in slices of this size, and stdout is collected in
# whole chunks of it (service_preload.read_bounded), so neither the pipe
# transport's copy of unsent input nor per-chunk object overhead grows with
# how a transfer is split.
_STDIO_SLICE = service_preload.STDIO_CHUNK_BYTES
# A bytes header, block waste and a (growing) list slot per collected chunk.
_STDIO_CHUNK_COST = 33 + _RETAINED_BLOCK_SLACK + 16
# One selector callback may allocate 256 KiB. Each of the two StreamReaders
# can retain twice its 64 KiB flow-control limit plus that final callback's
# data before pausing, including when an oversized output is rejected.
# Charge all buffers and one callback to each exchange, conservatively, rather
# than introducing a separate process-wide owner. uvloop needs less.
_STDIO_READ_TRANSIENT = 256 * 1024 + 33 + _RETAINED_BLOCK_SLACK
_STDIO_RECEIVE_COST = 3 * _STDIO_READ_TRANSIENT + 4 * _STDIO_SLICE


def _stdio_overhead(max_bytes: int) -> int:
  """What a running process adds beside its request and two stdout copies.

  The write transport copies the unsent part of at most two input slices;
  stdout and stderr chunks carry their headers; stderr is collected and joined
  like stdout. Reader buffers hold output not yet collected, so they fit the
  joined copy, which does not exist until reading ends. The callback transient
  and receive-buffer overshoot are additional, including for tiny routes.
  """
  return (
    _STDIO_RECEIVE_COST
    + min(max_bytes, 2 * _STDIO_SLICE)
    + (max_bytes // _STDIO_SLICE + 2) * _STDIO_CHUNK_COST
    + 2 * MAX_ERROR_BYTES
  )


def _json_text_width(text: str, start: int = 0, end: int | None = None) -> int:
  end = len(text) if end is None else end
  return (
    4 if _JSON_ASTRAL_TEXT.search(text, start, end) else
    2 if _JSON_WIDE_TEXT.search(text, start, end) else 1
  )


class ServiceExchangeAdmission:
  """Own request/response materialization, retained backlog and response handoff.

  The lexical scan measures resources only; stdlib JSON remains the sole
  syntax/value parser. Incremental stdlib text decoding and fixed-size scan
  chunks avoid constructing either the JSON tree or a full Unicode copy first.
  Estimates deliberately overcount duplicate keys, escapes and shared values.

  The reservation is the sum of three parts, each charged by the phase that
  owns it and never substituted for another: transfer, request and response.
  Transfer charges three copies of the bytes actually received during a read
  (``admit_received``), nothing while the decoded envelope waits for an
  execution slot (``retain``), the whole transfer ceiling from inside that slot
  before serialization or any process starts, including bounded stdio overhead
  (``admit_execution``), and actual stdout once the process has answered
  (``admit_output``). Request and
  response decoded costs are added, never substituted. After request
  materialization its transient Unicode decode copy is gone; retain() charges
  the surviving envelope. The response's decode/encode estimate stays held
  through HTTP rendering or tool handoff.
  """

  def __init__(self, max_bytes: int, *, public_ingress: bool = False):
    self.max_bytes = max_bytes
    self.public_ingress = public_ingress
    self.cost = 0
    self.transfer_cost = 0
    self.request_cost = 0
    self.response_cost = 0
    self.largest_part = 0
    self.encoded_bytes = 0
    self.encoded_depth = 0
    self.attribution: tuple[str, asyncio.Task] | None = None

  def __enter__(self):
    return self

  def __exit__(self, *_):
    global _admitted_exchange_cost
    self.finish_ingress()
    if self.attribution is not None:
      grant_id, task = self.attribution
      self.attribution = None
      calls = _browser_calls.get(grant_id)
      if calls is not None:
        calls.discard(task)
        if not calls:
          _browser_calls.pop(grant_id, None)
    _admitted_exchange_cost -= self.cost
    self.cost = 0
    self.transfer_cost = 0
    self.request_cost = 0
    self.response_cost = 0

  def attribute(self, grant_id: str):
    """Make the current task revocable with ``grant_id`` until the exchange ends.

    The exchange, not one call inside it, owns the attribution: an HTTP
    response still rendering under this reservation is cancelled by
    revocation before it can be handed off (cancel_browser_grant_calls).
    """
    task = asyncio.current_task()
    _browser_calls.setdefault(grant_id, set()).add(task)
    self.attribution = (grant_id, task)

  def finish_ingress(self):
    """Transfer a completed public read without releasing its total reservation."""
    global _public_ingress_cost
    if self.public_ingress:
      _public_ingress_cost -= self.cost
      self.public_ingress = False

  def _reserve(self, cost: int, *, direction: str = "request"):
    global _admitted_exchange_cost, _public_ingress_cost
    # No await between checking and reserving: all callers run on the owning
    # event loop, including HTTP, policy and agent-tool invocations.
    if _admitted_exchange_cost - self.cost + cost > MAX_ADMITTED_EXCHANGE_COST:
      self._reject(direction, "admission budget is exhausted")
    if self.public_ingress:
      if _public_ingress_cost - self.cost + cost > MAX_PUBLIC_INGRESS_COST:
        self._reject(direction, "public ingress budget is exhausted")
      _public_ingress_cost += cost - self.cost
    _admitted_exchange_cost += cost - self.cost
    self.cost = cost

  def _reject(self, direction: str, detail: str):
    if direction == "execution":
      # Nothing was serialized or started; the same request can succeed once
      # another exchange releases its transfer allowance.
      raise ServiceBoundaryError(
        "exchange_busy", 503, "App service is busy; retry shortly.", headers={"Retry-After": "1"},
      )
    if direction == "response":
      raise ServiceBoundaryError("response_limit", 502, f"App service response {detail}.")
    raise HTTPException(413, f"App service {direction} {detail}.")

  def _hold(
    self, direction: str, *, transfer: int | None = None,
    request: int | None = None, response: int | None = None,
  ):
    transfer = self.transfer_cost if transfer is None else transfer
    request = self.request_cost if request is None else request
    response = self.response_cost if response is None else response
    self._reserve(transfer + request + response, direction=direction)
    self.transfer_cost = transfer
    self.request_cost = request
    self.response_cost = response

  def admit_received(self, size: int):
    """Charge a body read for ``size`` bytes before its buffer grows to them.

    Three copies cover the growing buffer's slack, a reallocation's old and
    new buffers, and the final immutable copy (``read_capped_body``).
    """
    self._hold("request", transfer=3 * size)

  def admit_execution(self, max_bytes: int):
    """Reserve the whole transfer allowance from inside an execution slot.

    Runs before the request is serialized or a process is started, so an
    exhausted budget answers a retryable 503 with no side effect. Beyond
    the serialized input and two stdout copies, it charges bounded pending
    input, chunk headers, stderr and a selector-read transient. Encoding the
    largest retained string part is charged instead when its peak is higher
    (``retain``).
    """
    encoding = _json_encoding_peak(
      min(max_bytes, self.encoded_bytes), self.largest_part, self.encoded_depth,
    )
    self._hold("execution", transfer=max(3 * max_bytes + _stdio_overhead(max_bytes), encoding))

  def admit_output(self, stdout: bytes, diagnostic: str = ""):
    """Replace the transfer allowance by the stdout the process produced.

    The serialized request is gone; the response's raw, decoded and rendered
    copies are bounded by three copies of its actual stdout plus what the
    response estimate adds for rendering, plus the bounded diagnostic text.
    A finished exchange awaiting response handoff no longer holds a ceiling
    sized for output it
    did not produce.
    """
    diagnostic_cost = (
      _JSON_STRING_COST + len(diagnostic) * _json_text_width(diagnostic) if diagnostic else 0
    )
    self._hold("response", transfer=3 * len(stdout) + diagnostic_cost)

  def _decoded_cost(self, cost: int, *, direction: str = "request"):
    if cost > MAX_JSON_DECODE_COST:
      self._reject(direction, "decoded resource limit exceeded")
    self._hold(direction, **{direction: cost})

  async def decode(self, raw: bytes):
    return await self._decode(raw, direction="request")

  async def decode_response(self, raw: bytes):
    return await self._decode(raw, direction="response")

  async def _decode(self, raw: bytes, *, direction: str):
    """Scan, then materialize through the cheaper exactly charged Unicode copy.

    The scan keeps the text chunks it decodes. Joining them once costs their
    actual sizes plus the joined text; letting json.loads decode the bytes
    costs a buffer sized by the byte count, copied once per widening. Each
    chunk is reserved before it is created, the chosen copy before it is made,
    and the unused chunks are freed first. stdlib JSON parses either way.
    """
    if not raw and direction == "request":
      return None
    encoding = json.detect_encoding(raw)
    decoder = codecs.getincrementaldecoder(encoding)("surrogatepass")
    chunks = []
    held = 0
    in_string = False
    pending_escape = False
    string_length = 0
    string_width = 1
    string_ascii = True
    string_escaped = False
    string_content = 0
    string_text = 0
    string_chars = 0
    largest_part = 0
    text_length = 0
    text_width = 1
    text_bmp = False
    strings = lists = dicts = commas = members = 0
    number_work = 0
    number_length = 0
    number_shape = _JSON_INT
    number_signed = False
    number_exponent = _JSON_NO_EXPONENT
    # Whether the next chunk may start a value (the document's first, or one
    # after a separator and whitespace that ended the last chunk).
    value_pending = True
    # Invalid input's marks no valid document has more of (a closer with no
    # open container, '\\' outside a string): one unit each, so a run of
    # them is bounded like any other Python scan step.
    stray = 0
    depth = deepest = 0
    # Open containers, for whether a string is a list item (malformed input
    # may unbalance it; the stdlib parser stops there anyway).
    containers = []
    string_list_item = False
    cost = scanning = _JSON_DOCUMENT_COST
    chunk = mark = ""
    joining = widening = 0
    for start in range(0, len(raw), _JSON_SCAN_BYTES):
      final = start + _JSON_SCAN_BYTES >= len(raw)
      # Reserve what is held plus the next chunk's decode before creating it.
      # Yielding bounds uninterrupted scan work while the exchange lease owns
      # the raw bytes, and makes the scan cancellable without a worker thread.
      transient = _JSON_CHUNK_TRANSIENT_PER_BYTE * min(_JSON_SCAN_BYTES, len(raw) - start)
      scanning = max(scanning, _JSON_DOCUMENT_COST + held + transient)
      self._decoded_cost(max(cost, scanning), direction=direction)
      await asyncio.sleep(0)
      chunk = decoder.decode(raw[start:start + _JSON_SCAN_BYTES], final=final)
      chunks.append(chunk)
      held += sys.getsizeof(chunk) + _RETAINED_BLOCK_SLACK + 17
      text_length += len(chunk)
      # Measure Unicode width in C over bounded spans, not one Python
      # iteration per scalar character. A Unicode caption must not inflate
      # the separate ASCII base64 string sharing its final scan chunk.
      text_width = max(text_width, _json_text_width(chunk))
      text_bmp = text_bmp or text_width > 1 and _JSON_BMP_TEXT.search(chunk) is not None
      last = 0
      value = None
      if number_length:
        # A number token can continue across the chunk boundary.
        end = _JSON_NUMBER_TAIL.match(chunk).end()
        number_length += end
        number_shape = max(number_shape, _json_number_shape(chunk, 0, end))
        number_exponent = _json_exponent_state(chunk, 0, end, number_exponent)
        if end < len(chunk) or final:
          number_work += _json_number_work(number_length, number_shape, number_signed, number_exponent)
          number_length = 0
        last = end
      elif value_pending:
        value = _JSON_FIRST_VALUE.match(chunk)
        last = value.end()
      value_pending = False
      while True:
        if value is not None:
          # A value may start right after a separator (and whitespace).
          if value.start("number") != -1:
            index, end = value.span("number")
            shape = _json_number_shape(chunk, index, end)
            signed = chunk[index] == "-"
            exponent = (
              _JSON_NO_EXPONENT if shape != _JSON_EXPONENT
              else _json_exponent_state(chunk, index, end, _JSON_NO_EXPONENT)
            )
            if end == len(chunk) and not final:
              number_length = end - index
              number_shape = shape
              number_signed = signed
              number_exponent = exponent
            else:
              number_work += _json_number_work(end - index, shape, signed, exponent)
          elif value.start("pending") != -1:
            value_pending = True
          value = None
        if last >= len(chunk):
          break
        if in_string:
          # A final backslash can escape the first character of the next
          # incremental text chunk (also for UTF-16/32 inputs).
          if pending_escape:
            if chunk[last] == "u":
              string_width = 4
              string_ascii = False
            string_length += 1
            last += 1
            pending_escape = False
            if last == len(chunk):
              break
          end = _JSON_STRING_CONTENT.match(chunk, last).end()
          string_length += end - last
          string_width = max(string_width, _json_text_width(chunk, last, end))
          string_ascii = string_ascii and _JSON_NON_ASCII_TEXT.search(chunk, last, end) is None
          if chunk.find("\\", last, end) != -1:
            string_escaped = True
            if chunk.find("\\u", last, end) != -1:
              # A \u escape may decode to any scalar, astral included.
              string_width = 4
              string_ascii = False
          if end == len(chunk):
            break
          if chunk[end] == "\\":
            # Only an unpaired terminal backslash stops the content matcher.
            pending_escape = True
            string_escaped = True
            string_length += 1
          else:
            in_string = False
            string_content += _json_string_content(string_length, string_width, string_escaped)
            string_text += string_length * string_width
            string_chars += string_length + 2
            largest_part = max(largest_part, _json_part_peak(string_length + 2, string_width, string_ascii, string_list_item))
          last = end + 1
          continue
        mark = _JSON_RESOURCE_MARKS.search(chunk, last)
        if mark is None:
          break
        index, last = mark.span()
        char = chunk[index]
        if mark.lastindex is not None:
          value = mark
        if char == ",":
          commas += 1
        elif char == '"':
          in_string = True
          string_list_item = containers[-1:] == ["["]
          string_length = 0
          string_ascii = True
          string_width = 1
          string_escaped = False
          strings += 1
        elif char == "[" or char == "{":
          depth += 1
          if depth > MAX_JSON_DEPTH:
            self._reject(direction, "structural resource limit exceeded")
          deepest = max(deepest, depth)
          containers.append(char)
          if char == "[":
            lists += 1
          else:
            dicts += 1
        elif char == ":":
          members += 1
        elif char == "}" or char == "]":
          depth -= 1
          if containers:
            containers.pop()
          else:
            stray += 1
        else:
          stray += 1
      bare = text_length - string_chars - (string_length + 1 if in_string else 0)
      # Units only bound work done after the scan, so checking them per
      # bounded chunk still rejects before the stdlib parser runs.
      # A number still continuing into the next chunk is charged when it ends
      # (before any parse): its partial text does not yet show its class.
      units = (
        strings + lists + dicts + commas + members + number_work + stray
        + bare // _JSON_BARE_CHARS_PER_UNIT
      )
      if units > MAX_JSON_STRUCTURE:
        self._reject(direction, "structural resource limit exceeded")
      tree = (
        strings * _JSON_STRING_COST
        + lists * _JSON_LIST_COST + dicts * _JSON_DICT_COST
        + (lists + commas) * _JSON_ELEMENT_COST + members * _JSON_MEMBER_COST
        + bare * _JSON_BARE_CHAR_COST
      )
      building, built, part = string_content, string_text, largest_part
      if in_string:
        building += _json_string_content(string_length, string_width, string_escaped)
        built += string_length * string_width
        part = max(part, _json_part_peak(string_length + 2, string_width, string_ascii, string_list_item))
      text = text_length * text_width + _JSON_STRING_COST
      # Phases free their transients before the next starts. Unicode text is
      # made once: by joining the held chunks, or by stdlib decoding the bytes
      # into a byte-sized buffer copied per widening (ASCII -> UCS1 -> UCS2 ->
      # UCS4). Parsing builds strings beside that text; a response is then
      # rendered while only its built strings remain.
      widening = len(raw) * (text_width + (2 if text_width == 4 and text_bmp else 1))
      joining = held + text
      phases = [min(joining, widening), text + tree + building]
      if direction == "response":
        phases.append(tree + built + _json_render_excess(
          len(raw), encoding, text_length, bare, part, deepest,
        ))
      cost = max(scanning, _JSON_DOCUMENT_COST + max(phases))
      if cost > MAX_JSON_DECODE_COST:
        self._reject(direction, "decoded resource limit exceeded")
    self._decoded_cost(cost, direction=direction)
    # A match object keeps its chunk alive; drop every alias before copying.
    del chunk, mark, decoder
    if joining <= widening:
      text = "".join(chunks)
      del chunks
    else:
      del chunks
      text = raw
    # The parse is one uninterruptible call; never run it back to back with
    # the last scan chunk or the caller's next call (a render or retain walk).
    await asyncio.sleep(0)
    value = json.loads(text, parse_constant=_reject_json_constant)
    del text
    await asyncio.sleep(0)
    return value

  async def retain(self, envelope):
    """Charge the decoded envelope as the only request copy awaiting execution.

    This applies the same resource boundary to already-materialized
    tool/policy JSON. An HTTP body's raw bytes are released by then, so a
    queued exchange holds only its actual bounded envelope, not a transfer
    allowance. The walk counts the work units of the envelope's compact JSON
    text, the unit the lexical scan bounds, and the envelope itself is depth
    0, so a body nests exactly as it was sent. It is iterative and yields
    cooperatively, like the scan. A bounded scalar check needs no copy, so an
    oversize string fails before it waits; it also records the largest
    string part request encoding will hold (``admit_execution``).
    """
    marks = 0
    bare = 0
    number_work = 0
    largest_part = 0
    string_bytes = 0
    deepest = 0
    cost = _RETAINED_ENVELOPE_COST
    visited = 0

    def hold_walk(transient: int):
      # The envelope already exists; what the walk adds (its iterator stack,
      # one number's text, a sizing slice) is reserved before it is made, on
      # top of the estimate so far, and never lowers a held decode cost.
      needed = cost + _RETAINED_WALK_LEVEL_COST * len(pending) + transient
      if needed > self.request_cost:
        self._hold("request", request=needed + _RETAINED_WALK_HEADROOM)

    async def pace(steps: int):
      # Yield by work done, not values visited: one sized string slice is
      # worth _SIZING_SLICE_STEPS visited values.
      nonlocal visited
      visited += steps
      if visited >= _COOPERATIVE_STEP:
        visited = 0
        await asyncio.sleep(0)

    pending = []
    hold_walk(_RETAINED_WALK_HEADROOM)
    pending.append(iter((envelope,)))
    in_list = [False]
    while pending:
      item = next(pending[-1], _EXHAUSTED)
      if item is _EXHAUSTED:
        pending.pop()
        in_list.pop()
        continue
      await pace(1)
      hold_walk(_RETAINED_WALK_HEADROOM)
      children = None
      if isinstance(item, dict):
        marks += max(2 * len(item), 1)
        bare += 2 + max(2 * len(item) - 1, 0)
        children = itertools.chain.from_iterable(item.items())
      elif isinstance(item, (list, tuple)):
        marks += max(len(item), 1)
        bare += 2 + max(len(item) - 1, 0)
        children = iter(item)
      elif isinstance(item, str):
        marks += 1
        part, size = await _json_string_part(item, self.max_bytes, in_list[-1], hold_walk, pace)
        largest_part = max(largest_part, part)
        string_bytes += size
      elif item is True or item is None:
        bare += 4
      elif item is False:
        bare += 5
      elif isinstance(item, int):
        length = _json_int_length(item)
        bare += length
        number_work += _json_number_work(length, _JSON_INT, item < 0)
      elif isinstance(item, float):
        text = repr(item)
        bare += len(text)
        shape = _json_number_shape(text, 0, len(text))
        number_work += _json_number_work(
          len(text), shape, item < 0, _json_exponent_state(text, 0, len(text), _JSON_NO_EXPONENT),
        )
        del text
      cost += sys.getsizeof(item) + _RETAINED_BLOCK_SLACK * (1 if children is None else 2)
      depth = len(pending) - 1
      deepest = max(deepest, depth)
      units = marks + number_work + bare // _JSON_BARE_CHARS_PER_UNIT
      if units > MAX_JSON_STRUCTURE or (children is not None and depth > MAX_JSON_DEPTH):
        self._reject("request", "structural resource limit exceeded")
      if cost > MAX_JSON_DECODE_COST:
        self._reject("request", "decoded resource limit exceeded")
      if children is not None:
        pending.append(children)
        in_list.append(not isinstance(item, dict))
    self.largest_part = largest_part
    self.encoded_bytes = bare + string_bytes
    self.encoded_depth = deepest + 1
    # Encoding beyond three serialized copies is this request's own decoded
    # resource: refuse it now, while waiting can never make it fit, rather
    # than answer a retryable busy at execution.
    encoding = _json_encoding_peak(
      min(self.max_bytes, self.encoded_bytes), largest_part, self.encoded_depth,
    )
    if cost + max(0, encoding - 3 * self.max_bytes) > MAX_JSON_DECODE_COST:
      self._reject("request", "decoded resource limit exceeded")
    self._hold("request", transfer=0, request=cost)


_JSON_INT, _JSON_DECIMAL, _JSON_EXPONENT = 0, 1, 2


def _json_number_shape(text: str, start: int, end: int) -> int:
  if text.find("e", start, end) != -1 or text.find("E", start, end) != -1:
    return _JSON_EXPONENT
  return _JSON_DECIMAL if text.find(".", start, end) != -1 else _JSON_INT


# Bounded classifier state for an exponent, which a chunk boundary can split
# anywhere: no marker yet, right after it (a sign may follow), or its field
# of digits after the sign, encoded as _JSON_EXPONENT_FIELD + 100 * count +
# value. The count is capped at 3 and only a field of at most two characters
# keeps its value; leading zeros count as field characters.
_JSON_NO_EXPONENT, _JSON_EXPONENT_SIGN, _JSON_EXPONENT_FIELD = -1, 0, 1


def _json_exponent_state(text: str, start: int, end: int, state: int) -> int:
  """Advance ``state`` over one span of a number token: native searches and at
  most two Python character steps, never one per character."""
  if state == _JSON_NO_EXPONENT:
    marker = text.find("e", start, end)
    upper = text.find("E", start, end)
    if upper != -1 and (marker == -1 or upper < marker):
      marker = upper
    if marker == -1:
      return state
    start, state = marker + 1, _JSON_EXPONENT_SIGN
  if state == _JSON_EXPONENT_SIGN and start < end:
    if text[start] in "+-":
      start += 1
    state = _JSON_EXPONENT_FIELD
  if state >= _JSON_EXPONENT_FIELD and start < end:
    count, value = divmod(state - _JSON_EXPONENT_FIELD, 100)
    for char in text[start:min(end, start + max(0, 2 - count))]:
      value = value * 10 + int(char) if char.isdigit() else 100
    count = min(3, count + end - start)
    state = _JSON_EXPONENT_FIELD + 100 * count + (value if count <= 2 and value < 100 else 0)
  return state


def _json_exponent_value(state: int) -> int | None:
  """The exponent's value when its field is one or two digits, else None."""
  if state <= _JSON_EXPONENT_FIELD:
    return None
  count, value = divmod(state - _JSON_EXPONENT_FIELD, 100)
  return value if 1 <= count <= 2 else None


def _json_number_work(
  length: int, shape: int, signed: bool = False, exponent: int = _JSON_NO_EXPONENT,
) -> float:
  """Parse work of one number token beyond its mark and characters, in units.

  Calibrated in thread CPU time against the per-element cost of a small int
  (an int of at most 18 digits, the unit itself, so at par within measurement
  noise rather than with a margin). Every class with extra work was checked
  per token and with documents filled to the unit cap of that class alone,
  whose parse must take at most the small-int cap document's time over 1.2.
  A decimal without an exponent of at most 16 characters has at most 15
  digits and a decimal exponent within 15, so it takes the exact double fast
  path (measured under 1.5 units). Up to 24 characters a correction loop runs
  on small big ints (under 8). A token of at most 24 characters with a one-
  or two-digit exponent field (after its sign, leading zeros included) costs
  3 plus half its length up to exponent 22 and 8 plus half its length up to
  99 (worst near-halfway mantissas measured 12.9 units at 24 characters, and
  cap documents at most 0.52 of the small-int one). Any other exponent, or a
  longer decimal, can force big-int comparison near a halfway point
  ("2.2e-308" 13.3, "2.2250738585072014e-300" 15.5, and roughly 1.2 units per
  character past 40). Ints past 18 digits leave the machine-word path: up to
  90 digits they measured at most 6 units, under a charge growing a sixteenth
  of a unit per digit from 1.44 (cap documents at most 0.78), and they grow
  quadratically toward 4300 digits (the stdlib's limit, under 1,300 units).
  An int is charged by its digits: a ``signed`` one's minus sign changes
  neither path (measured within noise), so it costs only its character,
  wherever a chunk boundary or materialization puts it. An exponent token
  whose ``exponent`` state is unknown gets the costliest charge. Invalid
  short fields use the cheap tier: stdlib stops at the first syntax error,
  after at most the mantissa and one exponent digit. Fractions are exact
  sixteenths, summed per document.
  """
  if not length:
    return 0
  if shape == _JSON_INT:
    digits = length - signed
    if digits <= _JSON_LINEAR_DIGITS:
      return 0
    return (digits + 4) / 16 + digits * digits // 7000
  if shape == _JSON_DECIMAL and length <= 16:
    return 2 + length // 8
  if shape == _JSON_DECIMAL and length <= 24:
    return 2 + length // 2
  value = _json_exponent_value(exponent) if shape == _JSON_EXPONENT and length <= 24 else None
  if value is not None:
    return (3 if value <= 22 else 8) + length // 2
  return length + 12 + max(0, length - 24)


# Ints at most this long are machine words with no work beyond their characters.
_JSON_LINEAR_DIGITS = 18


def _json_int_length(value: int) -> int:
  """Compact decimal length of an int; past 64 bits an upper bound (at most
  two over), because converting a big int is itself quadratic work."""
  if value.bit_length() <= 64:
    return len(str(value))
  # log10(2) < 0.30103; one more for rounding, one for a sign.
  return value.bit_length() * 30103 // 100000 + 2


def _json_part_peak(chars: int, width: int, ascii_only: bool, list_item: bool) -> int:
  """What encoding one string scalar briefly holds: its quoted escaped part
  of ``chars`` characters and the UTF-8 encoder's buffer, which for non-ASCII
  text is sized for the widest character of its kind. A list item's part is
  the separator concatenated to the escaped form, which is freed before the
  part is encoded: two parts coexist, or one part and its UTF-8 buffer."""
  part = chars * width + _JSON_STRING_COST
  utf8 = chars if ascii_only else chars * min(width + 1, 4)
  return max(2 * part, part + utf8) if list_item else part + utf8


def _json_encoding_peak(output_bytes: int, largest_part: int, depth: int) -> int:
  """Encoding into a growing buffer: its old and 9/8 larger new block, the
  part being written (``_json_part_peak``), and the encoder's generators."""
  return (
    output_bytes + output_bytes * 9 // 8 + largest_part
    + _JSON_ENCODER_COST + _JSON_ENCODER_LEVEL_COST * depth
  )


def _json_render_excess(
  raw_bytes: int, encoding: str, text_length: int, bare: int, largest_part: int, depth: int,
) -> int:
  """What rendering a decoded response holds beyond its three stdout copies.

  Rendering never adds characters except to floats, up to 3.5 per input
  character ("1e15" -> "1000000000000000.0"); escapes and whitespace only
  shrink, so UTF-8 input renders to no more bytes than it had. Other
  encodings are bounded by four bytes per character.
  """
  text_bytes = raw_bytes if encoding.startswith("utf-8") else 4 * text_length
  output = text_bytes + bare * 7 // 2
  return max(0, _json_encoding_peak(output, largest_part, depth) - 3 * raw_bytes)


def _json_string_content(length: int, width: int, escaped: bool) -> int:
  """Bytes one decoded string can hold, beyond its counted header.

  An unescaped string is an exact substring. An escaped one is built by a
  writer that overallocates a quarter and coexists with its final or widened
  copy (2.25 times, plus a second header).
  """
  if escaped:
    return _JSON_STRING_COST + length * width * 9 // 4
  return length * width


def _reject_json_constant(value: str):
  raise ValueError(f"invalid JSON constant: {value}")


def service_contract(app, *, access: str) -> dict:
  contract = app.capability_contract if isinstance(app.capability_contract, dict) else {}
  service = contract.get("service")
  if not isinstance(service, dict) or service.get("protocol") != "json-v1":
    raise HTTPException(404, "App service not found.")
  accepted_access = service.get("access", "self")
  if access == "public" and accepted_access != "public":
    raise HTTPException(404, "Public app service not found.")
  if access == "apps" and accepted_access not in {"apps", "public"}:
    raise HTTPException(403, "This app service is private.")
  entry = service.get("entry")
  if not isinstance(entry, str) or not entry.endswith(".py"):
    raise ServiceBoundaryError("invalid_declaration", 503, "Accepted app service declaration is invalid.")
  _accepted_transfer_limits(service)
  return service


def public_requests_per_minute(service: dict) -> int:
  """One anonymous caller's per-minute allowance for this accepted public service."""
  from app.manifest_contract import (
    PUBLIC_SERVICE_REQUESTS_PER_MINUTE,
    PUBLIC_SERVICE_REQUESTS_PER_MINUTE_MAX,
  )
  value = service.get("public_requests_per_minute", PUBLIC_SERVICE_REQUESTS_PER_MINUTE)
  if isinstance(value, bool) or not isinstance(value, int):
    return PUBLIC_SERVICE_REQUESTS_PER_MINUTE
  return max(
    PUBLIC_SERVICE_REQUESTS_PER_MINUTE,
    min(PUBLIC_SERVICE_REQUESTS_PER_MINUTE_MAX, value),
  )


def service_max_bytes(service: dict) -> int:
  """The one reviewed serialized request/response ceiling, including old grants."""
  ceiling = accepted_service_max_bytes(service)
  if ceiling is None:
    raise ServiceBoundaryError("invalid_declaration", 503, "Accepted app service transfer limit is invalid.")
  return ceiling


def _accepted_transfer_limits(service: dict) -> tuple[int, dict[tuple[str, str], int]]:
  """The accepted whole ceiling and its per-route ceilings by (method, canonical path).

  An accepted contract is not trusted to be well formed: one the manifest
  contract could not have produced disables the service rather than silently
  falling back to its wider whole grant.
  """
  ceiling = service_max_bytes(service)
  routes = accepted_service_route_limits(service.get("route_limits", []), ceiling)
  if routes is None:
    raise ServiceBoundaryError("invalid_declaration", 503, "Accepted app service route limits are invalid.")
  return ceiling, routes


def exchange_max_bytes(service: dict, method, path) -> int:
  """The serialized per-direction ceiling for one exchange with an accepted service.

  The single route-selection seam for HTTP, tool and policy exchanges. An
  exact reviewed (method, canonical path) ceiling narrows the service's whole
  grant; any other exchange, including one whose envelope names no route,
  keeps the whole accepted ceiling. There is no prefix matching and no further
  decoding: `service_route_key` is the only normalization.
  """
  ceiling, routes = _accepted_transfer_limits(service)
  if not isinstance(method, str) or not isinstance(path, str):
    return ceiling
  return routes.get((method, service_route_key(path)), ceiling)


_INVALID_REQUEST_DATA = "App service request contains invalid JSON data."


async def _json_string_part(
  value: str, max_bytes: int, list_item: bool, reserve_sizing, pace,
) -> tuple[int, int]:
  """Reject a scalar before JSONEncoder can materialize its entire escaped form.

  Returns what encoding it will briefly hold (``_json_part_peak``) and its
  quoted UTF-8 size. Six UTF-8 bytes per character is a safe upper bound for
  one JSON string character with ensure_ascii=False; a longer string is
  measured exactly in bounded slices, whose copies ``reserve_sizing`` admits
  before the first is made, yielding through ``pace`` as slices add up. A
  string with no UTF-8 form is the 400 ``_serialize_request`` gives it.
  """
  if len(value) > max_bytes:
    raise HTTPException(413, "App service request is too large.")
  # The str header is 56 bytes when not ASCII; a cached UTF-8 copy only
  # overstates the width, which the clamp keeps a valid kind bound.
  ascii_only = value.isascii()
  width = 1 if ascii_only else min(4, (sys.getsizeof(value) - 56) // (len(value) + 1))
  chars = len(value) * 6 + 2
  if chars <= max_bytes and len(value) <= _JSON_SCAN_BYTES:
    return _json_part_peak(chars, width, ascii_only, list_item), chars
  # One slice, its escaped form (up to six characters each) and that form's
  # UTF-8 encoder buffer exist at once.
  escaped = 6 * _JSON_SCAN_BYTES + 2
  reserve_sizing(
    _JSON_SCAN_BYTES * width + escaped * width + escaped * min(width + 1, 4)
    + 3 * _JSON_STRING_COST,
  )
  chars = size = 2  # JSON quotes
  for start in range(0, len(value), _JSON_SCAN_BYTES):
    # The standard encoder's escaping, applied in bounded slices, has the
    # same result for a string regardless of where it is split.
    part = json.encoder.encode_basestring(value[start:start + _JSON_SCAN_BYTES])
    chars += len(part) - 2
    try:
      encoded = len(part.encode("utf-8"))
    except UnicodeEncodeError:
      # A lone surrogate has no UTF-8 form: the same invalid request
      # serialization refuses for a short string. The error is not chained,
      # since it would keep this slice alive after its reservation ends.
      encoded = None
    del part
    if encoded is None:
      raise HTTPException(400, _INVALID_REQUEST_DATA)
    size += encoded - 2
    if size > max_bytes:
      raise HTTPException(413, "App service request is too large.")
    await pace(_SIZING_SLICE_STEPS)
  return _json_part_peak(chars, width, ascii_only, list_item), size


async def render_json(value) -> bytes:
  """Compact UTF-8 JSON, byte-identical to Starlette's JSONResponse.

  The pure-Python encoder holds one part at a time, at that part's own
  width, rather than the whole text at its widest character's width, and
  yields cooperatively between bounded runs of parts. Callers reserve
  ``_json_encoding_peak`` for it before calling. Encoder errors (a
  non-finite float, a lone surrogate) propagate unchanged.
  """
  buffer = io.BytesIO()
  parts = json.JSONEncoder(
    ensure_ascii=False, allow_nan=False, separators=(",", ":"),
  ).iterencode(value)
  for count, part in enumerate(parts, 1):
    if count % _COOPERATIVE_STEP == 0:
      await asyncio.sleep(0)
    buffer.write(part.encode("utf-8"))
  return buffer.getvalue()


async def _serialize_request(request_envelope: dict, max_bytes: int) -> bytes:
  """Encode the stdin request without building more than ``max_bytes`` of it.

  Tool and policy calls bypass HTTP body reads, so this bounds their
  serialization too rather than building an arbitrarily large request first.
  The pure-Python encoder yields cooperatively between bounded runs of parts.
  """
  try:
    buffer = io.BytesIO()
    size = 0
    parts = json.JSONEncoder(
      ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).iterencode(request_envelope)
    for count, part in enumerate(parts, 1):
      if count % _COOPERATIVE_STEP == 0:
        await asyncio.sleep(0)
      encoded = part.encode("utf-8")
      size += len(encoded)
      if size > max_bytes:
        raise HTTPException(413, "App service request is too large.")
      buffer.write(encoded)
    return buffer.getvalue()
  except (TypeError, ValueError, RecursionError) as exc:
    raise HTTPException(400, _INVALID_REQUEST_DATA) from exc


def request_actor(db, principal, caller=None) -> dict:
  """Who is calling an app's service, as the request's `actor` states it.

  Trusted helpers get "write"; an orphaned or retired delegated identity gets
  "read" defensively so app services do not mistake it for an active helper.
  """
  access = "write"
  if principal.delegation_id is not None:
    delegation = db.get(models.Delegation, principal.delegation_id)
    access = (
      "write" if delegation is not None
      and delegation.scope == "write"
      and delegation.interrupted_at is None
      else "read"
    )
  actor = {
    "scope": principal.scope,
    "app_id": principal.app_id,
    "app_slug": caller.slug if caller is not None else None,
    "delegated": principal.delegation_id is not None,
    "access": access,
  }
  if principal.browser is not None:
    actor.update(browser_grant_id=principal.browser.grant_id,
                 browser_session_id=principal.browser.session_id)
  return actor


def service_entry(app, service: dict) -> Path:
  try:
    root = runtime_root(app)
  except AppliedRuntimeUnavailable as exc:
    raise ServiceBoundaryError("runtime_unavailable", 503, str(exc)) from exc
  entry = root / service["entry"]
  try:
    if entry.is_symlink() or not entry.is_file() or entry.parent != root:
      raise ServiceBoundaryError("entry_unavailable", 503, "Accepted app service entry is unavailable.")
  except OSError as exc:
    raise ServiceBoundaryError(
      "entry_unavailable", 503, "Accepted app service entry is unavailable.",
    ) from exc
  return entry


def service_python_env(app, entry: Path) -> Path | None:
  """The accepted service's own Python env, None when it declares none."""
  try:
    return app_python_env.resolve_env(get_settings().data_dir, app.id, entry.parent)
  except app_python_env.PythonEnvUnavailable as exc:
    log.warning("App service %s cannot start: %s", app.slug, exc)
    raise ServiceBoundaryError("python_env_unavailable", 503, str(exc)) from exc


def service_environment(app, owner, service: dict, *, public: bool, browser: BrowserLineage | None = None) -> dict[str, str]:
  """The environment of one invocation; its APP_TOKEN's authority follows the caller.

  A public invocation acts for an anonymous visitor, so its token has the narrow
  public-service scope (auth.create_app_token), which cannot start or drive the
  owner's agents. This bounds what the platform does on the owner's behalf, not
  file access: a service is owner-installed reviewed code.
  """
  allowed = {"PATH", "LANG", "LC_ALL", "TZ", "HOME"}
  if service.get("access", "self") != "public":
    # A service reached only through an authenticated caller may run a provider
    # CLI (Memory's recall navigator does), so it receives the same credential
    # locations as its scheduled job.
    allowed |= {"DATA_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME"}
  env = {key: value for key, value in os.environ.items() if key in allowed}
  settings = get_settings()
  env.update({
    "APP_ID": str(app.id),
    "APP_SLUG": str(app.slug),
    "APP_STORAGE_DIR": str(Path(settings.data_dir) / "apps" / str(app.id)),
    "API_BASE_URL": settings.api_base_url,
    "INSTANCE_DOMAIN": settings.domain,
    "INSTANCE_ORIGIN": settings.frontend_origin.rstrip("/"),
    "APP_TOKEN": auth.create_app_token(
      app.id,
      owner.username,
      owner.token_epoch,
      app_nonce=app.token_nonce,
      expires_delta=timedelta(minutes=5),
      service="public" if public else "private",
      browser=browser,
    ),
  })
  return env


_read_bounded = service_preload.read_bounded


async def _write_request(stream, body: bytes) -> None:
  # A pipe transport copies whatever one write leaves unsent; a single write of
  # the whole body would duplicate it while a service answers before reading.
  view = memoryview(body)
  for start in range(0, len(view), _STDIO_SLICE):
    stream.write(view[start:start + _STDIO_SLICE])
    await stream.drain()
  del view
  stream.close()
  await stream.wait_closed()


async def _stop_process(process, *tasks) -> None:
  try:
    os.killpg(process.pid, signal.SIGKILL)
  except ProcessLookupError:
    pass
  for task in tasks:
    task.cancel()
  await asyncio.gather(*tasks, return_exceptions=True)

  async def discard(reader):
    if reader is not None:
      while await reader.read(_STDIO_SLICE):
        pass

  # A rejected output can leave a paused pipe with unread bytes. wait() alone
  # cannot reap its transport until those bytes drain, even after SIGKILL.
  # Discard instead of communicate(), which retains another output copy.
  await asyncio.gather(
    process.wait(), discard(process.stdout), discard(process.stderr),
  )


async def _settled_stop(process, *tasks) -> None:
  """Stop a running child even if this request is cancelled meanwhile.

  SIGKILL is sent at once, but reaping the child, cancelling its pipe readers
  and, after that, releasing the runtime pin must not be cut short.
  """
  await settled.settle(_stop_process(process, *tasks), what="App service stop")


async def _stop_spawned(spawn: asyncio.Task) -> None:
  """Stop a child whose request was cancelled; the cancellation still wins."""
  try:
    process = await spawn
  except OSError:
    return
  except Exception:
    log.exception("An app service whose request was cancelled failed to start")
    return
  try:
    await _stop_process(process)
  except Exception:
    log.exception("Could not stop an app service whose request was cancelled")


def _response_headers(value, *, public: bool) -> dict[str, str]:
  if value is None:
    return {}
  if not isinstance(value, dict) or len(value) > 16:
    raise ValueError("response headers must be a bounded object")
  headers: dict[str, str] = {}
  for name, raw in value.items():
    lower = name.lower() if isinstance(name, str) else ""
    if lower not in _SERVICE_RESPONSE_HEADERS:
      continue
    if not isinstance(raw, str) or len(raw) > 4096 or "\r" in raw or "\n" in raw:
      raise ValueError("response contains an invalid header")
    if lower == "cache-control" and not public:
      directives = {part.split("=", 1)[0].strip().lower() for part in raw.split(",")}
      if directives & _SHARED_CACHE_DIRECTIVES and not directives & {"private", "no-store"}:
        # An owner-authenticated response must never be stored by a shared cache.
        continue
    headers[name] = raw
  return headers


async def _run_spawned(
  python: str, entry: Path, environment: dict[str, str], request_bytes: bytes,
  timeout_seconds: float, max_stdout: int = MAX_RESPONSE_BYTES,
) -> tuple[bytes, bytes, int]:
  """Run one request in a fresh interpreter; return (stdout, stderr, exit code)."""
  try:
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
      python,
      str(entry),
      stdin=asyncio.subprocess.PIPE,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
      cwd=str(entry.parent),
      env=environment,
      start_new_session=True,
      limit=_STDIO_SLICE,
    ))
    try:
      process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
      # Admission can be cancelled after the OS child exists but before
      # asyncio returns its handle. Stop that child before releasing the
      # runtime pin, even if shutdown cancels this request again.
      await settled.settle(_stop_spawned(spawn))
      raise
  except OSError as exc:
    raise ServiceBoundaryError("start_failed", 502, "App service could not start.") from exc
  assert process.stdin is not None
  assert process.stdout is not None
  assert process.stderr is not None
  stdout_task = asyncio.create_task(_read_bounded(process.stdout, max_stdout))
  stderr_task = asyncio.create_task(_read_bounded(process.stderr, MAX_ERROR_BYTES))
  write_task = asyncio.create_task(_write_request(process.stdin, request_bytes))
  try:
    _written, stdout, stderr, returncode = await asyncio.wait_for(
      asyncio.gather(write_task, stdout_task, stderr_task, process.wait()),
      timeout=timeout_seconds,
    )
  except (TimeoutError, ValueError) as exc:
    await _settled_stop(process, write_task, stdout_task, stderr_task)
    raise ServiceBoundaryError("execution_limit", 503, "App service exceeded its execution limits.")
  except OSError as exc:
    await _settled_stop(process, write_task, stdout_task, stderr_task)
    raise ServiceBoundaryError(
      "request_not_accepted", 502, "App service failed before accepting its request.",
    ) from exc
  except asyncio.CancelledError:
    await _settled_stop(process, write_task, stdout_task, stderr_task)
    raise
  try:
    os.killpg(process.pid, signal.SIGKILL)
  except ProcessLookupError:
    pass
  return stdout, stderr, returncode


# Invocation tasks, not a second job queue: existing subprocess cancellation owns
# process cleanup. A revoked grant cancels only its attributed in-flight calls.
_browser_calls: dict[str, set[asyncio.Task]] = {}


def _actor_browser(actor: dict) -> BrowserLineage | None:
  return BrowserLineage.of(actor.get("browser_grant_id"), actor.get("browser_session_id"))


def _validate_browser_call(owner, browser: BrowserLineage | None):
  if browser is not None:
    from app.database import SessionLocal
    with SessionLocal() as db:
      require_live(db, browser, owner.id)


def browser_grant_has_active_calls(grant_id: str) -> bool:
  """Whether attributed invocation cleanup is still outstanding."""
  return any(not task.done() for task in _browser_calls.get(grant_id, ()))


async def cancel_browser_grant_calls(grant_id: str) -> None:
  tasks = tuple(_browser_calls.get(grant_id, ()))
  for task in tasks:
    if task is not asyncio.current_task():
      task.cancel()
  await asyncio.gather(*(task for task in tasks if task is not asyncio.current_task()), return_exceptions=True)


async def _invoke_service(
  app, owner, request_envelope: dict, *,
  timeout_seconds: float = SERVICE_TIMEOUT_SECONDS,
  lane: str | None = None,
  admission: ServiceExchangeAdmission | None = None,
) -> tuple[int, object, dict[str, str], str | None]:
  service = service_contract(app, access="public" if request_envelope.get("public") is True else "self")
  # HTTP callers admit their body read under the same route ceiling.
  boundary = (
    contextlib.nullcontext(admission) if admission is not None
    else ServiceExchangeAdmission(exchange_max_bytes(
      service, request_envelope.get("method"), request_envelope.get("path"),
    ))
  )
  # Attribution includes cooperative response scanning and, for HTTP callers
  # holding their own admission, rendering: revocation must still cancel a
  # result that has not been handed off. The admission releases it.
  with boundary as admitted:
    return await _invoke_admitted_service(
      app, owner, request_envelope, timeout_seconds=timeout_seconds, lane=lane,
      admission=admitted,
    )


async def invoke_service(
  app, owner, request_envelope: dict, *,
  timeout_seconds: float = SERVICE_TIMEOUT_SECONDS,
  lane: str | None = None,
  admission: ServiceExchangeAdmission | None = None,
) -> tuple[int, object, dict[str, str], str | None]:
  # Traces record only the app slug and platform-authored categories, never
  # request values or app-supplied text.
  tracing.annotate(None, {"mobius.app.slug": app.slug})
  try:
    result = await _invoke_service(
      app, owner, request_envelope, timeout_seconds=timeout_seconds, lane=lane,
      admission=admission,
    )
  except ServiceBoundaryError as exc:
    tracing.annotate(None, {"mobius.service.error_type": exc.category})
    raise
  if result[0] >= 500:
    tracing.annotate(None, {"mobius.service.error_type": "app_http_error"})
  return result


async def _invoke_admitted_service(
  app, owner, request_envelope: dict, *,
  timeout_seconds: float = SERVICE_TIMEOUT_SECONDS,
  lane: str | None = None,
  admission: ServiceExchangeAdmission,
) -> tuple[int, object, dict[str, str], str | None]:
  public = request_envelope.get("public") is True
  service = service_contract(app, access="public" if public else "self")
  # One ceiling bounds both directions: request serialization, the stdout of
  # either execution backend, and the decoded binary response. It never
  # exceeds what admission reserved for.
  max_bytes = min(admission.max_bytes, exchange_max_bytes(
    service, request_envelope.get("method"), request_envelope.get("path"),
  ))
  # A service owns its persistence semantics. Serialize one app's private
  # requests so simple file-backed services do not need a platform-specific
  # lock API, and serialize its public requests on a separate lane. A private
  # federation write may synchronously cause the peer to call this instance's
  # public service (for example, to verify the sender's identity). Sharing one
  # lane for both directions deadlocks that callback behind the write waiting
  # for it. Public services still own their own file/SQLite locking where the
  # two lanes can touch the same state.
  #
  # Agent tool calls use a third lane that is not serialized per app: one
  # agent's long tool call (a Memory search can take minutes) must not stall
  # the app's own screen or another chat's call. A tool owns its concurrency,
  # as a public service already must.
  if lane is None:
    lane = "public" if public else "private"
  slot = (
    contextlib.nullcontext() if lane == "tools"
    else _app_slots.setdefault((app.id, lane), asyncio.Semaphore(1))
  )
  # Backlog for one app must not reserve all platform execution capacity while
  # waiting for that app's serialized request. Count only executable requests.
  # Queued requests already own an accepted revision. Pin before admission so
  # pruning or a migration drain cannot overlook a request waiting to run.
  browser = _actor_browser(request_envelope.get("actor") or {})
  _validate_browser_call(owner, browser)
  grant_id = browser.grant_id if browser is not None else None
  pin = hold_runtime(app.id)
  if grant_id is not None:
    admission.attribute(grant_id)
  try:
    # The envelope, with its metadata, is the only request copy retained
    # while queued; non-HTTP callers get the same structural boundary. Its
    # walk yields, so it runs pinned and attributed to a revocable grant.
    await admission.retain(request_envelope)
    async with slot, _global_slots[lane]:
      _validate_browser_call(owner, browser)
      # Waiting held only the retained envelope. The whole transfer allowance
      # is reserved here, before serialization or any process start, so
      # exhaustion is a side-effect-free retryable 503.
      admission.admit_execution(max_bytes)
      request_bytes = await _serialize_request(request_envelope, max_bytes)
      entry = service_entry(app, service)
      python_env = service_python_env(app, entry)
      python = app_python_env.python_for(python_env)
      # The preload host and its request children inherit this PATH too.
      environment = app_python_env.activated_environment(
        service_environment(
          app, owner, service, public=public,
          browser=browser,
        ), python_env,
      )
      outcome = None
      host = service_preload.ready_host(app, python, entry, environment)
      if host is not None:
        try:
          outcome = await service_preload.run(
            host, environment, request_bytes, timeout_seconds=timeout_seconds,
            max_stdout=max_bytes, max_stderr=MAX_ERROR_BYTES,
          )
        except service_preload.PreloadUnavailable:
          pass
        except (TimeoutError, ValueError) as exc:
          raise ServiceBoundaryError(
            "execution_limit", 503, "App service exceeded its execution limits.",
          ) from exc
        except OSError as exc:
          raise ServiceBoundaryError(
            "request_not_accepted", 502, "App service failed before accepting its request.",
          ) from exc
      if outcome is None:
        outcome = await _run_spawned(
          python, entry, environment, request_bytes, timeout_seconds, max_bytes,
        )
      del request_bytes
      stdout, stderr, returncode = outcome
      if returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
        log.warning("App service %s failed: %s", app.slug, detail or "no diagnostics")
        raise ServiceBoundaryError("process_failed", 502, "App service failed.")
      # Retain only the bounded text needed if the JSON reports a handled
      # failure; decode its raw stderr while execution headroom is still held.
      diagnostic = stderr.decode("utf-8", errors="replace").strip()[-1000:]
      del stderr, outcome
      admission.admit_output(stdout, diagnostic)
  finally:
    pin.close()
  try:
    response = await admission.decode_response(stdout)
  except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
    raise ServiceBoundaryError("invalid_json", 502, "App service returned invalid JSON.") from exc
  _validate_browser_call(owner, browser)
  if not isinstance(response, dict) or set(response) - {
    "status", "body", "body_base64", "headers", "media_type",
  }:
    raise ServiceBoundaryError("invalid_envelope", 502, "App service returned an invalid response envelope.")
  status = response.get("status", 200)
  if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
    raise ServiceBoundaryError("invalid_status", 502, "App service returned an invalid status.")
  if status >= 500 and diagnostic:
    # A handled app failure still exits its adapter successfully, so preserve
    # its bounded diagnostics without exposing them in the HTTP response.
    log.warning("App service %s returned %d: %s", app.slug, status, diagnostic)
  try:
    headers = _response_headers(
      response.get("headers"), public=bool(request_envelope.get("public")),
    )
  except ValueError as exc:
    raise ServiceBoundaryError("invalid_headers", 502, str(exc)) from exc
  encoded = response.get("body_base64")
  media_type = response.get("media_type")
  if encoded is not None:
    if response.get("body") is not None or not isinstance(encoded, str):
      raise ServiceBoundaryError(
        "invalid_binary_response", 502, "App service returned an invalid binary response.",
      )
    if not isinstance(media_type, str) or _MEDIA_TYPE.fullmatch(media_type) is None:
      raise ServiceBoundaryError("invalid_media_type", 502, "App service returned an invalid media type.")
    try:
      body = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
      raise ServiceBoundaryError(
        "invalid_binary_data", 502, "App service returned invalid binary data.",
      ) from exc
    if len(body) > max_bytes:
      raise ServiceBoundaryError("response_limit", 502, "App service returned too much binary data.")
    return status, body, headers, media_type
  if media_type is not None:
    raise ServiceBoundaryError("invalid_media_type", 502, "App service returned an invalid media type.")
  return status, response.get("body"), headers, None


async def invoke_policy(app, owner, path: str, body: dict) -> dict:
  """Invoke one authenticated app-owned policy and normalize its error shape."""
  status, result, _headers, media_type = await invoke_service(app, owner, {
    "schema": 1,
    "method": "POST",
    "path": path,
    "query": {},
    "headers": {},
    "body": body,
    "public": False,
    "actor": {"scope": "platform"},
  })
  if media_type is not None:
    raise HTTPException(502, "App policy returned binary data.")
  if status >= 400:
    detail = result.get("detail") if isinstance(result, dict) else None
    raise HTTPException(status, detail or "App policy rejected the request.")
  if not isinstance(result, dict):
    raise HTTPException(502, "App policy returned an invalid result.")
  return result
