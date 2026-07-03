import { useEffect, useRef } from 'react'
import { S } from '../constants.js'
import {
  clamp,
  cssVar,
  hashStr,
  labelScore,
  nodeRadius,
  parseRGB,
  shouldShowScreenLabel,
} from '../domain.js'

export function MemoryGraphRenderer({
  runtime,
  graphData,
  width,
  height,
  mode,
  selectedId,
  hoverId,
  colorForNode,
  radiusForNode,
  onNodeClick,
  onNodeHover,
  onBackgroundClick,
}) {
  const hostRef = useRef(null);
  const latestRef = useRef({});

  useEffect(() => {
    latestRef.current = {
      selectedId,
      hoverId,
      colorForNode,
      radiusForNode,
      onNodeClick,
      onNodeHover,
      onBackgroundClick,
    };
  }, [selectedId, hoverId, colorForNode, radiusForNode, onNodeClick, onNodeHover, onBackgroundClick]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !runtime || width <= 0 || height <= 0) return undefined;

    let disposed = false;
    let cleanup = () => {};
    host.replaceChildren();

    createMemoryGraphScene({
      host,
      runtime,
      graphData,
      width,
      height,
      mode,
      latestRef,
      isDisposed: () => disposed,
    }).then((nextCleanup) => {
      if (disposed) {
        try { nextCleanup?.(); } catch {}
      } else {
        cleanup = nextCleanup || cleanup;
      }
    }).catch((err) => {
      console.error('[Memory] Graph renderer failed', err);
      if (!disposed) host.textContent = 'Graph could not render.';
    });

    return () => {
      disposed = true;
      try { cleanup(); } catch {}
      host.replaceChildren();
    };
  }, [runtime, graphData, width, height, mode]);

  return (
    <div
      ref={hostRef}
      style={S.pixiGraph}
      className="mg-pixi-graph"
      aria-label={mode === 'local' ? 'Local note graph' : 'Memory graph'}
    />
  );
}

