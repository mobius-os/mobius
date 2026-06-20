"""Synthetic fixtures for the expanded memory-v2 eval harness."""
from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent


TreeSpec = Mapping[str, str] | str


@dataclass(frozen=True)
class TranscriptTurn:
  role: str
  content: str


@dataclass(frozen=True)
class CaptureCase:
  id: str
  transcripts: list[TranscriptTurn]
  daytime_inbox: str
  gold_facts: list[str]
  excluded_facts: list[str]


@dataclass(frozen=True)
class RetrievalCase:
  id: str
  memory_tree: TreeSpec
  query: str
  gold_node_ids: list[str]
  gold_answer: str
  should_abstain: bool


@dataclass(frozen=True)
class ConsolidationFixture:
  id: str
  memory_tree: TreeSpec
  expected_invariants: dict


@dataclass(frozen=True)
class E2EQuestion:
  text: str
  gold_answer: str
  should_abstain: bool
  gold_fact_strings: list[str]


@dataclass(frozen=True)
class E2ECase:
  id: str
  transcripts: list[TranscriptTurn]
  questions: list[E2EQuestion]
  memory_tree: TreeSpec = ""


def write_corpus(fixture: object, tmp_dir: str | Path) -> Path:
  """Materialise a fixture memory tree into ``tmp_dir`` and write ``.ready``.

  ``fixture`` may be a case with a ``memory_tree`` attribute, a raw mapping of
  relative file paths to contents, a path to an existing tree, or a simple text
  tree spec using ``--- path ---`` headers.
  """
  target = Path(tmp_dir)
  target.mkdir(parents=True, exist_ok=True)
  tree = getattr(fixture, "memory_tree", fixture)
  if isinstance(tree, Mapping):
    _write_mapping(tree, target)
  elif isinstance(tree, str) and tree.strip():
    source = Path(tree)
    if source.exists():
      _copy_tree(source, target)
    else:
      _write_mapping(_parse_tree_spec(tree), target)
  else:
    raise TypeError(f"Unsupported corpus fixture: {type(fixture)!r}")
  (target / ".ready").write_text("", encoding="utf-8")
  return target


def load_corpus() -> dict[str, list[object]]:
  return CORPUS


def _write_mapping(tree: Mapping[str, str], target: Path) -> None:
  for rel_path, content in tree.items():
    path = target / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).lstrip("\n").rstrip() + "\n", encoding="utf-8")


def _copy_tree(source: Path, target: Path) -> None:
  for path in source.rglob("*"):
    if path.is_dir():
      continue
    rel_path = path.relative_to(source)
    out = target / rel_path
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, out)


def _parse_tree_spec(spec: str) -> dict[str, str]:
  files: dict[str, list[str]] = {}
  current: str | None = None
  for line in dedent(spec).splitlines():
    if line.startswith("--- ") and line.endswith(" ---"):
      current = line[4:-4].strip()
      files[current] = []
    elif current:
      files[current].append(line)
  if not files:
    raise ValueError("tree spec must contain at least one '--- path ---' header")
  return {path: "\n".join(lines).strip() + "\n" for path, lines in files.items()}


def _note(title: str, kind: str, body: str) -> str:
  return f"---\ntitle: {title}\ntype: {kind}\n---\n\n{dedent(body).strip()}\n"


def _clean_tree(topic: str = "cooking") -> dict[str, str]:
  return {
    "index.md": f"# Memory router\n\n- {topic}: stable preferences and routines [open]({topic}/index.md)\n",
    f"{topic}/index.md": f"# {topic.title()}\n\n- Metric units: user prefers metric units [open](metric-units.md)\n",
    f"{topic}/metric-units.md": _note(
      "Metric units preference",
      "fact",
      "The user prefers metric units.\n\nsee also for units: [units](../hubs/units.md)",
    ),
    "hubs/index.md": "# Hubs\n\n- Units preference [open](units.md)\n",
    "hubs/units.md": _note("Units preference", "hub", "The user prefers metric units."),
  }


