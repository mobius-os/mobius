"""Static app-source checks: source_files completeness + external hosts.

The completeness check is the mechanical version of the launch audit that
caught the Editor shipping with two imported icons missing from
`source_files` — an app that installs from a git clone (whole repo on disk)
but breaks on every synthetic-fetch install path. The external-host check is
the automated "grep the build for https://" review step (prod CSP is
`connect-src 'self'`).
"""

from app.app_source_check import check_app_source, check_manifest_tree


def _codes(result):
  return sorted(f.code for f in result.findings)


# --- source_files completeness (errors) ------------------------------------


def test_single_file_app_is_complete():
  """A one-file app with no relative imports has nothing to declare."""
  files = {"index.jsx": "export default function App(){ return null }"}
  result = check_app_source(files, entry="index.jsx")
  assert result.ok
  assert result.findings == []


def test_declared_siblings_complete():
  """Every reachable sibling declared in source_files -> clean."""
  files = {
    "index.jsx": "import { A } from './a.js'\nexport default function App(){ return A }",
    "a.js": "export const A = 1",
  }
  result = check_app_source(files, entry="index.jsx", source_files=["a.js"])
  assert result.ok


def test_imported_sibling_missing_from_source_files_is_error():
  """The Editor bug: a sibling is imported and present on disk but absent
  from source_files, so a fetch install would ship an incomplete tree."""
  files = {
    "index.jsx": "import { Icon } from './ui/Icon.jsx'\nexport default () => Icon",
    "ui/Icon.jsx": "export const Icon = 1",
  }
  result = check_app_source(files, entry="index.jsx", source_files=[])
  assert not result.ok
  assert _codes(result) == ["undeclared_source"]
  assert result.errors[0].path == "ui/Icon.jsx"


def test_transitive_undeclared_sibling_is_error():
  """A declared sibling that itself imports an undeclared file is caught by
  the transitive walk, not just the first import level."""
  files = {
    "index.jsx": "import { A } from './a.js'\nexport default () => A",
    "a.js": "import { B } from './b.js'\nexport const A = B",
    "b.js": "export const B = 2",
  }
  # a.js declared, b.js is not.
  result = check_app_source(files, entry="index.jsx", source_files=["a.js"])
  assert not result.ok
  assert result.errors[0].code == "undeclared_source"
  assert result.errors[0].path == "b.js"


def test_undeclared_sibling_reported_once_across_importers():
  """A single undeclared file imported from two places yields one finding, not
  one per import edge."""
  files = {
    "index.jsx": "import './a.js'\nimport './b.js'\nexport default () => null",
    "a.js": "import { S } from './shared.js'\nexport const A = S",
    "b.js": "import { S } from './shared.js'\nexport const B = S",
    "shared.js": "export const S = 1",
  }
  result = check_app_source(
    files, entry="index.jsx", source_files=["a.js", "b.js"],
  )
  undeclared = [f for f in result.errors if f.code == "undeclared_source"]
  assert len(undeclared) == 1
  assert undeclared[0].path == "shared.js"


def test_import_resolving_to_no_file_is_error():
  """An import with no matching file anywhere (the shape a synthetic fetch
  hits when the sibling was never listed) is a missing_import error."""
  files = {"index.jsx": "import './gone.js'\nexport default () => null"}
  result = check_app_source(files, entry="index.jsx", source_files=[])
  assert not result.ok
  assert result.errors[0].code == "missing_import"


def test_declared_source_file_absent_from_tree_is_error():
  files = {"index.jsx": "export default () => null"}
  result = check_app_source(files, entry="index.jsx", source_files=["ghost.js"])
  assert not result.ok
  assert result.errors[0].code == "declared_source_missing"


def test_extension_and_directory_index_resolution():
  """`./x` resolves to x.jsx; `./ui` resolves to ui/index.jsx."""
  files = {
    "index.jsx": "import './util'\nimport './ui'\nexport default () => null",
    "util.js": "export const U = 1",
    "ui/index.jsx": "export const V = 2",
  }
  result = check_app_source(
    files, entry="index.jsx", source_files=["util.js", "ui/index.jsx"],
  )
  assert result.ok


def test_parent_relative_import_resolves():
  files = {
    "ui/panel.jsx": "import { C } from '../constants.js'\nexport const P = C",
    "index.jsx": "import { P } from './ui/panel.jsx'\nexport default () => P",
    "constants.js": "export const C = 1",
  }
  result = check_app_source(
    files, entry="index.jsx", source_files=["ui/panel.jsx", "constants.js"],
  )
  assert result.ok


def test_static_asset_import_target_not_flagged():
  """A relative import onto a declared static-asset dest is legal — the
  installer writes it, so completeness must not flag it missing."""
  files = {
    "index.jsx": "import logo from './static/logo.png'\nexport default () => logo",
    "static/logo.png": "",
  }
  result = check_app_source(
    files, entry="index.jsx", source_files=[], static_assets=["static/logo.png"],
  )
  assert result.ok


def test_bare_specifier_runtime_lib_ignored():
  """Bare specifiers (react, three) are runtime libs, not relative siblings."""
  files = {"index.jsx": "import { useState } from 'react'\nexport default () => null"}
  result = check_app_source(files, entry="index.jsx")
  assert result.ok


