# Platform support for Memory

Memory loads its runtime libraries via import-map **bare specifiers** resolved
same-origin from the Mobius shell's `/vendor` importmap — no `esm.sh` fallback:

- Markdown rendering: `import('marked')` + `import('dompurify')` (declared in
  `mobius.json` `runtime.imports`). The shell vendors both (the `app-frame.html`
  importmap + `backend/app/runtime_libs.py` `RUNTIME_LIBS`), exactly like Notes.
- Graph: `d3` + `PixiJS`, loaded as same-origin classic scripts from `/vendor`.

So markdown and graph rendering are fully deterministic offline on the current
platform — there is no third-party CDN dependency. If a future renderer needs an
additional library, vendor it in the shell first (Dockerfile vendor step +
`app-frame.html` importmap + `RUNTIME_LIBS`), then add the bare specifier to
`mobius.json` `runtime.imports`.
