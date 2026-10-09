"""Read-only handoff vocabulary; lifecycle owners retain admission authority."""

def project_handoff(*, owner_input: bool, running: bool, waits: list[dict],
                             helper_count: int, park: dict, goal: dict | None = None,
                             stranded_followup: str | None = None) -> dict:
  """One read-side handoff vocabulary for chat and exact-Goal surfaces."""
  if owner_input:
    return {"kind": "owner_input", "reason": "saved_card"}
  blockers = [wait.get("resume_blocker") for wait in waits if wait.get("delivery_pending")]
  if "owner_input" in blockers:
    return {"kind": "owner_input", "reason": "saved_card"}
  if running:
    return {"kind": "working", "reason": None}
  if park["kind"] in {"owner_input", "recovery"}:
    return park
  if any(blocker in {"manual_resume", "resume_failed", "restart"} for blocker in blockers):
    return {"kind": "recovery", "reason": next(
      blocker for blocker in blockers if blocker in {"manual_resume", "resume_failed", "restart"}
    )}
  if park["kind"] == "automatic" or helper_count > 0 or any(
    not wait.get("delivery_pending") or wait.get("resume_blocker") in {
      None, "platform_restart", "restoring_edits", "provider_park", "live_turn",
    }
    for wait in waits
  ):
    return {"kind": "automatic", "reason": park.get("reason")}
  if goal and goal.get("pause_reason") == "deferred":
    return {"kind": "on_hold", "reason": "deferred", "goal_id": goal["id"],
            "hold_reason": goal["hold_reason"]}
  if stranded_followup:
    return {"kind": "recovery", "reason": "stranded_helper_followup",
            "helper_id": stranded_followup}
  return {"kind": "none", "reason": None}
