"""Canonical list of mini-app runtime libraries externalized by esbuild."""

RUNTIME_LIBS: tuple[str, ...] = (
  "react",
  "react/jsx-runtime",
  "react-dom",
  "recharts",
  "date-fns",
  "three",
  "three/addons/*",
  "pdfjs-dist",
)
