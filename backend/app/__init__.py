"""Early process hooks shared by every Möbius backend entrypoint."""

# Capture source provenance before importing modules whose activation we prove.
from app.boot_source import BOOT_SOURCE_INPUTS  # noqa: F401

from app.memory_observability import (
  maybe_start_allocation_tracing,
  record_memory_checkpoint,
)


# Allocation tracing has to begin before the rest of ``app`` imports to make a
# controlled startup profile useful. It is strictly opt-in; the ordinary path
# does one cheap /proc checkpoint and carries no tracing overhead.
maybe_start_allocation_tracing()
record_memory_checkpoint("app_import")
