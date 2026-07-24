"""Contract tests for dependency-complete mini-app bundles.

Opaque in-shell frames are deliberately outside the shell service worker's
origin. A compiled app must therefore carry React, mobius-runtime, and its full
dependency graph in one module; an import map or externalized bare specifier
would work online and fail on a cold offline load.
"""

import json
import subprocess
from pathlib import Path

from app.app_compile_contract import (
  BUNDLED_RUNTIME_LIBS,
  COMPILED_RUNTIME_ABI,
  COMPILED_RUNTIME_ARTIFACT_REVISION,
  COMPILED_RUNTIME_BANNER,
  ESBUILD_TIMEOUT_SECS,
  esbuild_command,
  esbuild_environment,
  mobius_runtime_path,
  runtime_library_aliases,
  runtime_inject_path,
  runtime_node_path,
)


CODEMIRROR_DIRECT_IMPORTS = {
  "@codemirror/state",
  "@codemirror/view",
  "@codemirror/commands",
  "@codemirror/language",
  "@codemirror/lang-markdown",
  "@lezer/highlight",
}

MARKDOWN_DIRECT_IMPORTS = {
  "marked-highlight",
  "highlight.js/*",
}

REPO_ROOT = Path(__file__).resolve().parents[2]
FRAME = REPO_ROOT / "frontend" / "public" / "app-frame.html"
STANDALONE = REPO_ROOT / "backend" / "app" / "routes" / "standalone.py"
INJECT = REPO_ROOT / "backend" / "app" / "app_runtime_inject.js"
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _package_name(specifier: str) -> str:
  clean = specifier.removesuffix("/*")
  if clean.startswith("@"):
    return "/".join(clean.split("/")[:2])
  return clean.split("/", 1)[0]


def test_supported_runtime_packages_are_production_dependencies():
  package = json.loads((REPO_ROOT / "frontend" / "package.json").read_text())
  declared = set(package.get("dependencies", {}))
  required = {_package_name(specifier) for specifier in BUNDLED_RUNTIME_LIBS}
  missing = sorted(required - declared)
  assert not missing, (
    "supported app imports missing from frontend production dependencies: "
    f"{missing}"
  )


def test_compile_command_bundles_the_complete_runtime_graph():
  command = esbuild_command("entry.jsx", "app.js")
  assert "--bundle" in command
  assert '--define:process.env.NODE_ENV="production"' in command
  assert "--minify" in command
  assert "--keep-names" in command
  assert not any(arg.startswith("--external:") for arg in command)
  assert f"--banner:js={COMPILED_RUNTIME_BANNER}" in command
  assert f"--inject:{runtime_inject_path()}" in command
  assert not any(arg.startswith("--node-path") for arg in command)
  assert esbuild_environment()["NODE_PATH"]
  assert f"--alias:mobius-runtime={mobius_runtime_path()}" in command
  for specifier, path in runtime_library_aliases():
    assert f"--alias:{specifier}={path}" in command
  assert runtime_inject_path().is_file()
  assert mobius_runtime_path().is_file()


def test_compile_command_selects_production_react_and_keeps_one_module(tmp_path):
  """The size win must come from the real production graph, not externals."""
  entry = tmp_path / "entry.jsx"
  output = tmp_path / "app.js"
  metafile = tmp_path / "app-meta.json"
  entry.write_text(
    """import { useState } from 'react'

export default function NamedFixture() {
  const [value] = useState('ready')
  return <div>{value}</div>
}
"""
  )

  completed = subprocess.run(
    esbuild_command(entry, output, metafile=metafile),
    capture_output=True,
    check=False,
    env=esbuild_environment(),
    text=True,
    timeout=ESBUILD_TIMEOUT_SECS,
  )
  assert completed.returncode == 0, completed.stderr

  metadata = json.loads(metafile.read_text())
  inputs = set(metadata["inputs"])
  react_inputs = {name for name in inputs if "/react" in name}
  assert react_inputs
  assert not any(".development.js" in name for name in react_inputs)
  assert any(".production.js" in name for name in react_inputs)

  entry_outputs = [
    details for details in metadata["outputs"].values()
    if details.get("entryPoint")
  ]
  assert len(entry_outputs) == 1
  assert entry_outputs[0].get("imports") == []
  assert output.stat().st_size < 400_000
  assert "NamedFixture" in output.read_text()


