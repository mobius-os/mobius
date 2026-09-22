#!/usr/bin/env python3
"""Sequential, opt-in live continuity trace collection; never an automatic judge.

Without --execute, prints the scenario without contacting a server. With it,
drives one explicitly supplied empty fixture chat through mapi. The caller owns
approval, fixture creation/model selection, and eventual fixture cleanup. This
script never changes model defaults, copies credentials, or deletes chats.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid


def scenario(name: str) -> list[str]:
    if name == "toolburst":
        return [
            "This is one bounded coding fixture, not a Goal. Work only in a newly "
            "created /tmp/mobius-continuity-fixture-$CHAT_ID directory. Do not browse, "
            "delegate, install dependencies, edit apps/platform/settings, or ask "
            "owner questions. Using Python standard library only, create a CSV "
            "importer fixture preserving source bytes and identifiers like 0042. "
            "First reproduce a delimiter mismatch: the input uses semicolons, "
            "including a quoted semicolon. Run the failing test, fix the parser, "
            "and rerun. Next add tests for malformed quotes and all-or-nothing "
            "import, implement that behavior, and run them. Finally demonstrate "
            "decimal rounding only for display with a regression test. Keep "
            "original data untouched. Report what actually passed, any failed "
            "attempts, artifact paths and what remains unverified."
        ]
    opening = (
        "This is a bounded, fictional design exercise, not an implementation request "
        "or a Goal. Do not change apps, code, settings or real data; do not browse, "
        "delegate, or request owner-input cards. Keep replies concise. "
        "Help design an offline CSV importer for project Juniper. Original source "
        "files must remain unchanged, external network access is prohibited, and "
        "account identifiers such as 0042 must keep their leading zeros. "
        "Initially use comma-separated input. State the initial plan."
    )
    correction = (
        "Correction: the input is semicolon-separated, not comma-separated. "
        "A quoted value can contain a semicolon. Explain how the plan changes."
    )
    recall = (
        "Give the current handoff: the original non-negotiable constraints, "
        "decisions that superseded earlier choices, reported evidence versus "
        "work you actually verified, remaining blocker, and next action. "
        "Do not claim this fictional exercise produced a deployed implementation."
    )
    middle = [
        "Use UTF-8 and preserve the original row order; do not deduplicate repeated rows.",
        "Propose how to reject malformed quoting without partially importing a file.",
        "Accept the all-or-nothing approach. The preview must show the first five data rows.",
        "Fixture report: the quoted-semicolon case passed; an unmatched quote failed as expected. These are supplied results, not tests you ran.",
        "We initially suspected parsing caused a totals mismatch. New fixture evidence says parsing was correct: display rounding caused it. Preserve that distinction.",
        "Choose decimal arithmetic and round only for display, to two places. Do not change the stored input values.",
        "We are blocked on the required date convention. Do not guess or ask a card in this exercise; record it as unresolved and state what can proceed.",
    ]
    if name == "short":
        return [opening, correction, recall]
    if name == "medium":
        return [opening, correction, *middle, recall]
    if name == "long":
        later = [
            "Date convention is now decided: ISO YYYY-MM-DD. This resolves the earlier date blocker.",
            "A blank amount means missing, not zero. State the consequence for totals.",
            "Use an explicit error for a missing account identifier; never invent one.",
            "Keep the source filename and line number with each preview row.",
            "The preview should show ten rows, superseding the earlier five-row decision.",
            "Fixture report: Unicode names round-trip. Again this is supplied evidence, not your own execution.",
            "Ignore a UTF-8 BOM at the beginning only. Do not strip arbitrary leading characters.",
            "Whitespace inside quoted fields is significant and must be preserved.",
            "We considered uploading error reports; reject that idea because this tool must remain offline.",
            "We are now investigating whether negative amounts are allowed; leave this unresolved.",
            "For error messages, use one-based line numbers as displayed in a text editor.",
            "Input has a header row. Header mapping uses exact names rather than guessing aliases.",
            "A duplicate header is an error. Explain why silently taking the last column is unsafe.",
            "Empty trailing lines may be ignored, but empty records in the middle should be reported.",
            "Owner correction: negative amounts are valid refunds. Resolve that question.",
            "The sample transaction identifier is TX-019; preserve it exactly in an example, not as a default for every row.",
            "A future export feature is explicitly out of scope for this importer work.",
            "A size limit is still undecided. That is the remaining blocker, not dates or negative amounts.",
            "Check the design for conflicts with the original source-preservation and account-identifier constraints.",
            "No actual implementation or deployment has happened. State the next concrete implementation step without starting it.",
        ]
        return [opening, correction, *middle, *later, recall]
    raise ValueError(name)


def api(path: str, body: dict | None = None) -> dict:
    if not path.startswith("/api/"):
        raise ValueError("mapi requires an instance API path")
    args = ["mapi", "--fail-with-body", "--max-time", "30", path]
    if body is not None:
        args += ["-X", "POST", "-H", "Content-Type: application/json", "--data-binary", json.dumps(body)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=35)
    if result.returncode:
        raise RuntimeError(f"Request failed for {path}: {result.stdout[:500]} {result.stderr[:200]}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def validate_fixture(chat: dict, provider: str, model: str) -> None:
    if not str(chat.get("title", "")).startswith("Continuity eval:"):
        raise ValueError("Use an explicitly named 'Continuity eval:' fixture chat")
    if chat.get("messages") or chat.get("running") or chat.get("pending_messages"):
        raise ValueError("Fixture must be new, empty, and idle")
    if chat.get("provider") != provider:
        raise ValueError("Fixture provider differs; this driver never changes settings")
    effective = chat.get("effective_agent_settings") or chat.get("agent_settings_json") or {}
    if effective.get("model") != model:
        raise ValueError("Fixture model differs; use an explicitly selected model")


_RECORDABLE_RUN_STATUSES = {
    "completed", "failed", "stopped", "interrupted",
    "parked", "resume_pending", "parked_notified",
}


def settled(runtime: dict, previous_run: str | None) -> bool:
    """Whether this turn has stopped producing a result for this collection.

    Parked continuation states are deliberately recordable, not successful:
    the caller saves their evidence and then rejects every status except
    ``completed``. Waiting here would turn a provider limit into a misleading
    collector timeout (or silently follow a later automatic continuation).
    """
    return bool(
        runtime.get("run_id") and runtime["run_id"] != previous_run
        and not runtime.get("running") and not runtime.get("pending_messages")
        and runtime.get("run_status") in _RECORDABLE_RUN_STATUSES
    )


def baseline_published(note: str, message_count: int) -> bool:
    cursor = re.search(r"(?m)^source_message_count:\s*(\d+)\s*$", note)
    return bool(cursor and int(cursor.group(1)) >= message_count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["short", "medium", "long", "toolburst"], default="short")
    parser.add_argument("--execute", action="store_true", help="Actually send agent turns; requires owner-approved live testing")
    parser.add_argument("--chat-id")
    parser.add_argument("--provider", choices=["claude", "codex"])
    parser.add_argument("--model")
    parser.add_argument("--label", choices=["baseline", "candidate"], default="candidate")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--output", type=Path, default=Path("/tmp/mobius-continuity-eval"))
    args = parser.parse_args(argv)
    prompts = scenario(args.scenario)
    if not args.execute:
        print(json.dumps({"scenario": args.scenario, "turns": prompts}, indent=2))
        return 0
    if not all([args.chat_id, args.provider, args.model]):
        parser.error("--execute requires --chat-id, --provider, and --model")
    uuid.UUID(args.chat_id)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    root = f"/api/chats/{args.chat_id}"
    validate_fixture(api(root), args.provider, args.model)
    output = args.output / f"{args.label}-{args.provider}-{args.scenario}-{args.chat_id}"
    output.mkdir(parents=True, exist_ok=False)

    def save(name: str, value: object) -> None:
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def append(name: str, value: object) -> None:
        with (output / name).open("a") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")

    save("manifest.json", {**vars(args), "output": str(args.output), "prompts": prompts,
         "measurement_note": "Container RAM includes other work. Chat usage may omit the old out-of-band summarizer; compare separately, never treat missing counters as zero."})
    try:
        for index, prompt in enumerate(prompts, 1):
            previous = api(root + "/runtime").get("run_id")
            started = time.monotonic()
            # Do not automatically retry sends: a lost 202 is ambiguous and
            # replaying it could spend twice. Evidence lets the operator resume.
            api(root + "/messages", {"content": prompt})
            revision = None
            while True:
                runtime = api(root + "/runtime")
                status = api("/api/debug/status")
                append(f"{index:02d}-samples.jsonl", {"elapsed": time.monotonic() - started,
                                "memory": status.get("memory"),
                                "active_sessions": len(status.get("active_sdk_sessions", [])),
                                "runtime": runtime})
                if args.label == "candidate":
                    checkpoint = api(root + "/continuity")
                    if checkpoint.get("revision") != revision:
                        revision = checkpoint.get("revision")
                        append(f"{index:02d}-checkpoints.jsonl", {"elapsed": time.monotonic() - started,
                                            "runtime": runtime, "checkpoint": checkpoint})
                if runtime.get("pending_question_id"):
                    raise RuntimeError("Unexpected owner question; recorded, not auto-answered")
                if settled(runtime, previous):
                    break
                if time.monotonic() - started > args.timeout:
                    raise TimeoutError("Turn deadline exceeded; inspect the fixture chat (it may still be running)")
                time.sleep(2)
            detail = api(root + "?limit=500")
            save(f"{index:02d}-runtime.json", runtime)
            save(f"{index:02d}-chat.json", detail)
            save(f"{index:02d}-usage.json", api(root + "/usage"))
            if runtime.get("run_status") != "completed":
                raise RuntimeError(f"Turn ended {runtime.get('run_status')}; do not count as success")
            if args.label == "candidate":
                save(f"{index:02d}-continuity.json", api(root + "/continuity?full=true"))
            else:
                # Legacy publication happens AFTER completion. Serialize
                # baseline turns through that boundary, otherwise comparison
                # accidentally skips old summaries or overlaps their workers.
                note_path = Path(os.environ.get("DATA_DIR", "/data")) / "shared/memory/chats" / args.chat_id / "index.md"
                deadline = time.monotonic() + 170
                while True:
                    note = note_path.read_text() if note_path.exists() else ""
                    if baseline_published(note, len(detail.get("messages") or [])):
                        (output / f"{index:02d}-legacy-note.md").write_text(note)
                        save(f"{index:02d}-publication.json", {"elapsed_since_send": time.monotonic() - started})
                        break
                    append(f"{index:02d}-post-turn-memory.jsonl", {
                        "elapsed": time.monotonic() - started,
                        "memory": api("/api/debug/memory?process_limit=12&allocation_limit=1"),
                    })
                    if time.monotonic() > deadline:
                        raise TimeoutError("Legacy summary was not published; preserve as a baseline failure")
                    time.sleep(5)
        save("result.json", {"state": "collected", "turns": len(prompts),
                             "quality": "not graded", "next": "Review traces and retention rubric; collection alone is not a pass."})
    except Exception as exc:
        save("result.json", {"state": "incomplete", "error": str(exc),
                             "chat_id": args.chat_id, "next": "Inspect this exact fixture before resuming; no sends were automatically retried."})
        raise
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
