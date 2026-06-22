#!/usr/bin/env python3
"""Rebuilds /data/shared/memory/graph.json and lints the knowledge graph.

The agent (and the nightly reflection pass) run this after editing notes so the
graph viewer stays current. Exits non-zero with a problem report if the graph
has ERROR-severity issues (duplicate ids, dangling MOC links, missing index)
so a publish step can abort and keep the last-known-good graph.

Usage: python3 /app/scripts/build_memory_graph.py [DATA_DIR]
       python3 /app/scripts/build_memory_graph.py --root /path/to/memory
       (DATA_DIR defaults to $DATA_DIR or /data; --root points at a bare
        memory dir, used by the reflection wrapper to lint a staging tree.)
"""

import os
import sys
from pathlib import Path

# Make the `app` package importable whether baked (/app/app) or in-repo
# (backend/app): parents[1] is the dir holding both `app/` and `scripts/`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.memory_graph import write_graph  # noqa: E402


def main() -> int:
  args = sys.argv[1:]
  if args and args[0] == "--root":
    res = write_graph(root=args[1])
  else:
    data_dir = args[0] if args else os.environ.get("DATA_DIR", "/data")
    res = write_graph(data_dir)
  errors = res.errors
  warns = [p for p in res.problems if p["severity"] == "warn"]
  print(f"graph: {len(res.nodes)} nodes, {len(res.edges)} edges, "
        f"{len(errors)} errors, {len(warns)} warnings")
  for p in res.problems:
    print(f"  [{p['severity']}] {p['kind']}: {p['detail']}")
  if errors:
    print("graph.json NOT written (errors present); kept last-known-good")
    return 1
  print("wrote graph.json")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
