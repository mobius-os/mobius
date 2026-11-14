"""Deterministic hierarchical Goal views. No summaries, caches or extra state."""
from collections import Counter

from app.context_budget import (
  DEPENDENCY_IDS, LIST_ITEMS, NEXT_ACTION_CHARS, TEXT_CHARS, TITLE_CHARS, excerpt,
)

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

  Shrunk by structure first. Full detail belongs only to the focus, its
  ancestors' constraints, the results and notes of its prerequisites, and
  blocker reasons. Immediate children, siblings along its ancestor path, and
  blockers elsewhere are summaries: id, title, status, dependency ids, and
  has_result/has_note flags naming what focusing that task returns. The
  saved plan is untouched. read_goal returns this projection whole; the
  per-turn brief repeats in every turn, so ``fit_brief`` bounds it.
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

  counts = {}
  # Running tasks with no running descendant, per subtree; one bottom-up pass
  # without a Python recursion depth limit for deep plans.
  running_leaves = {}
  stack = [(None, False)]
  while stack:
    key, leaving = stack.pop()
    if not leaving:
      stack.append((key, True))
      stack.extend((child, False) for child in children.get(key, []))
      continue
    count = Counter()
    below = 0
    for child in children.get(key, []):
      count[by_id[child]["status"]] += 1
      count.update(counts[child])
      below += running_leaves[child]
    counts[key] = dict(sorted(count.items()))
    running_leaves[key] = below or int(key is not None and by_id[key]["status"] == "running")

  if task_id is None and running_leaves[None]:
    # A running coordinator plus running descendants should focus on those
    # descendants: the deepest task whose subtree holds every running leaf.
    total = running_leaves[None]
    while True:
      deeper = next((child for child in children.get(task_id, [])
                     if running_leaves[child] == total), None)
      if deeper is None:
        break
      task_id = deeper

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
      "unreachable, first seek an actionable owner decision. A temporary dependency "
      "keeps this Goal open with an exact durable handoff: request_restart for this "
      "chat's activation; a saved card for owner action; wait_for_decision or a "
      "confirmed claim dependency for peers; declare_wait for an external "
      "condition; a helper's result. Finish independent work first. A blocked "
      "task or peer promise is not a handoff. defer is only a deliberate owner "
      "manual hold, never waiting for an event. "
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
    prerequisite = set(dependencies)
    siblings = [sibling for key in path(task_id)
                for sibling in children.get(by_id[key].get("parent_id"), [])
                if sibling != key and sibling not in prerequisite]
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
    reach = _work_reach(task_id, ancestors, by_id, children)
    blockers = [key for key in blockers if key in reach]
  if blockers:
    payload["open_blockers"] = [summary(key) for key in blockers]
  if role == "coordinator":
    payload["totals"] = counts[None]
  if role == "helper":
    for key in COORDINATOR_ONLY_FIELDS:
      payload.pop(key, None)
  return payload


# A task text and its display limit in the brief.
_TASK_TEXT_LIMITS = (("completion_condition", TEXT_CHARS), ("note", TEXT_CHARS),
                     ("result", TEXT_CHARS))
# Lists that share LIST_ITEMS entries, in priority order: what constrains the
# focus, what feeds it, what it contains, what surrounds it. A plan of at most
# LIST_ITEMS tasks never fills them past that, so this is base's worst case.
_BRIEF_LISTS = ("ancestors", "dependencies", "children", "siblings", "open_blockers")
# A shortened title ends in this alone; the brief says once how to read it.
_TITLE_MARKER = "…"


def fit_brief(payload, task_id, *, role="coordinator"):
  """Show the per-turn brief's plan texts within their old input caps.

  Each task text and the next action over its limit becomes a marked excerpt
  naming the paged read that returns it whole (a title ends in a bare "…",
  explained once); the lists share at most LIST_ITEMS entries and each task at most
  DEPENDENCY_IDS dependency ids. The objective and other open Goals were never
  capped and stay whole. A plan valid under the old caps is shown exactly as
  before. The saved Goal and plan are untouched.
  """
  goal_read = "update_goal with no arguments" if role == "coordinator" else "read_goal"
  overview = f"read_goal(task={task_id})" if task_id else goal_read

  def clip(holder, key, limit, read_full):
    if isinstance(holder.get(key), str):
      holder[key] = excerpt(holder[key], limit, read_full)

  shortened_titles = []

  def clip_task(entry):
    title = entry.get("title")
    if isinstance(title, str) and len(title) > TITLE_CHARS:
      entry["title"] = title[:TITLE_CHARS - len(_TITLE_MARKER)].rstrip() + _TITLE_MARKER
      shortened_titles.append(entry["id"])
    for field, limit in _TASK_TEXT_LIMITS:
      clip(entry, field, limit, f"read_goal(task={entry['id']}, field={field}) reads it whole")
    ids = entry.get("depends_on") or []
    if len(ids) > DEPENDENCY_IDS:
      entry["depends_on"] = ids[:DEPENDENCY_IDS]
      entry["depends_on_omitted"] = (
        f"{len(ids) - DEPENDENCY_IDS} more; read_goal(task={entry['id']}) lists them all")

  clip(payload, "next_action", NEXT_ACTION_CHARS, "read_goal(field=next_action) reads it whole")
  if isinstance(payload.get("hold"), dict):
    payload["hold"] = dict(payload["hold"])
    clip(payload["hold"], "reason", NEXT_ACTION_CHARS, f"{goal_read} shows it whole")
  if "task" in payload:
    clip_task(payload["task"])
  # The budget counts distinct tasks: one listed both as a child and as a
  # prerequisite (any step-after-step chain) costs one entry, not two.
  room = LIST_ITEMS
  counted = set()
  for key in _BRIEF_LISTS:
    entries = payload.get(key)
    if not entries:
      continue
    # The nearest ancestors constrain the focus most, so they are kept first.
    kept = []
    for entry in (reversed(entries) if key == "ancestors" else entries):
      if entry["id"] not in counted:
        if not room:
          continue
        counted.add(entry["id"])
        room -= 1
      kept.append(dict(entry))
    if key == "ancestors":
      kept.reverse()
    for entry in kept:
      clip_task(entry)
    payload[key] = kept
    if len(kept) < len(entries):
      payload[f"{key}_omitted"] = f"{len(entries) - len(kept)} more; {overview} lists them all"
  if shortened_titles:
    payload["titles_shortened"] = (
      f"Titles ending in {_TITLE_MARKER} are shortened; "
      "read_goal(task=<id>, field=title) reads one whole.")
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
