"""The contribution lifecycle as DATA: regions, attention rules and the bridge to the stored record.

This file is the one source of the tables. It lives in two repositories under two names and
the copies are byte-identical: `lifecycle_tables.py` in the Contribute app (which also generates
`lifecycle-tables.js` from it: `python3 test/make_lifecycle_cases.py --write`) and
`contribution_lifecycle_tables.py` in the platform. A lock file in each repository pins the
tables, the engine and the shared case file by hash, so a change to one copy fails its tests
until the other copy and both locks follow.

A region is a flat machine: (state, event, when) -> (state', effects, raises). Regions on one
host object receive the same events. `waits` says who must act for a state to move:
none (nothing is owed), owner, other (someone else on GitHub), github, platform, agent. A state
that waits on a machine (github, platform, agent) MUST carry a deadline with a named exit.

Row shapes, kept as tuples so a table reads like a table:
  state:      name -> (waits, final, text, deadline or None), deadline = (seconds, event, to)
  transition: (from, event, when, to, effects, raises, vs, guard_text)
`when` is matched against the event's data (a missing field is None; a list means any of).
A guard the engine cannot decide stays in `guard_text` and is enforced by whoever raises the
event. `vs` records how the row relates to the behaviour before the machine: same, changed, new.
"""
from __future__ import annotations

import json
import sys

VERSION = 1
MINUTE, HOUR, DAY = 60, 3600, 86400

# A target written as *PUBLIC resolves from the event: a draft PR or one open for review.
TARGETS = {"*PUBLIC": {"field": "is_draft", "if_true": "DRAFT", "otherwise": "OPEN"}}

DEFAULT_BASE, OTHER_BASE = {"base_is_default": [True, None]}, {"base_is_default": False}
FINISHED = ["NOTIFY_ONCE", "CLOSE_OUT_FOLLOWUP"]

