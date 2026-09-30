"""Dependency-free sealed form validation shared by chats and reviewed apps."""
import re
from typing import Any

MAX_FIELDS = 8
MAX_FIELD_VALUE_CHARS = 16 * 1024
MAX_TOTAL_VALUE_CHARS = 64 * 1024
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


def validate_request_spec(
  *, title: Any, description: Any, mode: Any, fields: Any,
) -> tuple[str, str, str, list[dict[str, Any]]]:
  """Return a bounded, browser-safe request spec or raise ValueError."""
  if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
    raise ValueError("Secure input title must be 1–80 characters.")
  if not isinstance(description, str) or len(description) > 240:
    raise ValueError("Secure input description is too long.")
  if mode not in {"sealed", "reveal"}:
    raise ValueError("Secure input mode must be sealed or reveal.")
  if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
    raise ValueError(f"Secure input requires 1–{MAX_FIELDS} fields.")

  normalized: list[dict[str, Any]] = []
  seen: set[str] = set()
  for raw in fields:
    if not isinstance(raw, dict):
      raise ValueError("Each secure input field must be an object.")
    name = raw.get("name")
    label = raw.get("label")
    input_type = raw.get("type", "password")
    autocomplete = raw.get("autocomplete", "off")
    if not isinstance(name, str) or not FIELD_NAME_RE.fullmatch(name):
      raise ValueError("Secure input field names must be lowercase identifiers.")
    if name in seen:
      raise ValueError("Secure input field names must be unique.")
    seen.add(name)
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
      raise ValueError("Secure input field labels must be 1–80 characters.")
    if input_type not in {"password", "text"}:
      raise ValueError("Secure input fields must be text or password inputs.")
    if not isinstance(autocomplete, str) or len(autocomplete) > 64:
      autocomplete = "off"
    normalized.append({
      "name": name,
      "label": label.strip(),
      "type": input_type,
      "autocomplete": autocomplete,
    })
  return title.strip(), description.strip(), mode, normalized


def validate_submitted_values(
  request: Any, values: Any,
) -> dict[str, str]:
  """Validate against the request shape without reflecting any value."""
  if not isinstance(values, dict):
    raise ValueError("Secure input fields are required.")
  expected = [field["name"] for field in request.fields]
  if set(values) != set(expected):
    raise ValueError("Secure input fields do not match this request.")
  normalized: dict[str, str] = {}
  total = 0
  for name in expected:
    value = values.get(name)
    if not isinstance(value, str) or not value:
      raise ValueError("Every secure input field is required.")
    if len(value) > MAX_FIELD_VALUE_CHARS:
      raise ValueError("A secure input value is too long.")
    total += len(value)
    if total > MAX_TOTAL_VALUE_CHARS:
      raise ValueError("Secure input submission is too large.")
    normalized[name] = value
  return normalized
