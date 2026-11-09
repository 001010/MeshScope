/* Live data-flow animation on the Map and Topology views.
   Every logged packet carries the backend's best guess of the hop-by-hop path it took
   (solid when it matches the hop count / relay hint, dashed when it's only a plausible route).
   New entries arriving with each 1.5 s poll are spread out and animated along that path. */
"use strict";

const PORT_COLORS = {
  TEXT_MESSAGE_APP: ["#f472b6", "Text message"],
  POSITION_APP: ["#60a5fa", "Position"],
  TELEMETRY_APP: ["#a78bfa", "Telemetry"],
  NODEINFO_APP: ["#34d399", "Node info"],
  NEIGHBORINFO_APP: ["#fbbf24", "Neighbor info"],
  TRACEROUTE_APP: ["#fb923c", "Traceroute"],
  ROUTING_APP: ["#94a3b8", "Routing / ACK"],
  other: ["#e2e8f0", "Other"],
};
const TX_PORT = { traceroute: "TRACEROUTE_APP", ping: "ROUTING_APP", nodeinfo: "NODEINFO_APP", position: "POSITION_APP", telemetry: "TELEMETRY_APP" };
const portKey = p => PORT_COLORS[TX_PORT[p] || p] ? (TX_PORT[p] || p) : "other";
const portColor = p => PORT_COLORS[portKey(p)][0];

const LIVE = { seq: null, map: null, layer: null, renderer: null, flows: [], raf: 0, recent: [] };
const HOP_MS = 650, FADE_MS = 1600;

VIEWS.live = {
  mapReady(map) {
    LIVE.map = map;
    LIVE.renderer = L.canvas({ padding: 0.2 });
    LIVE.layer = L.layerGroup().addTo(map);
  },
};

SNAP_HOOKS.push(snap => {
  const top = snap.logSeq;
  if (LIVE.seq == null) { LIVE.seq = top; return; }   // don't replay history on load
  if (top < LIVE.seq) LIVE.seq = 0;                     // state swapped (demo on/off)
  const fresh = snap.log.filter(l => l.seq > LIVE.seq && (l.kind === "rx" || l.kind === "tx"));
  LIVE.seq = top;
  const now = Date.now() / 1000;
  for (const l of fresh) LIVE.recent.push({ t: l.t, mqtt: l.src === "mqtt" });
  LIVE.recent = LIVE.recent.filter(r => now - r.t < 60);
  const where = S.tab === "map" && $("#mapLive").checked ? "map" : S.tab === "topology" && $("#topoLive").checked ? "topo" : null;
  updateLegends();
  if (!where) return;
  const inclMqtt = where === "map" ? $("#liveMqtt").checked : $("#topoLiveMqtt").checked;
  const list = fresh.filter(l => Array.isArray(l.path) && l.path.length >= 2 && (inclMqtt || l.src !== "mqtt")).slice(-40);
  const spread = 1300 / Math.max(1, list.length);
  list.forEach((l, i) => setTimeout(() => spawnFlow(l, where), i * spread));
});

function spawnFlow(l, where) {
  if ((where === "map" && S.tab !== "map") || (where === "topo" && S.tab !== "topology")) return;
  const f = { where, path: l.path, color: portColor(l.port), known: !!l.known, mqtt: l.src === "mqtt", start: performance.now() };
  if (where === "map") {
    if (!LIVE.layer) return;
    const pts = l.path.map(num => S.byNum.get(num)).filter(hasPos).map(n => L.latLng(n.lat, n.lon));
    if (pts.length < 2) return;
    f.pts = pts;
    const opt = { renderer: LIVE.renderer, interactive: false };
    f.trail = L.polyline([pts[0]], { ...opt, color: f.color, weight: 3, opacity: .9, dashArray: f.known ? null : "5 7" }).addTo(LIVE.layer);
    f.dot = L.circleMarker(pts[0], { ...opt, radius: 5, color: "#0b1015", weight: 1.5, fillColor: f.color, fillOpacity: 1 }).addTo(LIVE.layer);
  } else {
    const g = $("#graph g.flows");
    if (!g) return;
    const nums = l.path.filter(num => G.pos.has(num));
    if (nums.length < 2) return;
    f.nums = nums;
    f.g = g;
    const ns = "http://www.w3.org/2000/svg";
    f.trail = document.createElementNS(ns, "polyline");
    f.trail.setAttribute("fill", "none");
    f.trail.setAttribute("stroke", f.color);
    f.trail.setAttribute("stroke-width", "3");
    f.trail.setAttribute("stroke-linecap", "round");
    if (!f.known) f.trail.setAttribute("stroke-dasharray", "5 6");
    f.dot = document.createElementNS(ns, "circle");
    f.dot.setAttribute("r", "5");
    f.dot.setAttribute("fill", f.color);
    f.dot.setAttribute("stroke", "#0b1015");
    f.dot.setAttribute("class", "flowdot");
    g.append(f.trail, f.dot);
  }
  LIVE.flows.push(f);
  if (!LIVE.raf) LIVE.raf = requestAnimationFrame(liveStep);
}

