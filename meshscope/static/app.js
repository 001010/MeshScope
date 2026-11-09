/* MeshScope UI - core: polling, nodes, drawer, map, topology, reach, activity.
   Feature modules (live.js, mynodes.js, messages.js, config.js, console.js, addons.js, mqtt.js)
   register themselves in VIEWS / SNAP_HOOKS before boot() runs. */
"use strict";

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const BCAST = 0xffffffff;

const S = {
  snap: null, version: -1, byNum: new Map(), tab: "nodes", selected: null,
  sort: { key: "lastHeard", dir: -1 },
  reach: null, reachKey: "", reachTimer: null, info: null,
};
/** name -> { show(), hide(), render() } for tabs implemented in other files */
const VIEWS = {};
/** fn(snap) called after every new snapshot, whatever tab is open */
const SNAP_HOOKS = [];

/* ---------------------------------------------------------------- helpers */
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function api(path, body) {
  const opt = { headers: { "X-MeshScope-Token": TOKEN } };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
/** GET an attachment (with the auth header) and hand it to the browser as a download */
async function download(path) {
  const r = await fetch(path, { headers: { "X-MeshScope-Token": TOKEN } });
  if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || r.statusText); }
  const cd = r.headers.get("Content-Disposition") || "";
  const m = /filename\*=UTF-8''([^;]+)/.exec(cd);
  const name = m ? decodeURIComponent(m[1]) : "download";
  const url = URL.createObjectURL(await r.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  return name;
}
/** read File objects as {name, data: base64} */
function readFiles(files) {
  return Promise.all([...files].map(f => new Promise((ok, fail) => {
    const rd = new FileReader();
    rd.onload = () => ok({ name: f.name, data: String(rd.result).split(",", 2)[1] || "" });
    rd.onerror = () => fail(rd.error);
    rd.readAsDataURL(f);
  })));
}
function toast(msg, ok = false) {
  const el = document.createElement("div");
  el.className = "toast" + (ok ? " ok" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}
function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function clock(ts) { return new Date(ts * 1000).toLocaleTimeString(); }
function hav(a, b, c, d) {
  const R = 6371.0088, r = Math.PI / 180;
  const x = Math.sin((c - a) * r / 2) ** 2 + Math.cos(a * r) * Math.cos(c * r) * Math.sin((d - b) * r / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(x)));
}
const hasPos = n => n && n.lat != null && n.lon != null;
const hexId = num => "!" + (num >>> 0).toString(16).padStart(8, "0");
const nodeName = num => {
  if (num === BCAST) return "everyone";
  const n = S.byNum.get(num);
  return n ? (n.longName || n.shortName || n.id) : hexId(num);
};
const shortName = n => n.shortName || n.id.slice(-4);
const shortOf = num => { const n = S.byNum.get(num); return n ? shortName(n) : hexId(num).slice(-4); };
function hopColor(n) {
  if (!n) return "var(--hx)";
  if (n.isLocal) return "var(--local)";
  const h = n.hopsAway;
  if (h == null) return "var(--hx)";
  return ["var(--h0)", "var(--h1)", "var(--h2)"][h] || "var(--h3)";
}
const CSSV = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
function hopHex(n) { const c = hopColor(n); return c.startsWith("var(") ? CSSV(c.slice(4, -1)) : c; }
function snrColor(snr) {
  if (snr == null) return "#64748b";
  if (snr >= 5) return "#22c55e";
  if (snr >= 0) return "#a3e635";
  if (snr >= -7) return "#eab308";
  if (snr >= -13) return "#f97316";
  return "#ef4444";
}
/** darken (f<0) or lighten (f>0) a #rrggbb colour */
function shade(hex, f) {
  const n = parseInt(hex.slice(1), 16);
  const ch = [n >> 16, (n >> 8) & 255, n & 255].map(c => Math.round(f < 0 ? c * (1 + f) : c + (255 - c) * f));
  return "#" + ch.map(c => c.toString(16).padStart(2, "0")).join("");
}
const REACH_COLORS = ["#60a5fa", "#22c55e", "#84cc16", "#eab308", "#f97316", "#ef4444", "#d946ef", "#a855f7"];
const edgeSnr = e => {
  const v = [e.snrAB, e.snrBA].filter(x => x != null);
  return v.length ? Math.min(...v) : null;
};
const fmtSnr = v => v == null ? "–" : `${v.toFixed(1)} dB`;
const localNode = () => S.snap && S.byNum.get(S.snap.localNum);
const connected = () => S.snap && S.snap.status === "connected";
const realRadio = () => connected() && S.snap.port !== "DEMO";
function store(key, val) {
  try {
    if (val === undefined) return JSON.parse(localStorage.getItem("ms." + key) || "null");
    localStorage.setItem("ms." + key, JSON.stringify(val));
  } catch { return null; }
}

/* ---------------------------------------------------------------- polling */
async function poll() {
  try {
    const d = await api(`/api/state?since=${S.version}`);
    if (!d.unchanged) {
      S.snap = d;
      S.version = d.version;
      S.byNum = new Map(d.nodes.map(n => [n.num, n]));
      renderAll();
      if (!S.booted) {
        S.booted = true;
        const [t, sub] = decodeURIComponent(location.hash.slice(1)).split("/");
        if (t && $("#view-" + t)) { VIEWS[t]?.deep?.(sub); setTab(t); }
      }
    }
  } catch (e) {
    $("#statusPill").textContent = "App offline";
    $("#statusPill").className = "pill error";
  }
  setTimeout(poll, 1500);
}

function renderAll() {
  renderHeader();
  if (S.tab === "nodes") renderNodes();
  if (S.tab === "map") renderMap();
  if (S.tab === "topology") renderGraph();
  if (S.tab === "reach") scheduleReach();
  if (S.tab === "activity") renderActivity();
  VIEWS[S.tab]?.render?.();
  renderDrawer();
  const pending = S.snap.jobs.filter(j => j.status === "pending" || j.status === "relayed").length;
  const b = $("#pendingBadge");
  b.hidden = !pending;
  b.textContent = pending;
  for (const f of SNAP_HOOKS) {
    try { f(S.snap); } catch (e) { console.error(e); }
  }
}

