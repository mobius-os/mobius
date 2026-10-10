"""Deterministic hierarchical Goal views. No summaries, caches or extra state."""
from collections import Counter

ROLES = ("coordinator", "helper")
# The coordinator owns the Goal outcome. A helper works one assigned branch
# and must not receive the owner's next step or outcome contract.
COORDINATOR_ONLY_FIELDS = ("outcome_contract", "next_action", "checkpoint")
# A helper's default view is its vertical branch: ancestors, its task, its
# children, prerequisites, and the blockers that can hold up its work (in its
# subtree or along its prerequisite chain). Neighbouring branches (siblings),
# unrelated blockers and whole-Goal counts are the coordinator's overview;
# read_goal(task=<parent>) lists a helper's neighbours on demand.


def project_goal(goal, task_id=None, *, role="coordinator"):
  """Focus on the common ancestor of running work, or the root overview.

  Shrunk by structure, never by clipping. Full detail belongs only to the
  focus, its ancestors' constraints, the results and notes of its
  prerequisites, and blocker reasons. Immediate children, siblings along its
  ancestor path, and
  blockers elsewhere are summaries: id, title, status, dependency ids, and
  has_result/has_note flags naming what focusing that task returns. Lists
  are whole. The saved plan is untouched; read_goal
  uses the identical projection.
  """
  if role not in ROLES:
    raise ValueError(f"Unknown Goal view role: {role}")
  tasks = (goal.plan_json or {}).get("tasks") or []
  by_id = {t["id"]: t for t in tasks}
  children = {}
  for task in tasks:
    children.setdefault(task.get("parent_id"), []).append(task["id"])

  def path(key):
    result = []
    while key is not None:
      result.append(key)
      key = by_id[key].get("parent_id")
    return list(reversed(result))

  if task_id is not None and task_id not in by_id:
    raise ValueError("Task not found in this Goal")
  if task_id is None:
    running = [path(t["id"]) for t in tasks if t["status"] == "running"]
    # A running coordinator plus running descendants should focus on those
    # descendants, not force a full coordinator view.
    leaves = [p for p in running if not any(
      len(other) > len(p) and other[:len(p)] == p for other in running
    )]
    if leaves:
      common = []
      for level in zip(*leaves):
        if len(set(level)) != 1:
          break
        common.append(level[0])
      task_id = common[-1] if common else None

  counts = {}

  # Count bottom-up without a Python recursion depth limit for deep plans.
  stack = [(None, False)]
  while stack:
    key, leaving = stack.pop()
    if not leaving:
      stack.append((key, True))
      stack.extend((child, False) for child in children.get(key, []))
      continue
    count = Counter()
    for child in children.get(key, []):
      count[by_id[child]["status"]] += 1
      count.update(counts[child])
    counts[key] = dict(sorted(count.items()))

  def summary(key, *, parent=True):
    t = by_id[key]
    entry = {k: t[k] for k in ("id", "title", "status", "progress", "depends_on")
             if t.get(k)}
    if parent and t.get("parent_id"):
      entry["parent_id"] = t["parent_id"]
    if t.get("result"):
      entry["has_result"] = True
    if t.get("note"):
      # A blocker's reason is the actionable part: show it, not a flag.
      if t["status"] in {"blocked", "failed"}:
        entry["note"] = t["note"]
      else:
        entry["has_note"] = True
    if counts[key]:
      entry["descendants"] = counts[key]
    return entry

  ancestors = path(task_id)[:-1] if task_id else []
  payload = {"id": goal.id, "revision": goal.revision, "objective": goal.objective,
             "status": goal.status, "focus": task_id}
  if goal.status == "stopped":
    from app.goals import goal_hold
    hold = goal_hold(goal)
    if hold:
      payload["hold"] = hold
  if goal.status == "open":
    payload["outcome_contract"] = (
      "Preserve the original objective. Complete only verified success; if it seems "
      "unreachable, first seek an actionable owner decision. Temporary owner action "
      "or approval leaves this Goal open with a saved card, not terminal failure. "
      "Cannot complete records the specific reason, efforts and partial results, "
      "and unmet outcome; Cancelled requires the owner to call it off. Settle "
      "the checklist and helpers honestly in the same terminal update."
    )
  for key in ("checkpoint", "next_action"):
    if getattr(goal, key, None):
      payload[key] = getattr(goal, key)
  shown = set(ancestors)
  if task_id:
    shown.add(task_id)
    payload["task"] = dict(by_id[task_id])
    # Ancestor completion conditions and notes constrain the focus: keep them
    # whole. Their order is the path, so parent ids and counts would repeat it.
    payload["ancestors"] = [
      {k: by_id[key][k] for k in ("id", "title", "status", "progress",
                                  "completion_condition", "note") if by_id[key].get(k)}
      for key in ancestors]
  child_keys = children.get(task_id, [])
  shown.update(child_keys)
  if child_keys:
    payload["children"] = [summary(key, parent=False) for key in child_keys]
  # Inherited prerequisites and children's prerequisites come with their
  # settled evidence in full. A scoped view must not hide a cross-branch blocker.
  relevant = ancestors + ([task_id] if task_id else []) + children.get(task_id, [])
  dependencies = list(dict.fromkeys(
    dep for key in relevant for dep in by_id[key].get("depends_on", [])
  ))
  if task_id and role == "coordinator":
    # A sibling that is also a prerequisite appears once, in full, below.
    siblings = [sibling for key in path(task_id)
                for sibling in children.get(by_id[key].get("parent_id"), [])
                if sibling != key and sibling not in dependencies]
    if siblings:
      shown.update(siblings)
      payload["siblings"] = [summary(key) for key in siblings]
  if dependencies:
    shown.update(dependencies)
    payload["dependencies"] = [{
      **{k: v for k, v in summary(key).items() if k not in ("has_result", "has_note")},
      **{field: by_id[key][field] for field in ("result", "note") if by_id[key].get(field)},
    } for key in dependencies]
  blockers = [t["id"] for t in tasks if t["status"] in {"blocked", "failed"} and t["id"] not in shown]
  if role == "helper" and task_id:
    # A helper sees what can hold up its own work: blockers in its subtree and
    # along its prerequisite chain (a prerequisite, anything it in turn needs,
    # and their subtrees). Unrelated branches' blockers are the coordinator's.
    blockers = [key for key in blockers if key in _work_reach(task_id, ancestors, by_id, children)]
  if blockers:
    payload["open_blockers"] = [summary(key) for key in blockers]
  if role == "coordinator":
    payload["totals"] = counts[None]
  if role == "helper":
    for key in COORDINATOR_ONLY_FIELDS:
      payload.pop(key, None)
  return payload


def _work_reach(task_id, ancestors, by_id, children):
  """Tasks whose state can hold up ``task_id``: its subtree plus the transitive
  prerequisites of it, its ancestors and its subtree, with their subtrees."""
  def subtree(key):
    found, stack = set(), [key]
    while stack:
      current = stack.pop()
      if current not in found:
        found.add(current)
        stack.extend(children.get(current, []))
    return found

  reach = subtree(task_id)
  pending = [dep for key in [*ancestors, *reach] for dep in by_id[key].get("depends_on", [])]
  while pending:
    dep = pending.pop()
    if dep in reach or dep not in by_id:
      continue
    for key in subtree(dep):
      if key not in reach:
        reach.add(key)
        pending.extend(by_id[key].get("depends_on", []))
  return reach
