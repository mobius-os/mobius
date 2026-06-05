# Mind Graph Renderer Notes

Last checked: 2026-06-05.

## Current choice

Mind currently uses `react-force-graph-2d`, which wraps `force-graph`: a
Canvas renderer backed by `d3-force`. This remains a good fit for the current
scope because it is small, works in the mini-app runtime through ESM imports,
supports pan/zoom/drag, lets us custom-paint labels and halos, and has examples
around a few thousand graph elements.

Keep it for now unless the real Mind graph starts showing frame drops or
interaction lag at target scale.

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

Useful links:

- https://github.com/jackyzha0/quartz/blob/9737bce7095f93c9fb41700449505d963a6b2bb8/docs/plugins/Graph.md
- https://github.com/jackyzha0/quartz/blob/9737bce7095f93c9fb41700449505d963a6b2bb8/docs/configuration.md
- https://github.com/quartz-community/graph

## Upgrade path

If Mind needs tens of thousands of visible nodes, revisit renderer choice:

- `sigma.js` + `graphology`: strongest general candidate for large interactive
  readable graphs. It is WebGL-first, targets thousands of nodes and edges, and
  has native node labels, `forceLabel`, and graph algorithms through graphology.
  Docs: https://www.sigmajs.org/docs/
- `@cosmos.gl/graph`: strongest raw-scale candidate. It runs simulation and
  rendering on the GPU and targets hundreds of thousands of points/links, but it
  is a bigger dependency and a less Obsidian-like note-graph UI out of the box.
  Repo: https://github.com/cosmosgl/graph
- Quartz community graph: good UX reference for local/global controls and a
  PixiJS+D3 implementation, but porting it wholesale would be more invasive
  than adapting our current data contract.

## Current UX decisions

- Global graph stays visible as the broad map.
- Clicking a node opens a split pane: note text on the left, local graph on the
  right.
- Local graph depth is selected inside that split pane so the global toolbar
  stays simple.
- `[[slug]]` links render using the target node title, matching Obsidian's feel.
- The simulation stays lightly warm with a small alpha target instead of cooling
  to a fully static layout.
