"""Mini-app compile contract shared by the compiler and local validator."""

import os
from collections.abc import Mapping
from pathlib import Path

ESBUILD_TIMEOUT_SECS = 30

# Bare imports supported by the platform's self-contained app compiler. These
# packages are resolved from the frontend runtime installation and bundled into
# every app that uses them; an opaque frame must never need a network import.
BUNDLED_RUNTIME_LIBS: tuple[str, ...] = (
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

COMPILED_RUNTIME_ABI = 1
COMPILED_RUNTIME_GLOBAL = "__mobiusCompiledRuntime"
COMPILED_RUNTIME_BANNER = (
  f"/* mobius-compiled-runtime-abi:{COMPILED_RUNTIME_ABI} */"
)


def runtime_inject_path() -> Path:
  return Path(__file__).with_name("app_runtime_inject.js")


def runtime_node_path() -> Path:
  configured = os.environ.get("MOBIUS_APP_NODE_PATH")
  if configured:
    return Path(configured)
  candidates = [
    Path("/app/shell-src/node_modules"),
    Path(__file__).resolve().parents[2] / "frontend" / "node_modules",
  ]
  return next((path for path in candidates if path.is_dir()), candidates[-1])


def mobius_runtime_path() -> Path:
  candidates = [
    Path("/app/shell-src/public/mobius-runtime.js"),
    Path(__file__).resolve().parents[2] / "frontend" / "public" / "mobius-runtime.js",
  ]
  return next((path for path in candidates if path.is_file()), candidates[-1])


def esbuild_environment() -> dict[str, str]:
  """Return a subprocess environment with app dependencies explicitly scoped.

  Esbuild's CLI consumes Node's ``NODE_PATH`` environment variable (its JS API
  calls the equivalent option ``nodePaths``); there is no ``--node-path`` CLI
  flag. Replace rather than append any inherited value so app builds resolve
  bare imports only from the platform's pinned frontend runtime installation.
  """
  environment = dict(os.environ)
  environment["NODE_PATH"] = str(runtime_node_path())
  return environment

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
    f"--banner:js={COMPILED_RUNTIME_BANNER}",
    f"--inject:{runtime_inject_path()}",
    f"--alias:mobius-runtime={mobius_runtime_path()}",
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