COOKING_UNITS_TREE = {
  "index.md": "# Memory router\n\n- Cooking: baking setup, recipes, cake, units [open](cooking/index.md)\n",
  "cooking/index.md": "# Cooking\n\n- Baking setup: cake recipes use grams, not cups [open](baking-setup.md)\n",
  "cooking/baking-setup.md": _note(
    "Baking setup",
    "fact",
    """
    The user wants cake recipes in grams.

    see also for units: [units preference](../hubs/units.md)
    """,
  ),
  "hubs/index.md": "# Hubs\n\n- Units preference [open](units.md)\n",
  "hubs/units.md": _note("Units preference", "hub", "The user prefers metric units."),
}


PIXEL_TREE = {
  "index.md": "# Memory router\n\n- Dog Pixel: epilepsy and enrichment constraints [open](pets/index.md)\n",
  "pets/index.md": "# Pets\n\n- Pixel epilepsy: enrichment should avoid flashing lights [open](pixel-epilepsy.md)\n",
  "pets/pixel-epilepsy.md": _note(
    "Pixel epilepsy",
    "fact",
    "The user's dog Pixel has epilepsy. Avoid flashing-light toys for Pixel enrichment.",
  ),
}


CAPTURE_CASES = [
  CaptureCase(
    id="metric-units-no-cups",
    transcripts=[
      TranscriptTurn(
        "user",
        "Keep everything in metric? I get annoyed converting cups and Fahrenheit.",
      ),
      TranscriptTurn("assistant", "Got it. I will use metric units."),
    ],
    daytime_inbox="",
    gold_facts=[
      "The user prefers metric units.",
      "The user dislikes converting cups/Fahrenheit.",
    ],
    excluded_facts=["The user is making a meal plan today."],
  ),
  CaptureCase(
    id="pixel-epilepsy",
    transcripts=[
      TranscriptTurn("user", "My dog Pixel has epilepsy, so enrichment toys cannot flash."),
      TranscriptTurn("assistant", "I will avoid flashing toys for Pixel."),
    ],
    daytime_inbox="- User said Pixel has epilepsy.\n",
    gold_facts=[
      "The user's dog is named Pixel.",
      "Pixel has epilepsy.",
      "Pixel should avoid flashing-light toys.",
    ],
    excluded_facts=["The user wants to buy a toy today."],
  ),
]


RETRIEVAL_CASES = [
  RetrievalCase(
    id="cake-recipe-units",
    memory_tree=COOKING_UNITS_TREE,
    query="cake recipe, cups or grams?",
    gold_node_ids=["cooking/baking-setup.md", "hubs/units.md"],
    gold_answer="grams",
    should_abstain=False,
  ),
  RetrievalCase(
    id="unknown-blood-type",
    memory_tree=COOKING_UNITS_TREE,
    query="what is my blood type?",
    gold_node_ids=[],
    gold_answer="I don't know.",
    should_abstain=True,
  ),
]