/* ----------------------------------------------------------- connection UI */
async function loadPorts() {
  try {
    const { ports, myNodes } = await api("/api/mynodes");
    const sel = $("#portSelect");
    const prev = sel.value;
    let html = "";
    if (myNodes.length) {
      html += `<optgroup label="My Nodes">` + myNodes.map(m => {
        const nm = m.nickname || m.longName || m.id;
        return `<option value="my:${m.num}" ${m.presentPort ? "" : "disabled"}>★ ${esc(nm)} - ${m.presentPort ? esc(m.presentPort) : "not plugged in"}</option>`;
      }).join("") + `</optgroup>`;
    }
    html += ports.length
      ? `<optgroup label="USB serial ports">` + ports.map(p => `<option value="${esc(p.device)}">${esc(p.device)} - ${esc(p.chip || p.description || "")}${p.likelyMeshtastic ? " ●" : ""}</option>`).join("") + `</optgroup>`
      : `<option value="">No USB serial ports found</option>`;
    sel.innerHTML = html;
    const opts = $$("option", sel).filter(o => !o.disabled);
    if (prev && opts.some(o => o.value === prev)) sel.value = prev;
    else if (opts.length) sel.value = (opts.find(o => o.value.startsWith("my:")) || opts[0]).value;
  } catch (e) { toast("Could not list ports: " + e.message); }
}
function renderHeader() {
  const s = S.snap;
  const pill = $("#statusPill");
  const ln = localNode();
  pill.className = "pill " + s.status;
  pill.textContent = {
    connected: s.port === "DEMO" ? "Demo mesh" : `Connected · ${ln ? shortName(ln) : ""} · ${s.port}`,
    connecting: "Connecting…", error: "Error", disconnected: "Disconnected",
  }[s.status] || s.status;
  const busy = s.status === "connected" || s.status === "connecting";
  $("#connectBtn").hidden = busy;
  $("#demoBtn").hidden = busy;
  $("#portSelect").hidden = busy;
  $("#refreshPorts").hidden = busy;
  $("#disconnectBtn").hidden = !busy;
  const det = $("#statusDetail");
  det.hidden = !(s.statusDetail && s.status !== "connected");
  det.className = "status-detail" + (s.status === "connecting" ? " info" : "");
  det.textContent = s.statusDetail;
  if (S.prevStatus !== s.status) {
    if (S.prevStatus && !busy) loadPorts();
    S.prevStatus = s.status;
  }
}

/* --------------------------------------------------------------- actions */
async function act(kind, num) {
  try {
    const hl = parseInt($("#drawerHops")?.value || "0", 10) || undefined;
    await api("/api/action", { kind, node: num, hopLimit: S.selected === num ? hl : undefined });
    toast(`${kind} sent to ${nodeName(num)}`, true);
  } catch (e) { toast(e.message); }
}
async function toggleFavorite(num) {
  const n = S.byNum.get(num);
  if (!n) return;
  n.favorite = !n.favorite; // optimistic
  renderAll();
  try { await api("/api/favorite", { node: num, favorite: n.favorite }); } catch (e) { toast(e.message); }
}
function actionButtons(num, compact = true) {
  const dis = connected() && num !== S.snap.localNum ? "" : "disabled";
  if (compact) {
    return `<div class="acts">
      <button ${dis} data-act="traceroute" data-num="${num}" title="Traceroute">Trace</button>
      <button ${dis} data-act="ping" data-num="${num}" title="Ping (want-ack round trip)">Ping</button>
      <button ${dis} data-act="nodeinfo" data-num="${num}" title="Request node info">Info</button></div>`;
  }
  return `<div class="actions">
    <button ${dis} data-act="traceroute" data-num="${num}">Traceroute</button>
    <button ${dis} data-act="ping" data-num="${num}">Ping</button>
    <button ${dis} data-act="nodeinfo" data-num="${num}">Request node info</button>
    <button ${dis} data-act="position" data-num="${num}">Request position</button>
    <button ${dis} data-act="telemetry" data-num="${num}">Request telemetry</button>
    <button data-reach="${num}">Show reach</button>
    <button class="wide" data-dm="${num}">Send direct message</button>
    </div>`;
}
document.addEventListener("click", e => {
  const a = e.target.closest("[data-act]");
  if (a) { e.stopPropagation(); if (!a.disabled) act(a.dataset.act, +a.dataset.num); return; }
  const f = e.target.closest("[data-fav]");
  if (f) { e.stopPropagation(); toggleFavorite(+f.dataset.fav); return; }
  const r = e.target.closest("[data-reach]");
  if (r) { e.stopPropagation(); showReach(+r.dataset.reach); return; }
  const dm = e.target.closest("[data-dm]");
  if (dm) { e.stopPropagation(); VIEWS.messages?.openDm?.(+dm.dataset.dm); return; }
  const sel = e.target.closest("[data-select]");
  if (sel) { e.stopPropagation(); selectNode(+sel.dataset.select); }
}, true);

/* ---------------------------------------------------------------- tabs */
function setTab(t) {
  const prev = S.tab;
  S.tab = t;
  $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === t));
  $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + t));
  if (prev !== t) VIEWS[prev]?.hide?.();
  history.replaceState(null, "", location.search + "#" + t);
  VIEWS[t]?.show?.();
  if (!S.snap) return;
  if (t === "map") { ensureMap(); setTimeout(() => { M.map.invalidateSize(); renderMap(); }, 0); }
  if (t === "topology") renderGraph(true);
  if (t === "reach") { ensureReachMap(); setTimeout(() => { R.map.invalidateSize(); fillReachOrigins(); scheduleReach(0); }, 0); }
  if (t === "activity") renderActivity();
  if (t === "nodes") renderNodes();
  VIEWS[t]?.render?.();
}
$$("#tabs button").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));

function selectNode(num) {
  S.selected = num;
  renderDrawer();
  if (S.tab === "nodes") renderNodes();
  if (S.tab === "topology") G.highlight();
}