def test_app_local_or_transitive_react_cannot_shadow_platform_runtime(tmp_path):
  """App-local dependencies must not create a second React dispatcher."""
  local_react = tmp_path / "node_modules" / "react"
  local_react.mkdir(parents=True)
  (local_react / "package.json").write_text(
    json.dumps({"name": "react", "version": "0.0.0-shadow", "main": "index.js"})
  )
  (local_react / "index.js").write_text(
    'export function useState() { throw new Error("shadow-react-copy") }\n'
  )
  local_widget = tmp_path / "node_modules" / "shadow-widget"
  local_widget.mkdir()
  (local_widget / "package.json").write_text(
    json.dumps({"name": "shadow-widget", "version": "1.0.0", "main": "index.js"})
  )
  (local_widget / "index.js").write_text(
    "import { useState } from 'react'\n"
    "export function useWidget() { return useState('platform-react') }\n"
  )
  entry = tmp_path / "entry.jsx"
  output = tmp_path / "app.js"
  entry.write_text(
    """import { useWidget } from 'shadow-widget'

export default function Fixture() {
  const [value] = useWidget()
  return <div>{value}</div>
}
"""
  )

  completed = subprocess.run(
    esbuild_command(entry, output),
    capture_output=True,
    check=False,
    env=esbuild_environment(),
    text=True,
    timeout=ESBUILD_TIMEOUT_SECS,
  )
  assert completed.returncode == 0, completed.stderr
  assert "shadow-react-copy" not in output.read_text()


def test_three_addons_resolve_from_the_pinned_runtime(tmp_path):
  """Documented addons imports must survive package-root runtime pinning."""
  aliases = dict(runtime_library_aliases())
  assert aliases["three"] == runtime_node_path() / "three"
  assert (
    aliases["three/addons"]
    == runtime_node_path() / "three" / "examples" / "jsm"
  )

  entry = tmp_path / "three-addons.jsx"
  output = tmp_path / "three-addons.js"
  metafile = tmp_path / "three-addons-meta.json"
  entry.write_text(
    """import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { STLLoader } from 'three/addons/loaders/STLLoader.js'

export default function ThreeAddonsFixture() {
  return [OrbitControls.name, STLLoader.name]
}
"""
  )

  completed = subprocess.run(
    esbuild_command(entry, output, metafile=metafile),
    capture_output=True,
    check=False,
    env=esbuild_environment(),
    text=True,
    timeout=ESBUILD_TIMEOUT_SECS,
  )
  assert completed.returncode == 0, completed.stderr

  metadata = json.loads(metafile.read_text())
  entry_outputs = [
    details for details in metadata["outputs"].values()
    if details.get("entryPoint")
  ]
  assert len(entry_outputs) == 1
  assert entry_outputs[0].get("imports") == [], (
    "Three addons escaped the pinned self-contained app bundle"
  )
  assert output.is_file() and output.stat().st_size > 0


def test_marked_root_import_uses_the_pinned_esm_entry(tmp_path):
  """The documented named export must not collapse through Marked's UMD build."""
  aliases = dict(runtime_library_aliases())
  assert (
    aliases["marked"]
    == runtime_node_path() / "marked" / "lib" / "marked.esm.js"
  )

  entry = tmp_path / "marked.jsx"
  output = tmp_path / "marked.js"
  metafile = tmp_path / "marked-meta.json"
  entry.write_text(
    """import { marked } from 'marked'

export default function MarkedFixture() {
  return marked.parse('# Working')
}
"""
  )

  completed = subprocess.run(
    esbuild_command(entry, output, metafile=metafile),
    capture_output=True,
    check=False,
    env=esbuild_environment(),
    text=True,
    timeout=ESBUILD_TIMEOUT_SECS,
  )
  assert completed.returncode == 0, completed.stderr

  inputs = json.loads(metafile.read_text())["inputs"]
  assert any(path.endswith("/marked/lib/marked.esm.js") for path in inputs)
  assert not any(path.endswith("/marked/lib/marked.umd.js") for path in inputs)


