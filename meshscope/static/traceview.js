/* Traceroute view: draws a finished traceroute on the Map tab, with arrows along the route
   towards the target in one colour and the route back in another. Each direction is drawn a
   few pixels to the right of its direction of travel, so a link used both ways shows as two
   lanes instead of one line on top of the other. */

const TR = { job: null, layer: null, renderer: null };
const TR_TO = "#38bdf8", TR_BACK = "#e879f9";
const TR_LANE_PX = 5;
const TR_NODE_GAP_PX = 11; // a little more than a node marker's radius

function traceJob(id) {
  return (S.snap?.jobs || []).find(j => j.id === id && j.kind === "traceroute" && j.result);
}

function showTrace(id) {
  const job = traceJob(id);
  if (!job) return;
  TR.job = job;
  setTab("map");
  setTimeout(() => { drawTrace(true); }, 50); // after the map has sized itself
}

function closeTrace() {
  TR.job = null;
  TR.layer?.clearLayers();
  $("#tracePanel").hidden = true;
  $("#map").classList.remove("tracing");
}

function ensureTraceLayer() {
  if (TR.layer) return;
  M.map.createPane("trace").style.zIndex = 450; // above links, below node markers
  TR.renderer = L.svg({ pane: "trace", padding: 0.5 });
  TR.layer = L.layerGroup().addTo(M.map);
  M.map.on("zoomend", () => { if (TR.job) drawTrace(false); });
}

/** consecutive positioned pairs along a leg; hops without a position are bridged (shown dashed) */
function traceSegments(hops) {
  const segs = [];
  let prev = null, skipped = [];
  hops.forEach((h, i) => {
    const n = S.byNum.get(h.num);
    if (!hasPos(n)) { if (prev) skipped.push(h); return; }
    if (prev) segs.push({ a: prev.h, b: h, la: prev.ll, lb: L.latLng(n.lat, n.lon), skipped, hop: i });
    prev = { h, ll: L.latLng(n.lat, n.lon) };
    skipped = [];
  });
  return segs;
}

function arrowIcon(color, deg) {
  return L.divIcon({
    className: "trace-arrow", iconSize: [18, 18], iconAnchor: [9, 9],
    html: `<svg width="18" height="18" viewBox="-9 -9 18 18" style="transform:rotate(${deg}deg)">
      <path d="M-6,-6 L7,0 L-6,6 L-3,0 Z" fill="${color}" stroke="#0b1015" stroke-width="1.5" stroke-linejoin="round"/></svg>`,
  });
}

function drawLeg(hops, color) {
  const map = M.map;
  for (const s of traceSegments(hops)) {
    const pa = map.latLngToLayerPoint(s.la), pb = map.latLngToLayerPoint(s.lb);
    const dx = pb.x - pa.x, dy = pb.y - pa.y, len = Math.hypot(dx, dy) || 1;
    const off = L.point(-dy / len * TR_LANE_PX, dx / len * TR_LANE_PX); // right of travel (screen y points down)
    // stop short of both nodes so the node markers stay easy to hover and click
    const trim = Math.min(TR_NODE_GAP_PX, len / 3), dir = L.point(dx / len * trim, dy / len * trim);
    const a = map.layerPointToLatLng(pa.add(off).add(dir)), b = map.layerPointToLatLng(pb.add(off).subtract(dir));
    const gap = s.skipped.length > 0;
    // nothing on this layer takes the mouse: it's drawn above the nodes, and they must stay hoverable
    // and clickable wherever a route passes (each hop's SNR is listed in the panel instead)
    L.polyline([a, b], { renderer: TR.renderer, color: "#0b1015", weight: 7, opacity: .55, interactive: false }).addTo(TR.layer);
    L.polyline([a, b], { renderer: TR.renderer, color, weight: 4, opacity: .95, dashArray: gap ? "8 7" : null, interactive: false }).addTo(TR.layer);
    const deg = Math.atan2(dy, dx) * 180 / Math.PI;
    const n = len > 220 ? 3 : len > 110 ? 2 : 1; // more arrows on long links
    for (let k = 1; k <= n; k++) {
      const t = k / (n + 1);
      L.marker(L.latLng(a.lat + (b.lat - a.lat) * t, a.lng + (b.lng - a.lng) * t),
        { icon: arrowIcon(color, deg), pane: "trace", interactive: false, keyboard: false }).addTo(TR.layer);
    }
  }
}

function drawTrace(fit) {
  if (!TR.job || !M.map) return;
  ensureTraceLayer();
  TR.layer.clearLayers();
  TR.key = traceKey();
  const { towards, back } = TR.job.result;
  drawLeg(towards, TR_TO);
  if (back) drawLeg(back, TR_BACK);
  $("#map").classList.add("tracing");

  const all = [...towards, ...(back || [])];
  const missing = [...new Map(all.filter(h => !hasPos(S.byNum.get(h.num))).map(h => [h.num, h])).values()];
  const placed = all.map(h => S.byNum.get(h.num)).filter(hasPos);
  const hops = towards.length - 2;
  const p = $("#tracePanel");
  p.innerHTML = `<div class="tp-hd"><b>Traceroute to ${esc(TR.job.targetName)}</b><button class="tp-x" title="Close">×</button></div>
    <div class="muted small">${clock(TR.job.started)} · ${hops === 0 ? "direct" : `${hops} hop${hops === 1 ? "" : "s"} towards`}${back ? ` · ${back.length - 2} back` : ""}</div>
    <div class="tp-key"><s style="--c:${TR_TO}"></s>towards ${esc(TR.job.targetName)}</div>
    <div class="tp-key"><s style="--c:${TR_BACK}"></s>${back ? "route back" : "route back not reported by this firmware"}</div>
    ${routeHtml("to", towards)}${routeHtml("back", back)}
    ${missing.length ? `<div class="muted small">Not on the map (no position): ${missing.map(h => esc(h.name)).join(", ")}. Dashed lines skip them.</div>` : ""}
    ${new Set(placed.map(n => n.num)).size < 2 ? `<div class="muted small">Too few nodes on this route have a position to draw it.</div>` : ""}`;
  p.hidden = false;
  if (fit && placed.length >= 2) {
    // keep the route clear of the panel in the top-left corner: beside it on a wide map, below it otherwise
    const sz = M.map.getSize(), beside = sz.x - p.offsetWidth > 420;
    M.map.fitBounds(L.latLngBounds(placed.map(n => [n.lat, n.lon])), {
      paddingTopLeft: beside ? [p.offsetLeft + p.offsetWidth + 40, 50] : [50, p.offsetTop + p.offsetHeight + 40],
      paddingBottomRight: [50, 50], maxZoom: 15 });
  }
}

document.addEventListener("click", e => {
  const v = e.target.closest("[data-trace]");
  if (v) { e.stopPropagation(); showTrace(+v.dataset.trace); return; }
  if (e.target.closest("#tracePanel .tp-x")) closeTrace();
}, true);

/* keep it current: positions and names can change while it's shown */
SNAP_HOOKS.push(() => {
  if (!TR.job) return;
  const j = traceJob(TR.job.id);
  if (!j) { closeTrace(); return; }
  TR.job = j;
  if (S.tab === "map" && traceKey() !== TR.key) drawTrace(false);
});

function traceKey() {
  const r = TR.job.result;
  return JSON.stringify([TR.job.id, ...[...r.towards, ...(r.back || [])].map(h => {
    const n = S.byNum.get(h.num);
    return [h.num, h.name, n?.lat, n?.lon];
  })]);
}
