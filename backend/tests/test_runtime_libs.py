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
  ROLLDOWN_TIMEOUT_SECS,
  mobius_runtime_path,
  rolldown_command,
  rolldown_runner_path,
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
STANDALONE_APP = (
  REPO_ROOT
  / "frontend"
  / "src"
  / "components"
  / "StandaloneApp"
  / "StandaloneApp.jsx"
)
INJECT = REPO_ROOT / "backend" / "app" / "app_runtime_inject.js"
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _compile(entry: Path, output: Path, *, report: Path | None = None):
  report = report or output.with_name(f"{output.name}.report.json")
  return subprocess.run(
    rolldown_command(entry, output, report=report),
    capture_output=True,
    check=False,
    text=True,
    timeout=ROLLDOWN_TIMEOUT_SECS,
  )


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
  command = rolldown_command("entry.jsx", "app.js", report="report.json")
  assert command[:2] == ["node", str(rolldown_runner_path())]
  config = json.loads(command[2])
  assert config["entry"] == "entry.jsx"
  assert config["output"] == "app.js"
  assert config["report"] == "report.json"
  assert config["banner"] == COMPILED_RUNTIME_BANNER
  assert config["runtimeInject"] == str(runtime_inject_path())
  aliases = dict(config["aliases"])
  assert aliases["mobius-runtime"] == str(mobius_runtime_path())
  for specifier, path in runtime_library_aliases():
    assert aliases[specifier] == str(path)
  assert runtime_inject_path().is_file()
  assert rolldown_runner_path().is_file()
  assert mobius_runtime_path().is_file()


def test_compiler_prefers_the_live_frame_runtime():
  live_runtime = REPO_ROOT / "frontend" / "public" / "mobius-runtime.js"
  assert mobius_runtime_path() == live_runtime