/* ---------------------------------------------------------------- nodes */
function visibleNodes() {
  const q = $("#nodeSearch").value.trim().toLowerCase();
  const age = +$("#nodeAge").value;
  const hideMqtt = $("#hideMqtt").checked;
  const favOnly = $("#favOnly").checked;
  const now = Date.now() / 1000;
  return S.snap.nodes.filter(n => {
    if (n.isLocal) return true;
    if (favOnly && !n.favorite) return false;
    if (age && !n.favorite && (!n.lastHeard || now - n.lastHeard > age)) return false;
    if (hideMqtt && n.viaMqtt) return false;
    if (q && ![n.longName, n.shortName, n.id, n.hwModel, n.role].some(v => v && String(v).toLowerCase().includes(q))) return false;
    return true;
  });
}
function renderNodes() {
  const ln = localNode();
  const rows = visibleNodes().map(n => ({
    ...n, name: n.longName || n.shortName || n.id,
    dist: hasPos(n) && hasPos(ln) && !n.isLocal ? hav(ln.lat, ln.lon, n.lat, n.lon) : null,
  }));
  const { key, dir } = S.sort;
  const favFirst = $("#favFirst").checked;
  rows.sort((a, b) => {
    if (a.isLocal !== b.isLocal) return a.isLocal ? -1 : 1;
    if (favFirst && a.favorite !== b.favorite) return a.favorite ? -1 : 1;
    const x = a[key], y = b[key];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir;
  });
  $$("#nodeTable th").forEach(th => {
    th.classList.toggle("sorted", th.dataset.sort === key);
    th.classList.toggle("asc", th.dataset.sort === key && dir === 1);
  });
  $("#nodeTable tbody").innerHTML = rows.map(n => `
    <tr data-select="${n.num}" class="${S.selected === n.num ? "selected" : ""}${n.favorite ? " favrow" : ""}">
      <td class="fav">${n.isLocal ? "" : `<button class="star${n.favorite ? " on" : ""}" data-fav="${n.num}" title="${n.favorite ? "Remove from favorites" : "Add to favorites"}">${n.favorite ? "★" : "☆"}</button>`}</td>
      <td><span class="short" style="--c:${hopColor(n)}">${esc(shortName(n))}</span>${esc(n.name)}${n.isLocal ? '<span class="tag local">this radio</span>' : ""}${n.viaMqtt ? '<span class="tag mqtt" title="The radio heard this node through its own MQTT link">MQTT</span>' : ""}</td>
      <td class="muted">${esc(n.id)}</td>
      <td class="num">${n.isLocal ? "–" : (n.hopsAway ?? "?")}</td>
      <td class="num" style="color:${n.isLocal ? "" : snrColor(n.hopsAway === 0 ? n.snr : null)}">${n.isLocal || n.hopsAway !== 0 ? "–" : fmtSnr(n.snr)}</td>
      <td>${n.isLocal ? "now" : ago(n.lastHeard)}</td>
      <td class="num">${n.dist == null ? "–" : n.dist.toFixed(1) + " km"}</td>
      <td class="num">${n.battery == null ? "–" : n.battery > 100 ? "powered" : n.battery + "%"}</td>
      <td class="muted">${esc(n.hwModel || "")}</td>
      <td class="muted">${esc(n.role || "")}</td>
      <td>${n.isLocal ? "" : actionButtons(n.num)}</td>
    </tr>`).join("");
  $("#nodesEmpty").hidden = S.snap.nodes.length > 0;
  const favs = S.snap.nodes.filter(n => n.favorite).length;
  $("#nodeCount").textContent = `${rows.length} of ${S.snap.nodes.length} nodes${favs ? ` · ${favs} favorite${favs > 1 ? "s" : ""}` : ""}`;
}
$$("#nodeTable th[data-sort]").forEach(th => th.addEventListener("click", () => {
  const k = th.dataset.sort;
  S.sort = S.sort.key === k ? { key: k, dir: -S.sort.dir } : { key: k, dir: ["name", "id", "hwModel", "role"].includes(k) ? 1 : -1 };
  renderNodes();
}));
["#nodeSearch", "#nodeAge", "#hideMqtt", "#favFirst", "#favOnly"].forEach(s => $(s).addEventListener("input", () => {
  store("nodeFilters", { favFirst: $("#favFirst").checked, favOnly: $("#favOnly").checked, hideMqtt: $("#hideMqtt").checked, age: $("#nodeAge").value });
  renderNodes();
}));
(() => {
  const f = store("nodeFilters");
  if (!f) return;
  $("#favFirst").checked = f.favFirst; $("#favOnly").checked = f.favOnly; $("#hideMqtt").checked = f.hideMqtt;
  if (f.age != null) $("#nodeAge").value = f.age;
})();

/* ---------------------------------------------------------------- drawer */
function renderDrawer() {
  const d = $("#drawer");
  const n = S.byNum.get(S.selected);
  if (!n) { d.hidden = true; return; }
  d.hidden = false;
  const ae = document.activeElement;
  if (ae && (ae.id === "drawerHops" || ae.id === "drawerAnt")) return; // don't clobber an open control
  const ln = localNode();
  const dist = hasPos(n) && hasPos(ln) && !n.isLocal ? hav(ln.lat, ln.lon, n.lat, n.lon) : null;
  const links = S.snap.edges.filter(e => e.a === n.num || e.b === n.num)
    .map(e => ({ other: e.a === n.num ? e.b : e.a, snr: edgeSnr(e), e }))
    .sort((x, y) => (y.snr ?? -99) - (x.snr ?? -99));
  const jobs = S.snap.jobs.filter(j => j.target === n.num).slice(0, 6);
  const prevHops = $("#drawerHops")?.value;
  const kv = [
    ["ID", n.id], ["Node number", n.num], ["Hardware", n.hwModel], ["Role", n.role],
    ["Hops away", n.isLocal ? "0 (this radio)" : n.hopsAway],
    ["SNR / RSSI", n.hopsAway === 0 && !n.isLocal ? `${fmtSnr(n.snr)}${n.rssi != null ? ` / ${n.rssi} dBm` : ""}` : null],
    ["Last heard", n.isLocal ? "now" : ago(n.lastHeard)],
    ["Heard via MQTT", n.lastHeardMqtt ? ago(n.lastHeardMqtt) : null],
    ["Position", hasPos(n) ? `${n.lat.toFixed(5)}, ${n.lon.toFixed(5)}${n.alt != null ? ` · ${n.alt} m` : ""}` : "unknown"],
    ["Distance", dist == null ? null : `${dist.toFixed(2)} km from this radio`],
    ["Battery", n.battery == null ? null : n.battery > 100 ? `external power${n.voltage ? ` (${n.voltage.toFixed(2)} V)` : ""}` : `${n.battery}%${n.voltage ? ` (${n.voltage.toFixed(2)} V)` : ""}`],
    ["Channel util", n.chUtil == null ? null : `${n.chUtil.toFixed(1)}%`],
    ["Air util TX", n.airUtilTx == null ? null : `${n.airUtilTx.toFixed(2)}%`],
    ["Uptime", n.uptime == null ? null : `${(n.uptime / 3600).toFixed(1)} h`],
    ["Via MQTT", n.viaMqtt ? "yes" : null],
  ].filter(([, v]) => v != null && v !== "");
  const defH = S.snap.settings?.defaultAntennaHeight ?? 2;
  $("#drawerBody").innerHTML = `
    <h2>${n.isLocal ? "" : `<button class="star${n.favorite ? " on" : ""}" data-fav="${n.num}" title="Favorite">${n.favorite ? "★" : "☆"}</button>`}<span class="short" style="--c:${hopColor(n)}">${esc(shortName(n))}</span>${esc(n.longName || n.id)}</h2>
    <dl class="kv">${kv.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    ${n.isLocal ? "" : `<label>Hop limit
      <select id="drawerHops">${[1, 2, 3, 4, 5, 6, 7].map(h => `<option ${h === (+prevHops || S.snap.hopLimit) ? "selected" : ""}>${h}</option>`).join("")}</select></label>`}
    ${n.isLocal ? `<div class="actions"><button class="wide" data-reach="${n.num}">Show reach</button></div>` : actionButtons(n.num, false)}
    <label class="field ant">Antenna height above ground (m), used by the coverage heatmap
      <input type="number" id="drawerAnt" min="0.1" max="500" step="0.5" placeholder="${defH} (default)" value="${n.antennaHeight ?? ""}"></label>
    <section><h3>Radio links (${links.length})</h3>
      ${links.length ? links.map(l => `<div class="nb" data-select="${l.other}">
        <span>${esc(nodeName(l.other))}</span>
        <span style="color:${snrColor(l.snr)}">${fmtSnr(l.snr)}${l.e.distanceKm != null ? ` · ${l.e.distanceKm.toFixed(1)} km` : ""}</span></div>`).join("")
        : `<p class="muted small">No direct links observed yet. Run a traceroute, or wait for NeighborInfo broadcasts.</p>`}
    </section>
    ${jobs.length ? `<section><h3>Recent requests</h3>${jobs.map(jobHtml).join("")}</section>` : ""}
    ${n.isLocal ? "" : `<section><button class="danger" id="forgetBtn">${S.forgetArmed === n.num ? "Click again to forget this node" : "Forget this node"}</button></section>`}`;
  $("#drawerAnt").onchange = async e => {
    try {
      await api("/api/node-settings", { node: n.num, antennaHeight: e.target.value });
      toast(e.target.value ? `Antenna height set to ${e.target.value} m` : "Antenna height reset to default", true);
    } catch (err) { toast(err.message); }
  };
  const f = $("#forgetBtn");
  if (f) f.onclick = async () => {
    if (S.forgetArmed === n.num) {
      S.forgetArmed = null;
      await api("/api/forget", { node: n.num }).catch(e => toast(e.message));
      S.selected = null;
      renderDrawer();
    } else {
      S.forgetArmed = n.num;
      f.textContent = "Click again to forget this node";
      setTimeout(() => { if (S.forgetArmed === n.num) { S.forgetArmed = null; renderDrawer(); } }, 5000);
    }
  };
}
$("#drawerClose").onclick = () => { S.selected = null; renderDrawer(); if (S.tab === "nodes") renderNodes(); if (S.tab === "topology") G.highlight(); };

