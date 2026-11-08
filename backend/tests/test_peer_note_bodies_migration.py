"""Peer-note bodies move into one shared row per send without losing mail."""
from sqlalchemy import create_engine, inspect, text

from app import schema_migrations as migrations


def test_existing_peer_mail_moves_its_bodies_and_retries_safely(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
  try:
    with eng.begin() as conn:
      conn.execute(text(
        "CREATE TABLE agent_coordination_messages (id VARCHAR(64) PRIMARY KEY, "
        "room_kind VARCHAR(16) NOT NULL, room_id VARCHAR(64) NOT NULL, "
        "from_chat_id VARCHAR(64) NOT NULL, to_chat_id VARCHAR(64), "
        "body TEXT NOT NULL, created_at TIMESTAMP NOT NULL)"
      ))
      conn.execute(text(
        "INSERT INTO agent_coordination_messages VALUES "
        "('m1', 'workspace', '1', 'a', 'b', 'first note', '2026-01-01 00:00:00'), "
        "('m2', 'delegation', 'd', 'a', NULL, 'second note', '2026-01-01 00:00:01')"
      ))

    migrations._store_peer_note_bodies_once(eng)
    migrations._store_peer_note_bodies_once(eng)

    columns = {c["name"] for c in inspect(eng).get_columns("agent_coordination_messages")}
    assert "body" not in columns and "note_id" in columns
    with eng.connect() as conn:
      rows = conn.execute(text(
        "SELECT m.id, n.body, n.body_chars FROM agent_coordination_messages m "
        "JOIN agent_coordination_notes n ON n.id = m.note_id ORDER BY m.id"
      )).all()
    assert [tuple(row) for row in rows] == [
      ("m1", "first note", 10), ("m2", "second note", 11),
    ]
  finally:
    eng.dispose()
