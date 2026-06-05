# Mind Graph Renderer Notes

Last checked: 2026-06-05.

## Current choice

Mind currently uses `react-force-graph-2d`, which wraps `force-graph`: a
Canvas renderer backed by `d3-force`. This is the short-term bridge, not the
long-term renderer target. It is small, works in the mini-app runtime through
ESM imports, supports pan/zoom/drag, and was useful for proving the Mind data
contract and panel UX quickly.

The weakness is labels. DOM overlay labels can be made selective and smoother,
but they are still synchronized from outside the renderer's scene graph. That is
more fragile than the Quartz approach and is already visible as label jitter and
density tuning work.

Do not treat the current renderer as future-proof for Mind. It is acceptable for
shipping incremental product fixes, but the robust target is a Quartz-style
renderer module.

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

## Target path

Create a Mobius-owned `MindGraphRenderer` module that ports/adapts Quartz's
D3 + Pixi pattern while preserving Mobius-specific behavior:

- input is the existing `/api/storage/shared/memory/graph.json` data contract
- labels are Pixi `Text` objects in the same transformed scene as nodes
- global graph supports zoom-aware label opacity, hover focus, radial/global
  layout tuning, MOC colors, and selected-node emphasis
- local graph supports depth 1-4 and center-node pinning/priority
- renderer emits `nodeClick` / `nodeHover` callbacks for the React note panel
- React owns data loading, markdown rendering, mini-app nav, and mobile layout

This gives us Quartz's maintainable rendering model without importing Quartz's
site-generator assumptions into the mini-app runtime.

## Other upgrade candidates

If Mind needs tens of thousands of visible nodes, revisit renderer choice:

- `sigma.js` + `graphology`: strongest general candidate for large interactive
  readable graphs. It is WebGL-first, targets thousands of nodes and edges, and
  has native node labels, `forceLabel`, and graph algorithms through graphology.
  Docs: https://www.sigmajs.org/docs/
- `@cosmos.gl/graph`: strongest raw-scale candidate. It runs simulation and
  rendering on the GPU and targets hundreds of thousands of points/links, but it
  is a bigger dependency and a less Obsidian-like note-graph UI out of the box.
  Repo: https://github.com/cosmosgl/graph
- Quartz graph: best UX/architecture fit for Obsidian-style Mind browsing, but
  it should be ported as a Mobius renderer module rather than vendored
  wholesale.

## Current UX decisions

- Global graph stays visible as the broad map.
- Clicking a node opens a split pane: note text on the left, local graph on the
  right.
- Local graph depth is selected inside that split pane so the global toolbar
  stays simple.
- `[[slug]]` links render using the target node title, matching Obsidian's feel.
- The current renderer keeps the simulation lightly warm with a small alpha
  target instead of cooling to a fully static layout. The Quartz-style renderer
  should make this explicit and configurable per local/global graph.