async function createMemoryGraphScene({
  host,
  runtime,
  graphData,
  width,
  height,
  mode,
  latestRef,
  isDisposed,
}) {
  const { d3, PIXI } = runtime;
  const graph = normalizeRendererGraphData(graphData, width, height);
  const app = new PIXI.Application();
  await app.init({
    width,
    height,
    antialias: true,
    backgroundAlpha: 0,
    autoDensity: true,
    resolution: window.devicePixelRatio || 1,
    // Drive rendering ourselves (below) so each app.render() is wrapped in a
    // guard — a single bad batcher frame can never take down the whole app.
    autoStart: false,
  });
  if (isDisposed()) {
    try { app.destroy(true, { children: true, texture: true, textureSource: true }); } catch {}
    return () => {};
  }

  const canvas = app.canvas || app.view;
  canvas.style.width = '100%';
  canvas.style.height = '100%';
  canvas.style.display = 'block';
  canvas.style.touchAction = 'none';
  host.appendChild(canvas);

  const scene = new PIXI.Container();
  const linkLayer = new PIXI.Graphics();
  const nodeLayer = new PIXI.Container();
  const labelLayer = new PIXI.Container();
  scene.addChild(linkLayer);
  scene.addChild(nodeLayer);
  scene.addChild(labelLayer);
  app.stage.addChild(scene);

  const neighbors = buildRendererNeighborMap(graph.links);
  const labelRanks = buildLabelRankMap(graph.nodes);
  const focus = new Map(graph.nodes.map((node) => [node.id, 1]));
  let currentTransform = d3.zoomIdentity.translate(width / 2, height / 2).scale(mode === 'local' ? 0.95 : 0.82);
  let lastFrame = performance.now();
  let dragStart = null;
  let activeDragNode = null;
  let lastNodeClickAt = 0;
  let lastHoverId = null;

  const linkDistance = mode === 'local' ? 42 : 64;
  const chargeStrength = mode === 'local' ? -120 : -185;
  const centerForce = d3.forceCenter(0, 0);
  if (typeof centerForce.strength === 'function') centerForce.strength(mode === 'local' ? 0.12 : 0.06);

  const simulation = d3.forceSimulation(graph.nodes)
    .force('charge', d3.forceManyBody().strength((node) => node.type === 'moc' ? chargeStrength * 1.25 : chargeStrength))
    .force('link', d3.forceLink(graph.links).distance((link) => link.kind === 'moc' ? linkDistance * 0.82 : linkDistance).strength((link) => link.kind === 'moc' ? 0.42 : 0.22))
    .force('center', centerForce)
    .force('collide', d3.forceCollide().radius((node) => latestRadius(node) + 8).iterations(2))
    .velocityDecay(mode === 'local' ? 0.34 : 0.3)
    .stop();

  simulation.tick(mode === 'local' ? 80 : 130);

  const renderNodes = graph.nodes.map((node) => {
    const gfx = new PIXI.Graphics();
    const label = new PIXI.Container();
    const labelBg = new PIXI.Graphics();
    const labelText = new PIXI.Text({
      text: truncateGraphLabel(node.title || node.id),
      style: {
        fontFamily: graphFontFamily(),
        fontSize: node.type === 'moc' ? 12 : 11,
        fontWeight: node.type === 'moc' ? '700' : '650',
        fill: colorNumber(cssVar('--text', '#e5e5e5'), '#e5e5e5'),
      },
      resolution: (window.devicePixelRatio || 1) * 3,
    });
    labelText.anchor.set(0.5, 0);
    label.addChild(labelBg);
    label.addChild(labelText);
    nodeLayer.addChild(gfx);
    labelLayer.addChild(label);
    return { node, gfx, label, labelBg, labelText };
  });

  function latestRadius(node) {
    return latestRef.current.radiusForNode?.(node) ?? nodeRadius(node);
  }

  function latestColor(node) {
    return latestRef.current.colorForNode?.(node) || cssVar('--muted', '#8a8a93');
  }

  function isFocused(id) {
    const hovered = latestRef.current.hoverId;
    if (!hovered) return true;
    return id === hovered || neighbors.get(hovered)?.has(id);
  }

  function focusOf(id, dt) {
    const goal = isFocused(id) ? 1 : 0.18;
    const current = focus.get(id) ?? 1;
    const k = 1 - Math.pow(0.002, Math.min(48, dt) / 1000);
    const next = current + (goal - current) * k;
    focus.set(id, next);
    return next;
  }

  function applyTransform(transform) {
    currentTransform = transform;
    scene.position.set(transform.x, transform.y);
    scene.scale.set(transform.k, transform.k);
  }

  function hitNode(screenX, screenY) {
    const k = currentTransform.k || 1;
    const x = (screenX - currentTransform.x) / k;
    const y = (screenY - currentTransform.y) / k;
    let best = null;
    let bestDist = Infinity;
    for (const node of graph.nodes) {
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
      const dx = x - node.x;
      const dy = y - node.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const hitR = latestRadius(node) + 10 / k;
      if (dist <= hitR && dist < bestDist) {
        best = node;
        bestDist = dist;
      }
    }
    return best;
  }

  function draw() {
    if (isDisposed()) return;
    const now = performance.now();
    const dt = now - lastFrame;
    lastFrame = now;
    const hover = latestRef.current.hoverId;
    const selected = latestRef.current.selectedId;
    const textColor = colorNumber(cssVar('--text', '#e5e5e5'), '#e5e5e5');
    const bgColor = colorNumber(cssVar('--bg', '#0d0d0d'), '#0d0d0d');
    const borderColor = colorNumber(cssVar('--text', '#e5e5e5'), '#e5e5e5');
    const linkColor = colorNumber(cssVar('--text', '#e5e5e5'), '#e5e5e5');
    const accentColor = colorNumber(cssVar('--accent', '#a78bfa'), '#a78bfa');
    const scale = currentTransform.k || 1;

    linkLayer.clear();
    for (const link of graph.links) {
      const s = link.source;
      const t = link.target;
      if (!Number.isFinite(s.x) || !Number.isFinite(s.y) || !Number.isFinite(t.x) || !Number.isFinite(t.y)) continue;
      const f = Math.min(focus.get(s.id) ?? 1, focus.get(t.id) ?? 1);
      const isMoc = link.kind === 'moc';
      linkLayer.moveTo(s.x, s.y);
      linkLayer.lineTo(t.x, t.y);
      linkLayer.stroke({
        width: isMoc ? 1.55 : 0.8,
        color: isMoc ? accentColor : linkColor,
        alpha: (isMoc ? 0.48 : 0.28) * (0.18 + 0.82 * f),
      });
    }

    for (const item of renderNodes) {
      const { node, gfx, label, labelBg, labelText } = item;
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
      const f = focusOf(node.id, dt);
      const r = latestRadius(node);
      const color = colorNumber(latestColor(node), '#8a8a93');
      const isHover = hover === node.id;
      const isSelected = selected === node.id;
      const isHub = node.type === 'moc';

      // A node is a flat colored disc with a thin ring — no glow halo, no
      // specular highlight dot (owner asked to simplify: the color carries the
      // identity, the ring carries hover/selected emphasis).
      gfx.clear();
      gfx.position.set(node.x, node.y);
      gfx.circle(0, 0, r);
      gfx.fill({ color, alpha: 0.18 + 0.82 * f });
      gfx.circle(0, 0, r);
      gfx.stroke({
        width: isHover || isSelected || isHub ? 1.4 / scale : 0.85 / scale,
        color: isHover || isSelected ? accentColor : borderColor,
        alpha: isHover || isSelected || isHub ? 0.62 : 0.24 * f,
      });

      const rank = labelRanks.get(node.id) ?? 9999;
      const showLabel = shouldShowScreenLabel(node, scale, rank, { mode, hoverId: hover, selectedId: selected });
      // Hide via alpha rather than .visible — toggling visibility on a label
      // mid-frame has tripped the Pixi v8 batcher; alpha=0 is the safe mute.
      if (!showLabel) {
        label.alpha = 0;
        continue;
      }

      const strong = isHub || isHover || isSelected || node.localDepth === 0;
      label.alpha = strong ? 1 : 0.7 + 0.25 * f;
      label.scale.set(1 / scale);
      label.position.set(node.x, node.y + r + 5 / scale);
      labelText.style.fill = textColor;
      labelText.style.fontSize = strong ? 12 : 11;
      labelText.style.fontWeight = strong ? '700' : '650';
      labelText.position.set(0, 2);

      const w = Math.ceil(labelText.width + 12);
      const h = Math.ceil(labelText.height + 5);
      labelBg.clear();
      labelBg.roundRect(-w / 2, 0, w, h, 7);
      labelBg.fill({ color: bgColor, alpha: strong ? 0.88 : 0.74 });
      labelBg.roundRect(-w / 2, 0, w, h, 7);
      labelBg.stroke({ width: 1, color: borderColor, alpha: strong ? 0.26 : 0.16 });
    }
  }

  const selection = d3.select(canvas);
  const zoom = d3.zoom()
    .extent([[0, 0], [width, height]])
    .scaleExtent([0.22, 4.6])
    .filter((event) => {
      if (event.type === 'mousedown') return !hitNode(event.offsetX, event.offsetY);
      if (event.type === 'touchstart') {
        // Same node-exclusion as mousedown. Without it, zoom and drag both
        // claim the touch (touch events skip the mousedown branch) and
        // dragging a node also pans the whole scene. Touch events carry no
        // offsetX/Y, so map the first touch into canvas coordinates by hand.
        const touch = event.touches?.[0];
        if (touch) {
          const rect = canvas.getBoundingClientRect();
          return !hitNode(touch.clientX - rect.left, touch.clientY - rect.top);
        }
      }
      return true;
    })
    .on('zoom', (event) => {
      applyTransform(event.transform);
      draw();
    });

  selection.call(zoom);
  const fit = computeRendererFitTransform(graph.nodes, width, height, {
    padding: mode === 'local' ? 34 : 72,
    minScale: mode === 'local' ? 0.72 : 0.42,
    maxScale: mode === 'local' ? 1.45 : 1.08,
  });
  selection.call(zoom.transform, d3.zoomIdentity.translate(fit.x, fit.y).scale(fit.k));

  const drag = d3.drag()
    .container(canvas)
    .subject((event) => hitNode(event.x, event.y))
    .on('start', (event) => {
      if (!event.subject) return;
      activeDragNode = event.subject;
      dragStart = { x: event.x, y: event.y, t: Date.now() };
      event.sourceEvent?.stopPropagation?.();
      simulation.alphaTarget(0.2).restart();
      event.subject.fx = event.subject.x;
      event.subject.fy = event.subject.y;
      latestRef.current.onNodeHover?.(event.subject);
    })
    .on('drag', (event) => {
      if (!event.subject) return;
      const k = currentTransform.k || 1;
      event.subject.fx = (event.x - currentTransform.x) / k;
      event.subject.fy = (event.y - currentTransform.y) / k;
    })
    .on('end', (event) => {
      if (!event.subject) return;
      event.sourceEvent?.stopPropagation?.();
      simulation.alphaTarget(0);
      event.subject.fx = null;
      event.subject.fy = null;
      const moved = dragStart
        ? Math.hypot(event.x - dragStart.x, event.y - dragStart.y)
        : Infinity;
      const quick = dragStart ? Date.now() - dragStart.t < 520 : false;
      activeDragNode = null;
      dragStart = null;
      if (moved < 7 && quick) {
        lastNodeClickAt = Date.now();
        latestRef.current.onNodeClick?.(event.subject);
      }
    });

  selection.call(drag);

  const onPointerMove = (event) => {
    if (activeDragNode) return;
    const node = hitNode(event.offsetX, event.offsetY);
    const nextId = node?.id || null;
    if (nextId === lastHoverId) return;
    lastHoverId = nextId;
    latestRef.current.onNodeHover?.(node || null);
  };
  const onPointerLeave = () => {
    lastHoverId = null;
    latestRef.current.onNodeHover?.(null);
  };
  const onCanvasClick = (event) => {
    if (Date.now() - lastNodeClickAt < 160) return;
    if (hitNode(event.offsetX, event.offsetY)) return;
    latestRef.current.onBackgroundClick?.();
  };
  canvas.addEventListener('pointermove', onPointerMove);
  canvas.addEventListener('pointerleave', onPointerLeave);
  canvas.addEventListener('click', onCanvasClick);

  // Self-driven render loop. app.render() is in a try/catch so a single bad
  // Pixi batcher frame is skipped (logged once) instead of throwing out of the
  // ticker and tearing down the whole app.
  let rafId = 0;
  let renderErrorLogged = false;
  const frame = () => {
    if (isDisposed()) return;
    draw();
    try {
      app.render();
    } catch (err) {
      if (!renderErrorLogged) {
        renderErrorLogged = true;
        console.warn('[Memory] Skipped a bad render frame', err);
      }
    }
    rafId = requestAnimationFrame(frame);
  };
  simulation.alpha(mode === 'local' ? 0.22 : 0.34).restart();
  rafId = requestAnimationFrame(frame);

  return () => {
    canvas.removeEventListener('pointermove', onPointerMove);
    canvas.removeEventListener('pointerleave', onPointerLeave);
    canvas.removeEventListener('click', onCanvasClick);
    try { selection.on('.zoom', null).on('.drag', null); } catch {}
    simulation.stop();
    if (rafId) cancelAnimationFrame(rafId);
    // Destroy children + textures explicitly: every label owns a canvas-backed
    // texture, and resize remounts rebuild the whole scene — without this the
    // old scene's textures linger until GC gets around to them.
    try { app.destroy(true, { children: true, texture: true, textureSource: true }); } catch {}
  };
}

