// Runtime files that app bundles intentionally keep as URLs rather than
// embedding in their compiled module. Keep the versions in lockstep with the
// Dockerfile copies. Stable aliases carry an explicit revision so a pin bump
// replaces their precached bytes; versioned URLs are immutable cache keys.

export const PDFJS_ASSET_VERSION = '4.10.38'
export const KATEX_ASSET_VERSION = '0.17.0'

export const KATEX_WOFF2_FILES = [
  'KaTeX_AMS-Regular.woff2',
  'KaTeX_Caligraphic-Bold.woff2',
  'KaTeX_Caligraphic-Regular.woff2',
  'KaTeX_Fraktur-Bold.woff2',
  'KaTeX_Fraktur-Regular.woff2',
  'KaTeX_Main-Bold.woff2',
  'KaTeX_Main-BoldItalic.woff2',
  'KaTeX_Main-Italic.woff2',
  'KaTeX_Main-Regular.woff2',
  'KaTeX_Math-BoldItalic.woff2',
  'KaTeX_Math-Italic.woff2',
  'KaTeX_SansSerif-Bold.woff2',
  'KaTeX_SansSerif-Italic.woff2',
  'KaTeX_SansSerif-Regular.woff2',
  'KaTeX_Script-Regular.woff2',
  'KaTeX_Size1-Regular.woff2',
  'KaTeX_Size2-Regular.woff2',
  'KaTeX_Size3-Regular.woff2',
  'KaTeX_Size4-Regular.woff2',
  'KaTeX_Typewriter-Regular.woff2',
]

const katexAssets = (base, revision) => [
  { url: `${base}/katex.min.css`, revision },
  ...KATEX_WOFF2_FILES.map(file => ({
    url: `${base}/fonts/${file}`,
    revision,
  })),
]

export const RETAINED_RUNTIME_ASSETS = [
  // pdfjs-dist itself is part of each compiled app module. Its worker remains
  // a real URL because Worker construction cannot consume that embedded graph.
  {
    url: '/vendor/pdfjs/pdf.worker.mjs',
    revision: `pdfjs-${PDFJS_ASSET_VERSION}`,
  },
  // Existing apps use the documented stable alias. Notes/Markdown use the
  // immutable versioned path. Precache both contracts until all app sources
  // naturally converge; the duplicate KaTeX payload is only ~284 KiB.
  ...katexAssets(
    '/vendor/katex',
    `katex-${KATEX_ASSET_VERSION}`,
  ),
  ...katexAssets(`/vendor/katex@${KATEX_ASSET_VERSION}`, null),
]