/* ---------------------------------------------------------------- map */
const M = { map: null, markers: new Map(), edges: null, fitted: false };
function baseLayers() {
  return {
    "Street": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" }),
    "Topographic": L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", { maxZoom: 17, attribution: "&copy; OpenStreetMap, SRTM | &copy; OpenTopoMap (CC-BY-SA)" }),
    "Satellite": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom: 19, attribution: "Tiles &copy; Esri" }),
  };
}
function ensureMap() {
  if (M.map) return;
  const layers = baseLayers();
  M.map = L.map("map", { preferCanvas: true, zoomControl: true }).setView([20, 0], 2);
  layers["Street"].addTo(M.map);
  L.control.layers(layers).addTo(M.map);
  L.control.scale({ imperial: true }).addTo(M.map);
  M.edges = L.layerGroup().addTo(M.map);
  M.map.on("click", () => { if (B.popup && M.map.hasLayer(B.popup)) M.map.closePopup(B.popup); });
  VIEWS.live?.mapReady?.(M.map);
}

/* Info bubble: opens on hover; moving into it keeps it open (so its buttons can be used);
   clicking the node or the bubble pins it until it's closed with × or a click on the map. */
const B = { popup: null, num: null, pinned: false, timer: 0, html: "" };
function ensureBubble() {
  if (B.popup) return;
  B.popup = L.popup({ className: "bubble", autoPan: false, closeButton: true, closeOnClick: false, autoClose: false,
    offset: [0, -4], maxWidth: 340, minWidth: 220 });
  B.popup.on("add", () => {
    const el = B.popup.getElement();
    if (!el || el._wired) return;
    el._wired = true;
    el.addEventListener("mouseenter", () => clearTimeout(B.timer));
    el.addEventListener("mouseleave", () => { if (!B.pinned) scheduleBubbleClose(); });
    el.addEventListener("click", e => { if (!e.target.closest(".leaflet-popup-close-button")) pinBubble(true); });
    L.DomEvent.disableClickPropagation(el);
    L.DomEvent.disableScrollPropagation(el);
  });
  B.popup.on("remove", () => { B.num = null; B.pinned = false; B.html = ""; });
}
function bubbleHtml(n) {
  const ln = localNode();
  const dist = hasPos(ln) && !n.isLocal ? hav(ln.lat, ln.lon, n.lat, n.lon) : null;
  const rows = [
    ["Hops", n.isLocal ? "this radio" : (n.hopsAway ?? "?")],
    ["Heard", n.isLocal ? "now" : ago(n.lastHeard)],
    ["SNR", !n.isLocal && n.hopsAway === 0 ? fmtSnr(n.snr) : null],
    ["Battery", n.battery == null ? null : n.battery > 100 ? "powered" : n.battery + "%"],
    ["Distance", dist == null ? null : dist.toFixed(2) + " km"],
    ["Role", n.role],
    ["Links", S.snap.edges.filter(e => e.a === n.num || e.b === n.num).length],
  ].filter(([, v]) => v != null && v !== "");
  return `<div class="pop"><h4>${n.isLocal ? "" : `<button class="star${n.favorite ? " on" : ""}" data-fav="${n.num}">${n.favorite ? "★" : "☆"}</button>`}${esc(n.longName || n.id)}</h4>
    <div class="muted">${esc(n.id)} · ${esc(n.hwModel || "")}</div>
    <div class="kv2">${rows.map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join("")}</div>
    ${n.isLocal ? "" : actionButtons(n.num)}
    <div class="acts"><button data-select="${n.num}">Details</button><button data-reach="${n.num}">Reach</button>${n.isLocal ? "" : `<button data-dm="${n.num}">Message</button>`}</div>
    <div class="pinhint">${B.pinned ? "Pinned · press × or click the map to close" : "Click the bubble to keep it open"}</div></div>`;
}
function openBubble(num, pin = false) {
  const n = S.byNum.get(num);
  if (!hasPos(n) || !M.map) return;
  ensureBubble();
  clearTimeout(B.timer);
  if (B.pinned && B.num !== num && !pin) return; // a pinned bubble stays put while hovering others
  const wasPinned = B.pinned && B.num === num;
  B.num = num;
  B.popup.setLatLng([n.lat, n.lon]);
  if (!M.map.hasLayer(B.popup)) B.popup.openOn(M.map);
  pinBubble(pin || wasPinned);
}
function pinBubble(p) {
  B.pinned = p;
  B.popup.getElement()?.classList.toggle("pinned", p);
  refreshBubble(true);
}
function refreshBubble(force = false) {
  if (!B.popup || B.num == null || !M.map?.hasLayer(B.popup)) return;
  const n = S.byNum.get(B.num);
  if (!hasPos(n)) { M.map.closePopup(B.popup); return; }
  B.popup.setLatLng([n.lat, n.lon]);
  const html = bubbleHtml(n);
  if (force || html !== B.html) { B.html = html; B.popup.setContent(html); }
}
function scheduleBubbleClose() {
  clearTimeout(B.timer);
  B.timer = setTimeout(() => { if (!B.pinned && B.popup && M.map.hasLayer(B.popup)) M.map.closePopup(B.popup); }, 450);
}

