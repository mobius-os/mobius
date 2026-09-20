"""Deterministic hierarchical Goal views. No summaries, caches or extra state."""
from collections import Counter


def project_goal(goal, task_id=None):
  """Focus on the common ancestor of running work, or the root overview.

  Full detail belongs only to the focus. Immediate children, siblings along
  its ancestor path, and dependency targets are summaries, never their trees.
  The saved plan is untouched; explicit navigation uses the identical projection.
  """
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

  def descendants(key):
    count = Counter()
    for child in children.get(key, []):
      count[by_id[child]["status"]] += 1
      count.update(descendants(child))
    counts[key] = dict(sorted(count.items()))
    return count

  descendants(None)

  def brief(key):
    t = by_id[key]
    result = {k: t[k] for k in ("id", "title", "status", "parent_id",
                               "completion_condition", "progress") if t.get(k) is not None}
    if t["status"] in {"blocked", "failed"} and t.get("note"):
      result["note"] = t["note"]
    if counts[key]:
      result["descendants"] = counts[key]
    return result

  ancestors = path(task_id)[:-1] if task_id else []
  payload = {"id": goal.id, "revision": goal.revision, "objective": goal.objective,
             "status": goal.status, "focus": task_id}
  for key in ("checkpoint", "next_action"):
    if getattr(goal, key, None):
      payload[key] = getattr(goal, key)
  if task_id:
    payload["task"] = dict(by_id[task_id])
    payload["ancestors"] = [{**brief(key), **({"note": by_id[key]["note"]} if by_id[key].get("note") else {})} for key in ancestors]
  payload["children"] = [
    {**brief(key), **({"result": by_id[key]["result"]} if task_id and by_id[key].get("result") else {})}
    for key in children.get(task_id, [])
  ]
  if task_id:
    route = path(task_id)
    siblings = []
    for key in route:
      siblings.extend(brief(sibling) for sibling in children.get(by_id[key].get("parent_id"), [])
                      if sibling != key)
    if siblings:
      payload["siblings"] = siblings
  # Include inherited prerequisites and children prerequisites, including
  # settled evidence. A scoped view must not hide a cross-branch blocker.
  relevant = ancestors + ([task_id] if task_id else []) + children.get(task_id, [])
  dependencies = list(dict.fromkeys(
    dep for key in relevant for dep in by_id[key].get("depends_on", [])
  ))
  if dependencies:
    payload["dependencies"] = [
      {**brief(key), **{k: by_id[key][k] for k in ("note", "result") if by_id[key].get(k)}}
      for key in dependencies
    ]
  payload["totals"] = counts[None]
  return payload