CONSOLIDATION_FIXTURES = [
  ConsolidationFixture(
    id="clean-cooking",
    memory_tree=_clean_tree("cooking"),
    expected_invariants={
      "orphan_count": 0,
      "duplicate_normalized_title_count": 0,
      "dangling_wikilink_count": 0,
      "missing_directory_index_count": 0,
      "broken_router_target_count": 0,
      "bootstrap_retired": True,
    },
  ),
  ConsolidationFixture(
    id="clean-pets",
    memory_tree={
      "index.md": "# Memory router\n\n- Pets: Pixel health and enrichment [open](pets/index.md)\n",
      "pets/index.md": "# Pets\n\n- Pixel epilepsy [open](pixel.md)\n",
      "pets/pixel.md": _note("Pixel epilepsy", "fact", "Pixel should avoid flashing-light toys."),
    },
    expected_invariants={"orphan_count": 0, "bootstrap_retired": True},
  ),
  ConsolidationFixture(
    id="defect-orphan",
    memory_tree={**_clean_tree(), "orphan.md": _note("Orphan", "fact", "Unlinked fact.")},
    expected_invariants={"orphan_count": 1},
  ),
  ConsolidationFixture(
    id="defect-duplicate-title",
    memory_tree={
      **_clean_tree(),
      "cooking/index.md": (
        "# Cooking\n\n"
        "- Metric units: user prefers metric units [open](metric-units.md)\n"
        "- Metric units duplicate title [open](metric-units-copy.md)\n"
      ),
      "cooking/metric-units-copy.md": _note("Metric units preference", "fact", "Duplicate title."),
    },
    expected_invariants={"duplicate_normalized_title_count": 1},
  ),
  ConsolidationFixture(
    id="defect-dangling-link",
    memory_tree={
      **_clean_tree(),
      "cooking/metric-units.md": _note(
        "Metric units preference",
        "fact",
        "The user prefers metric units.\n\nsee also: [missing](../hubs/missing.md)",
      ),
    },
    expected_invariants={"dangling_wikilink_count": 1},
  ),
  ConsolidationFixture(
    id="defect-bad-hub-fan-in",
    memory_tree={
      **_clean_tree(),
      "travel/index.md": "# Travel\n\n- Travel units [open](travel-units.md)\n",
      "travel/travel-units.md": _note(
        "Travel units",
        "fact",
        "Use metric while traveling.\n\nsee also: [units](../hubs/units.md)",
      ),
      "work/index.md": "# Work\n\n- Work units [open](work-units.md)\n",
      "work/work-units.md": _note(
        "Work units",
        "fact",
        "Use metric at work.\n\nsee also: [units](../hubs/units.md)",
      ),
    },
    expected_invariants={"hub_fan_in": {"hubs/units.md": 3}},
  ),
  ConsolidationFixture(
    id="defect-missing-dir-index",
    memory_tree={
      **_clean_tree(),
      "index.md": (
        "# Memory router\n\n"
        "- cooking: stable preferences and routines [open](cooking/index.md)\n"
        "- loose: directory deliberately lacks index [open](loose/note.md)\n"
      ),
      "loose/note.md": _note("Loose", "fact", "No directory index."),
    },
    expected_invariants={"missing_directory_index_count": 1},
  ),
  ConsolidationFixture(
    id="defect-bootstrap-not-retired",
    memory_tree={
      **_clean_tree(),
      "index.md": (
        "# Memory router\n\n"
        "- cooking: stable preferences and routines [open](cooking/index.md)\n"
        "- bootstrap: fresh-instance note [open](bootstrap-this-instance-is-fresh.md)\n"
      ),
      "bootstrap-this-instance-is-fresh.md": _note("Bootstrap", "bootstrap", "Retire me."),
    },
    expected_invariants={"bootstrap_retired": False},
  ),
]


E2E_CASES = [
  E2ECase(
    id="pixel-enrichment",
    transcripts=[
      TranscriptTurn("user", "My dog Pixel has epilepsy; avoid flashing-light toys."),
    ],
    memory_tree=PIXEL_TREE,
    questions=[
      E2EQuestion(
        text="what to avoid for Pixel enrichment?",
        gold_answer="flashing-light toys",
        should_abstain=False,
        gold_fact_strings=["Pixel should avoid flashing-light toys."],
      )
    ],
  ),
  E2ECase(
    id="metric-baking",
    transcripts=[TranscriptTurn("user", "Please keep recipes metric; cups annoy me.")],
    memory_tree=COOKING_UNITS_TREE,
    questions=[
      E2EQuestion(
        text="for a cake recipe should you use cups or grams?",
        gold_answer="grams",
        should_abstain=False,
        gold_fact_strings=["The user prefers metric units."],
      )
    ],
  ),
]


CORPUS: dict[str, list[object]] = {
  "capture": CAPTURE_CASES,
  "retrieval": RETRIEVAL_CASES,
  "consolidation": CONSOLIDATION_FIXTURES,
  "e2e": E2E_CASES,
}
