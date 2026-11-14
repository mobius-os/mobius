"""Service-owned model metadata. No credentials or provider routing enter this contract."""

from typing import Any


def parse_catalogue(payload: Any) -> dict[str, Any]:
  """Translate the versioned gateway contract into the shared Responses shape.

  Reject the entire snapshot on malformed metadata: a partial update could pair
  a new default with old capabilities. Replacement edges point directly to a
  selectable model, never to another retired name or a different provider.
  """
  if (not isinstance(payload, dict) or type(payload.get("contract_version")) is not int
      or payload.get("contract_version") != 1):
    raise ValueError("Unsupported Möbius model catalogue contract")
  data = payload.get("data")
  if not isinstance(data, list) or not data:
    raise ValueError("Möbius model catalogue has no models")
  models = []
  ids = set()
  for raw in data:
    if not isinstance(raw, dict):
      raise ValueError("Invalid Möbius model")
    mid, label = raw.get("id"), raw.get("name")
    if (not isinstance(mid, str) or not mid.strip() or mid != mid.strip()
        or mid in ids or not isinstance(label, str) or not label.strip()):
      raise ValueError("Invalid or duplicate Möbius model identity")
    if raw.get("owned_by") != "mobius":
      raise ValueError("Möbius catalogue contains a foreign model")
    context = raw.get("context_window")
    if type(context) is not int or context <= 0:
      raise ValueError("Invalid Möbius context window")
    efforts = raw.get("effort_levels")
    if (not isinstance(efforts, list)
        or any(not isinstance(e, str) or not e for e in efforts)
        or len(set(efforts)) != len(efforts)):
      raise ValueError("Invalid Möbius effort levels")
    modalities = raw.get("input_modalities")
    if (not isinstance(modalities, list) or "text" not in modalities
        or any(m not in ("text", "image") for m in modalities)):
      raise ValueError("Invalid Möbius input modalities")
    default_effort = raw.get("default_effort")
    if ((efforts and default_effort not in efforts)
        or (not efforts and default_effort not in (None, "none"))):
      raise ValueError("Invalid Möbius default effort")
    model = {"id": mid, "label": label.strip(), "context_window": context,
             "effort_levels": list(efforts), "input_modalities": list(modalities),
             "default_effort": default_effort, "effective_context_window_percent": 90}
    for field in ("supports_parallel_tool_calls", "supports_search_tool",
                  "supports_reasoning_summaries", "supports_image_detail_original"):
      value = raw.get(field, False)
      if not isinstance(value, bool):
        raise ValueError(f"Invalid Möbius capability: {field}")
      model[field] = value
    compact = raw.get("auto_compact_token_limit")
    if compact is not None:
      if type(compact) is not int or not 0 < compact <= context:
        raise ValueError("Invalid Möbius compaction limit")
      model["auto_compact_token_limit"] = compact
    ids.add(mid)
    models.append(model)
  default = payload.get("default_model")
  if not isinstance(default, str) or default not in ids:
    raise ValueError("Möbius default is not selectable")
  replacements = payload.get("replacements", {})
  if not isinstance(replacements, dict) or any(
    not isinstance(old, str) or not old or old in ids or not isinstance(new, str) or new not in ids
    for old, new in replacements.items()
  ):
    raise ValueError("Invalid Möbius model replacements")
  return {"name": "Möbius", "transport": "identity_broker",
          "base_url": "http://127.0.0.1:8765/v1", "default_model": default,
          "models": models, "replacements": dict(replacements)}
