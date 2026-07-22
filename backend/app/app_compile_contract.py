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
  "marked-highlight",
  "highlight.js/*",
  "dompurify",
)

COMPILED_RUNTIME_ABI = 1
# Bump this when every installed bundle must be rebuilt for an additive
# compiled-runtime change that remains host-compatible. Keep ABI for actual
# host/runtime incompatibilities: a revision-only rollout is safe while the
# live checkout and backend process briefly run different generations.
COMPILED_RUNTIME_ARTIFACT_REVISION = 3
COMPILED_RUNTIME_GLOBAL = "__mobiusCompiledRuntime"
COMPILED_RUNTIME_BANNER = (
  f"/* mobius-compiled-runtime-abi:{COMPILED_RUNTIME_ABI};"
  f"artifact-revision:{COMPILED_RUNTIME_ARTIFACT_REVISION} */"
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
  flag. Replace rather than append any inherited value. ``NODE_PATH`` is only a
  fallback after an entry's nearby ``node_modules``, so the compile command also
  aliases every supported package root to the platform runtime installation.
  """
  environment = dict(os.environ)
  environment["NODE_PATH"] = str(runtime_node_path())
  return environment


def _package_root(specifier: str) -> str:
  """Return the package root for a supported bare import or subpath."""
  clean = specifier.removesuffix("/*")
  if clean.startswith("@"):
    return "/".join(clean.split("/")[:2])
  return clean.split("/", 1)[0]


def runtime_library_aliases() -> tuple[tuple[str, Path], ...]:
  """Pin supported bare imports to one physical runtime package copy.

  Esbuild normally resolves an app entry's imports relative to that app before
  consulting ``NODE_PATH``. A gitignored development ``node_modules/react``
  beside an app could therefore be bundled alongside the platform React used by
  ``app_runtime_inject.js``. React then sees two dispatchers and every hook
  fails at first render. Package-root aliases apply to the root and its subpaths
  (for example ``react/jsx-runtime``), keeping each supported library singular.

  Three's documented ``three/addons/*`` export is backed by the physical
  ``examples/jsm`` directory rather than an ``addons`` directory. Once the
  package root is replaced with an absolute alias, esbuild no longer consults
  Three's package exports for that subpath. Pin the public addons spelling to
  its runtime-owned physical directory before adding the package roots.
  """
  node_path = runtime_node_path()
  roots = sorted({_package_root(specifier) for specifier in BUNDLED_RUNTIME_LIBS})
  subpaths = (("three/addons", node_path / "three" / "examples" / "jsm"),)
  return subpaths + tuple((root, node_path / root) for root in roots)


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
    *(
      f"--alias:{specifier}={path}"
      for specifier, path in runtime_library_aliases()
    ),
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
