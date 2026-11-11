// spacegraph/desktop/plugin.ts
import { host, PALETTE_AREA, queryClient, ROUTES_AREA, SIDEBAR_NAV_AREA, useQuery, useValue } from "@hermes/plugin-sdk";
import { useEffect, useMemo, useRef, useState } from "react";
import { jsx, jsxs } from "react/jsx-runtime";

// spacegraph/src/draw.ts
var EDGE_STYLES = {
  nbr: { colour: "#6c7086", width: 1.6 },
  call: { colour: "#fab387", width: 2.2 },
  cue: { colour: "#94e2d5", width: 1.8, dash: "6 4" },
  heritage: { colour: "#cba6f7", width: 1.6, dash: "3 3" },
  kid: { colour: "#45475a", width: 1 }
};
var DRAW_CSS = `
.sg-stage{position:relative;overflow:hidden;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;--lane-label-color:#9399b2}
.sg-stage>svg{position:absolute;left:0;top:0}
.sg-node{position:absolute;transform-origin:0 0;box-sizing:border-box}
.sg-card{box-sizing:border-box;width:100%;height:100%;background:#1e1e2e;border:1px solid #45475a;border-left:5px solid var(--lane);border-radius:8px;padding:6px 9px;color:#cdd6f4;overflow:hidden}
.sg-head{font-size:10.5px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:var(--lane);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sg-body{font-size:12px;line-height:1.3;margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sg-node.sg-dim{opacity:.25}
.sg-node.sg-hi .sg-card{box-shadow:0 0 0 3px #f9e2af,0 0 22px 6px rgba(249,226,175,.5)}
.sg-band{fill:rgba(137,180,250,.045);stroke:#45475a;stroke-dasharray:5 4}
.sg-band-label{fill:#9399b2;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.sg-edge-label{fill:#94e2d5;font-size:15px;font-weight:700;paint-order:stroke;stroke:#11111b;stroke-width:4px}
.sg-card.sg-big .sg-head{font-size:17px}
.sg-card.sg-big .sg-body{font-size:19px;-webkit-line-clamp:1;margin-top:3px}
`;
var esc = (text) => text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
function sizeOf(node) {
  if (node.size)
    return [node.size[0], node.size[1]];
  const w = Number(node.data?.w);
  const h = Number(node.data?.h);
  if (w > 0 && h > 0)
    return [w, h];
  return node.shape === "dot" ? [16, 16] : [200, 64];
}
function exitPoint(box, x, y) {
  const cx = box.x + box.w / 2;
  const cy = box.y + box.h / 2;
  const dx = x - cx;
  const dy = y - cy;
  if (dx === 0 && dy === 0)
    return [cx, cy];
  const t = Math.min(dx ? box.w / 2 / Math.abs(dx) : Infinity, dy ? box.h / 2 / Math.abs(dy) : Infinity);
  return [cx + dx * t, cy + dy * t];
}
var r1 = (n) => Math.round(n * 10) / 10;
function drawModel(model, options) {
  const { width, height } = options;
  const padding = options.padding ?? 24;
  const visible = model.nodes.filter((n) => n.visible !== false);
  const boxes = visible.map((n) => {
    const [w, h] = sizeOf(n);
    return { n, x0: n.position[0] - w / 2, y0: n.position[1] - h / 2, w, h };
  });
  const minX = options.viewport?.x ?? Math.min(...boxes.map((b) => b.x0));
  const minY = options.viewport?.y ?? Math.min(...boxes.map((b) => b.y0));
  const maxX = options.viewport ? options.viewport.x + options.viewport.w : Math.max(...boxes.map((b) => b.x0 + b.w));
  const maxY = options.viewport ? options.viewport.y + options.viewport.h : Math.max(...boxes.map((b) => b.y0 + b.h));
  const scale = Math.min((width - 2 * padding) / Math.max(1, maxX - minX), (height - 2 * padding) / Math.max(1, maxY - minY), options.maxScale ?? 1.5);
  const tx = (width - (maxX - minX) * scale) / 2 - minX * scale;
  const ty = (height - (maxY - minY) * scale) / 2 - minY * scale;
  const frame = new Map;
  for (const b of boxes)
    frame.set(b.n.id, { x: b.x0 * scale + tx, y: b.y0 * scale + ty, w: b.w * scale, h: b.h * scale });
  const lit = new Set(options.highlight ?? []);
  const svg = [
    `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`,
    '<defs><marker id="sg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">' + '<path d="M0 0L10 5L0 10z" fill="#fab387"/></marker></defs>'
  ];
  for (const b of boxes) {
    if (b.n.shape !== "group")
      continue;
    const f = frame.get(b.n.id);
    svg.push(`<rect class="sg-band" x="${r1(f.x)}" y="${r1(f.y)}" width="${r1(f.w)}" height="${r1(f.h)}" rx="${r1(14 * scale)}"/>`, `<text class="sg-band-label" x="${r1(f.x + 10 * scale)}" y="${r1(f.y + 19 * scale)}" style="font-size:${r1(12 * scale)}px">${esc(b.n.label)}</text>`);
  }
  const drawEdge = (e) => {
    const a = frame.get(e.source);
    const b = frame.get(e.target);
    if (!a || !b)
      return;
    const style = EDGE_STYLES[e.kind] ?? EDGE_STYLES.nbr;
    const [x1, y1] = exitPoint(a, b.x + b.w / 2, b.y + b.h / 2);
    const [x2, y2] = exitPoint(b, a.x + a.w / 2, a.y + a.h / 2);
    const dash = style.dash ? ` stroke-dasharray="${style.dash}"` : "";
    const arrow = e.directed ? ' marker-end="url(#sg-arrow)"' : "";
    const fade = options.dim && lit.size && !(lit.has(e.source) || lit.has(e.target)) ? ' stroke-opacity="0.25"' : "";
    svg.push(`<line x1="${r1(x1)}" y1="${r1(y1)}" x2="${r1(x2)}" y2="${r1(y2)}" stroke="${style.colour}" stroke-width="${r1(style.width * Math.max(0.8, scale))}"${dash}${arrow}${fade}/>`);
    if (e.label)
      svg.push(`<text class="sg-edge-label" x="${r1((x1 + x2) / 2 + 6)}" y="${r1((y1 + y2) / 2 - 6)}">${esc(e.label)}</text>`);
  };
  model.edges.filter((e) => e.visible !== false).forEach(drawEdge);
  svg.push("</svg>");
  const cards = [];
  for (const b of boxes) {
    if (b.n.shape === "group")
      continue;
    const f = frame.get(b.n.id);
    const cls = ["sg-node"];
    if (lit.has(b.n.id))
      cls.push("sg-hi");
    else if (options.dim && lit.size && b.n.kind !== "lane")
      cls.push("sg-dim");
    const body = b.n.html ?? `<div class="sg-card" style="--lane:#9399b2"><div class="sg-body">${esc(b.n.label)}</div></div>`;
    cards.push(`<div class="${cls.join(" ")}" data-id="${esc(b.n.id)}" style="left:${r1(f.x)}px;top:${r1(f.y)}px;width:${b.w}px;height:${b.h}px;transform:scale(${scale.toFixed(4)})">${body}</div>`);
  }
  return {
    html: `<div class="sg-stage" style="width:${width}px;height:${height}px">${svg.join("")}${cards.join("")}</div>`,
    scale,
    box: (id) => frame.get(id)
  };
}

// spacegraph/spacegraphjs/packages/adapter/src/diff.ts
function fingerprint(node) {
  return [
    node.kind,
    node.label,
    node.summary ?? "",
    node.status,
    node.shape,
    node.position.join(","),
    node.size?.join(",") ?? "",
    node.html ?? ""
  ].join("^@");
}
function edgeFingerprint(edge) {
  return [
    edge.source,
    edge.target,
    edge.kind,
    edge.label ?? "",
    edge.confidence ?? "",
    edge.visible?.toString() ?? "",
    edge.directed?.toString() ?? "",
    edge.style ?? ""
  ].join("^@");
}
function diffRenderModels(previous, next) {
  const before = new Map(previous.nodes.map((node) => [node.id, fingerprint(node)]));
  const after = new Map(next.nodes.map((node) => [node.id, fingerprint(node)]));
  const addedNodes = [];
  const changedNodes = [];
  for (const node of next.nodes) {
    const beforeFingerprint = before.get(node.id);
    if (beforeFingerprint === undefined)
      addedNodes.push(node);
    else if (beforeFingerprint !== fingerprint(node))
      changedNodes.push(node);
  }
  const removedNodeIds = [];
  for (const id of before.keys())
    if (!after.has(id))
      removedNodeIds.push(id);
  const beforeEdges = new Map(previous.edges.map((edge) => [edge.id, edgeFingerprint(edge)]));
  const afterEdges = new Map(next.edges.map((edge) => [edge.id, edgeFingerprint(edge)]));
  const addedEdges = [];
  const changedEdges = [];
  for (const edge of next.edges) {
    const beforeFingerprint = beforeEdges.get(edge.id);
    if (beforeFingerprint === undefined)
      addedEdges.push(edge);
    else if (beforeFingerprint !== edgeFingerprint(edge))
      changedEdges.push(edge);
  }
  const removedEdgeIds = [];
  for (const id of beforeEdges.keys())
    if (!afterEdges.has(id))
      removedEdgeIds.push(id);
  return {
    nodes: {
      added: addedNodes,
      removed: removedNodeIds,
      changed: changedNodes
    },
    edges: {
      added: addedEdges,
      removed: removedEdgeIds,
      changed: changedEdges
    }
  };
}

// spacegraph/spacegraphjs/packages/adapter/src/html.ts
var HTML_ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;"
};
function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (char) => HTML_ESCAPES[char]);
}
function formatOpacity(value) {
  return String(value).replace(/^0\./, ".");
}

// spacegraph/spacegraphjs/packages/adapter/src/ModelAdapter.ts
var ZERO = { x: 0, y: 0, z: 0 };
function paddingOf(padding) {
  if (typeof padding === "number")
    return { top: padding, right: padding, bottom: padding, left: padding };
  return {
    top: padding.top ?? 0,
    right: padding.right ?? 0,
    bottom: padding.bottom ?? 0,
    left: padding.left ?? 0
  };
}
var DEFAULT_GEOMETRY = {
  chipSize: [132, 44, 0],
  panelSize: [132, 44, 0],
  dimOpacity: 0.3,
  bandPadding: 24,
  laneLabelSize: [132, 28, 0],
  laneGutter: 20,
  axisLabelSize: [132, 24, 0]
};

