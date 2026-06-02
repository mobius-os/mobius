---
title: Import three with the bare specifier, never a versioned vendor URL
type: note
importance: 2
access_count: 0
last_accessed: null
tags: [apps, gotcha]
mocs: [building-mobius-apps]
created: 2026-06-02
updated: 2026-06-02
---
`import * as THREE from 'three'` and `import { OrbitControls } from
'three/addons/controls/OrbitControls.js'` just work — three is self-hosted via the
app-frame import map (no esm.sh waterfall).

**Why:** hardcoding `/vendor/three@<version>/…` pins you to a version; a three bump
then 404s the build → SPA HTML fallback → "failed to load dynamic module".

**How to apply:** always the bare `'three'` specifier; the import map points at the
pinned version, so it's version-proof.
