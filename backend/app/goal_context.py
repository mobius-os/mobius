"""Deterministic hierarchical Goal views. No summaries, caches or extra state."""
from collections import Counter

from app.context_budget import (
  FIELD_SHARE_CHARS, GOAL_BRIEF_LIST_ITEMS, TURN_SHARE_CHARS, Allowance, emitted_len,
  fair_shares, fit_emitted,
)
from app.prompt_blocks import json_block_data

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


_BRIEF_PROSE = ("title", "completion_condition", "note", "result")


# Of the brief's share, at most this part goes to structure: the listed
# entries' ids, statuses and dependency ids. The texts share the rest fairly.
_BRIEF_STRUCTURE_CHARS = TURN_SHARE_CHARS // 4
# The least head an excerpt keeps besides its marker. While long texts would
# get less, the lowest-priority list gives up an entry instead, so excerpts
# stay readable rather than being markers alone.
_MIN_EXCERPT_HEAD = 200
_BRIEF_LISTS = ("ancestors", "dependencies", "children", "siblings", "open_blockers")


def fit_brief(payload, task_id, *, role="coordinator"):
  """Fit the per-turn brief to its share of the turn's context.

  Lists keep their entries while the structure allowance lasts. Every text in
  the brief then shares the rest fairly: short texts stay whole, long ones
  split what remains (each at most FIELD_SHARE_CHARS) as marked excerpts, so
  no section can starve another. If long texts would keep too little, the
  lowest-priority list gives up entries first. A list that left entries out
  says once how many and where to read them. Widths are measured as emitted.
  The saved Goal and plan are untouched, and the reads named return
  everything whole, so the brief never alters the outcome contract.
  """
  structure = Allowance(_BRIEF_STRUCTURE_CHARS)
  goal_read = "update_goal with no arguments" if role == "coordinator" else "read_goal"
  overview = f"read_goal(task={task_id})" if task_id else goal_read
  fixed_texts = []  # goal-level and focus texts: never dropped
  listed = {key: [] for key in (*_BRIEF_LISTS, "other_open_goals")}  # (entry, cost, texts)
  totals = {}

  def text(holder, key, read_full):
    """One shareable text, measured once: (holder, key, read, width, floor).

    ``floor`` is the least share that still shows a readable excerpt.
    """
    value = holder[key]
    marker = emitted_len(f"… [excerpt: {FIELD_SHARE_CHARS} of {len(value)} characters; {read_full}]")
    return (holder, key, read_full, emitted_len(value), marker + _MIN_EXCERPT_HEAD)

  def entry_texts(entry):
    return [text(entry, field, f"read_goal(task={entry['id']}, field={field}) reads the full text")
            for field in _BRIEF_PROSE if isinstance(entry.get(field), str)]

  def admit(entry):
    """Charge one entry's structure; return its cost."""
    before = structure.remaining
    if entry.get("depends_on"):
      entry["depends_on"], omitted = structure.take_ids(entry["depends_on"])
      if omitted:
        entry["depends_on_omitted"] = structure.charge(
          f"{omitted} more; read_goal(task={entry['id']}) lists them all")
    skeleton = {key: value for key, value in entry.items()
                if key not in _BRIEF_PROSE and key != "depends_on"}
    structure.remaining -= len(json_block_data(skeleton))
    return before - structure.remaining

  fixed_texts.append(text(payload, "objective", "read_goal(field=objective) reads the full objective"))
  if isinstance((payload.get("hold") or {}).get("reason"), str):
    payload["hold"] = dict(payload["hold"])
    fixed_texts.append(text(payload["hold"], "reason", f"{goal_read} for the full reason"))
  if "task" in payload:
    admit(payload["task"])
    fixed_texts.extend(entry_texts(payload["task"]))
  for key in ("next_action", "checkpoint"):
    if isinstance(payload.get(key), str):
      fixed_texts.append(text(payload, key, f"read_goal(field={key}) reads the full text"))
  for key in _BRIEF_LISTS:
    entries = payload.get(key) or []
    totals[key] = len(entries)
    # The nearest ancestors constrain the focus most, so they are kept first.
    order = list(reversed(entries)) if key == "ancestors" else list(entries)
    for entry in order[:GOAL_BRIEF_LIST_ITEMS]:
      if structure.spent:
        break
      listed[key].append((entry, admit(entry), entry_texts(entry)))
  others = payload.get("other_open_goals") or []
  totals["other_open_goals"] = len(others)
  for other in others[:GOAL_BRIEF_LIST_ITEMS]:
    if structure.spent:
      break
    shown = dict(other)
    cost = len(json_block_data({"id": shown["id"]}))
    structure.remaining -= cost
    listed["other_open_goals"].append(
      (shown, cost, [text(shown, "objective", "shown whole only by that Goal's own reads")]))

  while True:
    texts = fixed_texts + [item for key in listed for _, _, entry in listed[key] for item in entry]
    available = max(TURN_SHARE_CHARS - (_BRIEF_STRUCTURE_CHARS - structure.remaining), 0)
    shares = fair_shares([min(width, FIELD_SHARE_CHARS) for *_, width, _ in texts], available)
    starved = any(share < width and share < floor
                  for (*_, width, floor), share in zip(texts, shares))
    droppable = next((key for key in ("other_open_goals", *reversed(_BRIEF_LISTS))
                      if listed[key]), None)
    if not starved or droppable is None:
      break
    _, cost, _ = listed[droppable].pop()
    structure.remaining += cost

  for (holder, key, read_full, _, _), share in zip(texts, shares):
    holder[key] = fit_emitted(holder[key], share, read_full)
  for key in (*_BRIEF_LISTS, "other_open_goals"):
    if key not in payload and not totals.get(key):
      continue
    kept = [entry for entry, _, _ in listed[key]]
    if key == "ancestors":
      kept.reverse()
    if kept or totals.get(key):
      payload[key] = kept
    if len(kept) < totals.get(key, 0):
      payload[f"{key}_omitted"] = (
        f"{totals[key] - len(kept)} more open Goals in this chat" if key == "other_open_goals"
        else f"{totals[key] - len(kept)} more; {overview} lists them all")
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