class ModelAdapter {
  config;
  geometry;
  #indexes = new WeakMap;
  constructor(config = {}) {
    this.config = config;
    this.geometry = { ...DEFAULT_GEOMETRY, ...config.geometry };
  }
  buildBands(_model, _level, _positions, _renderBand, _visible) {
    return [];
  }
  bundleEdges(_model, edges) {
    return [...edges];
  }
  defaultCardTemplate(node, _level, context) {
    const badge = context.count ? ` <span style="opacity:.6">${context.count}</span>` : "";
    const summary = node.summary ? `<span style="opacity:.7">${escapeHtml(node.summary)}</span>` : "";
    return `<div data-kind="${node.kind}" data-status="${node.status}" style="display:flex;flex-direction:column;gap:2px;padding:8px 10px;color:inherit;font-family:inherit"><span style="font-weight:600">${escapeHtml(node.label)}${badge}</span>${summary}</div>`;
  }
  defaultBandTemplate(node, count, bounds) {
    const header = escapeHtml(node.label);
    const width = Math.round(bounds.max.x - bounds.min.x);
    const height = Math.round(bounds.max.y - bounds.min.y);
    return `<div data-band="${node.kind}" style="width:${width}px;height:${height}px;box-sizing:border-box;border:1px dashed currentColor;border-radius:12px;opacity:.28;pointer-events:none;position:relative;font-family:inherit"><span style="position:absolute;bottom:2px;left:8px;font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase">${header} · ${count}</span></div>`;
  }
  build(model, options) {
    const level = options.level;
    const budget = options.cardBudget ?? this.resolveBudget(level);
    const renderCard = options.renderCard ?? this.config.renderCardTemplate ?? this.defaultCardTemplate;
    const renderBand = options.renderBand ?? this.config.renderBandTemplate ?? this.defaultBandTemplate;
    const visibleNodes = this.config.nodeFilter ? this.config.nodeFilter(model, level) : model.nodes;
    const visible = new Set(visibleNodes.map((node) => node.id));
    const candidates = model.edges.filter((edge) => visible.has(edge.source) && visible.has(edge.target));
    const rawEdges = options.edges ?? true ? this.config.edgeFilter ? this.config.edgeFilter(model, level, options.focus, candidates, visible) : candidates : [];
    const lit = options.focus ? new Set([options.focus, ...this.config.neighbors?.(model, options.focus) ?? []]) : undefined;
    let cards = 0;
    const content = visibleNodes.map((node) => {
      const position = options.positions.get(node.id) ?? ZERO;
      const shape = this.nodeShape(node);
      const chip = this.isClusterChip(node, level);
      const structural = this.isStructuralKind(node.kind);
      const asCard = (shape === "card" || chip || structural) && (structural || cards < budget);
      const dimmed = lit ? !lit.has(node.id) : false;
      if (asCard && !structural)
        cards += 1;
      const data = { ...this.nodeData(node, level, dimmed), dimmed };
      if (!asCard)
        data.opacity = dimmed ? this.geometry.dimOpacity : 1;
      return {
        id: node.id,
        kind: node.kind,
        label: node.label,
        summary: node.summary,
        status: this.nodeStatus(node),
        shape: asCard ? "card" : shape === "card" ? "dot" : shape,
        html: asCard ? this.wrapDim(renderCard(node, level, {
          count: chip ? node.clusterOf?.length ?? 0 : 0,
          dimmed
        }), dimmed) : undefined,
        size: chip ? this.geometry.chipSize : structural ? this.geometry.panelSize : undefined,
        position: [position.x, position.y, position.z],
        data
      };
    });
    const bands = options.bands ?? this.config.bands ?? false ? this.buildBands(model, level, options.positions, renderBand, visible) : [];
    const lanes = this.buildLaneLabels(options.laneLabels ?? this.config.laneLabels, visibleNodes, options.positions);
    const axes = (options.axisLabels ?? this.config.axisLabels ?? []).map((label) => this.buildAxisLabel(label));
    const edges = options.bundle ?? this.config.bundleEdges ?? false ? this.bundleEdges(model, rawEdges) : rawEdges;
    const groupsByMember = new Map;
    const areaOf = (group) => (group.size?.[0] ?? 0) * (group.size?.[1] ?? 0);
    for (const group of bands) {
      if (group.shape !== "group")
        continue;
      const members = group.data?.members;
      if (!Array.isArray(members))
        continue;
      for (const id of members) {
        if (typeof id !== "string" || id === group.id)
          continue;
        const current = groupsByMember.get(id);
        if (!current || areaOf(group) < areaOf(current))
          groupsByMember.set(id, group);
      }
    }
    const withParent = (node) => {
      const key = node.kind === "band" ? node.data?.cluster ?? node.id : node.id;
      const parent = groupsByMember.get(key);
      return parent && parent.id !== node.id ? { ...node, parent: parent.id } : node;
    };
    return {
      nodes: [...bands.map(withParent), ...lanes, ...axes, ...content.map(withParent)],
      edges: edges.map((edge) => this.toRenderEdge(edge, level))
    };
  }
  buildPatch(previous, model, options) {
    const next = this.build(model, options);
    return diffRenderModels(previous, next);
  }
  resolveBudget(level) {
    const budget = this.config.cardBudget;
    if (typeof budget === "function")
      return budget(level);
    return budget ?? 0;
  }
  bandBox(model, ids, positions, visible) {
    const byID = this.nodeIndex(model);
    const boxes = ids.flatMap((id) => {
      if (!visible.has(id))
        return [];
      const position = positions.get(id);
      if (!position)
        return [];
      const node = byID.get(id);
      const size = node ? this.config.nodeSizeFn?.(node) : undefined;
      const halfWidth = (size?.[0] ?? 0) / 2;
      const halfHeight = (size?.[1] ?? 0) / 2;
      return [
        {
          min: { x: position.x - halfWidth, y: position.y - halfHeight, z: 0 },
          max: { x: position.x + halfWidth, y: position.y + halfHeight, z: 0 }
        }
      ];
    });
    if (boxes.length === 0)
      return;
    const padding = paddingOf(this.geometry.bandPadding);
    return {
      min: {
        x: Math.min(...boxes.map((box) => box.min.x)) - padding.left,
        y: Math.min(...boxes.map((box) => box.min.y)) - padding.bottom,
        z: 0
      },
      max: {
        x: Math.max(...boxes.map((box) => box.max.x)) + padding.right,
        y: Math.max(...boxes.map((box) => box.max.y)) + padding.top,
        z: 0
      }
    };
  }
  nodeIndex(model) {
    const cached = this.#indexes.get(model);
    if (cached)
      return cached;
    const index = new Map(model.nodes.map((node) => [node.id, node]));
    this.#indexes.set(model, index);
    return index;
  }
  isClusterChip(node, level) {
    if (node.derived !== true)
      return false;
    if ((node.clusterOf?.length ?? 0) === 0)
      return false;
    return !(this.config.isClusterExpanded?.(level) ?? true);
  }
  buildLaneLabels(labels, nodes, positions) {
    if (!labels || labels.length === 0 || nodes.length === 0)
      return [];
    const byID = new Map(nodes.map((node) => [node.id, node]));
    const leftOf = (node) => {
      const position = positions.get(node.id);
      const width = this.config.nodeSizeFn?.(node)?.[0] ?? 0;
      return (position?.x ?? 0) - width / 2;
    };
    const left = Math.min(...nodes.map(leftOf));
    return labels.map((lane) => {
      const anchor = lane.anchorNodeId ? byID.get(lane.anchorNodeId) : undefined;
      return {
        id: `lane:${lane.id}`,
        kind: "lane",
        label: lane.label,
        shape: "panel",
        interactive: false,
        html: `<div data-lane="${escapeHtml(lane.id)}" style="display:flex;align-items:center;height:100%;box-sizing:border-box;padding:0 10px;font-family:inherit;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--lane-label-color);opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml(lane.label)}</div>`,
        size: this.geometry.laneLabelSize,
        position: [
          (anchor ? leftOf(anchor) : left) - this.geometry.laneGutter - this.geometry.laneLabelSize[0] / 2,
          lane.y,
          ZERO.z - 1
        ],
        data: { lane: lane.id, colorToken: "textMuted" }
      };
    });
  }
  buildAxisLabel(label) {
    return {
      id: `axis:${label.id}`,
      kind: "lane",
      label: label.label,
      shape: "panel",
      interactive: false,
      html: `<div data-axis="${label.role}" style="display:flex;align-items:center;height:100%;box-sizing:border-box;padding:0 8px;font-family:inherit;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:inherit;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml(label.label)}</div>`,
      size: this.geometry.axisLabelSize,
      position: [label.x, label.y, ZERO.z - 1],
      data: { axis: label.role }
    };
  }
  wrapDim(html, dimmed) {
    return dimmed ? `<div style="width:100%;height:100%;opacity:${formatOpacity(this.geometry.dimOpacity)}">${html}</div>` : html;
  }
}
// spacegraph/spacegraphjs/src/layout/LaneLayout.ts
var DEFAULT_GROUP = "__default__";

class LaneLayout {
  #lanes;
  #lanePitch;
  #step;
  #groupGap;
  #depthStep;
  #defaultLane;
  #positions = {};
  #columnBase = {};
  #columnSpan = {};
  #cellCursor = {};
  #columnExtent = {};
  #nextColumnX = 0;
  constructor(config) {
    this.#lanes = config.lanes ?? [];
    this.#lanePitch = config.lanePitch ?? 56;
    this.#step = config.step ?? 48;
    this.#groupGap = config.groupGap ?? 320;
    this.#depthStep = config.depthStep ?? 64;
    this.#defaultLane = config.defaultLane ?? this.#lanes[0] ?? DEFAULT_GROUP;
  }
  get positions() {
    return this.#positions;
  }
  get lanes() {
    return this.#lanes;
  }
  place(item) {
    const placed = this.#positions[item.id];
    if (placed)
      return placed;
    const group = item.group ?? DEFAULT_GROUP;
    const base = this.#baseOf(group);
    const width = item.width ?? 0;
    const indent = item.indent ?? 0;
    const row = this.#rowOf(item);
    const cell = `${group} ${row}`;
    const cursor = this.#cellCursor[cell] ?? base;
    const position = [
      cursor + indent + width / 2,
      row * this.#lanePitch,
      (item.depth ?? 0) * this.#depthStep
    ];
    this.#positions[item.id] = position;
    this.#cellCursor[cell] = cursor + width + this.#step;
    this.#columnExtent[group] = Math.max(this.#columnExtent[group] ?? 0, cursor + indent + width - base);
    return position;
  }
  placeAll(items) {
    for (const item of items)
      this.place(item);
    return this.#positions;
  }
  open(group, span) {
    this.#baseOf(group);
    if (span !== undefined)
      this.#columnSpan[group] = span;
  }
  close(group, span) {
    const base = this.#baseOf(group);
    const width = span ?? this.#columnSpan[group] ?? this.#columnExtent[group] ?? 0;
    this.#columnSpan[group] = width;
    this.#nextColumnX = base + width + this.#groupGap;
  }
  width(group) {
    return this.#columnSpan[group] ?? this.#columnExtent[group] ?? 0;
  }
  bounds(ids) {
    const points = (ids ?? Object.keys(this.#positions)).map((id) => this.#positions[id]).filter((p) => p !== undefined);
    if (!points.length)
      return;
    const min = [Infinity, Infinity, Infinity];
    const max = [-Infinity, -Infinity, -Infinity];
    for (const point of points) {
      for (let i = 0;i < 3; i++) {
        min[i] = Math.min(min[i], point[i]);
        max[i] = Math.max(max[i], point[i]);
      }
    }
    const center = [0, 1, 2].map((i) => (min[i] + max[i]) / 2);
    const size = [0, 1, 2].map((i) => max[i] - min[i]);
    return { min, max, center, size };
  }
  clear() {
    const records = [
      this.#positions,
      this.#columnBase,
      this.#columnSpan,
      this.#cellCursor,
      this.#columnExtent
    ];
    for (const record of records) {
      for (const key of Object.keys(record))
        delete record[key];
    }
    this.#nextColumnX = 0;
  }
  #rowOf(item) {
    if (item.row !== undefined)
      return item.row;
    const index = this.#lanes.indexOf(item.lane ?? this.#defaultLane);
    if (index >= 0)
      return index;
    return Math.max(this.#lanes.indexOf(this.#defaultLane), 0);
  }
  #baseOf(group) {
    const base = this.#columnBase[group];
    if (base !== undefined)
      return base;
    this.#columnBase[group] = this.#nextColumnX;
    return this.#nextColumnX;
  }
}
// spacegraph/spacegraphjs/src/layout/grid.ts
class GridPlacement {
  name = "grid";
  #layout;
  #minColumnWidth;
  #declared = new Set;
  constructor(config) {
    this.#layout = new LaneLayout(config);
    this.#minColumnWidth = config.minColumnWidth;
  }
  place(items) {
    let group;
    for (const item of items) {
      const next = item.meta?.group;
      if (next !== group) {
        this.#close(group);
        group = next;
        if (group !== undefined) {
          const declared = item.meta?.columnWidth;
          if (declared !== undefined)
            this.#declared.add(group);
          this.#layout.open(group, declared);
        }
      }
      if (item.meta?.empty)
        continue;
      this.#layout.place({
        id: item.id,
        lane: item.meta?.lane,
        row: item.meta?.row,
        group,
        width: item.width,
        indent: item.meta?.indent,
        depth: item.depth
      });
    }
    this.#close(group);
    return new Map(Object.entries(this.#layout.positions).map(([id, [x, y, z]]) => [id, { x, y, z }]));
  }
  #close(group) {
    if (group === undefined)
      return;
    const span = this.#declared.has(group) ? undefined : Math.max(this.#layout.width(group), this.#minColumnWidth ?? 0);
    this.#layout.close(group, span);
  }
}
// spacegraph/spacegraphjs/src/layout/tree.ts
var DEFAULT_COLUMN_PITCH = 260;
var DEFAULT_ROW_PITCH = 96;
var DEFAULT_DEPTH_STEP = 64;
var DEFAULT_COLUMNS = 4;
var DEFAULT_CELL_GAP = 24;

