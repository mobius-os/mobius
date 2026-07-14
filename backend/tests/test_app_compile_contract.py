"""Production compiler enforcement for the shared mini-app output contract."""

from pathlib import Path

import pytest

from app.compiler import CompileError, compile_jsx


@pytest.mark.asyncio
async def test_compile_accepts_default_reexport(tmp_path):
  output = tmp_path / "app.js"
  source = "const App = () => null;\nexport { App as default };"

  await compile_jsx(1, source, out_path=output)

  assert output.is_file()


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
