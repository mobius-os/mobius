"""Which installed app's recall receipts this platform will honor.

The platform binds one installed-app entry point to the app that owns shared
Memory and recognizes its two documented invocation arities. Memory owns the
retrieval protocol, paging, cursors, labels, and accounting; the platform only
authenticates the provider and carries its bounded result envelope.

The basename below is the last Memory-shaped string on the authorization path.
It lives here, next to installed-app discovery, so a future manifest-declared
entry point replaces one line rather than a filesystem pattern in core.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app import models
from app.memory_recall import EMPTY_RECALL_BINDING, RecallBinding

log = logging.getLogger(__name__)

# The owner-reviewed permission tier that carries authority over the shared
# memory graph. A recall citation is not a read claim, it is an AUTHORSHIP
# claim — "these notes exist in your graph, at these ids, and this card
# navigates to them". Only the app with write authority can honestly make it.
SHARED_MEMORY_PROVIDER_TIER = "write"

RECALL_ENTRY_BASENAME = "memory_search.py"


def shared_memory_level(contract: object) -> str:
  """Total read of ``data.shared_memory``; anything unexpected is "none"."""
  if not isinstance(contract, dict):
    return "none"
  data = contract.get("data")
  if not isinstance(data, dict):
    return "none"
  level = data.get("shared_memory")
  return level if isinstance(level, str) else "none"


def resolve_recall_binding(
  db: Session,
  *,
  include_uninstalled: bool = False,
) -> RecallBinding:
  """Bind the recall entry point to the app holding shared-memory authority.

  Minting a NEW citation requires a live grant. Recognizing HISTORY only
  requires that the path once belonged to a grant holder, which is why the
  read path passes ``include_uninstalled``: uninstall is a soft delete that
  keeps ``source_dir`` and ``capability_contract`` on the row, and an owner
  who removes the Memory app should not have last month's transcripts quietly
  lose their citations.

  Never raises. A citation is observability metadata layered onto a turn; it
  must not be able to fail the turn, or a transcript read, by throwing.
  """
  try:
    query = db.query(
      models.App.id,
      models.App.slug,
      models.App.source_dir,
      models.App.capability_contract,
    )
    if not include_uninstalled:
      query = query.filter(models.App.deleted_at.is_(None))
    pairs: list[tuple[str, str]] = []
    for app_id, slug, source_dir, contract in query.order_by(
      models.App.id.asc()
    ).all():
      if not source_dir:
        continue
      if shared_memory_level(contract) != SHARED_MEMORY_PROVIDER_TIER:
        continue
      label = slug or str(app_id)
      base = Path(source_dir)
      # Bind BOTH the stored and the resolved form. They are the same string
      # whenever no ancestor of the app directory is a symlink — but the skill
      # tells the agent to substitute the stored source_dir while the system
      # prompt prints the resolved one, so a single symlinked ancestor would
      # otherwise make every real lookup unrecognizable.
      forms = [base / RECALL_ENTRY_BASENAME]
      try:
        forms.append(base.resolve() / RECALL_ENTRY_BASENAME)
      except OSError:
        pass
      for form in forms:
        pairs.append((str(form), label))
    return RecallBinding.of(pairs)
  except Exception:
    log.exception("recall binding unavailable; citations disabled this pass")
    return EMPTY_RECALL_BINDING