class TreePlacement {
  name = "tree";
  #columnPitch;
  #rowPitch;
  #depthStep;
  #columns;
  #cellGap;
  constructor(config = {}) {
    this.#columnPitch = config.columnPitch ?? DEFAULT_COLUMN_PITCH;
    this.#rowPitch = config.rowPitch ?? DEFAULT_ROW_PITCH;
    this.#depthStep = config.depthStep ?? DEFAULT_DEPTH_STEP;
    this.#columns = Math.max(1, config.columns ?? DEFAULT_COLUMNS);
    this.#cellGap = config.cellGap ?? DEFAULT_CELL_GAP;
  }
  place(items, previous) {
    const byID = new Map(items.map((item) => [item.id, item]));
    const children = new Map;
    for (const item of items) {
      const parent = item.meta?.parent;
      if (!parent || !byID.has(parent))
        continue;
      const siblings = children.get(parent);
      if (siblings)
        siblings.push(item.id);
      else
        children.set(parent, [item.id]);
    }
    const blocks = new Map;
    const cells = new Map;
    const depths = new Map(items.map((item) => [item.id, item.depth ?? 0]));
    for (const item of items)
      this.#measure(item.id, children, blocks, cells, byID);
    const positions = new Map;
    const roots = items.filter((item) => {
      const parent = item.meta?.parent;
      return !parent || !byID.has(parent);
    });
    let cursor = 0;
    for (const root of roots) {
      const block = blocks.get(root.id);
      const centre = cursor + block.width / 2;
      this.#place(root.id, centre, 0, children, blocks, cells, depths, positions, previous);
      cursor += block.width;
    }
    return positions;
  }
  #measure(id, children, blocks, cells, byID) {
    const kids = children.get(id) ?? [];
    if (kids.length === 0) {
      const item = byID.get(id);
      const itemWidth = item?.width ?? this.#columnPitch;
      const leafWidth = Math.max(this.#columnPitch, itemWidth + this.#cellGap);
      blocks.set(id, { width: leafWidth, rows: 1, firstRow: 0, rows2: [] });
      return;
    }
    for (const kid of kids)
      this.#measure(kid, children, blocks, cells, byID);
    const widths = kids.map((kid) => blocks.get(kid).width);
    const maxKidWidth = Math.max(...widths);
    const cellWidth = Math.max(this.#columnPitch, maxKidWidth + this.#cellGap);
    const perRow = Math.min(this.#columns, kids.length);
    const gridRows = [];
    let height = 0;
    kids.forEach((kid, index) => {
      const row = Math.floor(index / perRow);
      const block = blocks.get(kid);
      if (!gridRows[row])
        gridRows[row] = { offset: height, height: block.rows };
      else
        gridRows[row].height = Math.max(gridRows[row].height, block.rows);
      cells.set(kid, { row, column: index % perRow, width: cellWidth });
      height = gridRows[row].offset + gridRows[row].height;
    });
    blocks.set(id, {
      width: Math.max(perRow * cellWidth, this.#columnPitch),
      rows: 1 + height,
      firstRow: 1,
      rows2: gridRows
    });
  }
  #place(id, centre, y, children, blocks, cells, depths, positions, previous) {
    const frozen = previous?.get(id);
    const block = blocks.get(id);
    const x = frozen ? frozen.x : centre;
    const row = frozen ? -frozen.y / this.#rowPitch : y;
    positions.set(id, {
      x,
      y: -row * this.#rowPitch || 0,
      z: (depths.get(id) ?? 0) * this.#depthStep
    });
    const kids = children.get(id) ?? [];
    if (kids.length === 0)
      return;
    const start = x - block.width / 2;
    for (const kid of kids) {
      const cell = cells.get(kid);
      const row2 = block.rows2[cell.row];
      this.#place(kid, start + cell.column * cell.width + cell.width / 2, y + row2.offset + 1, children, blocks, cells, depths, positions, previous);
    }
  }
}
// spacegraph/spacegraphjs/src/layout/octree.ts
var DEFAULT_BH_CONFIG = {
  theta: 0.5,
  maxDepth: 20,
  minCellSize: 1
};
function buildOctree(nodes, getMass, config = {}) {
  const { theta, maxDepth, minCellSize } = { ...DEFAULT_BH_CONFIG, ...config };
  let minX = Infinity, minY = Infinity, minZ = Infinity;
  let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
  for (const node of nodes) {
    const { x, y, z } = node.position;
    if (x < minX)
      minX = x;
    if (y < minY)
      minY = y;
    if (z < minZ)
      minZ = z;
    if (x > maxX)
      maxX = x;
    if (y > maxY)
      maxY = y;
    if (z > maxZ)
      maxZ = z;
  }
  const padding = Math.max(maxX - minX, maxY - minY, maxZ - minZ) * 0.1 + 10;
  minX -= padding;
  minY -= padding;
  minZ -= padding;
  maxX += padding;
  maxY += padding;
  maxZ += padding;
  const size = Math.max(maxX - minX, maxY - minY, maxZ - minZ);
  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  const centerZ = (minZ + maxZ) / 2;
  minX = centerX - size / 2;
  minY = centerY - size / 2;
  minZ = centerZ - size / 2;
  maxX = centerX + size / 2;
  maxY = centerY + size / 2;
  maxZ = centerZ + size / 2;
  const root = {
    minX,
    minY,
    minZ,
    maxX,
    maxY,
    maxZ,
    centerX,
    centerY,
    centerZ,
    halfSize: size / 2,
    mass: 0,
    comX: 0,
    comY: 0,
    comZ: 0,
    children: null,
    leaf: null
  };
  for (const node of nodes) {
    insertIntoOctree(root, node, getMass(node), node.id ?? "", 0, maxDepth, minCellSize);
  }
  return root;
}
function insertIntoOctree(cell, node, mass, id, depth, maxDepth, minCellSize) {
  const { x, y, z } = node.position;
  const newMass = cell.mass + mass;
  cell.comX = (cell.comX * cell.mass + x * mass) / newMass;
  cell.comY = (cell.comY * cell.mass + y * mass) / newMass;
  cell.comZ = (cell.comZ * cell.mass + z * mass) / newMass;
  cell.mass = newMass;
  if (!cell.children && cell.leaf === null) {
    cell.leaf = { x, y, z, mass, id };
    return;
  }
  if (!cell.children && cell.leaf) {
    if (depth >= maxDepth || cell.halfSize <= minCellSize) {
      const existing2 = cell.leaf;
      cell.leaf = null;
      return;
    }
    subdivide(cell, depth);
    const existing = cell.leaf;
    insertIntoOctree(cell, { position: { x: existing.x, y: existing.y, z: existing.z } }, existing.mass, existing.id, depth + 1, maxDepth, minCellSize);
    cell.leaf = null;
  }
  if (cell.children) {
    const childIndex = getChildIndex(cell, x, y, z);
    insertIntoOctree(cell.children[childIndex], node, mass, id, depth + 1, maxDepth, minCellSize);
  }
}
function subdivide(cell, depth) {
  const h = cell.halfSize / 2;
  const cx = cell.centerX;
  const cy = cell.centerY;
  const cz = cell.centerZ;
  cell.children = [];
  for (let i = 0;i < 8; i++) {
    const dx = i & 1 ? h : -h;
    const dy = i & 2 ? h : -h;
    const dz = i & 4 ? h : -h;
    const child = {
      minX: cx + dx - h,
      maxX: cx + dx + h,
      minY: cy + dy - h,
      maxY: cy + dy + h,
      minZ: cz + dz - h,
      maxZ: cz + dz + h,
      centerX: cx + dx,
      centerY: cy + dy,
      centerZ: cz + dz,
      halfSize: h,
      mass: 0,
      comX: 0,
      comY: 0,
      comZ: 0,
      children: null,
      leaf: null
    };
    cell.children.push(child);
  }
}
function getChildIndex(cell, x, y, z) {
  let index = 0;
  if (x > cell.centerX)
    index |= 1;
  if (y > cell.centerY)
    index |= 2;
  if (z > cell.centerZ)
    index |= 4;
  return index;
}
function computeBarnesHutForce(node, octree, theta, repulsion, minDistance, maxForce) {
  let fx = 0, fy = 0, fz = 0;
  const stack = [octree];
  while (stack.length > 0) {
    const cell = stack.pop();
    if (cell.mass === 0)
      continue;
    if (cell.leaf) {
      const leaf = cell.leaf;
      if (leaf.id === node.id)
        continue;
      const dx2 = node.position.x - leaf.x;
      const dy2 = node.position.y - leaf.y;
      const dz2 = node.position.z - leaf.z;
      const distSq2 = dx2 * dx2 + dy2 * dy2 + dz2 * dz2;
      const dist2 = Math.sqrt(distSq2) || 0.001;
      const effectiveDistSq = Math.max(distSq2, minDistance * minDistance);
      const force = Math.min(repulsion / effectiveDistSq, maxForce);
      fx += dx2 / dist2 * force;
      fy += dy2 / dist2 * force;
      fz += dz2 / dist2 * force;
      continue;
    }
    const dx = node.position.x - cell.comX;
    const dy = node.position.y - cell.comY;
    const dz = node.position.z - cell.comZ;
    const distSq = dx * dx + dy * dy + dz * dz;
    const dist = Math.sqrt(distSq) || 0.001;
    const cellSize = cell.halfSize * 2;
    if (cellSize / dist < theta) {
      const effectiveDistSq = Math.max(distSq, minDistance * minDistance);
      const force = Math.min(repulsion * cell.mass / effectiveDistSq, maxForce);
      fx += dx / dist * force;
      fy += dy / dist * force;
      fz += dz / dist * force;
    } else {
      if (cell.children) {
        for (const child of cell.children) {
          if (child.mass > 0)
            stack.push(child);
        }
      }
    }
  }
  return { x: fx, y: fy, z: fz };
}

// spacegraph/spacegraphjs/src/layout/force-core.ts
var DEFAULT_SPACING = 240;
var DEFAULT_ITERATIONS = 300;
var DEFAULT_DEPTH_STEP2 = 64;
var DEFAULT_NODE_PADDING = 20;
var DEFAULT_ATTRACTION = 0.01;
var DEFAULT_REPULSION = 1e4;
var DEFAULT_DAMPING = 0.9;
var DEFAULT_GRAVITY = 0.1;
var DEFAULT_MIN_DISTANCE = 50;
var DEFAULT_MAX_DISTANCE = 1000;
var DEFAULT_TEMPERATURE = 100;
var GOLDEN_ANGLE = 2.399963229728653;

class ForceCore {
  #spacing;
  #iterations;
  #seed;
  #depthStep;
  #nodePadding;
  #attraction;
  #repulsion;
  #damping;
  #gravity;
  #minDistance;
  #maxDistance;
  #temperature;
  #randomize;
  #onProgress;
  #useBarnesHut;
  #barnesHutTheta;
  #barnesHutMaxDepth;
  #barnesHutMinCellSize;
  constructor(config = {}) {
    this.#spacing = config.spacing ?? DEFAULT_SPACING;
    this.#iterations = config.iterations ?? DEFAULT_ITERATIONS;
    this.#seed = config.seed ?? 1;
    this.#depthStep = config.depthStep ?? DEFAULT_DEPTH_STEP2;
    this.#nodePadding = config.nodePadding ?? DEFAULT_NODE_PADDING;
    this.#attraction = config.attraction ?? DEFAULT_ATTRACTION;
    this.#repulsion = config.repulsion ?? DEFAULT_REPULSION;
    this.#damping = config.damping ?? DEFAULT_DAMPING;
    this.#gravity = config.gravity ?? DEFAULT_GRAVITY;
    this.#minDistance = config.minDistance ?? DEFAULT_MIN_DISTANCE;
    this.#maxDistance = config.maxDistance ?? DEFAULT_MAX_DISTANCE;
    this.#temperature = config.temperature ?? DEFAULT_TEMPERATURE;
    this.#randomize = config.randomize ?? false;
    this.#onProgress = config.onProgress;
    this.#useBarnesHut = config.useBarnesHut ?? (config.iterations && config.iterations > 1000);
    this.#barnesHutTheta = config.barnesHut?.theta ?? 0.5;
    this.#barnesHutMaxDepth = config.barnesHut?.maxDepth ?? 20;
    this.#barnesHutMinCellSize = config.barnesHut?.minCellSize ?? 1;
  }
  getNodeRadius(item, padding) {
    const effectivePadding = padding ?? this.#nodePadding;
    let width, height;
    if ("getLayoutBounds" in item) {
      const bounds = item.getLayoutBounds();
      width = bounds.width;
      height = bounds.height;
    } else {
      width = item.width;
      height = item.height ?? item.width;
    }
    const diagonal = Math.hypot(width, height);
    return diagonal / 2 + effectivePadding;
  }
  step(nodes, getLinks, getRadius, isPinned) {
    if (!nodes.length)
      return new Map;
    const forces = new Map;
    for (const node of nodes)
      forces.set(node.id ?? String(nodes.indexOf(node)), { x: 0, y: 0, z: 0 });
    const radii = new Map;
    for (const node of nodes) {
      const id = node.id ?? String(nodes.indexOf(node));
      radii.set(id, getRadius(node));
    }
    const maxForce = this.#repulsion / (this.#minDistance * this.#minDistance);
    if (this.#useBarnesHut && nodes.length > 100) {
      const octree = buildOctree(nodes, (node) => radii.get(node.id ?? "") ?? 1, {
        theta: this.#barnesHutTheta,
        maxDepth: this.#barnesHutMaxDepth,
        minCellSize: this.#barnesHutMinCellSize
      });
      for (const node of nodes) {
        const id = node.id ?? String(nodes.indexOf(node));
        const force = computeBarnesHutForce({ position: node.position, id }, octree, this.#barnesHutTheta, this.#repulsion, this.#minDistance, maxForce);
        const f = forces.get(id);
        f.x += force.x;
        f.y += force.y;
        f.z += force.z;
      }
    } else {
      for (let i = 0;i < nodes.length; i++) {
        for (let j = i + 1;j < nodes.length; j++) {
          const a = nodes[i];
          const b = nodes[j];
          const dx = a.position.x - b.position.x;
          const dy = a.position.y - b.position.y;
          const dz = a.position.z - b.position.z;
          const distSq = dx * dx + dy * dy + dz * dz;
          const dist = Math.sqrt(distSq) || 0.001;
          const idA = a.id ?? String(i);
          const idB = b.id ?? String(j);
          const minDist = (radii.get(idA) ?? 0) + (radii.get(idB) ?? 0);
          const effectiveDistSq = Math.max(distSq, minDist * minDist);
          const repulsion = this.#repulsion / effectiveDistSq;
          const force = Math.min(repulsion, maxForce);
          const fx = dx / dist * force;
          const fy = dy / dist * force;
          const fz = dz / dist * force;
          const forceA = forces.get(idA);
          const forceB = forces.get(idB);
          forceA.x += fx;
          forceA.y += fy;
          forceA.z += fz;
          forceB.x -= fx;
          forceB.y -= fy;
          forceB.z -= fz;
        }
      }
    }
    for (const node of nodes) {
      const id = node.id ?? String(nodes.indexOf(node));
      const force = forces.get(id);
      for (const link of getLinks(node)) {
        const target = nodes.find((n) => (n.id ?? String(nodes.indexOf(n))) === link);
        if (!target)
          continue;
        const dx = target.position.x - node.position.x;
        const dy = target.position.y - node.position.y;
        const dz = target.position.z - node.position.z;
        const dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 0.001;
        const pull = this.#attraction * dist;
        const fx = dx / dist * pull;
        const fy = dy / dist * pull;
        const fz = dz / dist * pull;
        force.x += fx;
        force.y += fy;
        force.z += fz;
      }
    }
    for (const node of nodes) {
      const id = node.id ?? String(nodes.indexOf(node));
      const force = forces.get(id);
      const dist = Math.sqrt(node.position.x ** 2 + node.position.y ** 2 + node.position.z ** 2);
      if (dist > 0) {
        const gx = -node.position.x / dist * this.#gravity * dist;
        const gy = -node.position.y / dist * this.#gravity * dist;
        const gz = -node.position.z / dist * this.#gravity * dist;
        force.x += gx;
        force.y += gy;
        force.z += gz;
      }
    }
    return forces;
  }
  separateOverlaps(nodes, getRadius, isPinned) {
    const radii = new Map;
    for (const node of nodes) {
      const id = node.id ?? String(nodes.indexOf(node));
      radii.set(id, getRadius(node));
    }
    for (let pass = 0;pass < 30; pass++) {
      let anyOverlap = false;
      for (let i = 0;i < nodes.length; i++) {
        for (let j = i + 1;j < nodes.length; j++) {
          const a = nodes[i];
          const b = nodes[j];
          if (isPinned(a) && isPinned(b))
            continue;
          const dx = a.position.x - b.position.x;
          const dy = a.position.y - b.position.y;
          const dz = a.position.z - b.position.z;
          const dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 0.001;
          const idA = a.id ?? String(i);
          const idB = b.id ?? String(j);
          const needed = (radii.get(idA) ?? 0) + (radii.get(idB) ?? 0);
          if (dist < needed) {
            anyOverlap = true;
            const shove = (needed - dist) / 2;
            const fx = dx / dist * shove;
            const fy = dy / dist * shove;
            const fz = dz / dist * shove;
            if (!isPinned(a)) {
              a.position.x += fx;
              a.position.y += fy;
              a.position.z += fz;
            }
            if (!isPinned(b)) {
              b.position.x -= fx;
              b.position.y -= fy;
              b.position.z -= fz;
            }
          }
        }
      }
      if (!anyOverlap)
        break;
    }
  }
  initializePositions(nodes, isPinned, previous) {
    for (const [i, node] of nodes.entries()) {
      const id = node.id ?? String(i);
      if (previous?.has(id)) {
        const prev = previous.get(id);
        node.position.x = prev.x;
        node.position.y = prev.y;
        node.position.z = prev.z;
        continue;
      }
      if (this.#randomize) {
        let t = this.#seed + i >>> 0;
        t += 1831565813;
        let r = Math.imul(t ^ t >>> 15, 1 | t);
        r ^= r + Math.imul(r ^ r >>> 7, 61 | r);
        const rand = ((r ^ r >>> 14) >>> 0) / 4294967296;
        t = t + 1831565813 >>> 0;
        const r2 = Math.imul(t ^ t >>> 15, 1 | t);
        const rand2 = ((r2 ^ r2 >>> 14) >>> 0) / 4294967296;
        t = t + 1831565813 >>> 0;
        const r3 = Math.imul(t ^ t >>> 15, 1 | t);
        const rand3 = ((r3 ^ r3 >>> 14) >>> 0) / 4294967296;
        node.position.set((rand - 0.5) * 400, (rand2 - 0.5) * 400, (rand3 - 0.5) * 400);
      } else {
        const angle = (i + this.#seed) * GOLDEN_ANGLE;
        const radius = this.#spacing * Math.sqrt(i + 1) / 2;
        node.position.x = Math.cos(angle) * radius;
        node.position.y = Math.sin(angle) * radius;
        node.position.z = 0;
      }
    }
  }
  run(nodes, getLinks, getRadius, isPinned, setDepth, previous) {
    this.initializePositions(nodes, isPinned, previous);
    const k = this.#spacing;
    const iterations = Math.max(60, Math.min(this.#iterations, Math.round(9000 * this.#iterations / (300 * nodes.length))));
    for (let step = 0;step < iterations; step++) {
      const heat = k / 2 * (1 - step / iterations);
      const forces = this.step(nodes, getLinks, getRadius, isPinned);
      for (const node of nodes) {
        if (isPinned(node))
          continue;
        const id = node.id ?? String(nodes.indexOf(node));
        const force = forces.get(id);
        const magnitude = Math.sqrt(force.x ** 2 + force.y ** 2 + force.z ** 2);
        if (magnitude === 0)
          continue;
        const limit = Math.min(magnitude, heat) / magnitude;
        node.position.x += force.x * limit;
        node.position.y += force.y * limit;
        node.position.z += force.z * limit;
      }
      this.#onProgress?.(step + 1, iterations);
    }
    this.separateOverlaps(nodes, getRadius, isPinned);
    for (const node of nodes) {
      if (isPinned(node))
        continue;
      setDepth(node, (node.depth ?? 0) * this.#depthStep);
    }
  }
}

// spacegraph/spacegraphjs/src/layout/force.ts
class ForcePlacement {
  name = "force";
  #core;
  constructor(config = {}) {
    this.#core = new ForceCore(config);
  }
  place(items, previous) {
    const nodes = items.map((item) => ({
      id: item.id,
      position: { x: 0, y: 0, z: 0 },
      width: item.width,
      height: item.height,
      meta: item.meta,
      depth: item.meta?.radius ? undefined : undefined
    }));
    const previousMap = previous ? new Map(previous.entries()) : undefined;
    const pinnedIds = new Set(previousMap?.keys() ?? []);
    this.#core.run(nodes, (node) => node.meta?.links ?? [], (node) => this.#core.getNodeRadius(node), (node) => pinnedIds.has(node.id), (node, z) => {
      node.position.z = z;
    }, previousMap);
    const positions = new Map;
    for (const node of nodes) {
      positions.set(node.id, {
        x: node.position.x,
        y: node.position.y,
        z: node.position.z
      });
    }
    return positions;
  }
}
// spacegraph/spacegraphjs/src/utils/record.ts
function lookup(record, key) {
  return record[key];
}

// spacegraph/spacegraphjs/src/layout/strategies.ts
var LANE_LAYOUT_STRATEGIES = [
  "lanes",
  "timeline",
  "hierarchy",
  "tree",
  "force"
];
var ZERO_BOUNDS = { min: { x: 0, y: 0, z: 0 }, max: { x: 0, y: 0, z: 0 } };
var SESSION_COLUMN = "__session__";
function depthOf(depthMap, kind) {
  return lookup(depthMap, kind) ?? 0;
}
function boundsOf(points) {
  const [first, ...rest] = points;
  const seed = { x: first.x, y: first.y, z: first.z };
  return rest.reduce((acc, point) => ({
    min: {
      x: Math.min(acc.min.x, point.x),
      y: Math.min(acc.min.y, point.y),
      z: Math.min(acc.min.z, point.z)
    },
    max: {
      x: Math.max(acc.max.x, point.x),
      y: Math.max(acc.max.y, point.y),
      z: Math.max(acc.max.z, point.z)
    }
  }), { min: { ...seed }, max: { ...seed } });
}
function memberPositions(ids, positions) {
  const members = (ids ?? []).flatMap((id) => {
    const position = positions.get(id);
    return position ? [position] : [];
  });
  return members.length > 0 ? boundsOf(members) : undefined;
}
function placedPositions(positions) {
  return new Map(positions);
}
function finishLayout(positions, strategy, clusters = new Map) {
  const all = [...positions.values()];
  return {
    positions,
    clusters,
    bounds: all.length > 0 ? boundsOf(all) : ZERO_BOUNDS,
    strategy
  };
}
function bucketByTurn(model, config) {
  const global = [];
  const byTurn = new Map;
  for (const node of model.nodes) {
    const turn = config.turnIdFn(node);
    if (!turn) {
      global.push(node);
      continue;
    }
    const bucket = byTurn.get(turn);
    if (bucket)
      bucket.push(node);
    else
      byTurn.set(turn, [node]);
  }
  return { global, byTurn };
}
function nodesByTurn(model, config) {
  return bucketByTurn(model, config).byTurn;
}
function turnColumnNodes(members, turnID, order, model) {
  const turnIndex = order.get(turnID);
  if (turnIndex === undefined)
    return [...members];
  const turnNode = model.nodes[turnIndex];
  if (members.some((node) => node.id === turnID))
    return [...members];
  const merged = [...members];
  const at = merged.findIndex((node) => (order.get(node.id) ?? 0) > turnIndex);
  merged.splice(at < 0 ? merged.length : at, 0, turnNode);
  return merged;
}
function turnBlocks(model, config) {
  const { global, byTurn } = bucketByTurn(model, config);
  const blocks = [];
  if (global.length > 0)
    blocks.push(global);
  for (const turnID of model.turns) {
    const nodes = byTurn.get(turnID);
    if (nodes && nodes.length > 0)
      blocks.push(nodes);
  }
  return blocks;
}
function lanesLayout(model, config, options) {
  const gap = options.gap ?? 24;
  const laneGap = options.laneGap ?? 24;
  const turnGap = options.turnGap ?? 96;
  const depthStep = options.depthStep ?? 64;
  const laneHeight = config.geometry.standardSize[1] + laneGap;
  const minColumnWidth = options.minColumnWidth ?? config.geometry.standardSize[0];
  const layout = new GridPlacement({
    lanes: config.laneList,
    lanePitch: laneHeight,
    step: gap,
    groupGap: turnGap,
    depthStep,
    minColumnWidth
  });
  const nodes = options.include ? model.nodes.filter(options.include) : model.nodes;
  const owners = new Map;
  for (const node of nodes) {
    const owner = config.ownerTurnFn?.(model, node);
    if (owner)
      owners.set(node.id, owner);
  }
  const byTurn = new Map;
  const sessionColumn = [];
  for (const node of nodes) {
    const turn = config.turnIdFn(node);
    const owner = owners.get(node.id);
    const column = node.id === model.root ? SESSION_COLUMN : owner ?? turn ?? SESSION_COLUMN;
    if (column === SESSION_COLUMN) {
      sessionColumn.push(node);
      continue;
    }
    const bucket = byTurn.get(column);
    if (bucket)
      bucket.push(node);
    else
      byTurn.set(column, [node]);
  }
  const columns = [];
  if (sessionColumn.length > 0)
    columns.push([SESSION_COLUMN, sessionColumn]);
  for (const turnID of model.turns)
    columns.push([turnID, byTurn.get(turnID) ?? []]);
  const items = [];
  for (const [column, members] of columns) {
    if (members.length === 0) {
      items.push({ id: column, width: 0, meta: { group: column, empty: true } });
      continue;
    }
    for (const node of members)
      items.push({
        id: node.id,
        width: config.nodeSizeFn(node)[0],
        depth: depthOf(config.depthMap, node.kind),
        meta: { group: column, lane: config.laneOfFn(node) }
      });
  }
  const positions = placedPositions(layout.place(items));
  const clusters = new Map;
  for (const node of model.nodes) {
    const members = config.clusterOfFn?.(node);
    if (!members)
      continue;
    const bounds = memberPositions(members, positions);
    if (bounds)
      clusters.set(node.id, bounds);
  }
  const allByTurn = nodesByTurn(model, config);
  for (const turnID of model.turns) {
    const members = memberPositions([turnID, ...(allByTurn.get(turnID) ?? []).map((node) => node.id)], positions);
    if (members)
      clusters.set(turnID, members);
  }
  return finishLayout(positions, "lanes", clusters);
}
function timelineLayout(model, config, options) {
  const gap = options.gap ?? 24;
  const rowGap = options.rowGap ?? 20;
  const depthStep = options.depthStep ?? 64;
  const columns = Math.max(1, options.columns ?? 6);
  const rowHeight = config.geometry.standardSize[1] + rowGap;
  const turnColumnWidth = columns * (config.geometry.standardSize[0] + gap) + config.geometry.padding;
  const layout = new GridPlacement({ lanePitch: rowHeight, step: gap, groupGap: 0, depthStep });
  const items = [];
  const [leading] = turnBlocks(model, config);
  if (leading && leading.length > 0)
    for (let i = 0;i < leading.length; i += columns)
      for (const node of leading.slice(i, i + columns))
        items.push({
          id: node.id,
          width: config.nodeSizeFn(node)[0],
          depth: depthOf(config.depthMap, node.kind),
          meta: {
            group: SESSION_COLUMN,
            row: Math.floor(i / columns),
            columnWidth: turnColumnWidth
          }
        });
  const byTurn = nodesByTurn(model, config);
  const order = new Map(model.nodes.map((node, index) => [node.id, index]));
  const columnsByTurn = new Map;
  for (const turnID of model.turns) {
    const turnNodes = turnColumnNodes(byTurn.get(turnID) ?? [], turnID, order, model);
    if (turnNodes.length === 0)
      continue;
    columnsByTurn.set(turnID, turnNodes);
    for (let i = 0;i < turnNodes.length; i += columns)
      for (const node of turnNodes.slice(i, i + columns))
        items.push({
          id: node.id,
          width: config.nodeSizeFn(node)[0],
          depth: depthOf(config.depthMap, node.kind),
          meta: {
            group: turnID,
            row: Math.floor(i / columns),
            columnWidth: turnColumnWidth
          }
        });
  }
  const positions = placedPositions(layout.place(items));
  const clusters = new Map;
  if (leading && leading.length > 0) {
    const members = memberPositions([model.root, ...leading.map((node) => node.id)], positions);
    if (members)
      clusters.set("session", members);
  }
  for (const turnID of model.turns) {
    const turnNodes = columnsByTurn.get(turnID);
    if (!turnNodes)
      continue;
    const members = memberPositions([turnID, ...turnNodes.filter((node) => node.id !== turnID).map((node) => node.id)], positions);
    if (members)
      clusters.set(turnID, members);
  }
  for (const node of model.nodes) {
    const members = config.clusterOfFn?.(node);
    if (!members)
      continue;
    const bounds = memberPositions(members, positions);
    if (bounds)
      clusters.set(node.id, bounds);
  }
  return finishLayout(positions, "timeline", clusters);
}
function hierarchyLayout(model, config, options) {
  const rowGap = options.rowGap ?? 12;
  const depthStep = options.depthStep ?? 64;
  const rowHeight = config.geometry.standardSize[1] + rowGap;
  const rowsPerColumn = Math.max(1, options.rows ?? 14);
  const indentStep = config.geometry.indentStep;
  const columnWidth = indentStep * config.geometry.depthLevels + config.geometry.standardSize[0] + config.geometry.padding;
  const layout = new GridPlacement({
    lanePitch: rowHeight,
    step: options.gap ?? 24,
    groupGap: 0,
    depthStep
  });
  const items = [];
  let column = 0;
  for (const nodes of turnBlocks(model, config)) {
    let row = 0;
    let group = `column:${column}`;
    for (const node of nodes) {
      if (row >= rowsPerColumn) {
        row = 0;
        column += 1;
        group = `column:${column}`;
      }
      const depth = depthOf(config.depthMap, node.kind);
      items.push({
        id: node.id,
        width: config.geometry.standardSize[0],
        depth,
        meta: {
          group,
          row,
          indent: depth * indentStep,
          columnWidth
        }
      });
      row += 1;
    }
    column += 1;
  }
  return finishLayout(placedPositions(layout.place(items)), "hierarchy");
}
function treeLayout(model, config, options) {
  const layout = new TreePlacement({
    columnPitch: config.geometry.standardSize[0] + (options.gap ?? 24),
    rowPitch: config.geometry.standardSize[1] + (options.rowGap ?? 24),
    depthStep: options.depthStep ?? 64,
    columns: options.treeColumns
  });
  const items = model.nodes.map((node) => ({
    id: node.id,
    width: config.nodeSizeFn(node)[0],
    depth: depthOf(config.depthMap, node.kind),
    meta: { parent: config.parentOfFn?.(model, node) }
  }));
  return finishLayout(placedPositions(layout.place(items, options.previous)), "tree");
}
function forceLayout(model, config, options) {
  const layout = new ForcePlacement({
    spacing: config.geometry.standardSize[0] + (options.gap ?? 24),
    depthStep: options.depthStep ?? 64
  });
  const items = model.nodes.map((node) => ({
    id: node.id,
    width: config.nodeSizeFn(node)[0],
    depth: depthOf(config.depthMap, node.kind),
    meta: {
      links: config.forceLinksFn?.(model, node),
      radius: config.nodeSizeFn(node)[0] / 2
    }
  }));
  return finishLayout(placedPositions(layout.place(items, options.previous)), "force");
}
function layoutGraph(model, config, options = {}) {
  const strategy = options.strategy ?? "lanes";
  if (strategy === "hierarchy")
    return hierarchyLayout(model, config, options);
  if (strategy === "timeline")
    return timelineLayout(model, config, options);
  if (strategy === "tree")
    return treeLayout(model, config, options);
  if (strategy === "force")
    return forceLayout(model, config, options);
  return lanesLayout(model, config, options);
}
function laneAnchors(config, options = {}) {
  const laneGap = options.laneGap ?? 24;
  const laneHeight = config.geometry.standardSize[1] + laneGap;
  return config.laneList.map((name, index) => ({ name, y: index * laneHeight }));
}
var Strategies = {
  LANE_LAYOUT_STRATEGIES,
  layoutGraph,
  laneAnchors
};
// spacegraph/src/graph.ts
var LANES = ["session", "user", "assistant", "tool", "summary", "other"];
var LANE_COLOURS = {
  session: "#cba6f7",
  user: "#89b4fa",
  assistant: "#a6e3a1",
  tool: "#fab387",
  summary: "#e64553",
  other: "#9399b2"
};
var SESSION_EDGES = ["nbr", "call", "heritage", "cue"];
var CARD = [232, 58];
var str = (v) => typeof v === "string" ? v : "";
function laneOf(node) {
  const role = str(node.meta?.role) || node.kind;
  if (node.kind === "sess")
    return "session";
  if (node.kind === "sum" || node.meta?.summary === true)
    return "summary";
  if (node.kind === "tool" || role.startsWith("tool"))
    return "tool";
  if (node.kind === "step" || role === "assistant")
    return "assistant";
  if (role === "user")
    return "user";
  return "other";
}
function turnsOf(nodes) {
  const out = new Map;
  let turn = -1;
  for (const node of nodes) {
    const lane = laneOf(node);
    if (lane === "session")
      continue;
    const given = node.meta?.turn;
    if (typeof given === "number" && Number.isInteger(given) && given >= 0) {
      turn = Math.max(turn, given);
      out.set(node.id, given);
      continue;
    }
    if (lane === "user")
      turn += 1;
    out.set(node.id, Math.max(turn, 0));
  }
  return out;
}
var PREFIX = /^(user|assistant|tool)(:[^:\s]*)?:?\s/;
function textOf(node) {
  let text = node.title.replace(/\s+/g, " ").trim();
  const tool = str(node.meta?.tool_name);
  const m = PREFIX.exec(`${text} `);
  if (m)
    text = `${text} `.slice(m[0].length).trim();
  if (tool && text.startsWith(`${tool} `))
    text = text.slice(tool.length + 1);
  return text;
}
function toolName(node) {
  const named = str(node.meta?.tool_name);
  if (named)
    return named;
  const role = str(node.meta?.role);
  return role.startsWith("tool:") ? role.slice(5) : "";
}
var clip = (text, max) => text.length > max ? `${text.slice(0, max - 1)}…` : text;
function toSpaceModel(graph, edgeKinds = SESSION_EDGES) {
  const turns = turnsOf(graph.nodes);
  const ids = new Set(graph.nodes.map((n) => n.id));
  const calls = new Map;
  for (const e of graph.edges) {
    if (e.kind !== "call")
      continue;
    const tool = graph.nodes.find((n) => n.id === e.source);
    if (tool)
      calls.set(e.target, [...calls.get(e.target) ?? [], toolName(tool) || "tool"]);
  }
  const nodes = graph.nodes.map((n) => {
    const lane = laneOf(n);
    const turn = turns.get(n.id);
    let body = textOf(n);
    if (!body && calls.has(n.id))
      body = `calls ${[...new Set(calls.get(n.id))].join(", ")}`;
    const tool = lane === "tool" ? toolName(n) : "";
    const head = [lane === "session" ? `session ${n.session ?? ""}`.trim() : lane, tool, turn === undefined ? "" : `t${turn}`].filter(Boolean).join(" · ");
    return {
      id: n.id,
      kind: lane,
      label: clip(body || lane, 60),
      status: "neutral",
      lane,
      turn: turn === undefined ? undefined : `turn:${turn}`,
      size: CARD,
      head,
      body: clip(body, 140),
      source: n
    };
  });
  const edges = graph.edges.filter((e) => edgeKinds.includes(e.kind) && ids.has(e.source) && ids.has(e.target) && e.source !== e.target).map((e) => ({
    id: `${e.kind}:${e.source}->${e.target}`,
    source: e.source,
    target: e.target,
    kind: e.kind,
    label: e.shared?.length ? e.shared.join(", ") : undefined,
    confidence: e.kind === "call" ? "explicit" : e.kind === "cue" ? "inferred" : "structural"
  }));
  const rootNode = graph.nodes.find((n) => n.id === graph.root);
  const root = rootNode && laneOf(rootNode) === "session" ? rootNode.id : `session:${graph.session ?? ""}`;
  const count = Math.max(-1, ...turns.values()) + 1;
  return { nodes, edges, root, turns: Array.from({ length: count }, (_, t) => `turn:${t}`) };
}
function linksOf(model, id) {
  return model.edges.flatMap((e) => e.source === id ? [e.target] : e.target === id ? [e.source] : []);
}
var LAYOUT_CONFIG = {
  nodeSizeFn: (node) => node.size,
  laneList: LANES,
  laneOfFn: (node) => node.lane,
  depthMap: { session: 0, user: 1, assistant: 1, tool: 2, summary: 1, other: 2 },
  turnIdFn: (node) => node.turn,
  forceLinksFn: (model, node) => linksOf(model, node.id),
  geometry: { standardSize: CARD, indentStep: 120, depthLevels: 3, padding: 24 }
};
var LAYOUT_OPTIONS = { gap: 20, laneGap: 26, turnGap: 64 };
function layoutSpace(model, strategy = "lanes", gap = LAYOUT_OPTIONS.gap) {
  return Strategies.layoutGraph({ nodes: model.nodes, edges: model.edges, root: model.root, turns: model.turns }, LAYOUT_CONFIG, { strategy, ...LAYOUT_OPTIONS, gap });
}
function cardMarkup(node) {
  return `<div class="sg-card${node.big ? " sg-big" : ""}" data-lane="${node.lane}" style="--lane:${LANE_COLOURS[node.lane]}">` + `<div class="sg-head">${escapeHtml(node.head)}</div>` + `<div class="sg-body">${escapeHtml(node.body)}</div></div>`;
}

class SpaceAdapter extends ModelAdapter {
  nodeShape() {
    return "card";
  }
  nodeStatus() {
    return "neutral";
  }
  isStructuralKind() {
    return false;
  }
  nodeData(node) {
    return { lane: node.lane, turn: node.turn ?? "", w: node.size[0], h: node.size[1] };
  }
  toRenderEdge(edge) {
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      kind: edge.kind,
      label: edge.label,
      confidence: edge.confidence,
      directed: edge.kind === "call",
      style: edge.kind === "cue" ? "dashed" : "solid"
    };
  }
  buildBands(model, _level, positions, _render, visible) {
    return model.turns.flatMap((turn) => {
      const members = model.nodes.filter((n) => n.turn === turn).map((n) => n.id);
      const box = this.bandBox(model, members, positions, visible);
      if (!box)
        return [];
      const width = box.max.x - box.min.x;
      const height = box.max.y - box.min.y;
      return [{
        id: turn,
        kind: "band",
        label: turn.replace("turn:", "turn "),
        shape: "group",
        interactive: false,
        position: [box.min.x + width / 2, box.min.y + height / 2, -1],
        size: [width, height, 0],
        data: { members }
      }];
    });
  }
}
function adapter() {
  return new SpaceAdapter({
    cardBudget: 1e5,
    renderCardTemplate: cardMarkup,
    nodeSizeFn: (node) => node.size,
    geometry: { bandPadding: { top: 12, right: 12, bottom: 32, left: 12 }, laneLabelSize: [110, 24, 0], laneGutter: 28 }
  });
}
function laneLabels(model) {
  const used = new Set(model.nodes.map((n) => n.lane));
  return Strategies.laneAnchors(LAYOUT_CONFIG, LAYOUT_OPTIONS).filter((a) => used.has(a.name) && a.name !== "session").map((a) => ({ id: a.name, label: a.name, y: a.y }));
}
function sessionView(graph, strategy = "lanes") {
  const model = toSpaceModel(graph);
  const layout = layoutSpace(model, strategy);
  const render = adapter().build(model, {
    level: "detail",
    positions: layout.positions,
    edges: true,
    bands: strategy === "lanes",
    laneLabels: strategy === "lanes" ? laneLabels(model) : []
  });
  return { model, layout, render };
}
function mapView(graph, gap = -60) {
  const sessions = {
    ...graph,
    root: null,
    nodes: graph.nodes.map((n) => ({ ...n, kind: "sess" }))
  };
  const flat = toSpaceModel(sessions, ["cue"]);
  const model = { ...flat, nodes: flat.nodes.map((n) => ({ ...n, big: true })) };
  const layout = layoutSpace(model, "force", gap);
  const render = adapter().build(model, { level: "detail", positions: layout.positions, edges: true });
  return { model, layout, render };
}

// spacegraph/src/gantt.ts
var EVENT_COLOURS = {
  own: "#6c7086",
  recall: "#7aa2ff",
  find: "#4fd1c5",
  open: "#f6c177",
  compress: "#e64553"
};
var ADDS = new Set(["recall", "find", "open"]);
function columnsOf(context) {
  const turns = Math.max(context.turns.length, ...context.events.map((e) => e.turn + 1), ...context.rows.flatMap((r) => r.bars.map((b) => b.t0 + 1)), 1);
  const byTurn = Array.from({ length: turns }, () => []);
  context.events.forEach((e, i) => byTurn[e.turn].push(i));
  const turnStart = [];
  const eventSlot = new Array(context.events.length).fill(0);
  let slot = 0;
  for (let t = 0;t < turns; t++) {
    turnStart.push(slot++);
    for (const i of byTurn[t])
      eventSlot[i] = slot++;
  }
  return { count: slot, turns, turnStart, eventSlot };
}
function barSpan(bar, cols) {
  const start = bar.from === null ? cols.turnStart[bar.t0] : cols.eventSlot[bar.from];
  const end = bar.to === null ? cols.count : cols.eventSlot[bar.to];
  return { start, end: Math.max(end, start + 1) };
}
function shortSid(sid) {
  return sid.length > 16 ? `${sid.slice(0, 14)}…` : sid;
}
function rowOrder(contexts) {
  const out = [];
  const ownCount = Math.max(0, ...contexts.map((c) => c.rows.filter((r) => r.session === c.session).length));
  if (ownCount > 0) {
    out.push({ kind: "group", label: "this session", session: "" });
    for (let i = 0;i < ownCount; i++)
      out.push({ kind: "own", index: i });
  }
  const bytes = new Map;
  const first = new Map;
  const sessionOf = new Map;
  for (const c of contexts) {
    const cols = columnsOf(c);
    for (const r of c.rows) {
      if (r.session === c.session)
        continue;
      sessionOf.set(r.id, r.session);
      const b = r.bars.reduce((n, bar) => n + bar.bytes, 0);
      bytes.set(r.session, (bytes.get(r.session) ?? 0) + b);
      const start = Math.min(...r.bars.map((bar) => barSpan(bar, cols).start)) / cols.count;
      first.set(r.id, Math.min(first.get(r.id) ?? Infinity, start));
    }
  }
  const sessions = [...bytes.keys()].sort((a, b) => a === "" ? 1 : b === "" ? -1 : (bytes.get(b) ?? 0) - (bytes.get(a) ?? 0) || a.localeCompare(b));
  for (const s of sessions) {
    const ids = [...sessionOf.keys()].filter((id) => sessionOf.get(id) === s);
    ids.sort((a, b) => (first.get(a) ?? 0) - (first.get(b) ?? 0) || a.localeCompare(b));
    out.push({ kind: "group", label: s === "" ? "no session (imported or summary)" : shortSid(s), session: s });
    for (const id of ids)
      out.push({ kind: "row", id });
  }
  return out;
}
var LABEL_MAX = 46;
function clip2(text, max = LABEL_MAX) {
  const one = text.replace(/\s+/g, " ").trim();
  return one.length > max ? `${one.slice(0, max - 1)}…` : one;
}
function bytesPeak(context) {
  let peak = 1;
  for (const e of context.events) {
    if (ADDS.has(e.ev))
      peak = Math.max(peak, e.used + Math.max(0, e.want - e.bytes));
    if (e.turn_budget)
      peak = Math.max(peak, e.turn_budget);
  }
  return peak;
}
function layoutGantt(context, options = {}) {
  const labelWidth = options.labelWidth ?? 300;
  const slotWidth = options.slotWidth ?? 24;
  const rowHeight = options.rowHeight ?? 13;
  const cols = columnsOf(context);
  const order = options.order ?? rowOrder([context]);
  const own = context.rows.filter((r) => r.session === context.session);
  const byId = new Map(context.rows.map((r) => [r.id, r]));
  const header = 74;
  const x = (slot) => labelWidth + slot * slotWidth;
  const rows = [];
  const bars = [];
  order.forEach((slot, i) => {
    const y = header + i * rowHeight;
    let row = null;
    let label = "";
    if (slot.kind === "group")
      label = slot.session && options.sessionTitles?.[slot.session] || slot.label;
    else {
      row = slot.kind === "own" ? own[slot.index] ?? null : byId.get(slot.id) ?? null;
      label = row ? clip2(row.label.startsWith(`${row.kind} `) ? row.label : `${row.kind} ${row.label}`) : "";
    }
    rows.push({ y, slot, row, label });
    for (const bar of row?.bars ?? []) {
      const span = barSpan(bar, cols);
      bars.push({ row: row.id, x0: x(span.start), x1: x(span.end), y, ev: bar.ev, level: bar.level, bytes: bar.bytes });
    }
  });
  const events = context.events.map((e, index) => ({
    index,
    x: x(cols.eventSlot[index]) + slotWidth / 2,
    ev: e.ev,
    cap: e.cap,
    bytes: e.bytes,
    want: e.want,
    used: e.used,
    q: e.q,
    turn: e.turn
  }));
  const turnLabels = [];
  for (let t = 0;t < cols.turns; t++) {
    const inTurn = context.events.filter((e) => e.turn === t);
    const budget = inTurn.find((e) => e.turn_budget)?.turn_budget ?? null;
    const used = Math.max(0, ...inTurn.filter((e) => ADDS.has(e.ev)).map((e) => e.used));
    const text = budget ? `t${t} ${Math.round(100 * used / budget)}%` : `t${t}`;
    turnLabels.push({ turn: t, x: x(cols.turnStart[t]) + 2, text });
  }
  const stripTop = header + order.length * rowHeight + 18;
  const stripHeight = options.stripHeight ?? 140;
  const peak = options.bytesMax ?? bytesPeak(context);
  const scale = peak / stripHeight;
  const yOf = (b) => stripTop + stripHeight - b / scale;
  const strip = { top: stripTop, height: stripHeight, scale, bars: [], budgets: [] };
  context.events.forEach((e, index) => {
    if (!ADDS.has(e.ev))
      return;
    strip.bars.push({
      index,
      x: x(cols.eventSlot[index]),
      y0: yOf(e.used - e.bytes),
      y1: yOf(e.used),
      ghost: Math.max(0, e.want - e.bytes) / scale,
      ev: e.ev
    });
  });
  for (let t = 0;t < cols.turns; t++) {
    const inTurn = context.events.map((e, i) => [e, i]).filter(([e]) => e.turn === t);
    const budget = inTurn.find(([e]) => e.turn_budget)?.[0].turn_budget;
    if (!budget)
      continue;
    const last = Math.max(cols.turnStart[t], ...inTurn.map(([, i]) => cols.eventSlot[i]));
    strip.budgets.push({ turn: t, x0: x(cols.turnStart[t]), x1: x(last + 1), y: yOf(budget), bytes: budget });
  }
  const adding = context.events.filter((e) => ADDS.has(e.ev));
  return {
    width: x(cols.count) + 16,
    height: stripTop + stripHeight + 30,
    cols,
    top: header,
    rows,
    bars,
    events,
    turnLabels,
    strip,
    totals: {
      added: adding.reduce((n, e) => n + e.bytes, 0),
      want: adding.reduce((n, e) => n + e.want, 0),
      events: context.events.length,
      cuts: context.events.filter((e) => e.cap).length,
      turnBudget: context.events.find((e) => e.turn_budget)?.turn_budget ?? null
    }
  };
}
function escapeXml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
var fmt = (n) => n.toLocaleString("en-US");
function headline(t) {
  const parts = [`${t.events} events`, `${fmt(t.added)} B added by events`];
  if (t.cuts)
    parts.push(`${t.cuts} cut`);
  if (t.turnBudget !== null)
    parts.push(`turn budget ${fmt(t.turnBudget)} B`);
  return parts.join(" · ");
}
function renderGanttSVG(context, options = {}) {
  const g = layoutGantt(context, options);
  const slotWidth = options.slotWidth ?? 24;
  const rowHeight = options.rowHeight ?? 13;
  const colour = (ev) => EVENT_COLOURS[ev] ?? EVENT_COLOURS.own;
  const out = [];
  out.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${g.width}" height="${g.height}" viewBox="0 0 ${g.width} ${g.height}" font-family="ui-monospace, Menlo, monospace" font-size="10">`, '<defs><pattern id="ghost" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">' + '<line x1="0" y1="0" x2="0" y2="5" stroke="#cdd6f4" stroke-width="1.4" stroke-opacity="0.7"/></pattern></defs>');
  const t = g.totals;
  const title = options.title ?? shortSid(context.session);
  out.push(`<text x="8" y="16" font-size="13" font-weight="bold" fill="#cdd6f4">${escapeXml(title)}</text>`, `<text x="8" y="32" fill="#a6adc8">${escapeXml(headline(t))}</text>`);
  for (const label of g.turnLabels) {
    out.push(`<line x1="${label.x - 2}" y1="40" x2="${label.x - 2}" y2="${g.strip.top + g.strip.height}" stroke="#45475a" stroke-width="1"/>`, `<text x="${label.x}" y="50" fill="#cdd6f4">${escapeXml(label.text)}</text>`);
  }
  for (const e of g.events) {
    const tip = `${e.index} ${e.ev}${e.q ? ` "${e.q}"` : ""}: ${fmt(e.bytes)} B` + (e.cap ? ` (cut: want ${fmt(e.want)} B)` : "") + (ADDS.has(e.ev) ? `, used ${fmt(e.used)} B` : "");
    out.push(`<g><title>${escapeXml(tip)}</title><circle cx="${e.x}" cy="62" r="4.5" fill="${colour(e.ev)}"/>`);
    if (e.cap)
      out.push(`<circle cx="${e.x}" cy="62" r="8" fill="none" stroke="${colour(e.ev)}" stroke-width="1.5" stroke-dasharray="2.5 2"/>`);
    out.push("</g>");
    if (e.ev === "compress") {
      out.push(`<line x1="${e.x}" y1="${g.top}" x2="${e.x}" y2="${g.strip.top - 8}" stroke="${colour("compress")}" stroke-width="1.5" stroke-dasharray="4 3"/>`);
    }
  }
  for (const r of g.rows) {
    if (r.slot.kind === "group") {
      out.push(`<text x="8" y="${r.y + rowHeight - 3}" font-weight="bold" fill="#bac2de">${escapeXml(r.label)}</text>`);
      continue;
    }
    out.push(`<line x1="8" y1="${r.y + rowHeight - 0.5}" x2="${g.width - 16}" y2="${r.y + rowHeight - 0.5}" stroke="#313244" stroke-width="0.5"/>`);
    if (r.row)
      out.push(`<text x="16" y="${r.y + rowHeight - 3}" fill="#a6adc8"><title>${escapeXml(`${r.row.id} ${r.row.label}`)}</title>${escapeXml(r.label)}</text>`);
  }
  for (const b of g.bars) {
    const opacity = (0.4 + 0.55 * Math.max(0, Math.min(1, b.level))).toFixed(2);
    out.push(`<rect x="${b.x0 + 1}" y="${b.y + 2}" width="${Math.max(2, b.x1 - b.x0 - 2)}" height="${rowHeight - 4}" rx="2" fill="${colour(b.ev)}" fill-opacity="${opacity}">` + `<title>${escapeXml(`${b.ev} level ${b.level} ${fmt(b.bytes)} B`)}</title></rect>`);
  }
  const usedOfTurn = new Map;
  for (const s of g.strip.bars)
    usedOfTurn.set(context.events[s.index].turn, context.events[s.index].used);
  const perTurn = [...usedOfTurn].map(([turn, used]) => `t${turn} ${fmt(used)} B`).join(" · ");
  out.push(`<text x="8" y="${g.strip.top + 10}" fill="#bac2de" font-weight="bold">${escapeXml(`bytes added in each turn${perTurn ? ` · ${perTurn}` : ""}`)}</text>`);
  out.push(`<line x1="${options.labelWidth ?? 300}" y1="${g.strip.top + g.strip.height}" x2="${g.width - 16}" y2="${g.strip.top + g.strip.height}" stroke="#585b70"/>`);
  for (const s of g.strip.bars) {
    const w = slotWidth - 6;
    out.push(`<rect x="${s.x + 3}" y="${s.y1}" width="${w}" height="${Math.max(1, s.y0 - s.y1)}" fill="${colour(s.ev)}"/>`);
    if (s.ghost > 0) {
      out.push(`<rect x="${s.x + 3}" y="${s.y1 - s.ghost}" width="${w}" height="${s.ghost}" fill="url(#ghost)" stroke="${colour(s.ev)}" stroke-width="1" stroke-dasharray="2.5 2"/>`);
    }
  }
  for (const b of g.strip.budgets) {
    out.push(`<line x1="${b.x0}" y1="${b.y}" x2="${b.x1}" y2="${b.y}" stroke="#f5e0dc" stroke-width="1.2" stroke-dasharray="6 3"/>`, `<text x="${b.x0 + 3}" y="${b.y - 4}" fill="#f5e0dc">${escapeXml(`turn budget ${fmt(b.bytes)} B`)}</text>`);
  }
  out.push(`<text x="8" y="${g.strip.top + g.strip.height + 20}" fill="#7f849c">${escapeXml(`top = ${fmt(Math.round(g.strip.scale * g.strip.height))} B`)}</text>`);
  out.push("</svg>");
  return out.join(`
`);
}

// spacegraph/src/venn.ts
var VENN_SETS = [
  { set: "recalled", ev: "recall" },
  { set: "found", ev: "find" },
  { set: "opened", ev: "open" }
];
function circleOverlap(r12, r2, d) {
  if (d >= r12 + r2)
    return 0;
  const small = Math.min(r12, r2);
  if (d <= Math.abs(r12 - r2))
    return Math.PI * small * small;
  const a1 = Math.acos((d * d + r12 * r12 - r2 * r2) / (2 * d * r12));
  const a2 = Math.acos((d * d + r2 * r2 - r12 * r12) / (2 * d * r2));
  const k = Math.sqrt((-d + r12 + r2) * (d + r12 - r2) * (d - r12 + r2) * (d + r12 + r2));
  return r12 * r12 * a1 + r2 * r2 * a2 - k / 2;
}
function distanceFor(r12, r2, area) {
  if (area <= 0)
    return r12 + r2;
  const small = Math.min(r12, r2);
  if (area >= Math.PI * small * small)
    return Math.abs(r12 - r2);
  let lo = Math.abs(r12 - r2);
  let hi = r12 + r2;
  for (let i = 0;i < 60; i++) {
    const mid = (lo + hi) / 2;
    if (circleOverlap(r12, r2, mid) > area)
      lo = mid;
    else
      hi = mid;
  }
  return (lo + hi) / 2;
}
function sharedBytes(venn, a, b) {
  let n = 0;
  for (const r of venn.regions) {
    if (!r.in.includes(a) || !r.in.includes(b))
      continue;
    const by = r.by ?? {};
    const share = r.bytes / r.in.length;
    n += Math.min(by[a] ?? share, by[b] ?? share);
  }
  return n;
}
function layoutVenn(venn, width = 420, height = 300, padding = 18) {
  const live = VENN_SETS.filter((s) => (venn.sets[s.set]?.bytes ?? 0) > 0).map((s) => ({ ...s, bytes: venn.sets[s.set].bytes, count: venn.sets[s.set].ids.length })).sort((p, q) => q.bytes - p.bytes);
  const empty = VENN_SETS.filter((s) => (venn.sets[s.set]?.bytes ?? 0) <= 0).map((s) => s.set);
  const radius = (bytes) => Math.sqrt(bytes / Math.PI);
  const placed = [];
  const gap = (p, q) => 0.12 * Math.min(p, q);
  const dist = (i, j) => {
    const ri = radius(live[i].bytes);
    const rj = radius(live[j].bytes);
    const want = sharedBytes(venn, live[i].set, live[j].set);
    return want > 0 ? distanceFor(ri, rj, want) : ri + rj + gap(ri, rj);
  };
  live.forEach((s, i) => {
    const r = radius(s.bytes);
    let x = 0;
    let y = 0;
    if (i === 1)
      x = dist(0, 1);
    if (i === 2) {
      const dab = dist(0, 1);
      const dac = dist(0, 2);
      const dbc = dist(1, 2);
      x = (dac * dac - dbc * dbc + dab * dab) / (2 * dab);
      y = Math.sqrt(Math.max(0, dac * dac - x * x));
    }
    placed.push({ set: s.set, ev: s.ev, x, y, r, bytes: s.bytes, count: s.count });
  });
  const pairs = [];
  for (let i = 0;i < placed.length; i++)
    for (let j = i + 1;j < placed.length; j++) {
      const p = placed[i];
      const q = placed[j];
      pairs.push({ a: p.set, b: q.set, want: sharedBytes(venn, p.set, q.set), drawn: circleOverlap(p.r, q.r, Math.hypot(p.x - q.x, p.y - q.y)) });
    }
  if (placed.length === 0)
    return { circles: [], pairs, empty, width, height, scale: 0 };
  const minX = Math.min(...placed.map((c) => c.x - c.r));
  const maxX = Math.max(...placed.map((c) => c.x + c.r));
  const minY = Math.min(...placed.map((c) => c.y - c.r));
  const maxY = Math.max(...placed.map((c) => c.y + c.r));
  const k = Math.min((width - 2 * padding) / (maxX - minX), (height - 2 * padding) / (maxY - minY));
  const ox = (width - (maxX - minX) * k) / 2 - minX * k;
  const oy = (height - (maxY - minY) * k) / 2 - minY * k;
  const circles = placed.map((c) => ({ ...c, x: c.x * k + ox, y: c.y * k + oy, r: c.r * k }));
  return { circles, pairs, empty, width, height, scale: k * k };
}
var fmt2 = (n) => Math.round(n).toLocaleString("en-US");
function vennHeader(venn, event) {
  const at = venn.at === null ? "now" : `at event ${venn.at}`;
  const did = event ? event.cap ? `: ${event.ev}, cut: ${fmt2(event.bytes)} of ${fmt2(event.want)} B` : `: ${event.ev}, ${fmt2(event.bytes)} B` : "";
  const empty = VENN_SETS.filter((s) => (venn.sets[s.set]?.bytes ?? 0) <= 0).map((s) => s.set);
  return [`${at}${did}`, `area = bytes${empty.length ? ` · empty: ${empty.join(", ")}` : ""}`];
}
function renderVennSVG(venn, options = {}) {
  const width = options.width ?? 420;
  const height = options.height ?? 340;
  const top = 44;
  const g = layoutVenn(venn, width, height - top);
  const [line1, line2] = vennHeader(venn, options.event);
  const out = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" font-family="ui-sans-serif, system-ui, sans-serif">`,
    `<text x="8" y="17" font-size="14" font-weight="700" fill="#cdd6f4">${escapeXml(line1)}</text>`,
    `<text x="8" y="35" font-size="12" fill="#a6adc8">${escapeXml(line2)}</text>`,
    `<g transform="translate(0 ${top})">`
  ];
  for (const c of g.circles) {
    const colour = EVENT_COLOURS[c.ev] ?? "#9399b2";
    out.push(`<circle cx="${c.x.toFixed(1)}" cy="${c.y.toFixed(1)}" r="${c.r.toFixed(1)}" fill="${colour}" fill-opacity="0.28" stroke="${colour}" stroke-width="2"/>`);
  }
  const cx = g.circles.reduce((n, c) => n + c.x, 0) / Math.max(1, g.circles.length);
  const cy = g.circles.reduce((n, c) => n + c.y, 0) / Math.max(1, g.circles.length);
  for (const c of g.circles) {
    const colour = EVENT_COLOURS[c.ev] ?? "#9399b2";
    const dx = c.x - cx;
    const dy = c.y - cy;
    const len = Math.hypot(dx, dy) || 1;
    const push = g.circles.length > 1 ? 0.45 * c.r : 0;
    const x = c.x + dx / len * push;
    const y = c.y + dy / len * push;
    out.push(`<text x="${x.toFixed(1)}" y="${(y - 4).toFixed(1)}" text-anchor="middle" font-size="13" font-weight="700" fill="${colour}">${escapeXml(c.set)}</text>`, `<text x="${x.toFixed(1)}" y="${(y + 12).toFixed(1)}" text-anchor="middle" font-size="11" fill="#cdd6f4">${escapeXml(`${fmt2(c.bytes)} B · ${c.count}`)}</text>`);
  }
  out.push("</g></svg>");
  return out.join(`
`);
}

// spacegraph/desktop/plugin.ts
var ID = "breadcrumbs";
var PATH = "/spacegraph";
var POLL = 5000;
var LIST_POLL = 15000;
var TABS = [
  ["map", "Map", "breadcrumbs: every session, joined by shared cue words"],
  ["session", "Session", "breadcrumbs: one session's crumbs, by role (lane) and turn (column)"],
  ["gantt", "Gantt", "breadcrumbs: what was in the context, event by event"],
  ["venn", "Venn", "breadcrumbs: how recall, find and open overlap, at one event"],
  ["log", "Log graph", "Hermes log (state.db): the messages and tool calls of one session"],
  ["logGantt", "Log Gantt", "Hermes log (state.db): which messages were in the context"]
];
var LOG_TABS = ["log", "logGantt"];
var CTX = null;
async function rest(path) {
  if (!CTX)
    throw new Error("SpaceGraph is not registered yet");
  const out = await CTX.rest(path, { timeoutMs: 120000 });
  if (out && typeof out === "object" && typeof out.error === "string")
    throw new Error(out.error);
  return out;
}
function qs(params) {
  return Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&");
}
var TEXT = /fill="#(?:cdd6f4|bac2de)"/g;
var SUBTEXT = /fill="#(?:a6adc8|9399b2|7f849c)"/g;
var PANEL = /fill="#(?:1e1e2e|181825|11111b)"/g;
function themed(svg) {
  return svg.replace(TEXT, 'fill="currentColor"').replace(SUBTEXT, 'fill="currentColor" fill-opacity="0.66"').replace(PANEL, 'fill="transparent"').replace(/stroke="#(?:45475a|313244|585b70)"/g, 'stroke="currentColor" stroke-opacity="0.22"');
}
var CSS = `${DRAW_CSS}
.sgp{display:flex;flex-direction:column;height:100%;min-height:0;color:var(--foreground);font-size:12px}
.sgp-bar{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--ui-stroke-secondary)}
.sgp-bar label{display:flex;gap:5px;align-items:center;color:var(--ui-text-tertiary)}
.sgp select{background:transparent;color:var(--foreground);border:1px solid var(--ui-stroke-secondary);border-radius:6px;padding:3px 6px;font-size:12px;max-width:360px}
.sgp select option{color:initial}
.sgp-tabs{display:flex;gap:4px;flex-wrap:wrap}
.sgp-tab{border:1px solid var(--ui-stroke-secondary);border-radius:6px;padding:3px 9px;background:transparent;color:var(--ui-text-secondary);cursor:pointer;font-size:12px}
.sgp-tab[data-on="1"]{color:var(--foreground);border-color:var(--ui-accent);box-shadow:inset 0 -2px 0 var(--ui-accent)}
.sgp-note{color:var(--ui-text-tertiary)}
.sgp-body{flex:1;min-height:0;overflow:auto;position:relative}
.sgp-fit{position:absolute;inset:0}
.sgp-stamp{position:absolute;right:10px;top:6px;z-index:2;font-size:11px;color:var(--ui-text-tertiary);pointer-events:none}
.sgp-msg{padding:18px;color:var(--ui-text-tertiary);font-size:13px;line-height:1.5;white-space:pre-wrap}
.sgp-err{color:var(--ui-text-secondary)}
.sgp-svg{padding:10px 12px;color:var(--foreground)}
.sgp-svg svg{max-width:none}
.sgp .sg-stage{--lane-label-color:var(--ui-text-tertiary)}
.sgp .sg-card{background:color-mix(in srgb,var(--foreground) 5%,transparent);border-color:var(--ui-stroke-secondary);color:var(--foreground)}
.sgp .sg-band{fill:color-mix(in srgb,var(--ui-accent) 5%,transparent);stroke:var(--ui-stroke-secondary)}
.sgp .sg-band-label{fill:var(--ui-text-tertiary)}
.sgp .sg-edge-label{stroke:transparent}
.sgp input[type=range]{width:180px}
`;
function useSize(ref) {
  const [size, setSize] = useState({ w: 900, h: 600 });
  useEffect(() => {
    const el = ref.current;
    if (!el)
      return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (!r)
        return;
      const w = Math.max(240, Math.floor(r.width));
      const h = Math.max(180, Math.floor(r.height));
      setSize((s) => Math.abs(s.w - w) < 2 && Math.abs(s.h - h) < 2 ? s : { w, h });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return size;
}
function message(text, error = false) {
  return jsx("div", { className: error ? "sgp-msg sgp-err" : "sgp-msg", children: text });
}
function errorText(e) {
  const text = e instanceof Error ? e.message : String(e);
  if (/404|not found/i.test(text)) {
    return `${text}

The SpaceGraph routes did not answer. Check that the breadcrumbs plugin is installed and enabled for this profile:
  hermes plugins list   (breadcrumbs: enabled)`;
  }
  return text;
}
function bcSessions(graph) {
  const nodes = graph?.nodes || [];
  return nodes.filter((n) => n.kind === "sess").map((n) => ({ id: String(n.session || n.id), title: String(n.title || n.session || n.id), ts: Number(n.ts || 0) })).sort((a, b) => b.ts - a.ts);
}
function logSessions(list) {
  return (list?.sessions || []).map((s) => ({ id: String(s.id), title: String(s.title || s.id), ts: Number(s.started || 0) }));
}
function label(item) {
  const when = item.ts ? new Date(item.ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
  const title = item.title.length > 60 ? item.title.slice(0, 59) + "…" : item.title;
  return when ? `${when} · ${title}` : title;
}
function GraphStage({ view, size, zoom }) {
  const html = useMemo(() => {
    try {
      return drawModel(view.render, { width: Math.round(size.w * zoom), height: Math.round(size.h * zoom), padding: 18, maxScale: 1.3 }).html;
    } catch (e) {
      return `<div class="sgp-msg sgp-err">${String(e instanceof Error ? e.message : e)}</div>`;
    }
  }, [view, size.w, size.h, zoom]);
  return jsx("div", { className: zoom === 1 ? "sgp-fit" : "", dangerouslySetInnerHTML: { __html: html } });
}
function SvgStage({ svg }) {
  return jsx("div", { className: "sgp-svg", dangerouslySetInnerHTML: { __html: themed(svg) } });
}
function View({ tab, store, sid, zoom, at, setAt }) {
  const ref = useRef(null);
  const size = useSize(ref);
  const need = tab !== "map";
  const path = tab === "map" ? `/graph?${qs({ store })}` : tab === "session" ? `/session?${qs({ store, sid })}` : tab === "gantt" || tab === "venn" ? `/context?${qs({ store, sid })}` : tab === "log" ? `/log/session?${qs({ store, sid })}` : `/log/context?${qs({ store, sid })}`;
  const main = useQuery({ queryKey: [ID, "view", path], queryFn: () => rest(path), enabled: Boolean(store) && (!need || Boolean(sid)), refetchInterval: POLL });
  const events = tab === "venn" ? main.data?.events || [] : [];
  const index = events.length ? Math.min(at ?? events.length - 1, events.length - 1) : null;
  const vennPath = `/venn?${qs({ store, sid, at: index })}`;
  const venn = useQuery({ queryKey: [ID, "venn", vennPath], queryFn: () => rest(vennPath), enabled: tab === "venn" && index !== null });
  let body;
  if (!store)
    body = message("No store yet.");
  else if (need && !sid)
    body = message("No session in this store yet.");
  else if (main.isError)
    body = message(errorText(main.error), true);
  else if (!main.data)
    body = message("Loading…");
  else if (tab === "map")
    body = jsx(GraphStage, { view: mapView(main.data), size, zoom });
  else if (tab === "session" || tab === "log")
    body = jsx(GraphStage, { view: sessionView(main.data), size, zoom });
  else if (tab === "gantt" || tab === "logGantt")
    body = jsx(SvgStage, { svg: renderGanttSVG(main.data, {}) });
  else if (!events.length)
    body = message("This session has no context event yet, so it has no Venn.");
  else if (venn.isError)
    body = message(errorText(venn.error), true);
  else if (!venn.data)
    body = message("Loading…");
  else {
    const e = events[index];
    body = jsxs("div", {
      children: [
        jsxs("div", {
          className: "sgp-bar",
          children: [
            jsxs("label", {
              children: [
                `Event ${index + 1} of ${events.length}`,
                jsx("input", { type: "range", min: 0, max: events.length - 1, value: index, onChange: (ev) => setAt(Number(ev.target.value)) })
              ]
            }),
            jsx("span", { className: "sgp-note", children: `${e.ev}, turn ${e.turn}` })
          ]
        }),
        jsx(SvgStage, { svg: renderVennSVG(venn.data, { width: Math.min(760, size.w - 24), height: Math.min(560, Math.max(260, size.h - 70)), event: { ev: e.ev, bytes: e.bytes, want: e.want, cap: e.cap } }) })
      ]
    });
  }
  const stamp = main.dataUpdatedAt ? new Date(main.dataUpdatedAt).toLocaleTimeString(undefined, { hour12: false }) + "." + String(main.dataUpdatedAt % 1000).padStart(3, "0") : "";
  return jsxs("div", {
    ref,
    className: "sgp-body",
    "data-sg-updated": main.dataUpdatedAt || 0,
    children: [stamp ? jsx("div", { className: "sgp-stamp", children: `updated ${stamp}` }) : null, body]
  });
}
function Page() {
  const focused = useValue(host.state.focusedStoredSessionId);
  const focusedProfile = useValue(host.state.focusedSessionProfile);
  const [store, setStore] = useState("");
  const [sid, setSid] = useState("");
  const [tab, setTab] = useState("session");
  const [zoom, setZoom] = useState(1);
  const [at, setAt] = useState(null);
  const isLog = LOG_TABS.includes(tab);
  const stores = useQuery({ queryKey: [ID, "stores"], queryFn: () => rest("/stores"), refetchInterval: 60000 });
  const list = stores.data?.stores || [];
  useEffect(() => {
    if (store || !list.length)
      return;
    setStore((list.find((s) => s.active) || list.find((s) => s.store === "default") || list[0]).store);
  }, [list.length]);
  useEffect(() => {
    const want = focusedProfile || "";
    if (want && want !== store && list.some((s) => s.store === want))
      setStore(want);
  }, [focusedProfile, list.length]);
  const graph = useQuery({ queryKey: [ID, "view", `/graph?${qs({ store })}`], queryFn: () => rest(`/graph?${qs({ store })}`), enabled: Boolean(store) && !isLog, refetchInterval: LIST_POLL });
  const logs = useQuery({ queryKey: [ID, "logs", store], queryFn: () => rest(`/log/sessions?${qs({ store, limit: 80 })}`), enabled: Boolean(store) && isLog, refetchInterval: LIST_POLL });
  const sessions = isLog ? logSessions(logs.data) : bcSessions(graph.data);
  const ids = sessions.map((s) => s.id).join(`
`);
  useEffect(() => {
    if (!sessions.length)
      return;
    if (focused && focused !== sid && sessions.some((s) => s.id === focused))
      return setSid(focused);
    if (!sessions.some((s) => s.id === sid))
      setSid(sessions[0].id);
  }, [ids, focused]);
  useEffect(() => setAt(null), [sid, store]);
  const info = TABS.find((t) => t[0] === tab);
  const graphTab = tab === "map" || tab === "session" || tab === "log";
  return jsxs("div", {
    className: "sgp",
    children: [
      jsx("style", { children: CSS }),
      jsxs("div", {
        className: "sgp-bar",
        children: [
          jsx("div", {
            className: "sgp-tabs",
            children: TABS.map(([key, name, tip]) => jsx("button", { type: "button", className: "sgp-tab", "data-on": key === tab ? "1" : "0", title: tip, onClick: () => setTab(key), children: name }, key))
          }),
          jsxs("label", {
            children: [
              "Store",
              jsx("select", {
                value: store,
                onChange: (e) => {
                  setStore(e.target.value);
                  setSid("");
                },
                children: stores.isError ? [jsx("option", { value: "", children: "routes not reachable" }, "x")] : list.map((s) => jsx("option", { value: s.store, children: `${s.store} (${s.sessions})${s.active ? " · this profile" : ""}` }, s.store))
              })
            ]
          }),
          tab === "map" ? null : jsxs("label", {
            children: [
              isLog ? "Session (log)" : "Session",
              jsx("select", {
                value: sid,
                onChange: (e) => setSid(e.target.value),
                children: sessions.map((s) => jsx("option", { value: s.id, children: (s.id === focused ? "● " : "") + label(s) }, s.id))
              })
            ]
          }),
          graphTab ? jsxs("label", {
            children: [
              "Zoom",
              jsx("select", {
                value: String(zoom),
                onChange: (e) => setZoom(Number(e.target.value)),
                children: [1, 1.5, 2, 3, 4].map((z) => jsx("option", { value: String(z), children: z === 1 ? "fit" : `${z}×` }, String(z)))
              })
            ]
          }) : null,
          jsx("span", { className: "sgp-note", children: info ? info[2] : "" })
        ]
      }),
      stores.isError ? message(errorText(stores.error), true) : jsx(View, { tab, store, sid, zoom, at, setAt })
    ]
  });
}
var plugin_default = {
  id: ID,
  name: "SpaceGraph",
  register(ctx) {
    CTX = ctx;
    const contributions = [
      { id: "page", area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(Page, {}) },
      { id: "nav", area: SIDEBAR_NAV_AREA, data: { path: PATH, label: "SpaceGraph", codicon: "type-hierarchy" } },
      {
        id: "open",
        area: PALETTE_AREA,
        data: {
          id: "spacegraph.open",
          label: "SpaceGraph: open the map, graph, Gantt and Venn",
          keywords: ["spacegraph", "breadcrumbs", "graph", "gantt", "venn", "map"],
          run: () => host.navigate(PATH)
        }
      }
    ];
    for (const contribution of contributions)
      ctx.register(contribution);
    const refresh = () => {
      queryClient.invalidateQueries({ queryKey: [ID] });
      ctx.setTimeout(() => void queryClient.invalidateQueries({ queryKey: [ID] }), 1500);
    };
    ctx.onEvent("message.complete", refresh);
    ctx.onEvent("message.error", refresh);
  }
};
export {
  plugin_default as default
};
