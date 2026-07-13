"""Pure mini-app compile contract shared by the compiler and local validator."""

import re
from pathlib import Path

ESBUILD_TIMEOUT_SECS = 30

# Bare imports supplied by app-frame.html's import map. The platform compiler
# must externalize these, and preflight validation must use the exact same list.
RUNTIME_LIBS: tuple[str, ...] = (
  "react",
  "react/jsx-runtime",
  "react-dom",
  "react-dom/client",
  "recharts",
  "date-fns",
  "three",
  "three/addons/*",
  "pdfjs-dist",
  "codemirror",
  "@codemirror/state",
  "@codemirror/view",
  "@codemirror/commands",
  "@codemirror/language",
  "@codemirror/lang-markdown",
  "@lezer/highlight",
  "katex",
  "d3-geo",
  "marked",
  "dompurify",
)

# Covers function/class/identifier/expression default exports while rejecting a
# bundle that compiles successfully but cannot be mounted as a mini-app.
EXPORT_DEFAULT_RE = re.compile(r"^\s*export\s+default\b", re.MULTILINE)


def esbuild_command(entry: str | Path, output: str | Path) -> list[str]:
  """Return the canonical production/preflight mini-app compile argv."""
  return [
    "esbuild",
    str(entry),
    "--bundle",
    "--format=esm",
    "--jsx=automatic",
    "--platform=browser",
    *[f"--external:{lib}" for lib in RUNTIME_LIBS],
    f"--outfile={output}",
  ]
