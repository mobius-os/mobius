"""Resolve manifest-declared app activity commands for one agent turn."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app import models
from app.agent_activity import (
  EMPTY_AGENT_ACTIVITY_BINDING,
  ActivityCommand,
  AgentActivityBinding,
)
from app.applied_app_runtime import AppliedRuntimeUnavailable, runtime_root
from app.manifest_contract import validate_manifest_contract

log = logging.getLogger(__name__)


def resolve_agent_activity_binding(db: Session) -> AgentActivityBinding:
  """Bind exact command paths from installed apps' applied manifests.

  This is presentation authority, not a data grant. The app's ordinary
  permission contract still controls every API or shared-data access it uses.
  """
  try:
    pairs: list[tuple[str, ActivityCommand]] = []
    rows = db.query(models.App).filter(
      models.App.deleted_at.is_(None),
    ).order_by(models.App.id.asc()).all()
    for app in rows:
      if not app.source_dir or not app.slug:
        continue
      base = Path(app.source_dir)
      try:
        manifest = json.loads(
          (runtime_root(app) / "mobius.json").read_text("utf-8"),
        )
        validate_manifest_contract(manifest)
      except (AppliedRuntimeUnavailable, OSError, UnicodeError, ValueError):
        continue
      declarations = manifest.get("agent_activities")
      if not isinstance(declarations, dict):
        continue
      for activity_id, raw in declarations.items():
        if not isinstance(raw, dict):
          continue
        entry = raw.get("entry")
        argument_count = raw.get("arguments")
        running_label = raw.get("running_label")
        if (
          not isinstance(entry, str)
          or not isinstance(argument_count, int)
          or isinstance(argument_count, bool)
          or not isinstance(running_label, str)
        ):
          continue
        command = ActivityCommand(
          app_slug=str(app.slug),
          app_name=str(app.name or app.slug),
          activity_id=str(activity_id),
          argument_count=argument_count,
          running_label=running_label.strip(),
        )
        forms = [base / entry]
        try:
          forms.append(base.resolve() / entry)
        except OSError:
          pass
        pairs.extend((str(path), command) for path in forms)
    return AgentActivityBinding.of(pairs)
  except Exception:
    log.exception("app activity binding unavailable; activity cards disabled")
    return EMPTY_AGENT_ACTIVITY_BINDING
