#!/usr/bin/env python3
"""App-independent GitHub review workflow: preview privately, then start with consent.

GitHub is connected in Settings. The platform owns grants, reviewers, receipts
and public actions; this client neither installs an app nor creates a queue.

Thin app adapters import api, prepare_selection(references, mode, chat_id,
app_id=None), freeze_preview(selection, options=None, agent=None, allow_mark_ready=False), and the
re-exported verify_selection, start_body and review_path helpers. A saved
selection binds app_id into its identity; the adapter must not reimplement
preview hashing, selection inspection or admission.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

if __package__:
  from scripts.github_review_selection import IDENTITY_FIELDS, start_body, frozen_options, verify_selection, review_path, selection_request_id
else:
  from github_review_selection import IDENTITY_FIELDS, start_body, frozen_options, verify_selection, review_path, selection_request_id


SELECTION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
PR_REFERENCE = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:#|/pull/)([1-9][0-9]*)/?$")


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
    "base_sha": base["object"]["sha"], "title": pull["title"], "url": pull["html_url"]}


def prepare_selection(references, mode, chat_id, app_id=None):
  if not 1 <= len(references) <= 20:
    raise ValueError("Select between 1 and 20 pull requests.")
  items = sorted((inspect_pr(ref) for ref in references), key=lambda item: (item["repo"], item["number"]))
  if len({(item["repo"], item["number"]) for item in items}) != len(items):
    raise ValueError("Select each pull request only once.")
  identity = {"mode": mode, "source_chat_id": chat_id,
              **({"app_id": app_id} if app_id is not None else {}),
              "items": [{key: item[key] for key in IDENTITY_FIELDS} for item in items]}
  return {**identity, "request_id": selection_request_id(identity), "items": items,
          "title": items[0]["title"] if len(items) == 1 else f"Review {len(items)} pull requests",
          "created_at": datetime.now(timezone.utc).isoformat()}


def freeze_preview(selection, options=None, agent=None, *, allow_mark_ready=False):
  """Save resolved prompts/model before consent; later start never re-resolves silently."""
  if allow_mark_ready and selection["mode"] != "review_fix_merge":
    raise ValueError("Mark-ready permission requires review_fix_merge.")
  preview = api(review_path(selection.get("app_id"), "/review-preview"), method="POST",
                data={"mode": selection["mode"], "options": options, "agent": agent})
  snapshot = preview["options"]
  if not re.fullmatch(r"[0-9a-f]{64}", preview["preview_sha256"]) or not snapshot.get("model"):
    raise ValueError("The platform did not return a complete frozen prompt/model preview.")
  scope = "named_pr_repairs_and_reviewed_successors" if selection["mode"] == "review_fix_merge" else None
  if allow_mark_ready:
    scope = "named_pr_repairs_ready_and_reviewed_successors"
  frozen = {**selection, **({"confirmation_scope": scope} if scope else {}), "options": frozen_options(snapshot),
    "agent": {"provider": snapshot["provider"], "model": snapshot["model"],
              "effort": snapshot.get("reasoning_effort")},
    "preview_sha256": preview["preview_sha256"], "resolved_snapshot": snapshot}
  frozen["request_id"] = selection_request_id(frozen)
  verify_selection(frozen)
  return frozen


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
  presets_command = commands.add_parser("presets", help="Read editable defaults and mandatory platform instructions")
  list_command = commands.add_parser("list", help="Read review runs")
  show = commands.add_parser("show", help="Read one core run")
  show.add_argument("run_id")
  observe = commands.add_parser("observe", help="Reconcile an existing public receipt read-only; never retry it")
  observe.add_argument("run_id")
  preview = commands.add_parser("preview", help="Inspect named PRs and freeze prompts/model without starting work")
  preview.add_argument("prs", nargs="+")
  preview.add_argument("--mode", choices=("review", "review_merge", "review_fix_merge"), default="review")
  preview.add_argument("--allow-mark-ready", action="store_true", help="Explicitly authorize draft readiness after independent review and passing checks")
  preview.add_argument("--options", help="JSON file with the five editable fields")
  preview.add_argument("--agent", help="JSON file: provider/model/effort")
  preview.add_argument("--output", required=True, help="New private JSON preview path; never overwrites")
  start = commands.add_parser("start", help="Use explicit approval in this owning chat for an exact saved preview")
  start.add_argument("--preview", required=True)
  start.add_argument("--approved-in-chat", action="store_true")
  start.add_argument("--approval-context", required=True)
  for command in (presets_command, list_command, show, observe, preview, start):
    command.add_argument("--app-id", type=int, help="Optional Contribute app context; core remains the default")
  args = parser.parse_args(argv)
  if args.app_id is not None and args.app_id < 1:
    parser.error("--app-id must be a positive integer")
  if args.command == "start" and (not args.approved_in_chat or not args.approval_context.strip()):
    parser.error("Starting requires explicit --approved-in-chat and a truthful --approval-context.")
  if args.command == "presets":
    result = api(review_path(args.app_id, "/review-presets"))
  elif args.command == "list":
    result = api(review_path(args.app_id, "/review-runs"))
  elif args.command in ("show", "observe"):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", args.run_id):
      raise ValueError("Invalid run identity.")
    path = review_path(args.app_id, "/review-runs/" + args.run_id)
    result = api(path + "/observe", method="POST", data={}) if args.command == "observe" else api(path)
  elif args.command == "preview":
    selection = freeze_preview(prepare_selection(args.prs, args.mode, os.environ["CHAT_ID"], args.app_id),
      read_object(args.options) if args.options else None, read_object(args.agent) if args.agent else None, allow_mark_ready=args.allow_mark_ready,
      )
    save_preview(args.output, selection)
    result = {"selection": selection, "preview_file": args.output, "approved": False}
  else:
    selection = load_preview(args.preview, os.environ["CHAT_ID"])
    if selection.get("app_id") != args.app_id:
      raise ValueError("The saved preview belongs to another app/core context.")
    body = {**start_body(selection), "chat_approval": {"context": args.approval_context.strip()}}
    try:
      result = {**api(review_path(args.app_id, "/review-runs"), method="POST", data=body), "approved": True}
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