PUBLICATION = {
  "title": "Publication", "host": "record", "initial": "PREPARED",
  "states": {
    "PREPARED": ("owner", False, "Private, reviewed, waiting for you to send it.", None),
    "SENDING": ("platform", False, "Send pressed; the platform is publishing.", (5 * MINUTE, "TIMEOUT", "UNCERTAIN")),
    "UNCERTAIN": ("github", False, "The answer was lost; reading GitHub to find out.", (10 * MINUTE, "TIMEOUT", "NOT_CONFIRMED")),
    "NOT_CONFIRMED": ("owner", False, "Could not confirm. Never resent; you decide.", None),
    "DRAFT": ("none", False, "Public draft PR.", None),
    "OPEN": ("none", False, "Public PR ready for review.", None),
    "QUEUED": ("github", False, "In GitHub's merge queue.", (6 * HOUR, "TIMEOUT", "QUEUED")),
    "MERGED": ("none", True, "Merged into the default branch.", None),
    "MERGED_ELSEWHERE": ("github", False, "Merged into another branch; waiting to see whether that branch reaches the default branch.", (14 * DAY, "TIMEOUT", "ORPHANED")),
    "ORPHANED": ("owner", False, "Merged into a branch that never reached the default branch. Needs you.", None),
    "CLOSED": ("none", False, "Closed without merging. Watched for 30 days in case it is reopened.", (30 * DAY, "TIMEOUT", "CLOSED_QUIET")),
    "CLOSED_QUIET": ("none", True, "Closed for over 30 days; no longer polled. A manual Sync still revives it.", None),
    "ABANDONED": ("none", True, "Discarded before sending. Restorable.", None),
    "SUPERSEDED": ("none", True, "Replaced by other work (successor recorded).", None),
  },
  "transitions": [
    ("PREPARED", "SEND_CONFIRMED", {}, "SENDING", ["CLAIM_RECORD"], [], "same", "all clear for this exact version and no attention"),
    ("PREPARED", "DISCARD", {}, "ABANDONED", ["DELETE_CHECKOUT_LATER"], [], "same", ""),
    ("PREPARED", "LANDED_ELSEWHERE", {}, "SUPERSEDED", ["RECORD_SUCCESSOR", "NOTIFY_ONCE"], [], "changed", "the reviewed work reached the default branch by another path"),
    ("ABANDONED", "RESTORE", {}, "PREPARED", [], [], "same", "the checkout still exists"),
    ("SENDING", "SUBMIT_OK", {}, "*PUBLIC", ["STAMP_PR", "STAMP_FOLLOWUP"], [], "same", ""),
    ("SENDING", "SUBMIT_REFUSED", {}, "PREPARED", ["SHOW_REFUSAL"], [], "changed", "the platform refused; nothing was pushed"),
    ("SENDING", "SUBMIT_LOST", {}, "UNCERTAIN", ["START_OBSERVING"], [], "same", ""),
    ("SENDING", "TIMEOUT", {}, "UNCERTAIN", ["START_OBSERVING"], [], "new", ""),
    ("UNCERTAIN", "OBSERVED_PR", {}, "*PUBLIC", ["STAMP_PR"], [], "same", "GitHub shows the PR"),
    ("UNCERTAIN", "SUBMIT_REFUSED", {}, "PREPARED", ["SHOW_REFUSAL"], [], "changed", "the read proved nothing was pushed"),
    ("UNCERTAIN", "TIMEOUT", {}, "NOT_CONFIRMED", ["REQUEST_OWNER"], [], "changed", "10 minutes without finding it"),
    ("NOT_CONFIRMED", "OBSERVED_PR", {}, "*PUBLIC", ["STAMP_PR"], [], "same", "GitHub shows the PR"),
    ("NOT_CONFIRMED", "OWNER_CHECKS", {}, "UNCERTAIN", ["START_OBSERVING"], [], "new", ""),
    ("NOT_CONFIRMED", "SUBMIT_REFUSED", {}, "PREPARED", ["SHOW_REFUSAL"], [], "changed", "the read proved nothing was pushed"),
    ("NOT_CONFIRMED", "DISCARD", {}, "ABANDONED", [], [], "same", ""),
    ("DRAFT", "MARKED_READY", {}, "OPEN", [], [], "same", ""),
    ("OPEN", "MARKED_DRAFT", {}, "DRAFT", [], [], "same", ""),
    ("QUEUED", "MARKED_DRAFT", {}, "DRAFT", [], [], "new", ""),
    ("OPEN", "QUEUE_ENTERED", {}, "QUEUED", [], [], "new", ""),
    ("QUEUED", "QUEUE_LEFT", {"merged": [False, None]}, "OPEN", [], [], "new", "not merged"),
    ("QUEUED", "TIMEOUT", {}, "QUEUED", ["ASK_GITHUB"], [], "new", ""),
    ("DRAFT", "MERGED", DEFAULT_BASE, "MERGED", FINISHED, ["PR_FINISHED", "TARGET_MERGED"], "same", "the base is the default branch"),
    ("OPEN", "MERGED", DEFAULT_BASE, "MERGED", FINISHED, ["PR_FINISHED", "TARGET_MERGED"], "same", "the base is the default branch"),
    ("QUEUED", "MERGED", DEFAULT_BASE, "MERGED", FINISHED, ["PR_FINISHED", "TARGET_MERGED"], "same", "the base is the default branch"),
    ("CLOSED", "MERGED", DEFAULT_BASE, "MERGED", FINISHED, ["PR_FINISHED", "TARGET_MERGED"], "same", "GitHub may list closed before merged"),
    ("CLOSED_QUIET", "MERGED", DEFAULT_BASE, "MERGED", FINISHED, ["PR_FINISHED", "TARGET_MERGED"], "new", "seen by a manual Sync"),
    ("DRAFT", "MERGED", OTHER_BASE, "MERGED_ELSEWHERE", ["START_ROUTE_WATCH"], ["PR_FINISHED", "TARGET_MERGED"], "new", "the base is another branch"),
    ("OPEN", "MERGED", OTHER_BASE, "MERGED_ELSEWHERE", ["START_ROUTE_WATCH"], ["PR_FINISHED", "TARGET_MERGED"], "new", "the base is another branch"),
    ("QUEUED", "MERGED", OTHER_BASE, "MERGED_ELSEWHERE", ["START_ROUTE_WATCH"], ["PR_FINISHED", "TARGET_MERGED"], "new", "the base is another branch"),
    ("CLOSED", "MERGED", OTHER_BASE, "MERGED_ELSEWHERE", ["START_ROUTE_WATCH"], ["PR_FINISHED", "TARGET_MERGED"], "new", "the base is another branch"),
    ("CLOSED_QUIET", "MERGED", OTHER_BASE, "MERGED_ELSEWHERE", ["START_ROUTE_WATCH"], ["PR_FINISHED", "TARGET_MERGED"], "new", "the base is another branch"),
    ("MERGED", "BASE_OBSERVED", OTHER_BASE, "MERGED_ELSEWHERE", ["START_ROUTE_WATCH"], [], "new", "a writer recorded merged before the base was known"),
    ("MERGED_ELSEWHERE", "ROUTE_RESOLVED", {"reached": "after"}, "MERGED", FINISHED, [], "new", "that branch reached the default branch after this merge"),
    ("MERGED_ELSEWHERE", "ROUTE_RESOLVED", {"reached": ["before", "dropped"]}, "ORPHANED", ["REQUEST_OWNER"], [], "new", "that branch landed before this merge, or was dropped"),
    ("MERGED_ELSEWHERE", "TIMEOUT", {}, "ORPHANED", ["REQUEST_OWNER"], [], "new", "14 days without an answer"),
    ("ORPHANED", "OWNER_RELANDS", {}, "SUPERSEDED", ["RECORD_SUCCESSOR"], [], "new", "the owner re-lands it as new work"),
    ("ORPHANED", "ROUTE_RESOLVED", {"reached": "after"}, "MERGED", FINISHED, [], "new", "the branch reached the default branch after all"),
    ("DRAFT", "CLOSED", {}, "CLOSED", FINISHED, ["PR_FINISHED", "TARGET_CLOSED"], "same", ""),
    ("OPEN", "CLOSED", {}, "CLOSED", FINISHED, ["PR_FINISHED", "TARGET_CLOSED"], "same", ""),
    ("QUEUED", "CLOSED", {}, "CLOSED", FINISHED, ["PR_FINISHED", "TARGET_CLOSED"], "same", ""),
    ("CLOSED", "REOPENED", {}, "*PUBLIC", ["NOTIFY_ONCE"], ["PR_REOPENED"], "new", ""),
    ("CLOSED", "TIMEOUT", {}, "CLOSED_QUIET", [], [], "new", "30 days closed"),
    ("CLOSED_QUIET", "REOPENED", {}, "*PUBLIC", ["NOTIFY_ONCE"], ["PR_REOPENED"], "new", "seen by a manual Sync"),
    ("MERGED", "REOPENED", {}, "MERGED", [], [], "new", "never happens on GitHub; kept and logged"),
    ("MERGED", "CLOSED", {}, "MERGED", [], [], "new", "GitHub lists closed right after merged"),
    ("MERGED", "QUEUE_LEFT", {}, "MERGED", [], [], "new", "the queue entry ends when the PR merges"),
    ("MERGED_ELSEWHERE", "CLOSED", {}, "MERGED_ELSEWHERE", [], [], "new", "GitHub lists closed right after merged"),
    ("MERGED_ELSEWHERE", "QUEUE_LEFT", {}, "MERGED_ELSEWHERE", [], [], "new", "the queue entry ends when the PR merges"),
  ],
}

STRANDED = ["NOTIFY_ONCE", "REQUEST_OWNER"]

