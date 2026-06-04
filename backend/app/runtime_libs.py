"""Canonical list of mini-app runtime libraries externalized by esbuild."""

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
  # CodeMirror 6 + KaTeX — the importmap (app-frame.html) resolves these
  # at runtime; this list must externalize them or esbuild tries to bundle
  # the bare specifier and the install fails ("Could not resolve
  # 'codemirror'"). They were in the importmap (for the Notes app) but not
  # here, which made Notes uninstallable. tests/test_runtime_libs.py locks
  # the two lists together so the next addition can't desync.
  "codemirror",
  "katex",
)