def test_compile_command_selects_production_react_and_keeps_one_module(tmp_path):
  """The size win must come from the real production graph, not externals."""
  entry = tmp_path / "entry.jsx"
  output = tmp_path / "app.js"
  report_path = tmp_path / "app-meta.json"
  entry.write_text(
    """import { useState } from 'react'

export default function NamedFixture() {
  const [value] = useState('ready')
  return <div>{value}</div>
}
"""
  )

  completed = _compile(entry, output, report=report_path)
  assert completed.returncode == 0, completed.stderr

  report = json.loads(report_path.read_text())
  inputs = set(report["inputs"])
  react_inputs = {name for name in inputs if "/react" in name}
  assert react_inputs
  assert not any(".development.js" in name for name in react_inputs)
  assert any(".production.js" in name for name in react_inputs)

  entry_outputs = [
    details for details in report["outputs"]
    if details.get("isEntry")
  ]
  assert len(entry_outputs) == 1
  assert entry_outputs[0].get("imports") == []
  assert output.stat().st_size < 400_000
  compiled = output.read_text()
  assert compiled.startswith(COMPILED_RUNTIME_BANNER)
  assert "NamedFixture" not in compiled


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

  completed = _compile(entry, output)
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
  report_path = tmp_path / "three-addons-meta.json"
  entry.write_text(
    """import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { STLLoader } from 'three/addons/loaders/STLLoader.js'

export default function ThreeAddonsFixture() {
  return [OrbitControls.name, STLLoader.name]
}
"""
  )

  completed = _compile(entry, output, report=report_path)
  assert completed.returncode == 0, completed.stderr

  report = json.loads(report_path.read_text())
  entry_outputs = [
    details for details in report["outputs"]
    if details.get("isEntry")
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
  report_path = tmp_path / "marked-meta.json"
  entry.write_text(
    """import { marked } from 'marked'

export default function MarkedFixture() {
  return marked.parse('# Working')
}
"""
  )

  completed = _compile(entry, output, report=report_path)
  assert completed.returncode == 0, completed.stderr

  inputs = json.loads(report_path.read_text())["inputs"]
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
  assert "__mobiusRuntimeConfig" in frame
  assert "__mobiusCompiledRuntime" in frame
  assert "__mobiusRuntimeConfig" not in standalone
  assert "__mobiusCompiledRuntime" not in standalone
  assert "__mobius-standalone-app__" in standalone
  assert "AppCanvas" in STANDALONE_APP.read_text()


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


def test_compiler_and_shared_frame_agree_on_runtime_abi():
  inject = INJECT.read_text()
  frame = FRAME.read_text()
  standalone = STANDALONE.read_text()
  assert f"abi: {COMPILED_RUNTIME_ABI}" in inject
  assert f"COMPILED_RUNTIME_ABI = {COMPILED_RUNTIME_ABI}" in frame
  # The standalone URL mounts this same frame through AppCanvas. It must not
  # grow a second ABI check or executable runtime of its own.
  assert "compiledRuntime.abi" not in standalone
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
  report_path = tmp_path / "markdown-meta.json"
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

  completed = _compile(entry, output, report=report_path)
  assert completed.returncode == 0, completed.stderr

  report = json.loads(report_path.read_text())
  entry_outputs = [
    details for details in report["outputs"]
    if details.get("isEntry")
  ]
  assert len(entry_outputs) == 1
  assert entry_outputs[0].get("imports") == [], (
    "Markdown runtime dependencies escaped the self-contained app bundle"
  )
  assert output.is_file() and output.stat().st_size > 0


def test_openai_app_icons_compile_from_the_supported_public_entry(tmp_path):
  """Generic mini-app chrome uses the same maintained icon set as the shell."""
  aliases = dict(runtime_library_aliases())
  icon_entry = (
    runtime_node_path() / "@openai" / "apps-sdk-ui" / "dist" / "es"
    / "components" / "Icon" / "index.js"
  )
  assert aliases["@openai/apps-sdk-ui/components/Icon"] == icon_entry

  entry = tmp_path / "openai-icons.jsx"
  output = tmp_path / "openai-icons.js"
  report_path = tmp_path / "openai-icons-meta.json"
  entry.write_text(
    """import { ArrowLeft, Chat, Check, Copy, Search, Trash } from '@openai/apps-sdk-ui/components/Icon'

export default function OpenAIIconsFixture() {
  return <div>{[ArrowLeft, Chat, Check, Copy, Search, Trash].map((Icon) => <Icon key={Icon.name} />)}</div>
}
"""
  )

  completed = _compile(entry, output, report=report_path)
  assert completed.returncode == 0, completed.stderr

  report = json.loads(report_path.read_text())
  entry_outputs = [
    details for details in report["outputs"]
    if details.get("isEntry")
  ]
  assert len(entry_outputs) == 1
  assert entry_outputs[0].get("imports") == [], (
    "OpenAI app icons escaped the pinned self-contained app bundle"
  )
  assert output.is_file() and output.stat().st_size > 0


def test_lucide_icons_compile_from_the_supported_public_entry(tmp_path):
  """Existing apps use Lucide's package root for small inline icons."""
  aliases = dict(runtime_library_aliases())
  assert aliases["lucide-react"] == runtime_node_path() / "lucide-react"

  entry = tmp_path / "lucide-icons.jsx"
  output = tmp_path / "lucide-icons.js"
  report_path = tmp_path / "lucide-icons-meta.json"
  entry.write_text(
    """import { Search } from 'lucide-react'

export default function LucideIconsFixture() {
  return <Search aria-label="search" />
}
"""
  )

  completed = _compile(entry, output, report=report_path)
  assert completed.returncode == 0, completed.stderr

  report = json.loads(report_path.read_text())
  entry_outputs = [
    details for details in report["outputs"]
    if details.get("isEntry")
  ]
  assert len(entry_outputs) == 1
  assert entry_outputs[0].get("imports") == []
  assert output.is_file() and output.stat().st_size > 0


def test_recharts_common_chart_api_compiles_into_one_offline_module(tmp_path):
  """The Recharts 3 migration keeps the ordinary declarative chart API."""
  entry = tmp_path / "recharts.jsx"
  output = tmp_path / "recharts.js"
  report_path = tmp_path / "recharts-meta.json"
  entry.write_text(
    """import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

const data = [{ label: 'ready', value: 1 }]

export default function RechartsFixture() {
  return <ResponsiveContainer width="100%" height={240}>
    <BarChart data={data}>
      <CartesianGrid strokeDasharray="3 3" />
      <XAxis dataKey="label" />
      <YAxis />
      <Tooltip />
      <Bar dataKey="value" />
    </BarChart>
  </ResponsiveContainer>
}
"""
  )

  completed = _compile(entry, output, report=report_path)
  assert completed.returncode == 0, completed.stderr

  report = json.loads(report_path.read_text())
  entry_outputs = [
    details for details in report["outputs"]
    if details.get("isEntry")
  ]
  assert len(entry_outputs) == 1
  assert entry_outputs[0].get("imports") == []
  assert output.is_file() and output.stat().st_size > 0


def test_pdfjs_browser_worker_fallback_remains_supported(tmp_path):
  """PDF.js intentionally retains import(this.workerSrc) as a fallback."""
  entry = tmp_path / "pdfjs.jsx"
  output = tmp_path / "pdfjs.js"
  report_path = tmp_path / "pdfjs-meta.json"
  entry.write_text(
    """import { getDocument } from 'pdfjs-dist'

export default function PdfFixture() {
  return typeof getDocument
}
"""
  )

  completed = _compile(entry, output, report=report_path)
  assert completed.returncode == 0, completed.stderr

  report = json.loads(report_path.read_text())
  entry_outputs = [
    details for details in report["outputs"]
    if details.get("isEntry")
  ]
  assert len(entry_outputs) == 1
  assert entry_outputs[0].get("imports") == []
  assert output.is_file() and output.stat().st_size > 0