UPDATE = {
  "title": "Update", "host": "record", "initial": "NONE",
  "states": {
    "NONE": ("none", False, "No update waiting.", None),
    "PREPARED_UPDATE": ("owner", False, "An update is reviewed and waiting for you to press Update PR.", None),
    "SENDING_UPDATE": ("platform", False, "Pushing the update.", (5 * MINUTE, "TIMEOUT", "UPDATE_UNCERTAIN")),
    "UPDATE_UNCERTAIN": ("github", False, "Answer lost; reading the PR to see whether the push arrived.", (10 * MINUTE, "TIMEOUT", "UPDATE_NOT_CONFIRMED")),
    "UPDATE_NOT_CONFIRMED": ("owner", False, "Could not confirm the push. Needs you.", None),
    "STRANDED_UPDATE": ("owner", False, "The PR merged or closed before the update went out. Needs you, with choices.", (7 * DAY, "TIMEOUT", "STRANDED_UPDATE")),
  },
  "transitions": [
    ("NONE", "STAGE_UPDATE", {}, "PREPARED_UPDATE", ["MARK_ATTENTION_ADDRESSED"], [], "changed", "the PR is public (draft, open or queued)"),
    ("PREPARED_UPDATE", "STAGE_UPDATE", {}, "PREPARED_UPDATE", ["MARK_ATTENTION_ADDRESSED"], [], "same", "restaged after a fix"),
    ("PREPARED_UPDATE", "SEND_UPDATE_CONFIRMED", {}, "SENDING_UPDATE", ["CLAIM_RECORD"], [], "same", "all clear for this exact version"),
    ("PREPARED_UPDATE", "DISCARD_UPDATE", {}, "NONE", ["KEEP_PUBLICATION_UNTOUCHED"], [], "changed", ""),
    ("PREPARED_UPDATE", "TARGET_MERGED", {}, "STRANDED_UPDATE", STRANDED, [], "new", ""),
    ("PREPARED_UPDATE", "TARGET_CLOSED", {}, "STRANDED_UPDATE", STRANDED, [], "new", ""),
    ("SENDING_UPDATE", "UPDATE_OK", {}, "NONE", ["STAMP_UPDATED"], [], "same", ""),
    ("SENDING_UPDATE", "UPDATE_REFUSED", {}, "PREPARED_UPDATE", ["SHOW_REFUSAL"], [], "changed", "the platform refused; nothing was pushed"),
    ("SENDING_UPDATE", "UPDATE_LOST", {}, "UPDATE_UNCERTAIN", ["START_OBSERVING"], [], "same", ""),
    ("SENDING_UPDATE", "TIMEOUT", {}, "UPDATE_UNCERTAIN", ["START_OBSERVING"], [], "new", ""),
    ("SENDING_UPDATE", "TARGET_MERGED", {}, "STRANDED_UPDATE", STRANDED, [], "new", ""),
    ("SENDING_UPDATE", "TARGET_CLOSED", {}, "STRANDED_UPDATE", STRANDED, [], "new", ""),
    ("UPDATE_UNCERTAIN", "OBSERVED_HEAD", {}, "NONE", ["STAMP_UPDATED"], [], "same", "the PR's head contains the update"),
    ("UPDATE_UNCERTAIN", "UPDATE_REFUSED", {}, "PREPARED_UPDATE", ["SHOW_REFUSAL"], [], "changed", "the read proved nothing was pushed"),
    ("UPDATE_UNCERTAIN", "TIMEOUT", {}, "UPDATE_NOT_CONFIRMED", ["REQUEST_OWNER"], [], "changed", ""),
    ("UPDATE_UNCERTAIN", "TARGET_MERGED", {}, "STRANDED_UPDATE", STRANDED, [], "new", ""),
    ("UPDATE_UNCERTAIN", "TARGET_CLOSED", {}, "STRANDED_UPDATE", STRANDED, [], "new", ""),
    ("UPDATE_NOT_CONFIRMED", "OBSERVED_HEAD", {}, "NONE", ["STAMP_UPDATED"], [], "same", "the PR's head contains the update"),
    ("UPDATE_NOT_CONFIRMED", "UPDATE_REFUSED", {}, "PREPARED_UPDATE", ["SHOW_REFUSAL"], [], "changed", "the read proved nothing was pushed"),
    ("UPDATE_NOT_CONFIRMED", "DISCARD_UPDATE", {}, "NONE", ["KEEP_PUBLICATION_UNTOUCHED"], [], "changed", ""),
    ("UPDATE_NOT_CONFIRMED", "TARGET_MERGED", {}, "STRANDED_UPDATE", STRANDED, [], "new", ""),
    ("UPDATE_NOT_CONFIRMED", "TARGET_CLOSED", {}, "STRANDED_UPDATE", STRANDED, [], "new", ""),
    ("STRANDED_UPDATE", "COVERED_BY_MERGE", {}, "NONE", ["RECORD_NOTE"], [], "new", "the merged PR already contains these changes"),
    ("STRANDED_UPDATE", "PRESERVE_AS_NEW", {}, "NONE", ["STAGE_NEW_RECORD", "RECORD_SUCCESSOR"], [], "new", ""),
    ("STRANDED_UPDATE", "DISCARD_UPDATE", {}, "NONE", ["KEEP_PUBLICATION_UNTOUCHED"], [], "changed", ""),
    ("STRANDED_UPDATE", "TIMEOUT", {}, "STRANDED_UPDATE", ["REMIND_OWNER"], [], "new", "7 days"),
    ("STRANDED_UPDATE", "PR_REOPENED", {}, "PREPARED_UPDATE", [], [], "new", "the PR is open again, so the update can go out"),
  ],
}

CLOSE_OUT = ["TURN_OFF_GRANT", "HIDE_CHAT", "MIRROR"]

FOLLOWUP = {
  "title": "Follow-up", "host": "record", "initial": "OFF",
  "states": {
    "OFF": ("none", False, "Not following this PR.", None),
    "IDLE": ("none", False, "Following; nothing to answer.", None),
    "RESPONDING": ("agent", False, "A round is working on feedback.", (45 * MINUTE, "LEASE_EXPIRED", "IDLE")),
    "PAUSED": ("owner", False, "You paused it.", None),
    "BLOCKED": ("owner", False, "Needs a person: an escalation or two failed rounds.", None),
    "CLOSED_OUT": ("none", True, "The PR is finished; grant off, chat hidden.", None),
  },
  "transitions": [
    ("OFF", "FOLLOW_ON", {}, "IDLE", ["STAMP_GRANT", "MIRROR"], [], "same", ""),
    ("IDLE", "FOLLOW_OFF", {}, "OFF", ["MIRROR"], [], "same", ""),
    ("PAUSED", "FOLLOW_OFF", {}, "OFF", ["MIRROR"], [], "same", ""),
    ("BLOCKED", "FOLLOW_OFF", {}, "OFF", ["MIRROR"], [], "same", ""),
    ("RESPONDING", "FOLLOW_OFF", {}, "OFF", ["MIRROR"], [], "same", ""),
    ("IDLE", "PAUSE", {}, "PAUSED", ["MIRROR"], [], "same", ""),
    ("IDLE", "ROUND_STARTED", {}, "RESPONDING", ["MIRROR"], [], "same", "attention that is not a human-required flag"),
    ("RESPONDING", "ROUND_DONE", {}, "IDLE", ["MIRROR"], [], "same", ""),
    ("RESPONDING", "ROUND_FAILED", {"blocks": [False, None]}, "IDLE", ["COUNT_FAILURE", "MIRROR"], [], "same", "first failure"),
    ("RESPONDING", "ROUND_FAILED", {"blocks": True}, "BLOCKED", ["REQUEST_OWNER", "MIRROR"], [], "same", "second failure in a row"),
    ("RESPONDING", "ESCALATED", {}, "BLOCKED", ["REQUEST_OWNER", "MIRROR"], [], "same", ""),
    ("IDLE", "ESCALATED", {}, "BLOCKED", ["REQUEST_OWNER", "MIRROR"], [], "same", "an escalation recorded after the round ended"),
    ("RESPONDING", "LEASE_EXPIRED", {}, "IDLE", ["COUNT_FAILURE", "MIRROR"], [], "changed", "45 minutes without a result"),
    ("BLOCKED", "RESUME", {}, "IDLE", ["CLEAR_ONLY_HUMAN_REQUIRED", "MIRROR"], [], "changed", ""),
    ("PAUSED", "RESUME", {}, "IDLE", ["MIRROR"], [], "same", ""),
    ("OFF", "PR_FINISHED", {}, "CLOSED_OUT", ["MIRROR"], [], "same", ""),
    ("IDLE", "PR_FINISHED", {}, "CLOSED_OUT", CLOSE_OUT, [], "same", ""),
    ("RESPONDING", "PR_FINISHED", {}, "CLOSED_OUT", CLOSE_OUT, [], "same", ""),
    ("PAUSED", "PR_FINISHED", {}, "CLOSED_OUT", CLOSE_OUT, [], "same", ""),
    ("BLOCKED", "PR_FINISHED", {}, "CLOSED_OUT", CLOSE_OUT, [], "same", ""),
    ("CLOSED_OUT", "PR_REOPENED", {}, "OFF", ["MIRROR"], [], "new", ""),
  ],
}

