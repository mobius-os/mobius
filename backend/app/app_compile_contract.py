"""Pure mini-app compile contract shared by the compiler and local validator."""

from collections.abc import Mapping
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

NO_DEFAULT_EXPORT_ERROR = (
  "JSX source has no default export — mini-apps must export a default React "
  "component. Add `export default function MyApp(...)`, `export default "
  "ComponentName`, or an equivalent default re-export."
)
CSS_IMPORT_ERROR = (
  "CSS imports are not supported by the single-module mini-app runtime. "
  "Export CSS from a JavaScript module as a string, or serve a complete static "
  "site through `static_assets`."
)


def esbuild_command(
  entry: str | Path,
  output: str | Path,
  *,
  metafile: str | Path | None = None,
) -> list[str]:
  """Return the canonical production/preflight mini-app compile argv."""
  command = [
    "esbuild",
    str(entry),
    "--bundle",
    "--format=esm",
    "--jsx=automatic",
    "--platform=browser",
    *[f"--external:{lib}" for lib in RUNTIME_LIBS],
    f"--outfile={output}",
  ]
  if metafile is not None:
    command.append(f"--metafile={metafile}")
  return command


def esbuild_metafile_contract_error(metafile: Mapping) -> str | None:
  """Return an unsupported-output/default-export error from esbuild metadata.

  Esbuild, rather than a source regex, is authoritative here. This recognizes
  default re-exports and cannot be fooled by comments or string literals. The
  same metadata also exposes CSS side output, which the mini-app module route
  cannot serve.
  """
  outputs = metafile.get("outputs")
  if not isinstance(outputs, Mapping):
    return "esbuild metadata did not describe any outputs"
  entry_outputs = [
    details for details in outputs.values()
    if isinstance(details, Mapping) and details.get("entryPoint")
  ]
  if not entry_outputs:
    return "esbuild metadata did not describe the entry output"
  if any(details.get("cssBundle") for details in entry_outputs):
    return CSS_IMPORT_ERROR
  if not any("default" in (details.get("exports") or []) for details in entry_outputs):
    return NO_DEFAULT_EXPORT_ERROR
  return None
