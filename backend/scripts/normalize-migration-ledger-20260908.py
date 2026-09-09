#!/usr/bin/env python3
"""Explicit 2026-09-08 ledger cutover; never runs migration bodies.

Default is read-only. After backup/clone validation and owner approval, use
--apply before deploying the exact-ID runner. Preserve this finite mapping for
old-backup restores; do not extend it for future source reconciliations.
"""
import argparse
import json
import sqlite3
from pathlib import Path


# Historical completion does not assert byte-identical bodies. Behavioral
# differences need new forward migrations, never replay of recorded work.
PUBLISHED_LOCAL_IDS = {
  '0018_app_connect_manage': '0016_app_connect_manage',
  '0023_retire_restart_resume_toggle': '0017_retire_restart_resume_toggle',
  '0019_explicit_legacy_chat_models': '0018_explicit_legacy_chat_models',
  '0025_chat_active_assistant_identity': '0019_chat_active_assistant_identity',
  '0026_project_color': '0023_project_color',
  '0017_chat_goal_dismissal': '0024_chat_goal_dismissal',
  '0030_attached_delegation_work': '0025_attached_delegation_work',
  '0038_chat_wait_condition_owner': '0026_chat_wait_condition_owner',
  '0034_chat_run_goal_identity_index': '0027_chat_run_goal_identity_index',
  '0035_agent_coordination_rooms': '0028_agent_coordination_rooms',
  '0036_agent_coordination_send_identity': '0029_agent_coordination_send_identity',
  '0037_agent_coordination_send_target': '0030_agent_coordination_send_target',
  '0016_chat_retention_orphan_repair': '0031_chat_retention_orphan_repair',
  '0024_owner_auth_mode': '0032_owner_auth_mode',
  '0027_shared_app_retention': '0033_shared_app_retention',
  '0028_shared_app_path_state': '0034_shared_app_path_state',
  '0029_project_artifact_drawer_state': '0035_project_artifact_drawer_state',
  '0031_chat_app_artifacts': '0036_chat_app_artifacts',
  '0032_explicit_active_chat_models': '0037_explicit_active_chat_models',
  '0033_repair_post_0032_model_gaps': '0038_repair_active_chat_model_gaps',
}


def pending_completions(conn):
  applied = dict(conn.execute("SELECT version, applied_at FROM schema_migrations"))
  return [(canonical, applied[old]) for old, canonical in PUBLISHED_LOCAL_IDS.items()
          if old in applied and canonical not in applied]


def normalize_ledger(conn):
  """Atomically add completion names; retain every original row and timestamp."""
  conn.execute("BEGIN IMMEDIATE")
  try:
    pending = pending_completions(conn)
    conn.executemany(
      "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", pending,
    )
    conn.commit()
  except BaseException:
    conn.rollback()
    raise
  return pending


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("database", type=Path)
  parser.add_argument("--apply", action="store_true",
                      help="Write completion rows; requires an approved, validated backup")
  args = parser.parse_args()
  # mode=rw refuses a typo rather than creating an empty database.
  uri = args.database.resolve().as_uri() + ("?mode=rw" if args.apply else "?mode=ro")
  with sqlite3.connect(uri, uri=True) as conn:
    rows = normalize_ledger(conn) if args.apply else pending_completions(conn)
  print(json.dumps({"applied": args.apply, "completions": rows}, indent=2))


if __name__ == "__main__":
  main()