RESPOND = {
  "title": "Respond answer", "host": "respond_result", "initial": "NONE",
  "states": {
    "NONE": ("none", False, "No saved answer.", None),
    "READY": ("owner", False, "Replies ready. Waiting for you to press Post.", None),
    "APPROVED": ("platform", False, "You pressed Post; waiting for the chat to start the post.", (30, "TIMEOUT", "NOT_STARTED")),
    "NOT_STARTED": ("owner", False, "The chat did not start. Send again or take it back.", None),
    "POSTING": ("platform", False, "The helper is pushing and posting, step by step.", (3 * MINUTE, "TIMEOUT", "FAILED")),
    "FAILED": ("owner", False, "Stopped part-way. Try again skips what already went out.", None),
    "POSTED": ("none", True, "Posted.", None),
    "DISCARDED": ("none", True, "Discarded.", None),
    "OUT_OF_DATE": ("owner", False, "The PR moved after the answer was built. Respond again or discard.", None),
  },
  "transitions": [
    ("NONE", "SAVE", {}, "READY", [], [], "same", ""),
    ("READY", "SAVE", {}, "READY", [], [], "same", "the agent saved a newer answer"),
    ("READY", "APPROVE", {}, "APPROVED", ["TELL_CHAT"], [], "same", "your confirmation was recorded"),
    ("READY", "DISCARD", {}, "DISCARDED", [], [], "same", ""),
    ("READY", "PR_MOVED", {}, "OUT_OF_DATE", [], [], "same", ""),
    ("APPROVED", "CHAT_STARTED", {}, "POSTING", [], [], "same", ""),
    ("APPROVED", "TIMEOUT", {}, "NOT_STARTED", ["REQUEST_OWNER"], [], "changed", "30 seconds"),
    ("APPROVED", "PR_MOVED", {}, "OUT_OF_DATE", [], [], "same", ""),
    ("NOT_STARTED", "APPROVE", {}, "APPROVED", ["TELL_CHAT"], [], "same", ""),
    ("NOT_STARTED", "CHAT_STARTED", {}, "POSTING", [], [], "new", "the chat started late"),
    ("NOT_STARTED", "WITHDRAW", {}, "READY", [], [], "new", ""),
    ("NOT_STARTED", "PR_MOVED", {}, "OUT_OF_DATE", [], [], "same", ""),
    ("POSTING", "PROGRESS", {}, "POSTING", [], [], "same", "a step went out; the deadline starts again"),
    ("POSTING", "POST_OK", {}, "POSTED", ["CLEAR_ANSWERED_FINDINGS", "ASK_REREVIEW"], [], "changed", ""),
    ("POSTING", "POST_FAILED", {}, "FAILED", ["KEEP_RECEIPTS"], [], "same", ""),
    ("POSTING", "TIMEOUT", {}, "FAILED", ["RELEASE_LOCK", "KEEP_RECEIPTS"], [], "new", "3 minutes without progress; the helper is gone"),
    ("FAILED", "APPROVE", {}, "APPROVED", ["TELL_CHAT"], [], "same", "a fresh confirmation; the PR is still where the work was built"),
    ("FAILED", "PR_MOVED", {}, "OUT_OF_DATE", [], [], "same", ""),
    ("FAILED", "DISCARD", {}, "DISCARDED", [], [], "changed", ""),
    ("OUT_OF_DATE", "RESPOND_AGAIN", {}, "NONE", [], [], "same", ""),
    ("OUT_OF_DATE", "DISCARD", {}, "DISCARDED", [], [], "same", ""),
  ],
}

