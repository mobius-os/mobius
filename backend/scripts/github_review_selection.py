"""Pure, versioned selection and preview serialization for core/app adapters."""
import hashlib
import json

IDENTITY_FIELDS = ("repo", "number", "head_sha", "base_ref", "base_sha")
OPTION_FIELDS = ("review_prompt", "merge_prompt")
OPTIONAL_OPTION_FIELDS = ("post_review", "fix_prompt", "max_rounds")


def preview_digest(snapshot):
  return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def selection_identity(selection):
  if selection.get("preview_sha256"):
    identity = start_body(selection)
    identity.pop("request_id")
  else:
    identity = {"mode": selection["mode"], "source_chat_id": selection["source_chat_id"],
                "items": [{key: item[key] for key in IDENTITY_FIELDS} for item in selection["items"]]}
  if selection.get("app_id") is not None:
    identity["app_id"] = selection["app_id"]
  return identity


def selection_request_id(selection):
  return "selection-" + hashlib.sha256(json.dumps(selection_identity(selection), sort_keys=True).encode()).hexdigest()[:32]

def review_path(app_id, suffix):
  prefix = f"/api/github/contributions/{app_id}" if app_id is not None else "/api/github"
  return prefix + suffix


def start_body(selection):
  return {"request_id": selection["request_id"], "mode": selection["mode"],
          "source_chat_id": selection["source_chat_id"],
          **{key: selection[key] for key in ("options", "agent", "preview_sha256", "confirmation_scope") if key in selection},
          "items": [{key: item[key] for key in IDENTITY_FIELDS} for item in selection["items"]]}


def frozen_options(snapshot):
  options = {key: snapshot[key] for key in OPTION_FIELDS}
  return {**options, **{key: snapshot[key] for key in OPTIONAL_OPTION_FIELDS if key in snapshot}}


def verify_selection(selection):
  """Reject edited saved input before interpreting consent for it."""
  if selection.get("mode") not in ("review", "review_merge", "review_fix_merge"):
    raise ValueError("Invalid saved selection mode.")
  if selection.get("mode") == "review_fix_merge" and (
      not selection.get("preview_sha256") or selection.get("confirmation_scope") != "named_pr_repairs_and_reviewed_successors"):
    raise ValueError("Takeover requires an exact frozen scoped preview.")
  if selection.get("preview_sha256"):
    snapshot = selection["resolved_snapshot"]
    digest = preview_digest(snapshot)
    expected_agent = {"provider": snapshot["provider"], "model": snapshot["model"],
                      "effort": snapshot.get("reasoning_effort")}
    if (digest != selection["preview_sha256"]
        or selection["options"] != frozen_options(snapshot)
        or selection["agent"] != expected_agent):
      raise ValueError("The frozen preview changed. Prepare and present a fresh selection.")
  else:
    if any(key in selection for key in ("options", "agent", "resolved_snapshot")):
      raise ValueError("Legacy selections cannot acquire options or model settings without a fresh frozen preview.")
  expected_id = selection_request_id(selection)
  if selection["request_id"] != expected_id:
    raise ValueError("The saved selection identity changed. Prepare a fresh selection.")
  start_body(selection)