function renderMap() {
  if (!M.map) return;
  const age = +$("#mapLinkAge").value;
  const now = Date.now() / 1000;
  const nodes = S.snap.nodes.filter(hasPos);
  const seen = new Set();
  const labels = $("#mapLabels").checked;
  for (const n of nodes) {
    seen.add(n.num);
    let m = M.markers.get(n.num);
    const style = { radius: n.isLocal ? 9 : 7, color: n.favorite ? "#fbbf24" : "#0b1015", weight: n.favorite ? 3 : 2, fillColor: hopHex(n), fillOpacity: .95 };
    if (!m) {
      const num = n.num;
      m = L.circleMarker([n.lat, n.lon], style).addTo(M.map);
      m.on("mouseover", () => openBubble(num));
      m.on("mouseout", () => { if (!B.pinned) scheduleBubbleClose(); });
      m.on("click", e => { L.DomEvent.stopPropagation(e); openBubble(num, true); });
      M.markers.set(n.num, m);
    } else {
      m.setLatLng([n.lat, n.lon]);
      m.setStyle(style);
    }
    const lbl = labels ? shortName(n) : "";
    if (m._lbl !== lbl) {
      m.unbindTooltip();
      if (lbl) m.bindTooltip(esc(lbl), { permanent: true, direction: "right", offset: [7, 0], className: "lbl", interactive: false });
      m._lbl = lbl;
    }
    m.bringToFront();
  }
  for (const [num, m] of M.markers) if (!seen.has(num)) { m.remove(); M.markers.delete(num); }

  M.edges.clearLayers();
  if ($("#mapLinks").checked) {
    for (const e of S.snap.edges) {
      if (age && now - e.lastSeen > age) continue;
      const a = S.byNum.get(e.a), b = S.byNum.get(e.b);
      if (!hasPos(a) || !hasPos(b)) continue;
      const snr = edgeSnr(e);
      L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
        color: snrColor(snr), weight: 2 + Math.min(3, Math.log2(1 + e.count) / 2), opacity: .75,
        dashArray: e.sources.every(s => s === "relay") ? "4 6" : null,
      }).bindTooltip(`${esc(nodeName(e.a))} ⇄ ${esc(nodeName(e.b))}<br>SNR ${fmtSnr(e.snrAB)} / ${fmtSnr(e.snrBA)}`
        + `${e.distanceKm != null ? `<br>${e.distanceKm.toFixed(2)} km` : ""}<br>seen ${ago(e.lastSeen)} via ${esc(e.sources.join(", "))}`, { sticky: true })
        .addTo(M.edges);
    }
  }
  for (const m of M.markers.values()) m.bringToFront();
  refreshBubble();
  const missing = S.snap.nodes.length - nodes.length;
  const note = $("#mapNote");
  note.hidden = !(S.snap.nodes.length && !nodes.length);
  note.textContent = "None of the known nodes have reported a position yet. Use “Request position” or see the Topology tab.";
  if (!M.fitted && nodes.length) { fitMap(); M.fitted = true; }
  $("#mapFit").title = missing ? `${missing} node(s) have no position and are not shown` : "";
}
function fitMap() {
  const pts = S.snap.nodes.filter(hasPos).map(n => [n.lat, n.lon]);
  if (pts.length === 1) M.map.setView(pts[0], 12);
  else if (pts.length) M.map.fitBounds(pts, { padding: [40, 40], maxZoom: 14 });
}
$("#mapFit").onclick = fitMap;
["#mapLinks", "#mapLabels", "#mapLinkAge"].forEach(s => $(s).addEventListener("input", renderMap));