# --- schedule job as an import root ----------------------------------------


def test_job_script_is_a_root_and_need_not_be_declared():
  """The schedule job is a graph root but is intentionally NOT in
  source_files; a sibling it imports still must be declared."""
  files = {
    "index.jsx": "export default () => null",
    "remind.mjs": "import { fmt } from './fmt.js'\nfmt()",
    "fmt.js": "export const fmt = () => 0",
  }
  ok = check_app_source(
    files, entry="index.jsx", source_files=["fmt.js"], job="remind.mjs",
  )
  assert ok.ok

  bad = check_app_source(
    files, entry="index.jsx", source_files=[], job="remind.mjs",
  )
  assert not bad.ok
  assert bad.errors[0].path == "fmt.js"


def test_declared_job_missing_from_tree_is_error():
  files = {"index.jsx": "export default () => null"}
  result = check_app_source(files, entry="index.jsx", job="remind.sh")
  assert not result.ok
  assert result.errors[0].code == "job_missing"


# --- external-host references (warnings) ------------------------------------


def test_external_import_host_is_warning_not_error():
  files = {
    "index.jsx": "const m = import('https://esm.sh/leaflet')\nexport default () => m",
  }
  result = check_app_source(files, entry="index.jsx")
  assert result.ok  # warnings don't fail
  assert [f.code for f in result.warnings] == ["external_host"]
  assert "esm.sh" in result.warnings[0].detail


def test_external_fetch_host_is_warning():
  files = {
    "index.jsx": (
      "export default function App(){ "
      "fetch('https://fonts.googleapis.com/css'); return null }"
    ),
  }
  result = check_app_source(files, entry="index.jsx")
  assert [f.code for f in result.warnings] == ["external_host"]
  assert "fonts.googleapis.com" in result.warnings[0].detail


def test_url_in_comment_is_not_flagged():
  files = {
    "index.jsx": (
      "// see https://example.com/docs for the format\n"
      "export default () => null"
    ),
  }
  result = check_app_source(files, entry="index.jsx")
  assert result.warnings == []


def test_svg_namespace_is_not_flagged():
  """`xmlns=\"http://www.w3.org/2000/svg\"` is a namespace id, never fetched."""
  files = {
    "index.jsx": (
      "export default () => "
      "<svg xmlns=\"http://www.w3.org/2000/svg\"><path/></svg>"
    ),
  }
  result = check_app_source(files, entry="index.jsx")
  assert result.warnings == []


def test_proxied_url_still_warns_but_does_not_fail():
  """A URL routed through /api/proxy is legitimate, but flagging it as a
  reviewable warning matches the documented grep-for-https step; it must not
  be an error."""
  files = {
    "index.jsx": (
      "const U='https://cdn.jsdelivr.net/x.css';"
      "fetch(`/api/proxy?url=${encodeURIComponent(U)}`);"
      "export default () => null"
    ),
  }
  result = check_app_source(files, entry="index.jsx")
  assert result.ok
  assert result.warnings[0].detail.count("cdn.jsdelivr.net") == 1


def test_external_scan_ignores_unreachable_files():
  """Only the module graph reachable from the entry ships under the CSP, so a
  URL in an unreferenced file (a build script, a test) is not flagged."""
  files = {
    "index.jsx": "export default () => null",
    "build.mjs": "import 'https://esm.sh/esbuild'",
  }
  result = check_app_source(files, entry="index.jsx", source_files=["build.mjs"])
  assert result.warnings == []


# --- commented / string-embedded delimiters (regression) -------------------


def test_commented_out_import_is_not_flagged():
  """A `//` line comment (that even mentions `/*`) must not corrupt the scan
  or be read as a real import."""
  files = {
    "index.jsx": (
      "// legacy: ui/*.jsx modules, import { Old } from './old.js'\n"
      "import { A } from './a.js'\n"
      "export default () => A"
    ),
    "a.js": "export const A = 1",
  }
  result = check_app_source(files, entry="index.jsx", source_files=["a.js"])
  assert result.ok


def test_block_comment_does_not_swallow_following_imports():
  files = {
    "index.jsx": (
      "/* a block comment with a // and a path ui/*.jsx inside */\n"
      "import { A } from './a.js'\n"
      "export default () => A"
    ),
    "a.js": "export const A = 1",
  }
  result = check_app_source(files, entry="index.jsx", source_files=["a.js"])
  assert result.ok


# --- manifest wrapper ------------------------------------------------------


def test_check_manifest_tree_derives_fields():
  manifest = {
    "entry": "index.jsx",
    "source_files": ["a.js"],
    "schedule": {"job": "job.mjs", "default": "* * * * *"},
  }
  files = {
    "index.jsx": "import { A } from './a.js'\nexport default () => A",
    "a.js": "export const A = 1",
    "job.mjs": "console.log('run')",
  }
  result = check_manifest_tree(manifest, files)
  assert result.ok


def test_check_manifest_tree_flags_incomplete():
  manifest = {"entry": "index.jsx", "source_files": []}
  files = {
    "index.jsx": "import { A } from './a.js'\nexport default () => A",
    "a.js": "export const A = 1",
  }
  result = check_manifest_tree(manifest, files)
  assert not result.ok
  assert result.errors[0].code == "undeclared_source"