REVIEW = {
  "title": "Agent review", "host": "review_item", "initial": "REVIEWING",
  "states": {
    "REVIEWING": ("agent", False, "The agent is reviewing.", (60 * MINUTE, "TIMEOUT", "STALLED")),
    "STALLED": ("owner", False, "The agent stopped without a result. Review again or stop.", None),
    "CLEAR": ("none", False, "Reviewed this version; nothing found.", None),
    "PROBLEMS": ("owner", False, "Findings. Fix them, set them aside, or post them.", None),
    "NOT_FINISHED": ("owner", False, "The review hit an obstacle and says so. Review again.", None),
    "ASKS": ("owner", False, "The agent asked you something.", None),
    "ANSWERED": ("none", True, "The findings were answered by a posted reply.", None),
    "SET_ASIDE": ("none", True, "You set these findings aside (for this review only).", None),
    "STALE": ("owner", False, "The PR has new commits since this review.", None),
    "STOPPED": ("none", True, "You stopped it.", None),
    "REPLACED": ("none", True, "A newer review replaced this one.", None),
  },
  "transitions": [
    ("REVIEWING", "OUTCOME_CLEAR", {}, "CLEAR", [], [], "same", ""),
    ("REVIEWING", "OUTCOME_PROBLEMS", {}, "PROBLEMS", [], [], "same", ""),
    ("REVIEWING", "OUTCOME_BLOCKED", {}, "NOT_FINISHED", [], [], "changed", "the agent named an obstacle"),
    ("REVIEWING", "AGENT_ASKS", {}, "ASKS", [], [], "same", ""),
    ("REVIEWING", "CHAT_ENDED", {}, "STALLED", ["REQUEST_OWNER"], [], "new", "the review chat is over and no result was saved"),
    ("REVIEWING", "TIMEOUT", {}, "STALLED", ["REQUEST_OWNER"], [], "new", "60 minutes"),
    ("REVIEWING", "STOP", {}, "STOPPED", [], [], "same", ""),
    ("REVIEWING", "REVIEW_AGAIN", {}, "REPLACED", ["START_NEW_REVIEW"], [], "same", ""),
    ("ASKS", "OWNER_ANSWERS", {}, "REVIEWING", [], [], "same", ""),
    ("ASKS", "CHAT_ENDED", {}, "STALLED", ["REQUEST_OWNER"], [], "new", ""),
    ("ASKS", "STOP", {}, "STOPPED", [], [], "same", ""),
    ("STALLED", "OUTCOME_CLEAR", {}, "CLEAR", [], [], "new", "a late result still counts"),
    ("STALLED", "OUTCOME_PROBLEMS", {}, "PROBLEMS", [], [], "new", "a late result still counts"),
    ("STALLED", "OUTCOME_BLOCKED", {}, "NOT_FINISHED", [], [], "new", "a late result still counts"),
    ("STALLED", "CHAT_WORKING", {}, "REVIEWING", [], [], "new", "the review chat works again: the owner reopened it, or its helper is back"),
    ("STALLED", "REVIEW_AGAIN", {}, "REPLACED", ["START_NEW_REVIEW"], [], "changed", ""),
    ("STALLED", "STOP", {}, "STOPPED", [], [], "new", ""),
    ("NOT_FINISHED", "REVIEW_AGAIN", {}, "REPLACED", ["START_NEW_REVIEW"], [], "same", ""),
    ("PROBLEMS", "REVIEW_AGAIN", {}, "REPLACED", ["START_NEW_REVIEW"], [], "same", ""),
    ("CLEAR", "REVIEW_AGAIN", {}, "REPLACED", ["START_NEW_REVIEW"], [], "same", ""),
    ("PROBLEMS", "SET_ASIDE", {}, "SET_ASIDE", [], [], "changed", "tied to this review, not to the PR version"),
    ("PROBLEMS", "ANSWER_POSTED", {}, "ANSWERED", [], [], "new", "a posted reply covers every point"),
    ("PROBLEMS", "HEAD_PUSHED", {}, "STALE", [], [], "same", ""),
    ("CLEAR", "HEAD_PUSHED", {}, "STALE", [], [], "same", ""),
    ("NOT_FINISHED", "HEAD_PUSHED", {}, "STALE", [], [], "same", ""),
    ("STALE", "REVIEW_AGAIN", {}, "REPLACED", ["START_NEW_REVIEW"], [], "same", ""),
  ],
}

VERDICT = {
  "title": "Verdict", "host": "review_verdict", "initial": "NONE",
  "states": {
    "NONE": ("none", False, "No verdict proposed.", None),
    "PROPOSED": ("owner", False, "Waiting for you to submit or discard.", None),
    "POSTING": ("platform", False, "Submitting to GitHub.", (5 * MINUTE, "TIMEOUT", "UNKNOWN")),
    "UNKNOWN": ("github", False, "Answer lost; reading GitHub for the marker.", (60 * MINUTE, "TIMEOUT", "NOT_DISPATCHED")),
    "NOT_DISPATCHED": ("owner", False, "Nothing reached GitHub. Submit again or discard.", None),
    "POSTED": ("none", True, "On GitHub.", None),
    "DISCARDED": ("none", True, "Discarded.", None),
    "OUTDATED": ("none", True, "The PR moved on; this proposal is for an older version.", None),
  },
  "transitions": [
    ("NONE", "PROPOSE", {}, "PROPOSED", [], [], "same", ""),
    ("PROPOSED", "SUBMIT", {}, "POSTING", [], [], "same", "your confirmation; one verdict per version"),
    ("PROPOSED", "DISCARD", {}, "DISCARDED", [], [], "same", ""),
    ("PROPOSED", "HEAD_PUSHED", {}, "OUTDATED", [], [], "changed", ""),
    ("POSTING", "POSTED_OK", {}, "POSTED", [], [], "same", ""),
    ("POSTING", "SUBMIT_LOST", {}, "UNKNOWN", ["START_OBSERVING"], [], "same", ""),
    ("POSTING", "TIMEOUT", {}, "UNKNOWN", ["START_OBSERVING"], [], "new", ""),
    ("POSTING", "NOT_SENT", {}, "NOT_DISPATCHED", [], [], "same", "GitHub refused it"),
    ("UNKNOWN", "OBSERVED_MARKER", {}, "POSTED", [], [], "same", ""),
    ("UNKNOWN", "TIMEOUT", {}, "NOT_DISPATCHED", [], [], "same", "1 hour and a full scan found nothing"),
    ("UNKNOWN", "DISMISS", {}, "NOT_DISPATCHED", [], [], "same", ""),
    ("NOT_DISPATCHED", "SUBMIT", {}, "POSTING", [], [], "same", ""),
    ("NOT_DISPATCHED", "DISCARD", {}, "DISCARDED", [], [], "same", ""),
    ("NOT_DISPATCHED", "OBSERVED_MARKER", {}, "POSTED", [], [], "new", "a late read found the marker"),
  ],
}