/* ---------------------------------------------------------------- topology */
const G = {
  pos: new Map(), vel: new Map(), alpha: 0, sig: "", raf: 0,
  view: { x: -500, y: -350, w: 1000, h: 700 }, drag: null,
  highlight() {
    $$("#graph .gnode").forEach(g => g.classList.toggle("sel", +g.dataset.num === S.selected));
  },
};
function graphData() {
  const age = +$("#topoLinkAge").value;
  const now = Date.now() / 1000;
  const edges = S.snap.edges.filter(e => !age || now - e.lastSeen <= age);
  const linked = new Set(edges.flatMap(e => [e.a, e.b]));
  const nodes = S.snap.nodes.filter(n => n.isLocal || linked.has(n.num) || $("#topoIsolated").checked);
  const ids = new Set(nodes.map(n => n.num));
  return { nodes, edges: edges.filter(e => ids.has(e.a) && ids.has(e.b)) };
}
function renderGraph(force = false) {
  const svg = $("#graph");
  const { nodes, edges } = graphData();
  const sig = nodes.map(n => n.num + (n.favorite ? "f" : "")).join(",") + "|" + edges.map(e => e.a + "-" + e.b).join(",");
  const local = S.snap.localNum;
  const structural = sig !== G.sig;
  for (const n of nodes) {
    if (!G.pos.has(n.num)) {
      // seed new nodes next to an already placed neighbour so the layout stays stable
      const nb = edges.find(e => (e.a === n.num && G.pos.has(e.b)) || (e.b === n.num && G.pos.has(e.a)));
      const base = nb ? G.pos.get(nb.a === n.num ? nb.b : nb.a) : { x: 0, y: 0 };
      G.pos.set(n.num, n.num === local ? { x: 0, y: 0 } : { x: base.x + (Math.random() - .5) * 120, y: base.y + (Math.random() - .5) * 120 });
      G.vel.set(n.num, { x: 0, y: 0 });
    }
  }
  if (structural || force) {
    G.sig = sig;
    svg.innerHTML = `<g class="edges">${edges.map(e => {
      const snr = edgeSnr(e);
      return `<line class="edge" data-a="${e.a}" data-b="${e.b}" stroke="${snrColor(snr)}" stroke-opacity=".8" stroke-width="${1.5 + Math.min(3, Math.log2(1 + e.count) / 2)}"
        ${e.sources.every(s => s === "relay") ? 'stroke-dasharray="4 5"' : ""}><title>${esc(nodeName(e.a))} ⇄ ${esc(nodeName(e.b))}: SNR ${fmtSnr(e.snrAB)} / ${fmtSnr(e.snrBA)} (${esc(e.sources.join(", "))})</title></line>`;
    }).join("")}</g><g class="flows"></g><g class="nodes">${nodes.map(n => `
      <g class="gnode${n.favorite ? " fav" : ""}" data-num="${n.num}"><circle r="${n.isLocal ? 11 : 8}" fill="${hopHex(n)}" stroke="${n.favorite ? "#fbbf24" : "#0b1015"}" stroke-width="${n.favorite ? 3 : 2}"></circle>
      <text x="12" y="4">${esc(shortName(n))}</text><title>${esc(n.longName || n.id)}</title></g>`).join("")}</g>`;
    if (structural) G.alpha = Math.max(G.alpha, force && !G.raf ? 1 : .6);
    G.nodes = nodes; G.edges = edges;
    G.highlight();
    startSim();
  }
  applyView();
  drawGraph();
}
function startSim() { if (!G.raf) G.raf = requestAnimationFrame(simStep); }
function simStep() {
  G.raf = 0;
  if (S.tab !== "topology" || G.alpha < 0.01) { drawGraph(); return; }
  const nodes = G.nodes, P = G.pos, V = G.vel, local = S.snap.localNum;
  for (let i = 0; i < nodes.length; i++) {
    const a = P.get(nodes[i].num), va = V.get(nodes[i].num);
    for (let j = i + 1; j < nodes.length; j++) {
      const b = P.get(nodes[j].num), vb = V.get(nodes[j].num);
      let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + .01;
      const f = 2600 / d2 * G.alpha, d = Math.sqrt(d2);
      dx /= d; dy /= d;
      va.x += dx * f; va.y += dy * f; vb.x -= dx * f; vb.y -= dy * f;
    }
    va.x -= a.x * .004 * G.alpha; va.y -= a.y * .004 * G.alpha;
  }
  for (const e of G.edges) {
    const a = P.get(e.a), b = P.get(e.b);
    const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
    const snr = edgeSnr(e);
    const target = snr == null ? 90 : 60 + Math.max(0, 10 - snr) * 3; // weaker link -> longer edge
    const f = (d - target) * .04 * G.alpha;
    const va = V.get(e.a), vb = V.get(e.b);
    va.x += dx / d * f; va.y += dy / d * f; vb.x -= dx / d * f; vb.y -= dy / d * f;
  }
  for (const n of nodes) {
    const p = P.get(n.num), v = V.get(n.num);
    if (n.num === local || (G.drag && G.drag.num === n.num)) { v.x = v.y = 0; continue; }
    v.x *= .6; v.y *= .6;
    p.x += Math.max(-30, Math.min(30, v.x)); p.y += Math.max(-30, Math.min(30, v.y));
  }
  G.alpha *= .985;
  if (!G.userView) fitGraph();
  drawGraph();
  G.raf = requestAnimationFrame(simStep);
}
function drawGraph() {
  $$("#graph line.edge").forEach(l => {
    const a = G.pos.get(+l.dataset.a), b = G.pos.get(+l.dataset.b);
    if (!a || !b) return;
    l.setAttribute("x1", a.x); l.setAttribute("y1", a.y); l.setAttribute("x2", b.x); l.setAttribute("y2", b.y);
  });
  $$("#graph .gnode").forEach(g => {
    const p = G.pos.get(+g.dataset.num);
    if (p) g.setAttribute("transform", `translate(${p.x},${p.y})`);
  });
}
function fitGraph() {
  const pts = (G.nodes || []).map(n => G.pos.get(n.num)).filter(Boolean);
  if (!pts.length) return;
  const svg = $("#graph"), r = svg.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const xs = pts.map(p => p.x), ys = pts.map(p => p.y), pad = 60;
  const x0 = Math.min(...xs) - pad, x1 = Math.max(...xs) + pad + 40, y0 = Math.min(...ys) - pad, y1 = Math.max(...ys) + pad;
  const w = Math.max(x1 - x0, (y1 - y0) * r.width / r.height, 300);
  G.view.w = w;
  G.view.h = w * r.height / r.width;
  G.view.x = (x0 + x1) / 2 - w / 2;
  G.view.y = (y0 + y1) / 2 - G.view.h / 2;
  applyView();
}
function applyView() {
  const svg = $("#graph"), r = svg.getBoundingClientRect();
  if (r.width) G.view.h = G.view.w * r.height / r.width;
  svg.setAttribute("viewBox", `${G.view.x} ${G.view.y} ${G.view.w} ${G.view.h}`);
}
function svgPoint(ev) {
  const svg = $("#graph"), r = svg.getBoundingClientRect();
  return { x: G.view.x + (ev.clientX - r.left) / r.width * G.view.w, y: G.view.y + (ev.clientY - r.top) / r.height * G.view.h };
}
(() => {
  const svg = $("#graph");
  svg.addEventListener("wheel", ev => {
    ev.preventDefault();
    G.userView = true;
    const p = svgPoint(ev), k = ev.deltaY > 0 ? 1.15 : 1 / 1.15;
    G.view.x = p.x - (p.x - G.view.x) * k; G.view.y = p.y - (p.y - G.view.y) * k;
    G.view.w *= k; G.view.h *= k;
    applyView();
  }, { passive: false });
  svg.addEventListener("pointerdown", ev => {
    const g = ev.target.closest(".gnode");
    svg.setPointerCapture(ev.pointerId);
    G.drag = g ? { num: +g.dataset.num, moved: false } : { pan: true, cx: ev.clientX, cy: ev.clientY, vx: G.view.x, vy: G.view.y };
    if (!g) svg.classList.add("panning");
  });
  svg.addEventListener("pointermove", ev => {
    const d = G.drag;
    if (!d) return;
    if (d.pan) {
      G.userView = true;
      const r = svg.getBoundingClientRect();
      G.view.x = d.vx - (ev.clientX - d.cx) / r.width * G.view.w;
      G.view.y = d.vy - (ev.clientY - d.cy) / r.height * G.view.h;
      applyView();
    } else {
      d.moved = true;
      const p = svgPoint(ev);
      G.pos.set(d.num, p);
      G.alpha = Math.max(G.alpha, .15);
      drawGraph(); startSim();
    }
  });
  svg.addEventListener("pointerup", () => {
    const d = G.drag;
    G.drag = null;
    svg.classList.remove("panning");
    if (d && !d.pan && !d.moved) selectNode(d.num);
  });
  window.addEventListener("resize", () => { if (S.tab === "topology") applyView(); });
})();
$("#topoRelayout").onclick = () => { G.pos.clear(); G.vel.clear(); G.sig = ""; G.alpha = 1; G.userView = false; renderGraph(true); };
["#topoLinkAge", "#topoIsolated"].forEach(s => $(s).addEventListener("input", () => renderGraph(true)));

