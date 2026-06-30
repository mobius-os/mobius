# Platform support for Memory

Memory now tries runtime libraries in this order:

1. Import-map bare specifier.
2. Same-origin `/vendor` module.
3. Current `esm.sh` URL fallback.

That means the app keeps working on today's platform, but fully deterministic
offline graph and markdown rendering needs a small Mobius platform vendor pass.

## Required vendor modules

Serve these files from the Mobius shell:

```json
{
  "react-force-graph-2d": "/vendor/react-force-graph-2d@1.27.1/react-force-graph-2d.mjs",
  "marked": "/vendor/marked@14.1.4/marked.mjs",
  "dompurify": "/vendor/dompurify@3.1.7/dompurify.mjs"
}
```

`react-force-graph-2d` must be bundled with `react` and `react-dom` externalized
so it shares the app frame's singleton React instance. The bundle may need to
include the force-graph/d3 transitive graph, similar to the existing one-file
CodeMirror vendor pattern in Notes.

## Mobius changes

- Add the files above under `/app/static/vendor/...` during the image build.
- Add the three import-map entries to `frontend/public/app-frame.html` and
  `backend/app/routes/standalone.py`.
- Add the same bare specifiers to the runtime library allowlist/schema before
  moving them from `mobius.json.runtime.esm_deps` to `runtime.imports`.
- Keep `esm.sh` CSP/runtime-cache support until all installed Memory versions have
  the fallback removed.

Until that lands, Memory intentionally keeps the three libraries in
`runtime.esm_deps` so the App Store still discloses the network fallback.