RUN = {
  "title": "Project run", "host": "project_cycle", "initial": "NONE",
  "states": {
    "NONE": ("none", False, "Nothing running.", None),
    "SENDING": ("platform", False, "The start message is on its way.", (2 * MINUTE, "TIMEOUT", "NOT_SENT")),
    "NOT_SENT": ("owner", False, "Could not confirm the start. Send again.", None),
    "RUNNING": ("agent", False, "The agent is working.", (60 * MINUTE, "TIMEOUT", "RUNNING")),
    "QUESTION": ("owner", False, "The agent asked you something.", None),
    "STOPPED": ("owner", False, "The run stopped before finishing. Your move.", None),
    "FAILED": ("owner", False, "The run failed. Your move.", None),
    "DONE": ("none", True, "Finished.", None),
  },
  "transitions": [
    ("NONE", "START", {}, "SENDING", ["WRITE_CYCLE_RECORD"], [], "same", "your confirmation"),
    ("SENDING", "DELIVERED", {}, "RUNNING", [], [], "same", "the chat answered"),
    ("SENDING", "TIMEOUT", {}, "NOT_SENT", ["REQUEST_OWNER"], [], "changed", ""),
    ("NOT_SENT", "SEND_AGAIN", {}, "SENDING", [], [], "same", ""),
    ("NOT_SENT", "DELIVERED", {}, "RUNNING", [], [], "new", "the chat answered late"),
    ("RUNNING", "RUN_WAITING", {}, "QUESTION", ["REQUEST_OWNER"], [], "same", ""),
    ("RUNNING", "RUN_STOPPED", {}, "STOPPED", ["REQUEST_OWNER"], [], "changed", "the platform says stopped, parked or interrupted"),
    ("RUNNING", "RUN_FAILED", {}, "FAILED", ["REQUEST_OWNER"], [], "changed", ""),
    ("RUNNING", "RUN_DONE", {}, "DONE", [], [], "same", ""),
    ("RUNNING", "TIMEOUT", {}, "RUNNING", ["ASK_PLATFORM"], [], "new", "an hour of silence: ask the platform what the chat is doing"),
    ("QUESTION", "OWNER_ANSWERS", {}, "RUNNING", [], [], "same", ""),
    ("QUESTION", "RUN_STOPPED", {}, "STOPPED", ["REQUEST_OWNER"], [], "changed", ""),
    ("QUESTION", "RUN_FAILED", {}, "FAILED", ["REQUEST_OWNER"], [], "changed", ""),
    ("QUESTION", "RUN_DONE", {}, "DONE", [], [], "same", ""),
    ("STOPPED", "START", {}, "SENDING", ["WRITE_CYCLE_RECORD"], [], "same", ""),
    ("FAILED", "START", {}, "SENDING", ["WRITE_CYCLE_RECORD"], [], "same", ""),
    ("STOPPED", "RUN_DONE", {}, "DONE", [], [], "new", "a parked run was resumed and finished"),
    ("STOPPED", "DISMISS", {}, "DONE", [], [], "new", "you stopped it yourself, or looked and need nothing more"),
    ("FAILED", "DISMISS", {}, "DONE", [], [], "new", "you looked and need nothing more"),
    ("NOT_SENT", "DISMISS", {}, "NONE", [], [], "new", "you gave up starting it"),
  ],
}

REGIONS = {
  "publication": PUBLICATION, "update": UPDATE, "followup": FOLLOWUP, "respond": RESPOND,
  "review": REVIEW, "verdict": VERDICT, "run": RUN,
}

# Attention: a set of flags on the record. One is shown (the first by priority), the rest wait.
# Flag names are the `attention.type` values the ledger already stores.
# rule: (event, when, action, flags, logged, vs, guard_text); action = raise | clear | clear_all | keep
GITHUB_FLAGS = ["merge_conflict", "checks_failed", "changes_requested", "github_activity", "outside_activity"]
ATTENTION = {
  "priority": ["human_required", "merge_conflict", "checks_failed", "changes_requested", "github_activity", "outside_activity"],
  "rules": [
    ("CHECKS_CHANGED", {"state": "failing", "all_inherited": [False, None]}, "raise", ["checks_failed"], False, "same", "at least one failing check does not also fail on the default branch"),
    ("CHECKS_CHANGED", {"state": "failing", "all_inherited": True}, "clear", ["checks_failed"], True, "new", "every failing check also fails on the default branch"),
    ("CHECKS_CHANGED", {"state": "passing"}, "clear", ["checks_failed"], False, "same", ""),
    ("CHECKS_CHANGED", {"state": "pending"}, "keep", [], False, "same", "a run in progress decides nothing"),
    ("REVIEW_CHANGED", {"changes_requested": True, "trusted": [True, None]}, "raise", ["changes_requested"], False, "same", "a trusted reviewer requests changes"),
    ("REVIEW_CHANGED", {"changes_requested": True, "trusted": False}, "keep", [], True, "same", "an untrusted reviewer's request raises nothing"),
    ("REVIEW_CHANGED", {"changes_requested": False}, "clear", ["changes_requested"], False, "changed", "no change request stands any more"),
    ("REVIEW_CHANGED", {"changes_requested": None}, "keep", [], False, "same", "the read did not ask for the review decision, so it decides nothing"),
    ("COMMENTED", {"by_owner": True}, "keep", [], True, "new", "written by you"),
    ("COMMENTED", {"by_owner": [False, None], "trusted": [True, None]}, "raise", ["github_activity"], False, "same", "written by someone else, trusted"),
    ("COMMENTED", {"by_owner": [False, None], "trusted": False}, "raise", ["outside_activity"], False, "same", "written by someone outside the project"),
    ("ANSWERED", {}, "clear", ["github_activity", "outside_activity"], False, "same", "a round settled it, or you replied"),
    ("MERGEABLE_CHANGED", {"state": "conflicting"}, "raise", ["merge_conflict"], False, "same", ""),
    ("MERGEABLE_CHANGED", {"state": "mergeable"}, "clear", ["merge_conflict"], False, "same", ""),
    ("MERGEABLE_CHANGED", {"state": "unknown"}, "keep", [], False, "same", "GitHub has not computed it yet"),
    ("ESCALATED", {}, "raise", ["human_required"], False, "changed", "an agent needs a person; held, never overwritten"),
    ("RESUME", {}, "clear", ["human_required"], False, "changed", "you pressed Resume"),
    ("STAGE_UPDATE", {}, "clear", GITHUB_FLAGS, True, "changed", "the update answers what GitHub raised"),
    ("PR_FINISHED", {}, "clear_all", [], False, "changed", "merged or closed: every flag goes, the waiting ones too"),
    ("HEAD_PUSHED", {}, "keep", [], False, "same", "checks and conflict are read again for the new head"),
  ],
}

