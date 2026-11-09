"""Production compiler enforcement for the shared mini-app output contract."""

from pathlib import Path

import pytest

from app.compiler import CompileError, compile_jsx


@pytest.mark.asyncio
async def test_compile_accepts_default_reexport(tmp_path):
  output = tmp_path / "compiled" / "app.js"
  source = "const App = () => null;\nexport { App as default };"

  await compile_jsx(source, out_path=output)

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

  await compile_jsx(
    source, out_path=output, source_path=entry, source_root=tmp_path,
  )

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
    await compile_jsx(
    source, out_path=output, source_path=entry, source_root=tmp_path,
  )

  assert "Could not resolve 'index.js'" in exc.value.stderr
  assert not output.exists()


@pytest.mark.asyncio
async def test_compile_preserves_explicit_online_dynamic_import(tmp_path):
  output = tmp_path / "app.js"
  source = (
    "export const load = () => import('https://esm.sh/example-package');\n"
    "export default function App(){ return null }"
  )

  await compile_jsx(source, out_path=output)

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

    await compile_jsx(
    source, out_path=output, source_path=entry, source_root=tmp_path,
  )
    outputs.append(output.read_bytes())

  assert outputs[0] == outputs[1]


@pytest.mark.asyncio
async def test_compile_rejects_comment_that_only_mentions_default_export(tmp_path):
  output = tmp_path / "app.js"
  source = "// export default function Fake() {}\nexport const value = 1;"

  with pytest.raises(CompileError, match="Compilation failed") as exc:
    await compile_jsx(source, out_path=output)

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
    await compile_jsx(
    source, out_path=output, source_path=entry, source_root=tmp_path,
  )

  assert "CSS imports are not supported" in exc.value.stderr
  assert not output.exists()
  assert not Path(output.with_suffix(".css")).exists()


# The bundler runs as the server user, so it can read owner tokens, secrets
# and other apps' data. A package or project must not be able to inline any
# of that into its bundle, or see it quoted back in a compile error.


def _app_beside_secret(tmp_path):
  app = tmp_path / "app"
  app.mkdir()
  secret = tmp_path / "secret.txt"
  secret.write_text("OUTSIDE-SECRET-VALUE")
  return app, secret


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["absolute", "relative"])
async def test_compile_never_reads_files_outside_the_app_tree(tmp_path, spelling):
  app, secret = _app_beside_secret(tmp_path)
  specifier = str(secret) if spelling == "absolute" else "../secret.txt"
  entry = app / "index.jsx"
  source = (
    f"import value from {specifier!r}\n"
    "export default function App(){ return value }"
  )
  entry.write_text(source)
  output = tmp_path / "out.js"

  with pytest.raises(CompileError) as exc:
    await compile_jsx(source, out_path=output, source_path=entry, source_root=app)

  assert "only files inside its own app folder" in exc.value.stderr
  assert "OUTSIDE-SECRET-VALUE" not in exc.value.stderr
  assert not output.exists()


@pytest.mark.asyncio
async def test_compile_error_never_quotes_an_unparsable_outside_file(tmp_path):
  app = tmp_path / "app"
  app.mkdir()
  key = tmp_path / "key"
  key.write_text("9f" * 32 + "\n")
  entry = app / "index.jsx"
  source = f"import k from {str(key)!r}\nexport default function App(){{ return k }}"
  entry.write_text(source)

  with pytest.raises(CompileError) as exc:
    await compile_jsx(
      source, out_path=tmp_path / "out.js", source_path=entry, source_root=app,
    )

  assert "9f9f9f" not in exc.value.stderr


@pytest.mark.asyncio
async def test_compile_refuses_a_symlink_that_leaves_the_app_tree(tmp_path):
  app, secret = _app_beside_secret(tmp_path)
  (app / "data.txt").symlink_to(secret)
  entry = app / "index.jsx"
  source = "import v from './data.txt'\nexport default function App(){ return v }"
  entry.write_text(source)

  with pytest.raises(CompileError) as exc:
    await compile_jsx(
      source, out_path=tmp_path / "out.js", source_path=entry, source_root=app,
    )

  assert "OUTSIDE-SECRET-VALUE" not in exc.value.stderr


@pytest.mark.asyncio
async def test_nested_entry_still_imports_from_its_app_root(tmp_path):
  app = tmp_path / "app"
  (app / "src").mkdir(parents=True)
  (app / "shared.js").write_text("export const label = 'APP-ROOT-MODULE'")
  entry = app / "src" / "index.jsx"
  source = (
    "import React from 'react'\nimport { label } from '../shared.js'\n"
    "export default function App(){ return <p>{label}</p> }"
  )
  entry.write_text(source)
  output = tmp_path / "out.js"

  await compile_jsx(source, out_path=output, source_path=entry, source_root=app)

  assert "APP-ROOT-MODULE" in output.read_text()


@pytest.mark.asyncio
async def test_source_only_compile_cannot_read_other_temp_files(tmp_path):
  import tempfile

  neighbour = Path(tempfile.gettempdir()) / f"mobius-neighbour-{tmp_path.name}.txt"
  neighbour.write_text("TEMP-NEIGHBOUR-VALUE")
  try:
    source = (
      f"import v from {str(neighbour)!r}\n"
      "export default function App(){ return v }"
    )
    with pytest.raises(CompileError) as exc:
      await compile_jsx(source, out_path=tmp_path / "out.js")
    assert "TEMP-NEIGHBOUR-VALUE" not in exc.value.stderr
  finally:
    neighbour.unlink()
