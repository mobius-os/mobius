"""Deterministic hierarchical Goal views. No summaries, caches or extra state."""
from collections import Counter

from app.context_budget import (
  FIELD_SHARE_CHARS, GOAL_BRIEF_LIST_ITEMS, TURN_SHARE_CHARS, emitted_len, excerpt,
  fair_shares,
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


# Lists the brief may shorten, highest priority first: what feeds the focus,
# then what it contains, then what surrounds it.
_BRIEF_LISTS = ("dependencies", "children", "siblings", "open_blockers", "other_open_goals")
# The least head an excerpted list text keeps besides its marker. Below it a
# list gives up whole entries instead, lowest priority first.
_MIN_EXCERPT_HEAD = 200


def fit_brief(payload, task_id, *, role="coordinator"):
  """Fit the per-turn brief to its share of the turn's context.

  Tier 1 is the Goal and the work in hand: the objective, hold reason, next
  action, checkpoint, the focused task and its ancestors' constraints. Each
  text there shows whole up to FIELD_SHARE_CHARS and never shrinks to make
  room for lists. Tier 2 is the lists, which share whatever of
  TURN_SHARE_CHARS remains fairly; when their long texts would keep less than
  a readable excerpt, the lowest-priority list gives up whole entries. Every
  emitted character counts, so the brief stays within TURN_SHARE_CHARS unless
  tier 1 alone exceeds it. Excerpts name the read that returns the full text;
  the saved Goal and plan are untouched.
  """
  goal_read = "update_goal with no arguments" if role == "coordinator" else "read_goal"
  overview = f"read_goal(task={task_id})" if task_id else goal_read

  def fit(holder, key, read_full, limit=FIELD_SHARE_CHARS):
    holder[key] = excerpt(holder[key], limit, read_full, width=emitted_len)

  def shorten_ids(entry):
    ids = entry.get("depends_on") or []
    if len(ids) > GOAL_BRIEF_LIST_ITEMS:
      entry["depends_on"] = ids[:GOAL_BRIEF_LIST_ITEMS]
      entry["depends_on_omitted"] = (
        f"{len(ids) - GOAL_BRIEF_LIST_ITEMS} more; read_goal(task={entry['id']}) lists them all")

  def task_texts(entry):
    return [(entry, field, f"read_goal(task={entry['id']}, field={field}) reads the full text")
            for field in _BRIEF_PROSE if isinstance(entry.get(field), str)]

  # Tier 1.
  fit(payload, "objective", "read_goal(field=objective) reads the full objective")
  if isinstance((payload.get("hold") or {}).get("reason"), str):
    payload["hold"] = dict(payload["hold"])
    fit(payload["hold"], "reason", f"{goal_read} for the full reason")
  for key in ("next_action", "checkpoint"):
    if isinstance(payload.get(key), str):
      fit(payload, key, f"read_goal(field={key}) reads the full text")
  if "task" in payload:
    shorten_ids(payload["task"])
    for holder, key, read in task_texts(payload["task"]):
      fit(holder, key, read)
  ancestors = payload.get("ancestors") or []
  if ancestors:
    nearest = ancestors[-GOAL_BRIEF_LIST_ITEMS:]
    for entry in nearest:
      for holder, key, read in task_texts(entry):
        fit(holder, key, read)
    payload["ancestors"] = nearest
    if len(nearest) < len(ancestors):
      payload["ancestors_omitted"] = (
        f"{len(ancestors) - len(nearest)} more; {overview} lists them all")

  # Tier 2: measure tier 1 once, then each list entry as structure plus texts.
  lists = {}
  for key in _BRIEF_LISTS:
    entries = payload.pop(key, None)
    if entries:
      kept = [dict(entry) for entry in entries[:GOAL_BRIEF_LIST_ITEMS]]
      for entry in kept:
        shorten_ids(entry)
      lists[key] = (len(entries), kept)
  base = len(json_block_data(payload))

  def omitted(key, count):
    return (f"{count} more open Goals in this chat" if key == "other_open_goals"
            else f"{count} more; {overview} lists them all")

  measured = {}  # id(entry) -> (structure width, [(holder, field, read, width, floor)])

  def measure(key, entry):
    if id(entry) not in measured:
      items = ([(entry, "objective", "the full objective is not readable from this Goal")]
               if key == "other_open_goals" else task_texts(entry))
      structure = len(json_block_data({**entry, **{field: "" for _, field, _ in items}}))
      texts = []
      for holder, field, read in items:
        marker = emitted_len(f"… [excerpt: {FIELD_SHARE_CHARS} of {len(holder[field])} characters; {read}]")
        texts.append((holder, field, read, emitted_len(holder[field]), marker + _MIN_EXCERPT_HEAD))
      measured[id(entry)] = (structure, texts)
    return measured[id(entry)]

  def plan():
    """Room left for list texts, and every list text with its width and floor."""
    room = TURN_SHARE_CHARS - base
    texts = []
    for key, (total, kept) in lists.items():
      # `,"key":[...]` with commas between entries, and `,"key_omitted":"..."`.
      room -= len(json_block_data({key: []})) - 1 + max(len(kept) - 1, 0)
      if len(kept) < total:
        room -= len(json_block_data({f"{key}_omitted": omitted(key, total - len(kept))})) - 1
      for entry in kept:
        structure, items = measure(key, entry)
        room -= structure
        texts.extend(items)
    return room, texts

  while True:
    room, texts = plan()
    shares = fair_shares([min(width, FIELD_SHARE_CHARS) for *_, width, _ in texts], max(room, 0))
    starved = room < 0 or any(share < width and share < floor
                              for (*_, width, floor), share in zip(texts, shares))
    lowest = next((key for key in reversed(_BRIEF_LISTS) if key in lists and lists[key][1]), None)
    if not starved or lowest is None:
      break
    lists[lowest][1].pop()
  for (holder, field, read, _, _), share in zip(texts, shares):
    holder[field] = excerpt(holder[field], share, read, width=emitted_len)
  for key, (total, kept) in lists.items():
    payload[key] = kept
    if len(kept) < total:
      payload[f"{key}_omitted"] = omitted(key, total - len(kept))
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