export function normalizeRendererGraphData(graphData = {}, width = 0, height = 0) {
  const rawNodes = Array.isArray(graphData.nodes) ? graphData.nodes : [];
  const rawLinks = Array.isArray(graphData.links) ? graphData.links : [];
  const spread = Math.max(80, Math.min(Math.max(width, 1), Math.max(height, 1)) * 0.34);
  const nodes = rawNodes
    .filter((node) => node && node.id)
    .map((node, index) => {
      const seeded = seededGraphPosition(node.id, index, rawNodes.length || 1, spread);
      return {
        ...node,
        x: Number.isFinite(node.x) ? node.x : seeded.x,
        y: Number.isFinite(node.y) ? node.y : seeded.y,
      };
    });
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const links = rawLinks
    .map((link) => {
      const sourceId = typeof link.source === 'object' ? link.source.id : link.source;
      const targetId = typeof link.target === 'object' ? link.target.id : link.target;
      const source = byId.get(sourceId);
      const target = byId.get(targetId);
      if (!source || !target) return null;
      return { ...link, source, target, sourceId, targetId };
    })
    .filter(Boolean);
  return { nodes, links };
}

export function computeRendererFitTransform(nodes = [], width = 0, height = 0, opts = {}) {
  const finiteNodes = (Array.isArray(nodes) ? nodes : [])
    .filter((node) => Number.isFinite(node.x) && Number.isFinite(node.y));
  const padding = Number.isFinite(opts.padding) ? opts.padding : 64;
  const minScale = Number.isFinite(opts.minScale) ? opts.minScale : 0.35;
  const maxScale = Number.isFinite(opts.maxScale) ? opts.maxScale : 1.15;
  if (!finiteNodes.length || width <= 0 || height <= 0) {
    return { x: width / 2, y: height / 2, k: 1 };
  }
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of finiteNodes) {
    minX = Math.min(minX, node.x);
    minY = Math.min(minY, node.y);
    maxX = Math.max(maxX, node.x);
    maxY = Math.max(maxY, node.y);
  }
  const graphW = Math.max(1, maxX - minX);
  const graphH = Math.max(1, maxY - minY);
  const scale = clamp(
    Math.min((width - padding * 2) / graphW, (height - padding * 2) / graphH),
    minScale,
    maxScale,
  );
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  return {
    x: width / 2 - cx * scale,
    y: height / 2 - cy * scale,
    k: scale,
  };
}

