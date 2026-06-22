# Memory Graph Renderer Notes

Last checked: 2026-06-05.

## Current choice

Memory now uses a Mobius-owned `MemoryGraphRenderer` inside
`core-apps/memory/index.jsx`. It follows the Quartz rendering architecture:
`d3-force` owns layout, while PixiJS renders links, nodes, and labels in one
transformed scene.

This replaced the previous `react-force-graph-2d` bridge and its DOM label
overlay. That bridge was useful for proving the Memory data contract and panel UX,
but synchronizing labels outside the graph scene was the wrong long-term shape:
it introduced jitter, density tuning work, and a fragile dependency on
screen-coordinate polling.

The current renderer is the right ownership boundary for Memory: React owns data
loading, markdown, mini-app navigation, and mobile layout; the graph renderer
owns force layout, zoom/pan/drag, hit testing, hover focus, and label placement.

## Quartz reference

Quartz's graph plugin exposes separate local and global graph options:
`depth`, `scale`, `repelForce`, `centerForce`, `linkDistance`, `fontSize`,
`opacityScale`, `focusOnHover`, and `enableRadial`.

The current community implementation advertises:

- local and global graph views
- D3 force simulation
- PixiJS rendering
- node labels
- tag nodes
- hover focus
- fullscreen global graph

Quartz is not a drop-in React library. Its graph code is a Quartz component
script wired to Quartz's static content index, slugs, SPA navigation, and theme
lifecycle. The reusable part for Mobius is its architecture: `d3-force` for
layout and PixiJS for nodes, links, and labels in one render loop.

Useful links:

- https://github.com/jackyzha0/quartz/blob/9737bce7095f93c9fb41700449505d963a6b2bb8/docs/plugins/Graph.md
- https://github.com/jackyzha0/quartz/blob/9737bce7095f93c9fb41700449505d963a6b2bb8/docs/configuration.md
- https://github.com/quartz-community/graph

## Implemented path

`MemoryGraphRenderer` ports/adapts Quartz's D3 + Pixi pattern while preserving
Mobius-specific behavior:

- input is the existing `/api/storage/shared/memory/graph.json` data contract
- labels are Pixi `Text` objects in the same transformed scene as nodes
- global graph supports zoom-aware labels, hover focus, MOC colors, and
  selected-node emphasis
- local graph supports depth 1-4 and center-node pinning/priority
- renderer emits `nodeClick` / `nodeHover` callbacks for the React note panel
- React owns data loading, markdown rendering, mini-app nav, and mobile layout

This gives us Quartz's maintainable rendering model without importing Quartz's
site-generator assumptions into the mini-app runtime.

Remaining hardening candidates:

- move `MemoryGraphRenderer` into its own source module if/when core apps support
  multi-file entries cleanly
- add persisted node positions if the graph grows enough that deterministic
  seeded starts feel too mobile between reloads
- add radial/MOC-group layout tuning for larger global graphs
- add a tiny renderer perf probe around frame time and visible node count before
  Memory becomes a central navigation surface for hundreds of notes

## Other upgrade candidates

If Memory needs tens of thousands of visible nodes, revisit renderer choice:

- `sigma.js` + `graphology`: strongest general candidate for large interactive
  readable graphs. It is WebGL-first, targets thousands of nodes and edges, and
  has native node labels, `forceLabel`, and graph algorithms through graphology.
  Docs: https://www.sigmajs.org/docs/
- `@cosmos.gl/graph`: strongest raw-scale candidate. It runs simulation and
  rendering on the GPU and targets hundreds of thousands of points/links, but it
  is a bigger dependency and a less Obsidian-like note-graph UI out of the box.
  Repo: https://github.com/cosmosgl/graph
- Quartz graph: best UX/architecture fit for Obsidian-style Memory browsing, but
  it should be ported as a Mobius renderer module rather than vendored
  wholesale.

## Current UX decisions

- Global graph stays visible as the broad map.
- Clicking a node opens a full-height note panel. Desktop uses note text on the
  left and local graph on the right; phone uses local graph above scrollable note
  text with browser/app back navigation closing the note.
- Local graph depth is selected inside that split pane so the global toolbar
  stays simple.
- `[[slug]]` links render using the target node title, matching Obsidian's feel.
- The renderer warms from deterministic seeded positions, fits the graph to the
  viewport, then lets D3 settle while Pixi keeps labels in the same scene.
