"""Binding the recall recognizer to whichever app holds memory authority.

The platform used to find the Memory app with a regex over
``/data/apps/memory(-N)?/memory_search.py`` — a filesystem root, a slug family,
and a filename compiled into core. These tests pin the replacement: authority
comes from the installed app that holds shared-memory write access, so the
platform never needs to know any app's name or address.
"""

from pathlib import Path

from app import models
from app.memory_provider import resolve_recall_binding
from app.memory_recall import recall_from_command


def _memory_app(db, *, slug, source_dir, level="write", deleted_at=None):
  app = models.App(
    name=f"App {slug}",
    description="test",
    jsx_source="export default function App() { return <div/> }",
    slug=slug,
    source_dir=str(source_dir),
    capability_contract={"data": {"shared_memory": level}},
    deleted_at=deleted_at,
  )
  db.add(app)
  db.commit()
  return app


def test_binding_follows_the_install_not_a_slug_pattern(db, tmp_path):
  """An app at ANY slug provides recall when it holds the capability."""
  source_dir = tmp_path / "apps" / "second-brain"
  _memory_app(db, slug="second-brain", source_dir=source_dir)

  binding = resolve_recall_binding(db)
  script = str(source_dir / "memory_search.py")

  assert binding.entry_for(script) == ("second-brain", "catalog")
  assert recall_from_command(
    f'python3 {script} "what did we decide" "chat-1"', binding,
  ) == {
    "status": "searching",
    "app_slug": "second-brain",
    "phase": "catalog",
    "query": "what did we decide",
  }
  read_script = str(source_dir / "memory_read.py")
  assert recall_from_command(
    f'python3 {read_script} "{"a" * 64}" "all" "start" "chat-1"',
    binding,
  )["phase"] == "read"


def test_an_app_without_memory_authority_cannot_mint_citations(db, tmp_path):
  """A citation is an authorship claim, so read-only access is not enough.

  Any app can be told to print the receipt line. Only the app the owner gave
  authority over the graph can honestly claim the notes exist in it.
  """
  none_dir = tmp_path / "apps" / "unrelated"
  read_dir = tmp_path / "apps" / "reader"
  _memory_app(db, slug="unrelated", source_dir=none_dir, level="none")
  _memory_app(db, slug="reader", source_dir=read_dir, level="read")

  binding = resolve_recall_binding(db)

  assert binding.is_empty
  assert binding.entry_for(str(none_dir / "memory_search.py")) is None
  assert binding.entry_for(str(read_dir / "memory_search.py")) is None


def test_uninstalling_the_provider_does_not_erase_past_citations(db, tmp_path):
  """Minting needs a live grant; recognizing history does not.

  Uninstall is a soft delete that keeps source_dir and the contract on the row.
  An owner who removes Memory should not find last month's transcripts quietly
  stripped of the citations they already read.
  """
  from datetime import UTC, datetime

  source_dir = tmp_path / "apps" / "memory"
  _memory_app(
    db, slug="memory", source_dir=source_dir,
    deleted_at=datetime.now(UTC),
  )
  script = str(source_dir / "memory_search.py")

  assert resolve_recall_binding(db).entry_for(script) is None
  assert resolve_recall_binding(
    db, include_uninstalled=True,
  ).entry_for(script) == ("memory", "catalog")


def test_a_broken_contract_disables_citations_rather_than_raising(db, tmp_path):
  """A citation is metadata layered onto a turn; it must never fail one."""
  app = _memory_app(
    db, slug="memory", source_dir=tmp_path / "apps" / "memory",
  )
  for junk in ("not-a-dict", {"data": "not-a-dict"}, {}, None):
    app.capability_contract = junk
    db.commit()
    assert resolve_recall_binding(db).is_empty


def test_a_symlinked_app_root_binds_both_path_forms(db, tmp_path):
  """The skill hands the agent source_dir; the prompt prints it resolved.

  Those are different strings whenever any ancestor of the app directory is a
  symlink, and binding only one form would make every real lookup on such an
  instance silently uncitable.
  """
  real = tmp_path / "real-apps"
  (real / "memory").mkdir(parents=True)
  link = tmp_path / "apps"
  link.symlink_to(real)
  _memory_app(db, slug="memory", source_dir=link / "memory")

  binding = resolve_recall_binding(db)

  assert binding.entry_for(str(link / "memory" / "memory_search.py")) == ("memory", "catalog")
  assert binding.entry_for(str(real / "memory" / "memory_search.py")) == ("memory", "catalog")


def test_the_platform_names_no_app_on_the_authorization_path():
  """The regression this whole module exists to prevent.

  memory_recall is the PROTOCOL owner. If a slug, an apps root, or an entry
  filename reappears in it, the platform has started guessing at an app's
  address again instead of being told.
  """
  source = Path(__file__).resolve().parents[1] / "app" / "memory_recall.py"
  text = source.read_text(encoding="utf-8")

  assert "/data/apps" not in text
  # A quoted entry filename is the hardcoding; prose describing the protocol
  # is not. Check for the literal as CODE would spell it.
  assert '"memory_search.py"' not in text
  assert "'memory_search.py'" not in text