function lerp(a, b, t) { return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t }; }

function liveStep(now) {
  LIVE.raf = 0;
  LIVE.flows = LIVE.flows.filter(f => stepFlow(f, now));
  if (LIVE.flows.length) LIVE.raf = requestAnimationFrame(liveStep);
}

/** advance one flow; returns false when it is finished and removed */
function stepFlow(f, now) {
  const active = f.where === "map" ? S.tab === "map" && $("#mapLive").checked : S.tab === "topology" && $("#topoLive").checked;
  if (f.where === "topo" && !f.dot.isConnected) return false; // graph was rebuilt
  if (!active) { removeFlow(f); return false; }
  const segs = (f.where === "map" ? f.pts.length : f.nums.length) - 1;
  const t = (now - f.start) / HOP_MS;
  const seg = Math.min(Math.floor(t), segs);
  const frac = seg >= segs ? 1 : t - seg;
  const arrived = t >= segs;
  const fade = arrived ? Math.min(1, (now - f.start - segs * HOP_MS) / FADE_MS) : 0;
  if (fade >= 1) { removeFlow(f); return false; }
  if (f.where === "map") {
    const a = f.pts[Math.min(seg, segs - 1)], b = f.pts[Math.min(seg + 1, segs)];
    const k = arrived ? 1 : frac;
    const pos = L.latLng(a.lat + (b.lat - a.lat) * k, a.lng + (b.lng - a.lng) * k);
    f.trail.setLatLngs(f.pts.slice(0, Math.min(seg, segs) + 1).concat(arrived ? [] : [pos]));
    f.trail.setStyle({ opacity: .9 * (1 - fade) });
    f.dot.setLatLng(pos);
    f.dot.setStyle({ radius: arrived ? 5 + fade * 12 : 5, fillOpacity: 1 - fade, opacity: 1 - fade });
  } else {
    const P = f.nums.map(n => G.pos.get(n)).filter(Boolean);
    if (P.length < 2) { removeFlow(f); return false; }
    const s = Math.min(seg, P.length - 2);
    const pos = arrived ? P[P.length - 1] : lerp(P[s], P[s + 1], frac);
    const pts = P.slice(0, s + 1).concat([pos]);
    f.trail.setAttribute("points", pts.map(p => `${p.x},${p.y}`).join(" "));
    f.trail.setAttribute("stroke-opacity", String(.9 * (1 - fade)));
    f.dot.setAttribute("cx", pos.x);
    f.dot.setAttribute("cy", pos.y);
    f.dot.setAttribute("r", String(arrived ? 5 + fade * 12 : 5));
    f.dot.setAttribute("opacity", String(1 - fade));
  }
  return true;
}

function removeFlow(f) {
  if (f.where === "map") { f.trail?.remove(); f.dot?.remove(); }
  else { f.trail?.remove(); f.dot?.remove(); }
}

function legendHtml(inclMqtt) {
  const now = Date.now() / 1000;
  const recent = LIVE.recent.filter(r => now - r.t < 60 && (inclMqtt || !r.mqtt)).length;
  return `<div><b style="margin:0">Live</b><span class="muted">&nbsp;${recent} packets / min</span></div>`
    + Object.values(PORT_COLORS).map(([c, label]) => `<div><s style="--c:${c};width:16px;height:3px"></s>${label}</div>`).join("")
    + `<div class="muted small">solid: path matches the hop count<br>dashed: likely path (guessed)</div>`;
}
function updateLegends() {
  const ml = $("#mapFlowLegend"), tl = $("#topoFlowLegend");
  ml.hidden = !$("#mapLive").checked;
  tl.hidden = !$("#topoLive").checked;
  if (!ml.hidden) ml.innerHTML = legendHtml($("#liveMqtt").checked);
  if (!tl.hidden) tl.innerHTML = legendHtml($("#topoLiveMqtt").checked);
}

(() => {
  const saved = store("live") || {};
  for (const id of ["mapLive", "liveMqtt", "topoLive", "topoLiveMqtt"]) {
    if (saved[id] != null) $("#" + id).checked = saved[id];
    $("#" + id).addEventListener("input", () => {
      store("live", Object.fromEntries(["mapLive", "liveMqtt", "topoLive", "topoLiveMqtt"].map(k => [k, $("#" + k).checked])));
      updateLegends();
    });
  }
  updateLegends();
})();