/* ---------------------------------------------------------------- reach */
const R = { map: null, layer: null, fitKey: "", heat: null };
function ensureReachMap() {
  if (R.map) return;
  const layers = baseLayers();
  R.map = L.map("reachMap", { preferCanvas: true }).setView([20, 0], 2);
  layers["Topographic"].addTo(R.map);
  L.control.layers(layers).addTo(R.map);
  L.control.scale({ imperial: true }).addTo(R.map);
  R.map.createPane("heat").style.zIndex = 350; // under the reach circles/markers
  R.layer = L.layerGroup().addTo(R.map);
}
function fillReachOrigins() {
  const sel = $("#reachOrigin");
  const cur = sel.value || (S.reachOrigin ?? S.snap.localNum);
  const nodes = [...S.snap.nodes].sort((a, b) => (b.isLocal - a.isLocal) || (b.favorite - a.favorite) || (a.longName || a.id).localeCompare(b.longName || b.id));
  const html = nodes.map(n => `<option value="${n.num}">${n.favorite ? "★ " : ""}${esc(n.longName || n.id)} (${esc(shortName(n))})${n.isLocal ? " - this radio" : ""}</option>`).join("");
  if (sel.dataset.sig !== html) { sel.innerHTML = html; sel.dataset.sig = html; }
  if (cur != null && S.byNum.has(+cur)) sel.value = cur;
}
function showReach(num) {
  S.reachOrigin = num;
  setTab("reach");
  $("#reachOrigin").value = num;
  scheduleReach(0);
}
function scheduleReach(delay = 800) {
  clearTimeout(S.reachTimer);
  S.reachTimer = setTimeout(loadReach, delay);
}
async function loadReach() {
  if (!S.snap || S.tab !== "reach") return;
  fillReachOrigins();
  const origin = $("#reachOrigin").value;
  if (!origin) return;
  const hops = $("#reachHops").value;
  const q = `node=${origin}&hops=${hops}&bidir=${$("#reachBidir").checked ? 1 : 0}&maxAgeH=${$("#reachAge").value}`;
  try {
    S.reach = await api(`/api/reach?${q}`);
    S.reachQ = q;
    drawReach(q);
  } catch (e) { toast("Reach: " + e.message); }
}
function drawReach(q) {
  const r = S.reach;
  R.layer.clearLayers();
  const hops = new Map(Object.entries(r.hops).map(([k, v]) => [+k, v]));
  const fallback = +$("#reachRadius").value;
  const circles = $("#reachCircles").checked;

  // coverage footprints (drawn first, underneath): solid enough to read, with a darker rim
  if (circles) {
    const order = [...hops].sort((a, b) => b[1] - a[1]); // far hops first, origin on top
    for (const [num, h] of order) {
      const n = S.byNum.get(num);
      if (!hasPos(n)) continue;
      const rad = r.radiusKm[num] ?? fallback;
      if (!rad) continue;
      const c = REACH_COLORS[Math.min(h, 7)];
      L.circle([n.lat, n.lon], {
        radius: rad * 1000, color: shade(c, -0.5), weight: h === 0 ? 3 : 2, opacity: .95,
        fillColor: c, fillOpacity: h === 0 ? .32 : .24, interactive: false,
      }).addTo(R.layer);
    }
  }
  // flood tree
  for (const t of r.tree) {
    const a = S.byNum.get(t.parent), b = S.byNum.get(t.child);
    if (!hasPos(a) || !hasPos(b)) continue;
    L.polyline([[a.lat, a.lon], [b.lat, b.lon]], { color: REACH_COLORS[Math.min(hops.get(t.child), 7)], weight: 3, opacity: .95 })
      .bindTooltip(`${esc(nodeName(t.parent))} → ${esc(nodeName(t.child))} (hop ${hops.get(t.child)})`, { sticky: true }).addTo(R.layer);
  }
  // nodes past the hop limit that would hear the final rebroadcast
  for (const num of r.beyond) {
    const n = S.byNum.get(num);
    if (!hasPos(n)) continue;
    L.circleMarker([n.lat, n.lon], { radius: 6, color: "#cbd5e1", weight: 2, dashArray: "2 3", fillOpacity: 0 })
      .bindTooltip(`${esc(nodeName(num))}: one hop past the limit (not reached)`).addTo(R.layer);
  }
  // unreached nodes, faint
  for (const n of S.snap.nodes) {
    if (!hasPos(n) || hops.has(n.num) || r.beyond.includes(n.num)) continue;
    L.circleMarker([n.lat, n.lon], { radius: 4, color: "#475569", weight: 1, fillColor: "#334155", fillOpacity: .6 })
      .bindTooltip(`${esc(nodeName(n.num))}: not reached`).addTo(R.layer);
  }
  const pts = [];
  for (const [num, h] of hops) {
    const n = S.byNum.get(num);
    if (!hasPos(n)) continue;
    pts.push([n.lat, n.lon]);
    L.circleMarker([n.lat, n.lon], { radius: h === 0 ? 10 : 7, color: "#0b1015", weight: 2, fillColor: REACH_COLORS[Math.min(h, 7)], fillOpacity: 1 })
      .bindTooltip(`${esc(shortName(n))}${h ? ` · hop ${h}` : " · origin"}`, { permanent: h === 0 || hops.size < 40, direction: "right", offset: [8, 0], className: "lbl" })
      .on("click", () => selectNode(num)).addTo(R.layer);
  }
  const fitKey = q.replace(/&bidir.*/, "") + "|" + pts.length;
  if (pts.length && R.fitKey !== fitKey) {
    R.fitKey = fitKey;
    if (pts.length === 1) R.map.setView(pts[0], 12); else R.map.fitBounds(pts, { padding: [50, 50], maxZoom: 14 });
  }

  // stats panel
  const perHop = Object.entries(r.perHop).filter(([h]) => +h > 0);
  const maxCount = Math.max(1, ...perHop.map(([, c]) => c));
  const noPos = [...hops.keys()].filter(n => !hasPos(S.byNum.get(n)));
  const origin = S.byNum.get(r.origin);
  $("#reachStats").innerHTML = `
    <div class="stat-grid">
      <div class="stat"><div class="v">${r.reachable}</div><div class="k">nodes reached</div></div>
      <div class="stat"><div class="v">${r.farthest ? r.farthest.distanceKm.toFixed(1) + " km" : "–"}</div><div class="k">farthest${r.farthest ? `: ${esc(r.farthest.name)}` : ""}</div></div>
      <div class="stat"><div class="v">${r.beyond.length}</div><div class="k">just out of reach</div></div>
      <div class="stat"><div class="v">${Math.max(0, S.snap.nodes.length - 1 - r.reachable)}</div><div class="k">not reached</div></div>
    </div>
    ${!r.hasLinks ? `<p class="muted small">No radio links have been observed for <b>${esc(r.originName)}</b> yet. Run a traceroute to it, or pick a node that broadcasts NeighborInfo.</p>` : ""}
    ${!hasPos(origin) ? `<p class="muted small">This node has no position, so it can't be drawn on the map, but its flood is still computed.</p>` : ""}
    ${perHop.length ? `<div class="hopbars">${perHop.map(([h, c]) => `<div><span>hop ${h}</span><span class="bar" style="--c:${REACH_COLORS[Math.min(+h, 7)]};width:${c / maxCount * 100}%"></span><span>${c}</span></div>`).join("")}</div>` : ""}
    ${noPos.length ? `<div><b class="small">Reached, no position (${noPos.length})</b><div class="reach-list">${noPos.map(n => `<div data-select="${n}">hop ${hops.get(n)} · ${esc(nodeName(n))}</div>`).join("")}</div></div>` : ""}`;
  $("#reachLegend").innerHTML = `<b>Hop</b>` + REACH_COLORS.slice(0, +$("#reachHops").value + 1).map((c, i) => `<div><i style="--c:${c};border:2px solid ${shade(c, -0.5)}"></i>${i === 0 ? "origin" : i}</div>`).join("")
    + `<div><i style="--c:transparent;border:2px dashed #cbd5e1;width:12px;height:12px"></i>past limit</div><div><i style="--c:#334155"></i>not reached</div>`;
}
$("#reachHops").addEventListener("input", () => { $("#reachHopsVal").textContent = $("#reachHops").value; scheduleReach(150); });
$("#reachRadius").addEventListener("input", () => { $("#reachRadiusVal").textContent = $("#reachRadius").value; if (S.reach) drawReach(S.reachQ); });
["#reachOrigin", "#reachBidir", "#reachAge", "#reachCircles"].forEach(s => $(s).addEventListener("input", () => {
  if (s === "#reachOrigin") S.reachOrigin = +$(s).value;
  scheduleReach(0);
}));

/* ----------------------------------------------------- terrain heatmap */
function heatNodes() {
  const scope = $("#heatScope").value;
  let nums;
  if (scope === "origin") nums = [+$("#reachOrigin").value];
  else if (scope === "reached") nums = Object.keys(S.reach?.hops || {}).map(Number);
  else nums = S.snap.nodes.filter(n => n.favorite).map(n => n.num);
  return nums.filter(num => hasPos(S.byNum.get(num)));
}
$("#heatRadius").addEventListener("input", () => { $("#heatRadiusVal").textContent = $("#heatRadius").value; });
$("#heatRun").onclick = async () => {
  const nodes = heatNodes();
  if (!nodes.length) { toast("None of those nodes has a known position"); return; }
  const btn = $("#heatRun");
  btn.disabled = true;
  const t0 = Date.now();
  $("#heatInfo").textContent = `Computing coverage for ${nodes.length} transmitter${nodes.length > 1 ? "s" : ""}… (the first run downloads elevation tiles)`;
  const tick = setInterval(() => { $("#heatInfo").textContent = $("#heatInfo").textContent.replace(/ \d+s$/, "") + ` ${Math.round((Date.now() - t0) / 1000)}s`; }, 1000);
  try {
    const gain = +$("#heatGain").value;
    const r = await api("/api/coverage", { nodes, params: {
      radiusKm: +$("#heatRadius").value, preset: $("#heatPreset").value, txPower: +$("#heatPower").value,
      txGain: gain, rxGain: gain, environment: $("#heatEnv").value, rxHeight: +$("#heatRxH").value,
    } });
    if (R.heat) R.heat.remove();
    R.heat = L.imageOverlay(r.image, r.bounds, { opacity: .85, interactive: false, pane: "heat" }).addTo(R.map);
    $("#heatLegend").hidden = false;
    $("#heatInfo").innerHTML = `<b>${r.areaKm2} km²</b> predicted coverage · ${r.seconds}s · elevation zoom ${r.zoom} (${r.stepM} m steps)<br>`
      + r.transmitters.map(t => `${esc(nodeName(t.num))}: ground ${t.groundM} m, antenna ${t.antennaM} m, mean reach ${t.meanReachKm} km`).join("<br>");
    if (nodes.length === 1 && hasPos(S.byNum.get(nodes[0]))) R.map.fitBounds(r.bounds, { padding: [20, 20] });
  } catch (e) {
    $("#heatInfo").textContent = "Failed: " + e.message;
  } finally {
    clearInterval(tick);
    btn.disabled = false;
  }
};
$("#heatClear").onclick = () => { if (R.heat) { R.heat.remove(); R.heat = null; } $("#heatLegend").hidden = true; $("#heatInfo").textContent = ""; };

/* ---------------------------------------------------------------- activity */
function routeHtml(label, hops) {
  if (!hops) return "";
  return `<div class="route"><span class="lab">${label}</span>${hops.map((h, i) =>
    `${i ? `<span class="arrow" style="color:${snrColor(h.snr)}">→${h.snr != null ? ` ${h.snr.toFixed(1)}dB` : ""}</span>` : ""}<span class="hop" data-select="${h.num}">${esc(h.name)}</span>`).join("")}</div>`;
}
function jobHtml(j) {
  const res = j.kind === "traceroute" && j.result ? routeHtml("to", j.result.towards) + routeHtml("back", j.result.back) : "";
  return `<div class="job ${j.status}">
    <div class="hd"><span class="kind">${esc(j.kind)}</span><span data-select="${j.target}" style="cursor:pointer">${esc(j.targetName)}</span>
      <span class="when">${clock(j.started)} · ${esc(j.status)}</span></div>
    <div class="sum">${esc(j.summary)}</div>${res}</div>`;
}
function renderActivity() {
  $("#jobs").innerHTML = S.snap.jobs.length ? S.snap.jobs.map(jobHtml).join("")
    : `<p class="muted">No requests yet. Use Trace / Ping / Info on any node.</p>`;
  const rxOnly = $("#logRxOnly").checked, textOnly = $("#logTextOnly").checked, hideMqtt = $("#logHideMqtt").checked;
  const kindTxt = { rx: "rx", tx: "tx", job: "done", conn: "conn", error: "err", mqtt: "mqtt", notify: "addon" };
  $("#log").innerHTML = [...S.snap.log].reverse().filter(l =>
    (!rxOnly || l.kind === "rx") && (!textOnly || l.port === "TEXT_MESSAGE_APP") && (!hideMqtt || !(l.mqtt || l.kind === "mqtt"))
  ).map(l =>
    `<div><span class="t">${clock(l.t)}</span><span class="k k-${esc(l.kind)}">${kindTxt[l.kind] || esc(l.kind)}</span><span>${esc(l.text)}${l.kind === "rx" ? `<span class="muted">${l.hops != null ? ` · ${l.hops} hop` : ""}${l.snr != null ? ` · ${l.snr} dB` : ""}</span>${l.src === "mqtt" ? ' <span class="tag mqtt">via MQTT</span>' : ""}` : ""}</span></div>`).join("");
}
["#logRxOnly", "#logTextOnly", "#logHideMqtt"].forEach(s => $(s).addEventListener("input", renderActivity));

/* addon notifications pop up as toasts */
SNAP_HOOKS.push(snap => {
  const notes = snap.log.filter(l => l.kind === "notify");
  const top = Math.max(0, ...snap.log.map(l => l.seq));
  if (S.notifySeq == null || top < S.notifySeq) { S.notifySeq = top; return; } // first load / state swap
  for (const l of notes) if (l.seq > S.notifySeq) toast(l.text, true);
  S.notifySeq = top;
});

/* ---------------------------------------------------------------- wiring */
$("#refreshPorts").onclick = loadPorts;
$("#connectBtn").onclick = async () => {
  const v = $("#portSelect").value;
  if (!v) { toast("Plug in a Meshtastic radio over USB, then press ↻ to rescan."); return; }
  try {
    if (v.startsWith("my:")) await api("/api/connect", { myNode: +v.slice(3) });
    else await api("/api/connect", { port: v });
  } catch (e) { toast(e.message); }
};
$("#demoBtn").onclick = () => api("/api/connect", { demo: true }).catch(e => toast(e.message));
$("#disconnectBtn").onclick = () => api("/api/disconnect", {}).catch(e => toast(e.message));
document.addEventListener("keydown", e => { if (e.key === "Escape" && S.selected != null) $("#drawerClose").click(); });
setInterval(() => { if (S.snap && S.tab === "nodes") renderNodes(); }, 15000); // keep "x ago" fresh

async function loadInfo() {
  try {
    S.info = await api("/api/info");
    $(".brand span").title = `MeshScope ${S.info.version}`;
    $("#heatPreset").innerHTML = Object.entries(S.info.presets).map(([k, p]) => `<option value="${k}" ${k === "LONG_FAST" ? "selected" : ""}>${esc(p.label)}</option>`).join("");
  } catch (e) { console.error(e); }
}

function boot() {
  loadInfo();
  loadPorts();
  poll();
}
