"""Small helpers for values that must cross JSON persistence/wire boundaries."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def json_safe(value: Any) -> Any:
  """Return a recursively JSON-serializable representation of ``value``.

  Provider SDKs sometimes wrap simple scalars in Pydantic/root/path objects.
  Those wrappers are useful inside the SDK, but JSON columns and JSON wire
  payloads should only see plain Python scalars, lists, and dictionaries.
  """
  if value is None or isinstance(value, (str, int, float, bool)):
    return value
  if isinstance(value, os.PathLike):
    return os.fspath(value)
  if isinstance(value, Mapping):
    return {str(json_safe(k)): json_safe(v) for k, v in value.items()}
  if isinstance(value, (list, tuple, set, frozenset)):
    return [json_safe(v) for v in value]
  model_dump = getattr(value, "model_dump", None)
  if callable(model_dump):
    try:
      return json_safe(model_dump(by_alias=True, exclude_none=True, mode="json"))
    except TypeError:
      try:
        return json_safe(model_dump(mode="json"))
      except TypeError:
        return json_safe(model_dump())
    except Exception:
      pass
  return str(value)
