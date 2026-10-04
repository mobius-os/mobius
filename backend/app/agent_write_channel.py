"""Explicit, provider-neutral assistant write frames.

Only attributed, authoritative message-item snapshots authorize an intent.
Provisional deltas are filtered for presentation, never executed. Item completion
can precede further model work; the protocol adds no tool reply or model call.
The chat owner must durably admit intents before acknowledge(), and fence the
channel before releasing the exact run. Existing tool handlers retain authority.

This module is not a tool or a free-text command recognizer. Only complete,
nonce-scoped protocol lines have meaning. The nonce routes bytes, not permission.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import math
import re


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True, init=False)
class WriteIntent:
    id: str
    tool: str
    _arguments_json: str

    def __init__(self, id: str, tool: str, arguments: dict):
        object.__setattr__(self, 'id', id)
        object.__setattr__(self, 'tool', tool)
        object.__setattr__(self, '_arguments_json', json.dumps(arguments, sort_keys=True, allow_nan=False))

    @property
    def arguments(self):
        # No mutable payload is shared with the caller or replay comparator.
        return json.loads(self._arguments_json)


def _object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ProtocolError("Duplicate JSON field")
        obj[key] = value
    return obj


def _nonfinite(_):
    raise ProtocolError("Non-finite JSON number")


def _float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ProtocolError("Non-finite JSON number")
    return parsed


def decode(payload: str) -> WriteIntent:
    try:
        value = json.loads(payload, object_pairs_hook=_object, parse_constant=_nonfinite, parse_float=_float)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError("Invalid write JSON") from exc
    if not isinstance(value, dict) or set(value) != {"id", "tool", "arguments"}:
        raise ProtocolError("A write needs exactly id, tool, arguments")
    if value.get("id") in (".", "..") or any(not isinstance(value[k], str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", value[k])
           for k in ("id", "tool")):
        raise ProtocolError("Invalid write id or tool name")
    if not isinstance(value["arguments"], dict):
        raise ProtocolError("Write arguments must be an object")
    return WriteIntent(**value)


class FrameDecoder:
    """Incremental text/command separation, including delimiters split at any byte.

    Only the exact turn marker is special. Markdown fences, quoted JSON, tool
    output, reasoning, and other turns are never searched for plausible commands.
    The marker is a routing nonce, NOT a replacement for run authorization.
    """
    def __init__(self, nonce: str, limit: int = 65536, *, validate=True):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", nonce):
            raise ValueError("Invalid frame nonce")
        self.open = f"\n<MOBIUS_WRITE {nonce}>\n"
        self.close = "\n</MOBIUS_WRITE>\n"
        self.limit = limit
        self.validate = validate
        self.frames_seen = 0
        self.buffer = ""
        self.at_start = True
        self.private = False
        self.writes: list[WriteIntent] = []
        self.fence: tuple[str, int] | None = None
        self.line_phase = "indent"
        self.line_indent = 0
        self.line_char = ""
        self.line_run = 0

    def _advance_fence(self, segment: str, *, eligible: bool) -> tuple[bool, bool]:
        """Recognize fence lines with constant state, including split delimiters."""
        opened = closed = False
        for char in segment:
            if char == "\n":
                valid = eligible and self.line_run >= (self.fence[1] if self.fence else 3)
                valid = valid and self.line_phase in {"run", "tail"}
                if valid:
                    if self.fence:
                        self.fence = None
                        closed = True
                    else:
                        self.fence = (self.line_char, self.line_run)
                        opened = True
                self.line_phase = "indent"
                self.line_indent = 0
                self.line_char = ""
                self.line_run = 0
                continue
            if not eligible or self.line_phase == "other":
                self.line_phase = "other"
            elif self.line_phase == "indent":
                if char == " " and self.line_indent < 3:
                    self.line_indent += 1
                elif (char == self.fence[0] if self.fence else char in "`~"):
                    self.line_char = char
                    self.line_run = 1
                    self.line_phase = "run"
                else:
                    self.line_phase = "other"
            elif self.line_phase == "run":
                if char == self.line_char:
                    self.line_run += 1
                elif self.fence:
                    self.line_phase = "tail" if char in " \t\r" else "other"
                else:
                    self.line_phase = "other" if self.line_char == "`" and char == "`" else "tail"
            elif self.line_phase == "tail":
                if self.fence and char not in " \t\r":
                    self.line_phase = "other"
                elif not self.fence and self.line_char == "`" and char == "`":
                    self.line_phase = "other"
        return opened, closed

    def feed(self, chunk: str) -> str:
        visible = []
        start = 0
        while start < len(chunk):
            end = chunk.find("\n", start)
            segment = chunk[start:] if end < 0 else chunk[start:end + 1]
            start += len(segment)
            fenced = self.fence is not None
            was_private = self.private
            visible.append(segment if fenced else self._feed_protocol(segment))
            opened, closed = self._advance_fence(
                segment, eligible=fenced or not (was_private or self.private),
            )
            if opened:
                # The protocol scanner may be holding the opener's newline as
                # a possible command prefix. It is ordinary Markdown text.
                visible.append(self.buffer)
                self.buffer = ""
            if closed:
                # The next line is a protocol line start even though the
                # closing fence bypassed the command scanner entirely.
                self.at_start = True
        return "".join(visible)

    def _feed_protocol(self, chunk: str) -> str:
        self.buffer += chunk
        visible = []
        while self.buffer:
            # Item boundaries are line boundaries too: a write-only message
            # must not require a dummy leading blank line to remain private.
            if self.at_start:
                opening_line = self.open[1:]
                if self.buffer.startswith(opening_line):
                    self.buffer = self.buffer[len(opening_line):]
                    self.at_start = False
                    self.private = True
                    continue
                if opening_line.startswith(self.buffer):
                    break
                self.at_start = False
            marker = self.close if self.private else self.open
            at = self.buffer.find(marker)
            if at >= 0:
                before, self.buffer = self.buffer[:at], self.buffer[at + len(marker):]
                if self.private:
                    if len(before.encode("utf-8")) > self.limit:
                        raise ProtocolError("Write frame too large")
                    self.frames_seen += 1
                    if self.frames_seen > 8:
                        raise ProtocolError("Too many writes in one item")
                    if self.validate:
                        self.writes.append(decode(before))
                else:
                    visible.append(before)
                self.private = not self.private
                # The closing delimiter consumes its newline. The next byte
                # is still a logical line start, including an adjacent frame.
                self.at_start = not self.private
                continue
            if self.private:
                if len(self.buffer.encode("utf-8")) > self.limit + len(self.close):
                    raise ProtocolError("Write frame too large")
                break
            # Hold only the possible marker prefix, not a paragraph or answer.
            keep = next((n for n in range(min(len(marker)-1, len(self.buffer)), 0, -1)
                         if self.buffer.endswith(marker[:n])), 0)
            visible.append(self.buffer[:-keep] if keep else self.buffer)
            self.buffer = self.buffer[-keep:] if keep else ""
            break
        return "".join(visible)

    def finish(self) -> str:
        # An authoritative item-end terminates a line, just as a newline does.
        # Require the COMPLETE closing marker; truncated markers still fail.
        if self.private and self.buffer.endswith(self.close[:-1]):
            self.feed("\n")
        if self.private or (self.buffer and self.buffer.startswith(("\n<MOBIUS", "<MOBIUS"))):
            raise ProtocolError("Unfinished write frame; nothing from this item dispatched")
        tail, self.buffer = self.buffer, ""
        return tail


def frame(nonce: str, write: WriteIntent) -> str:
    return f"\n<MOBIUS_WRITE {nonce}>\n" + json.dumps(
        {"id": write.id, "tool": write.tool, "arguments": write.arguments}
    ) + "\n</MOBIUS_WRITE>\n"


@dataclass
class _Item:
    decoder: FrameDecoder | None = None
    raw_fingerprint: str | None = None
    admitted_fingerprint: str | None = None
    pending_fingerprint: str | None = None
    abandoned: bool = False
    quarantined: bool = False


class OutputChannel:
    """One attributed turn; snapshots, not streamed guesses, authorize writes.

    The caller forwards only its own normalized public text events. Child
    routing must already be resolved. Real permissions remain in tool dispatch.
    Completed items are immutable. A changed final is an error, never a retry.
    """
    def __init__(self, nonce: str):
        self.nonce = nonce
        self.items: dict[str, _Item] = {}
        self.closed = False

    def _open_item(self, item) -> _Item:
        if self.closed:
            raise ProtocolError("Output channel is closed")
        if not isinstance(item, str) or not item or len(item) > 256:
            raise ProtocolError("Missing authoritative item identity")
        if not self.has_capacity(item):
            raise ProtocolError("Too many output items in this turn")
        state = self.items.get(item)
        if state is None:
            self.items[item] = state = _Item()
        return state

    def has_capacity(self, item) -> bool:
        return item in self.items or len(self.items) < 256

    def has_unfinished_item(self, item) -> bool:
        """Whether an observed item still awaits its authoritative snapshot."""
        state = self.items.get(item)
        return bool(not self.closed and state is not None
                    and state.decoder is not None and not state.abandoned
                    and state.raw_fingerprint is None)

    def delta(self, item: str, text: str) -> str:
        state = self._open_item(item)
        if state.abandoned or state.admitted_fingerprint or state.raw_fingerprint:
            return ""
        if state.quarantined:
            return ""
        try:
            if state.decoder is None:
                state.decoder = FrameDecoder(self.nonce, validate=False)
            return state.decoder.feed(text)
        except ProtocolError:
            # A provisional oversize frame must not expose its remaining bytes.
            # The authoritative snapshot may still repair/replace it.
            state.quarantined = True
            return ""

    def replace(self, item: str):
        state = self._open_item(item)
        if state.admitted_fingerprint:
            raise ProtocolError("An accepted item cannot be replaced")
        if state.pending_fingerprint:
            raise ProtocolError("An admitting item cannot be replaced")
        state.decoder = None
        state.abandoned = True

    def final(self, item: str, text: str) -> tuple[str, tuple[WriteIntent, ...]]:
        state = self._open_item(item)
        # Reserve identity before asynchronous durable admission can lag behind
        # a burst of final-only snapshots (including rejected snapshots).
        if state.abandoned:
            return "", ()
        raw_fingerprint = hashlib.sha256(text.encode()).hexdigest()
        prior_final = state.raw_fingerprint
        if prior_final is not None and prior_final != raw_fingerprint:
            raise ProtocolError("An authoritative item changed")
        state.raw_fingerprint = raw_fingerprint
        parsed = FrameDecoder(self.nonce)
        visible = parsed.feed(text) + parsed.finish()
        value = (visible, tuple(parsed.writes))
        old = state.admitted_fingerprint
        if old is not None:
            if old != self._fingerprint(*value):
                raise ProtocolError("An accepted item changed")
            return visible, ()
        if not parsed.writes:
            # Plain prose has no external admission to wait for.
            self.acknowledge(item, *value)
        # Parsing is not successful admission. Caller acknowledges ONLY after
        # the durable inbox accepted these writes (or an explicit rejection is
        # durably recorded). A rejected admission cannot disappear on replay.
        return value

    def reserve_admission(self, item, visible, writes) -> str | None:
        """Own one actor submission for this final; None means it is in flight."""
        state = self.items[item]
        fingerprint = self._fingerprint(visible, writes)
        if state.pending_fingerprint is not None:
            if state.pending_fingerprint != fingerprint:
                raise ProtocolError("An admitting item changed")
            return None
        state.pending_fingerprint = fingerprint
        return fingerprint

    @staticmethod
    def _fingerprint(visible, writes):
        payload = json.dumps([visible, [(w.id, w.tool, w._arguments_json) for w in writes]])
        return hashlib.sha256(payload.encode()).hexdigest()

    def acknowledge(self, item, visible, writes):
        state = self._open_item(item)
        fingerprint = self._fingerprint(visible, writes)
        old = state.admitted_fingerprint
        if old is not None and old != fingerprint:
            raise ProtocolError("An accepted item changed")
        state.admitted_fingerprint = fingerprint
        state.pending_fingerprint = None
        state.decoder = None
        state.quarantined = False

    def finish(self):
        self.closed = True
        # Even syntactically complete provisional commands are NOT accepted if
        # Stop/crash prevents the authoritative item-end from arriving.
        unaccepted = any(
            state.quarantined or (
                not state.abandoned and (
                    (state.raw_fingerprint is not None and state.admitted_fingerprint is None) or
                    (state.decoder is not None and (state.decoder.frames_seen or state.decoder.private))
                )
            ) for state in self.items.values()
        )
        for state in self.items.values():
            state.decoder = None
        if unaccepted:
            raise ProtocolError("Turn ended with unaccepted write instructions")
