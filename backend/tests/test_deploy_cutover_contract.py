"""Build-time contract for the shell-owned production cutover loop."""

from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "deploy-prod.sh"


def test_prod_replacement_uses_exact_chat_handoff_and_rollback_receipt():
  source = SCRIPT.read_text(encoding="utf-8")

  assert "external-cutover-v1" in source
  assert 'cutover_ledger open-cutover "$CUTOVER_ID"' in source
  assert 'prepare-container-cutover.py "$CUTOVER_ID"' in source
  assert 'cutover_ledger accept-cutover "$CUTOVER_ID"' in source
  assert source.index("prepare_chat_cutover") < source.index(
    'docker compose "${COMPOSE_ARGS[@]}" up -d "${recreate_args[@]}" app'
  )
  assert 'cutover_ledger rearm-cutover "$CUTOVER_ID"' in source
  assert 'cutover_ledger finalize-cutover "$CUTOVER_ID"' in source
  assert "attempt_rollback || true" in source


def test_same_image_compose_recreation_is_inside_the_exact_handoff_gate():
  source = SCRIPT.read_text(encoding="utf-8")

  assert 'RUNNING_CONFIG_HASH=$(' in source
  assert 'config --hash app' in source
  assert 'compose_recreation_needed "$TARGET_IMAGE"' in source
  assert source.index('compose_recreation_needed "$TARGET_IMAGE"') < source.index(
    'docker compose "${COMPOSE_ARGS[@]}" up -d "${recreate_args[@]}" app'
  )
  assert 'recreate_args=(--force-recreate)' in source