def test_app_hosts_have_no_runtime_import_map_or_static_module_imports():
  frame = FRAME.read_text()
  standalone = STANDALONE.read_text()
  for source in (frame, standalone):
    assert 'type="importmap"' not in source
    assert "await import('react')" not in source
    assert 'await import("react")' not in source
    assert "await import('/mobius-runtime.js')" not in source
    assert 'await import("/mobius-runtime.js")' not in source
    assert "__mobiusRuntimeConfig" in source
    assert "__mobiusCompiledRuntime" in source


def test_image_does_not_build_obsolete_package_facades():
  dockerfile = DOCKERFILE.read_text()
  for builder in (
    "build-react-vendor",
    "build-codemirror-vendor",
    "build-recharts-vendor",
    "build-date-fns-vendor",
    "build-d3-geo-vendor",
    "build-marked-vendor",
    "build-dompurify-vendor",
  ):
    assert builder not in dockerfile
  assert "pdf.worker.mjs" in dockerfile
  assert "katex.min.css" in dockerfile


def test_compiler_and_both_hosts_agree_on_runtime_abi():
  inject = INJECT.read_text()
  frame = FRAME.read_text()
  standalone = STANDALONE.read_text()
  assert f"abi: {COMPILED_RUNTIME_ABI}" in inject
  assert f"COMPILED_RUNTIME_ABI = {COMPILED_RUNTIME_ABI}" in frame
  assert f"compiledRuntime.abi !== {COMPILED_RUNTIME_ABI}" in standalone
  assert (
    f"artifact-revision:{COMPILED_RUNTIME_ARTIFACT_REVISION}"
    in COMPILED_RUNTIME_BANNER
  )


def test_codemirror_direct_imports_remain_supported():
  missing = sorted(CODEMIRROR_DIRECT_IMPORTS - set(BUNDLED_RUNTIME_LIBS))
  assert not missing, f"CodeMirror direct imports missing: {missing}"


def test_markdown_direct_imports_remain_supported():
  missing = sorted(MARKDOWN_DIRECT_IMPORTS - set(BUNDLED_RUNTIME_LIBS))
  assert not missing, f"Markdown direct imports missing: {missing}"


def test_markdown_dynamic_imports_compile_into_one_offline_module(tmp_path):
  entry = tmp_path / "markdown.jsx"
  output = tmp_path / "markdown.js"
  metafile = tmp_path / "markdown-meta.json"
  entry.write_text(
    """import React from 'react'

export async function loadMarkdownRuntime() {
  return Promise.all([
    import('marked-highlight'),
    import('highlight.js/lib/common'),
  ])
}

export default function MarkdownFixture() {
  return React.createElement('div', null, 'markdown')
}
"""
  )

  completed = subprocess.run(
    esbuild_command(entry, output, metafile=metafile),
    capture_output=True,
    check=False,
    env=esbuild_environment(),
    text=True,
    timeout=ESBUILD_TIMEOUT_SECS,
  )
  assert completed.returncode == 0, completed.stderr

  metadata = json.loads(metafile.read_text())
  entry_outputs = [
    details for details in metadata["outputs"].values()
    if details.get("entryPoint")
  ]
  assert len(entry_outputs) == 1
  assert entry_outputs[0].get("imports") == [], (
    "Markdown runtime dependencies escaped the self-contained app bundle"
  )
  assert output.is_file() and output.stat().st_size > 0