function buildRendererNeighborMap(links = []) {
  const map = new Map();
  const add = (a, b) => {
    if (!map.has(a)) map.set(a, new Set());
    map.get(a).add(b);
  };
  for (const link of links) {
    const s = link.source?.id || link.sourceId || link.source;
    const t = link.target?.id || link.targetId || link.target;
    if (!s || !t) continue;
    add(s, t);
    add(t, s);
  }
  return map;
}

function buildLabelRankMap(nodes = []) {
  const ranked = [...nodes]
    .map((node) => ({ node, score: labelScore(node) }))
    .sort((a, b) => b.score - a.score);
  return new Map(ranked.map(({ node }, index) => [node.id, index]));
}

function seededGraphPosition(id, index, total, spread) {
  const h = hashStr(String(id));
  const angle = ((h % 3600) / 3600) * Math.PI * 2;
  const ring = 0.35 + ((hashStr(String(id) + ':r') % 1000) / 1000) * 0.65;
  const fallbackAngle = total > 0 ? (index / total) * Math.PI * 2 : angle;
  const a = Number.isFinite(angle) ? angle : fallbackAngle;
  return {
    x: Math.cos(a) * spread * ring,
    y: Math.sin(a) * spread * ring,
  };
}

function truncateGraphLabel(label) {
  const text = String(label || '');
  if (text.length <= 34) return text;
  return text.slice(0, 31).trimEnd() + '...';
}

function graphFontFamily() {
  try {
    return cssVar('--font', 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif');
  } catch {
    return 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
  }
}

function colorNumber(color, fallback) {
  const rgb = parseRGB(color) || parseRGB(fallback) || [138, 138, 147];
  return (rgb[0] << 16) + (rgb[1] << 8) + rgb[2];
}

// ----------------------------------------------------------------- helpers ---

export function loadScriptOnce(src) {
  if (typeof document === 'undefined') return Promise.reject(new Error('document is not available'));
  const existing = document.querySelector(`script[src="${src}"]`);
  if (existing?.dataset.loaded === 'true') return Promise.resolve();
  if (existing?.dataset.loading === 'true') {
    return new Promise((resolve, reject) => {
      existing.addEventListener('load', resolve, { once: true });
      existing.addEventListener('error', reject, { once: true });
    });
  }
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.async = true;
    script.crossOrigin = 'anonymous';
    script.dataset.loading = 'true';
    script.onload = () => {
      script.dataset.loading = 'false';
      script.dataset.loaded = 'true';
      resolve();
    };
    script.onerror = () => reject(new Error('Failed to load ' + src));
    document.head.appendChild(script);
  });
}
