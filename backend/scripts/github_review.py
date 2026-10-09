#!/usr/bin/env python3
"""App-independent GitHub review workflow: preview privately, then start with consent.

GitHub is connected in Settings. The platform owns grants, reviewers, receipts
and public actions; this client neither installs an app nor creates a queue.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


IDENTITY_FIELDS = ("repo", "number", "head_sha", "base_ref", "base_sha")
SELECTION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
PR_REFERENCE = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:#|/pull/)([1-9][0-9]*)/?$")
OPTION_FIELDS = ("review_prompt", "fix_prompt", "merge_prompt", "max_rounds", "autopilot")
# The platform freezes post_review only when chosen, so older snapshots keep
# their hash; it is carried only when present.
OPTIONAL_OPTION_FIELDS = ("post_review",)
TAKEOVER_SCOPE = "named_pr_repairs_and_reviewed_successors"
DRAFT_TAKEOVER_SCOPE = "named_pr_repairs_ready_and_reviewed_successors"


def api(path, *, method="GET", data=None, headers=None):
  base = os.environ["API_BASE_URL"].rstrip("/")
  token = os.environ["AGENT_TOKEN"]
  request = Request(base + path, method=method,
    data=json.dumps(data).encode() if data is not None else None,
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", **(headers or {})})
  with urlopen(request, timeout=60) as response:
    body = response.read()
    return json.loads(body) if body else None


def gh_api(endpoint):
  result = subprocess.run(["gh", "api", endpoint], capture_output=True,
    text=True, check=True, timeout=60)
  return json.loads(result.stdout)


def inspect_pr(reference):
  match = PR_REFERENCE.fullmatch(reference)
  if not match:
    raise ValueError("Use owner/repository#123 or a github.com pull-request URL.")
  repo, number = match.group(1), int(match.group(2))
  pull = gh_api(f"repos/{repo}/pulls/{number}")
  if pull.get("state") != "open" or pull.get("merged"):
    raise ValueError(f"{reference} is no longer open.")
  canonical_repo = pull["base"]["repo"]["full_name"]
  if canonical_repo.lower() != repo.lower():
    raise ValueError(f"{reference} moved to another repository; inspect its destination first.")
  base_ref = pull["base"]["ref"]
  base = gh_api(f"repos/{canonical_repo}/git/ref/heads/{quote(base_ref, safe='')}")
  return {"repo": canonical_repo.lower(), "number": number,
    "head_sha": pull["head"]["sha"], "base_ref": base_ref,
    "base_sha": base["object"]["sha"], "is_draft": pull.get("draft") is True, "title": pull["title"], "url": pull["html_url"]}


def prepare_selection(references, mode, chat_id):
  if not 1 <= len(references) <= 20:
    raise ValueError("Select between 1 and 20 pull requests.")
  items = sorted((inspect_pr(ref) for ref in references), key=lambda item: (item["repo"], item["number"]))
  if len({(item["repo"], item["number"]) for item in items}) != len(items):
    raise ValueError("Select each pull request only once.")
  identity = {"mode": mode, "source_chat_id": chat_id,
              "items": [{key: item[key] for key in IDENTITY_FIELDS} for item in items]}
  digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
  return {**identity, "request_id": "selection-" + digest, "items": items,
          "title": items[0]["title"] if len(items) == 1 else f"Review {len(items)} pull requests",
          "created_at": datetime.now(timezone.utc).isoformat()}


def start_body(selection):
  if selection["mode"] == "review_fix_merge" and not selection.get("preview_sha256"):
    raise ValueError("Scoped takeover requires a frozen prompt/model preview. Prepare a new selection.")
  return {"request_id": selection["request_id"], "mode": selection["mode"],
          "source_chat_id": selection["source_chat_id"],
          **{key: selection[key] for key in ("options", "agent", "preview_sha256", "confirmation_scope") if key in selection},
          "items": [{key: item[key] for key in IDENTITY_FIELDS} for item in selection["items"]]}


def frozen_options(snapshot):
  options = {key: snapshot[key] for key in OPTION_FIELDS}
  return {**options, **{key: snapshot[key] for key in OPTIONAL_OPTION_FIELDS if key in snapshot}}


def freeze_preview(selection, options=None, agent=None, *, allow_mark_ready=False, post_review=False):
  """Save resolved prompts/model before consent; later start never re-resolves silently."""
  if allow_mark_ready and selection["mode"] != "review_fix_merge":
    raise ValueError("Mark-ready permission requires review_fix_merge.")
  if post_review:
    if selection["mode"] != "review":
      raise ValueError("Posting the review on GitHub is available for review mode only.")
    options = {**(options or {}), "post_review": True}
  if selection["mode"] == "review_fix_merge" and any(item.get("is_draft") for item in selection["items"]) and not allow_mark_ready:
    raise ValueError("Draft takeover needs --allow-mark-ready and explicit consent to mark ready after review and checks.")
  preview = api("/api/github/review-preview", method="POST",
                data={"options": options, "agent": agent})
  snapshot = preview["options"]
  if not re.fullmatch(r"[0-9a-f]{64}", preview["preview_sha256"]) or not snapshot.get("model"):
    raise ValueError("The platform did not return a complete frozen prompt/model preview.")
  if post_review and snapshot.get("post_review") is not True:
    raise ValueError("The platform did not freeze the GitHub review posting choice.")
  frozen = {**selection, "options": frozen_options(snapshot),
    "agent": {"provider": snapshot["provider"], "model": snapshot["model"],
              "effort": snapshot.get("reasoning_effort")},
    "preview_sha256": preview["preview_sha256"], "resolved_snapshot": snapshot}
  if selection["mode"] == "review_fix_merge":
    frozen["confirmation_scope"] = DRAFT_TAKEOVER_SCOPE if allow_mark_ready else TAKEOVER_SCOPE
  identity = start_body(frozen)
  identity.pop("request_id")
  frozen["request_id"] = "selection-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
  verify_selection(frozen)
  return frozen


def verify_selection(selection):
  """Reject edited saved input before interpreting consent for it."""
  if selection.get("mode") not in ("review", "review_merge", "review_fix_merge"):
    raise ValueError("Invalid saved selection mode.")
  if selection.get("preview_sha256"):
    snapshot = selection["resolved_snapshot"]
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    expected_agent = {"provider": snapshot["provider"], "model": snapshot["model"],
                      "effort": snapshot.get("reasoning_effort")}
    if (digest != selection["preview_sha256"]
        or selection["options"] != frozen_options(snapshot)
        or selection["agent"] != expected_agent):
      raise ValueError("The frozen preview changed. Prepare and present a fresh selection.")
    identity = start_body(selection)
    identity.pop("request_id")
  else:
    if any(key in selection for key in ("options", "agent", "confirmation_scope", "resolved_snapshot")):
      raise ValueError("Legacy selections cannot acquire options or model settings without a fresh frozen preview.")
    identity = {"mode": selection["mode"], "source_chat_id": selection["source_chat_id"],
                "items": [{key: item[key] for key in IDENTITY_FIELDS} for item in selection["items"]]}
  expected_id = "selection-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
  if selection["request_id"] != expected_id:
    raise ValueError("The saved selection identity changed. Prepare a fresh selection.")
  start_body(selection)


def read_object(path):
  with open(path, encoding="utf-8") as source:
    value = json.load(source)
  if not isinstance(value, dict):
    raise ValueError("Options and agent files must contain JSON objects.")
  return value


def save_preview(path, selection):
  # A preview is immutable command input, not an editable grant or credential.
  fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  with os.fdopen(fd, "w", encoding="utf-8") as output:
    json.dump(selection, output, indent=2, ensure_ascii=False)
    output.write("\n")


def load_preview(path, chat_id):
  selection = read_object(path)
  if selection.get("source_chat_id") != chat_id:
    raise ValueError("This preview belongs to another source chat; continue there, never borrow its consent.")
  if not selection.get("preview_sha256") or not selection.get("resolved_snapshot"):
    raise ValueError("Prepare a frozen prompt/model preview before requesting consent.")
  if not SELECTION_ID.fullmatch(selection.get("request_id", "")):
    raise ValueError("Invalid preview identity.")
  verify_selection(selection)
  return selection


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  commands = parser.add_subparsers(dest="command", required=True)
  commands.add_parser("presets", help="Read editable defaults and mandatory platform instructions")
  commands.add_parser("list", help="Read app-independent runs")
  show = commands.add_parser("show", help="Read one core run")
  show.add_argument("run_id")
  observe = commands.add_parser("observe", help="Reconcile an existing public receipt read-only; never retry it")
  observe.add_argument("run_id")
  preview = commands.add_parser("preview", help="Inspect named PRs and freeze prompts/model without starting work")
  preview.add_argument("prs", nargs="+")
  preview.add_argument("--mode", choices=("review", "review_merge", "review_fix_merge"), default="review")
  preview.add_argument("--allow-mark-ready", action="store_true", help="Include draft mark-ready permission after review/checks in this new takeover preview")
  preview.add_argument("--post-review", action="store_true", help="Review mode only: post the verdict as a GitHub review comment when the run finishes")
  preview.add_argument("--options", help="JSON file with the five editable fields")
  preview.add_argument("--agent", help="JSON file: provider/model/effort")
  preview.add_argument("--output", required=True, help="New private JSON preview path; never overwrites")
  start = commands.add_parser("start", help="Use explicit approval in this owning chat for an exact saved preview")
  start.add_argument("--preview", required=True)
  start.add_argument("--approved-in-chat", action="store_true")
  start.add_argument("--approval-context", required=True)
  args = parser.parse_args(argv)
  if args.command == "start" and (not args.approved_in_chat or not args.approval_context.strip()):
    parser.error("Starting requires explicit --approved-in-chat and a truthful --approval-context.")
  if args.command == "presets":
    result = api("/api/github/review-presets")
  elif args.command == "list":
    result = api("/api/github/review-runs")
  elif args.command in ("show", "observe"):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", args.run_id):
      raise ValueError("Invalid run identity.")
    path = "/api/github/review-runs/" + args.run_id
    result = api(path + "/observe", method="POST", data={}) if args.command == "observe" else api(path)
  elif args.command == "preview":
    selection = freeze_preview(prepare_selection(args.prs, args.mode, os.environ["CHAT_ID"]),
      read_object(args.options) if args.options else None, read_object(args.agent) if args.agent else None, allow_mark_ready=args.allow_mark_ready,
      post_review=args.post_review)
    save_preview(args.output, selection)
    result = {"selection": selection, "preview_file": args.output, "approved": False}
  else:
    selection = load_preview(args.preview, os.environ["CHAT_ID"])
    body = {**start_body(selection), "chat_approval": {"context": args.approval_context.strip()}}
    try:
      result = {**api("/api/github/review-runs", method="POST", data=body), "approved": True}
    except HTTPError as error:
      if error.code != 409:
        raise
      detail = json.loads(error.read()).get("detail")
      if not isinstance(detail, dict) or not detail.get("chat_id"):
        raise ValueError(detail or "The preview changed. Present a fresh preview before consent.") from error
      result = {"approved": False, "already_owned": True, "message": detail.get("message"),
                "review_url": "/shell/?" + urlencode({"chat": detail["chat_id"]})}
  print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
  try:
    main()
  except HTTPError as error:
    print(f"GitHub workflow returned {error.code}: {error.read().decode(errors='replace')}", file=sys.stderr)
    sys.exit(1)
  except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
    print(f"Could not run this GitHub workflow: {error}", file=sys.stderr)
    sys.exit(1)