# The stored `status` word, derived from the regions so existing readers keep working.
# First matching row wins: (where, word, other words the row also accepts). An update that waits
# on a public PR still reads `prepared`, because Send claims a record by that word. A send whose
# answer was lost reads `submitting` or `prepared` depending on which writer noticed, so the
# stored word is kept when the row accepts it. `landing` is the platform's own claim word while it
# lands an existing stack: the pull request is open, and the word stays the platform's to change.
PUBLIC = ["DRAFT", "OPEN", "QUEUED"]
STATUS = [
  ({"publication": PUBLIC, "update": ["PREPARED_UPDATE", "UPDATE_NOT_CONFIRMED"]}, "prepared", []),
  ({"publication": PUBLIC, "update": "SENDING_UPDATE"}, "submitting", []),
  ({"publication": PUBLIC, "update": "UPDATE_UNCERTAIN"}, "submitting", ["prepared"]),
  ({"publication": ["PREPARED", "NOT_CONFIRMED"]}, "prepared", []),
  ({"publication": "SENDING"}, "submitting", []),
  ({"publication": "UNCERTAIN"}, "submitting", ["prepared"]),
  ({"publication": "DRAFT"}, "draft", []),
  ({"publication": ["OPEN", "QUEUED"]}, "open", ["landing"]),
  ({"publication": ["MERGED", "MERGED_ELSEWHERE", "ORPHANED"]}, "merged", []),
  ({"publication": ["CLOSED", "CLOSED_QUIET"]}, "closed", []),
  ({"publication": "ABANDONED"}, "abandoned", []),
  ({"publication": "SUPERSEDED"}, "superseded", []),
]

# A record that has no lifecycle yet starts from the facts the ledger already stores.
# facts: status, is_update (plan.action is pr_update), has_pr (a PR url or number), unconfirmed
# (the last send's outcome is not known), following (the follow-up mirror: off, idle,
# responding, paused, blocked). First matching row wins: (facts, states, events raised once the
# regions stand). A PR that is already finished raises PR_FINISHED, so a follow-up grant that
# was left on is closed out the first time the record is seen. A record an older app wrote
# `abandoned` when only its update was discarded still has a public PR: it starts as OPEN and
# the next GitHub read says where the PR really stands.
FINISHED_PR = ["PR_FINISHED"]
SEED = [
  ({"status": "prepared", "is_update": True, "has_pr": True, "unconfirmed": True}, {"publication": "OPEN", "update": "UPDATE_UNCERTAIN"}, []),
  ({"status": "prepared", "is_update": True, "has_pr": True}, {"publication": "OPEN", "update": "PREPARED_UPDATE"}, []),
  ({"status": "submitting", "is_update": True, "has_pr": True}, {"publication": "OPEN", "update": "SENDING_UPDATE"}, []),
  ({"status": "prepared", "unconfirmed": True}, {"publication": "UNCERTAIN"}, []),
  ({"status": "prepared"}, {"publication": "PREPARED"}, []),
  ({"status": "submitting"}, {"publication": "SENDING"}, []),
  ({"status": "draft"}, {"publication": "DRAFT"}, []),
  ({"status": ["open", "landing"]}, {"publication": "OPEN"}, []),
  ({"status": "merged"}, {"publication": "MERGED"}, FINISHED_PR),
  ({"status": "closed"}, {"publication": "CLOSED"}, FINISHED_PR),
  ({"status": "abandoned", "is_update": True, "has_pr": True}, {"publication": "OPEN"}, []),
  ({"status": "abandoned"}, {"publication": "ABANDONED"}, []),
  ({"status": "superseded"}, {"publication": "SUPERSEDED"}, FINISHED_PR),
]
SEED_FOLLOWUP = {"off": "OFF", "idle": "IDLE", "responding": "RESPONDING", "paused": "PAUSED", "blocked": "BLOCKED"}

# Another writer changed the status word (the platform's routes, an older app). The engine
# turns that into the events that explain it, logged as inferred. `where` matches the region
# states, `facts` the same facts as SEED; each event is (type, region or None, data).
# No matching row means the regions are seeded again from the facts. A row marked "corrects"
# overrules the stored word instead of following it: discarding an update must not abandon a
# pull request that is still public.
SENT_UPDATE = ["SENDING_UPDATE", "UPDATE_UNCERTAIN", "UPDATE_NOT_CONFIRMED"]
UPDATE_ARRIVED = [("UPDATE_OK", "update", {}), ("OBSERVED_HEAD", "update", {})]
STATUS_EDGES = [
  ({"publication": "PREPARED"}, {"status": "submitting"}, [("SEND_CONFIRMED", "publication", {})]),
  ({"publication": "SENDING"}, {"status": "draft"}, [("SUBMIT_OK", "publication", {"is_draft": True})]),
  ({"publication": "SENDING"}, {"status": "open"}, [("SUBMIT_OK", "publication", {"is_draft": False})]),
  ({"publication": "SENDING"}, {"status": "prepared", "unconfirmed": True}, [("SUBMIT_LOST", "publication", {})]),
  ({"publication": "SENDING"}, {"status": "prepared"}, [("SUBMIT_REFUSED", "publication", {})]),
  ({"publication": ["UNCERTAIN", "NOT_CONFIRMED"]}, {"status": "draft"}, [("OBSERVED_PR", "publication", {"is_draft": True})]),
  ({"publication": ["UNCERTAIN", "NOT_CONFIRMED"]}, {"status": "open"}, [("OBSERVED_PR", "publication", {"is_draft": False})]),
  ({"publication": ["UNCERTAIN", "NOT_CONFIRMED"]}, {"status": "prepared", "unconfirmed": [False, None]}, [("SUBMIT_REFUSED", "publication", {})]),
  ({"publication": "NOT_CONFIRMED"}, {"status": "submitting"}, [("OWNER_CHECKS", "publication", {})]),
  ({"publication": ["PREPARED", "NOT_CONFIRMED"]}, {"status": "abandoned"}, [("DISCARD", "publication", {})]),
  ({"publication": "ABANDONED"}, {"status": "prepared"}, [("RESTORE", "publication", {})]),
  ({"publication": "DRAFT", "update": "NONE"}, {"status": "open"}, [("MARKED_READY", "publication", {})]),
  ({"publication": ["OPEN", "QUEUED"], "update": "NONE"}, {"status": "draft"}, [("MARKED_DRAFT", "publication", {})]),
  ({"publication": PUBLIC + ["CLOSED"]}, {"status": "merged"}, [("MERGED", "publication", {})]),
  ({"publication": PUBLIC}, {"status": "closed"}, [("CLOSED", "publication", {})]),
  ({"publication": PUBLIC, "update": "NONE"}, {"status": "prepared", "is_update": True}, [("STAGE_UPDATE", None, {})]),
  ({"publication": PUBLIC, "update": "PREPARED_UPDATE"}, {"status": "submitting"}, [("SEND_UPDATE_CONFIRMED", "update", {})]),
  ({"publication": PUBLIC, "update": "SENDING_UPDATE"}, {"status": "prepared", "unconfirmed": True}, [("UPDATE_LOST", "update", {})]),
  ({"publication": PUBLIC, "update": "SENDING_UPDATE"}, {"status": "prepared"}, [("UPDATE_REFUSED", "update", {})]),
  ({"publication": PUBLIC, "update": ["UPDATE_UNCERTAIN", "UPDATE_NOT_CONFIRMED"]}, {"status": "prepared", "unconfirmed": [False, None]}, [("UPDATE_REFUSED", "update", {})]),
  ({"publication": "DRAFT", "update": SENT_UPDATE}, {"status": "draft"}, UPDATE_ARRIVED),
  ({"publication": ["OPEN", "QUEUED"], "update": SENT_UPDATE}, {"status": "open"}, UPDATE_ARRIVED),
  ({"publication": "DRAFT", "update": SENT_UPDATE}, {"status": "open"}, UPDATE_ARRIVED + [("MARKED_READY", "publication", {})]),
  ({"publication": PUBLIC, "update": ["PREPARED_UPDATE", "UPDATE_NOT_CONFIRMED"]}, {"status": "abandoned"}, [("DISCARD_UPDATE", "update", {})], "corrects"),
  ({"publication": PUBLIC, "update": "NONE"}, {"status": "abandoned", "is_update": True, "has_pr": True}, [], "corrects"),
]

