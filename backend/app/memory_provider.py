"""Which installed app's recall receipts this platform will honor.

``memory_recall`` owns the PROTOCOL — the receipt format, the argv arity, the
meaning of a non-zero exit. That is legitimately platform-owned, in the same
family as ``entry`` having to be ``index.jsx``. What the platform must never
own is the provider's street address, and until this module existed it guessed
one with a regex over ``/data/apps/memory(-N)?/memory_search.py``: a filesystem
root, a slug family, and a filename compiled into core.

The basename below is the last Memory-shaped string on the authorization path.
It lives here, next to the query, rather than in ``memory_recall``, because it
belongs to FINDING the provider and not to the wire format — and because this
is the one line a future manifest-declared entry point replaces.
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

# Keep discovery and expansion on the same authority boundary.  The operation
# name is protocol metadata; the provider still owns the street addresses.
RECALL_ENTRYPOINTS = (
  ("memory_search.py", "catalog"),
  ("memory_read.py", "read"),
)


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
    pairs: list[tuple[str, str, str]] = []
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
      forms = [
        (base / basename, operation)
        for basename, operation in RECALL_ENTRYPOINTS
      ]
      try:
        forms.extend(
          (base.resolve() / basename, operation)
          for basename, operation in RECALL_ENTRYPOINTS
        )
      except OSError:
        pass
      for form, operation in forms:
        pairs.append((str(form), label, operation))
    return RecallBinding.of(pairs)
  except Exception:
    log.exception("recall binding unavailable; citations disabled this pass")
    return EMPTY_RECALL_BINDING
