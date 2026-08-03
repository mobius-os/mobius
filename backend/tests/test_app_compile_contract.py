"""Production compiler enforcement for the shared mini-app output contract."""

from pathlib import Path

import pytest

from app.compiler import CompileError, compile_jsx


@pytest.mark.asyncio
async def test_compile_accepts_default_reexport(tmp_path):
  output = tmp_path / "compiled" / "app.js"
  source = "const App = () => null;\nexport { App as default };"

  await compile_jsx(1, source, out_path=output)

  assert output.is_file()


@pytest.mark.asyncio
async def test_compile_inlines_dynamic_imports(tmp_path):
  entry = tmp_path / "index.jsx"
  dependency = tmp_path / "detail.js"
  output = tmp_path / "app.js"
  source = (
    "export async function detail(){ return import('./detail.js') }\n"
    "export default function App(){ return null }"
  )
  entry.write_text(source)
  dependency.write_text("export const value = 'inlined-detail'")

  await compile_jsx(1, source, out_path=output, source_path=entry)

  compiled = output.read_text()
  assert "inlined-detail" in compiled
  assert "./detail.js" not in compiled


@pytest.mark.asyncio
async def test_compile_rejects_unresolved_import_matching_output_name(tmp_path):
  entry = tmp_path / "index.jsx"
  output = tmp_path / "app.js"
  source = (
    "export const load = () => import('index.js');\n"
    "export default function App(){ return null }"
  )
  entry.write_text(source)

  with pytest.raises(CompileError, match="Compilation failed") as exc:
    await compile_jsx(1, source, out_path=output, source_path=entry)

  assert "Could not resolve 'index.js'" in exc.value.stderr
  assert not output.exists()


@pytest.mark.asyncio
async def test_compile_preserves_explicit_online_dynamic_import(tmp_path):
  output = tmp_path / "app.js"
  source = (
    "export const load = () => import('https://esm.sh/example-package');\n"
    "export default function App(){ return null }"
  )

  await compile_jsx(1, source, out_path=output)

  assert "https://esm.sh/example-package" in output.read_text()


@pytest.mark.asyncio
async def test_compile_is_deterministic_across_snapshot_directories(tmp_path):
  source = (
    "import { value } from './detail.js';\n"
    "export default function App(){ return value }"
  )
  outputs = []
  for name in ("first", "second"):
    root = tmp_path / name
    root.mkdir()
    entry = root / "index.jsx"
    entry.write_text(source)
    (root / "detail.js").write_text("export const value = 'stable'")
    output = tmp_path / f"{name}.js"

    await compile_jsx(1, source, out_path=output, source_path=entry)
    outputs.append(output.read_bytes())

  assert outputs[0] == outputs[1]


@pytest.mark.asyncio
async def test_compile_rejects_comment_that_only_mentions_default_export(tmp_path):
  output = tmp_path / "app.js"
  source = "// export default function Fake() {}\nexport const value = 1;"

  with pytest.raises(CompileError, match="Compilation failed") as exc:
    await compile_jsx(1, source, out_path=output)

  assert "no default export" in exc.value.stderr
  assert not output.exists()


@pytest.mark.asyncio
async def test_compile_rejects_css_and_removes_side_output(tmp_path):
  entry = tmp_path / "index.jsx"
  css = tmp_path / "theme.css"
  output = tmp_path / "app.js.staging"
  source = "import './theme.css';\nexport default function App(){ return null }"
  entry.write_text(source)
  css.write_text("body { color: red; }")

  with pytest.raises(CompileError, match="Compilation failed") as exc:
    await compile_jsx(1, source, out_path=output, source_path=entry)

  assert "CSS imports are not supported" in exc.value.stderr
  assert not output.exists()
  assert not Path(output.with_suffix(".css")).exists()