# What a GitHub read shows is a level, not an edge. `where` is the publication state, `level`
# the read (pr: OPEN, CLOSED or MERGED; is_draft; queued; base_is_default); the events are the edges that explain
# it. Their data comes from the read itself (is_draft, base_is_default, merged).
LEVEL_EDGES = [
  (["DRAFT"], {"pr": "OPEN", "is_draft": False, "queued": [False, None]}, ["MARKED_READY"]),
  (["DRAFT"], {"pr": "OPEN", "is_draft": False, "queued": True}, ["MARKED_READY", "QUEUE_ENTERED"]),
  (["OPEN"], {"pr": "OPEN", "is_draft": True}, ["MARKED_DRAFT"]),
  (["OPEN"], {"pr": "OPEN", "is_draft": False, "queued": True}, ["QUEUE_ENTERED"]),
  (["QUEUED"], {"pr": "OPEN", "is_draft": True}, ["MARKED_DRAFT"]),
  (["QUEUED"], {"pr": "OPEN", "is_draft": False, "queued": [False, None]}, ["QUEUE_LEFT"]),
  (PUBLIC + ["CLOSED", "CLOSED_QUIET"], {"pr": "MERGED"}, ["MERGED"]),
  (["MERGED"], {"pr": "MERGED", "base_is_default": False}, ["BASE_OBSERVED"]),
  (PUBLIC, {"pr": "CLOSED"}, ["CLOSED"]),
  (["CLOSED", "CLOSED_QUIET"], {"pr": "OPEN", "queued": [False, None]}, ["REOPENED"]),
  (["CLOSED", "CLOSED_QUIET"], {"pr": "OPEN", "is_draft": False, "queued": True}, ["REOPENED", "QUEUE_ENTERED"]),
  (["UNCERTAIN", "NOT_CONFIRMED"], {"pr": "OPEN"}, ["OBSERVED_PR"]),
  (["UNCERTAIN", "NOT_CONFIRMED"], {"pr": "MERGED"}, ["OBSERVED_PR", "MERGED"]),
  (["UNCERTAIN", "NOT_CONFIRMED"], {"pr": "CLOSED"}, ["OBSERVED_PR", "CLOSED"]),
]

HISTORY_LIMIT = 30


def _state(row: tuple) -> dict:
  waits, final, text, deadline = row
  out = {"waits": waits, "final": final, "text": text}
  if deadline:
    out["deadline"] = {"seconds": deadline[0], "event": deadline[1], "to": deadline[2]}
  return out


def _transition(row: tuple) -> dict:
  source, event, when, target, effects, raises, vs, guard = row
  return {"from": source, "event": event, "when": when, "to": target, "effects": list(effects),
          "raises": list(raises), "vs": vs, "guard": guard}


def _region(region: dict) -> dict:
  return {"title": region["title"], "host": region["host"], "initial": region["initial"],
          "states": {name: _state(row) for name, row in region["states"].items()},
          "transitions": [_transition(row) for row in region["transitions"]]}


def _attention_rule(row: tuple) -> dict:
  event, when, action, flags, logged, vs, guard = row
  return {"event": event, "when": when, "action": action, "flags": list(flags), "logged": logged,
          "vs": vs, "guard": guard}


def _event(row: tuple) -> dict:
  kind, region, data = row
  return {"type": kind, "region": region, "data": data}


def tables() -> dict:
  """The tables in the plain form both interpreters read."""
  return {
    "version": VERSION, "history_limit": HISTORY_LIMIT, "targets": TARGETS,
    "regions": {name: _region(region) for name, region in REGIONS.items()},
    "attention": {"priority": ATTENTION["priority"],
                  "rules": [_attention_rule(row) for row in ATTENTION["rules"]]},
    "status": [{"where": where, "status": status, "also": also} for where, status, also in STATUS],
    "seed": [{"facts": facts, "states": states, "events": list(events)} for facts, states, events in SEED],
    "seed_followup": SEED_FOLLOWUP,
    "status_edges": [{"where": row[0], "facts": row[1], "events": [_event(event) for event in row[2]],
                      "corrects": "corrects" in row[3:]} for row in STATUS_EDGES],
    "level_edges": [{"where": where, "level": level, "events": list(events)}
                    for where, level, events in LEVEL_EDGES],
  }


def canonical_json() -> str:
  """One byte-exact rendering, so two copies can be compared by hash."""
  return json.dumps(tables(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def javascript_module() -> str:
  body = json.dumps(tables(), sort_keys=True, indent=1, ensure_ascii=True)
  return ("// GENERATED from lifecycle_tables.py by `python3 lifecycle_tables.py --emit-js`. Do not edit:\n"
          "// test/lifecycle-engine.test.mjs fails when this file and the Python tables differ.\n"
          "export const LIFECYCLE_TABLES = " + body + "\n")


if __name__ == "__main__":
  sys.stdout.write(javascript_module() if "--emit-js" in sys.argv else canonical_json() + "\n")
