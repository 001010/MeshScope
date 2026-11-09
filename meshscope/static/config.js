/* Node Config: edit the connected radio's settings (schema-driven from the firmware protobufs)
   and back up / restore the whole configuration. Every apply saves a backup first. */
"use strict";

const CF = {
  schema: null, err: "", sel: store("cfgSel") || "owner", loadingFor: null,
  ch: { sections: {}, owner: {}, channels: {} }, backups: [], dir: "", armed: null, confirming: false,
};

VIEWS.config = {
  show() { loadConfig(); loadBackups(); },
  render() {
    const want = realRadio() ? S.snap.localNum : null;
    if (want !== CF.loadingFor && !(CF.schema && CF.schema.node.num === want)) loadConfig();
  },
  open(section) { CF.sel = section; store("cfgSel", section); setTab("config"); },
  deep(section) { if (section) { CF.sel = section; store("cfgSel", section); } },
};

const humanize = s => s.replaceAll("_", " ").replace(/\./g, " › ");
const changeCount = () => Object.values(CF.ch.sections).reduce((a, s) => a + Object.keys(s).length, 0)
  + Object.keys(CF.ch.owner).length + Object.values(CF.ch.channels).reduce((a, c) => a + Object.keys(c).length, 0);

async function loadConfig() {
  const want = realRadio() ? S.snap.localNum : null;
  CF.loadingFor = want;
  if (want == null) {
    CF.schema = null;
    CF.err = connected() ? "Configuration isn't available in demo mode. Connect a real radio over USB." : "Connect a radio over USB to view and edit its configuration.";
    renderConfig();
    return;
  }
  try {
    const s = await api("/api/config");
    if (CF.loadingFor !== want) return;
    if (!CF.schema || CF.schema.node.num !== s.node.num) CF.ch = { sections: {}, owner: {}, channels: {} };
    CF.schema = s;
    CF.err = "";
  } catch (e) { CF.err = e.message; CF.schema = null; }
  renderConfig();
}

async function loadBackups() {
  try {
    const d = await api("/api/config/backups");
    CF.backups = d.backups;
    CF.dir = d.dir;
    if (CF.sel === "backups") renderConfigBody();
    renderConfigNav();
  } catch (e) { console.error(e); }
}

function renderConfig() {
  renderConfigNav();
  renderConfigBody();
  renderConfigBar();
}

function renderConfigNav() {
  const s = CF.schema;
  const dot = n => n ? `<span class="badge">${n}</span>` : "";
  const item = (key, label, n = 0, extra = "") => `<div class="cfg-item${CF.sel === key ? " sel" : ""}${key === "backups" ? " nocap" : ""}" data-cfg="${key}">${label}${extra}${dot(n)}</div>`;
  let html = item("backups", "Backups &amp; restore", 0, `<span class="muted small">&nbsp;${CF.backups.length}</span>`);
  if (s) {
    html += `<div class="subhead">This radio</div>` + item("owner", "Owner (name)", Object.keys(CF.ch.owner).length)
      + item("channels", "Channels", Object.values(CF.ch.channels).reduce((a, c) => a + Object.keys(c).length, 0));
    for (const [grp, title] of [["config", "Radio config"], ["module", "Modules"]]) {
      html += `<div class="subhead">${title}</div>` + s.sections.filter(x => x.group === grp)
        .map(x => item("sec:" + x.name, humanize(x.name), Object.keys(CF.ch.sections[x.name] || {}).length)).join("");
    }
  }
  $("#cfgNav").innerHTML = html;
}

// esc() plus <wbr> after "." and "_" so long setting names wrap at word boundaries
const breakable = t => esc(t).replace(/([._])/g, "$1<wbr>");

function fieldInput(sec, f) {
  const cur = sec in CF.ch.sections && f.path in CF.ch.sections[sec] ? CF.ch.sections[sec][f.path] : f.value;
  const changed = sec in CF.ch.sections && f.path in CF.ch.sections[sec];
  const attrs = `data-sec="${esc(sec)}" data-path="${esc(f.path)}" data-kind="${f.kind}"`;
  let input;
  if (f.readOnly) input = `<span class="ro" title="read-only here">${esc(Array.isArray(f.value) ? f.value.join(", ") || "(none)" : f.value)}</span>`;
  else if (f.kind === "bool") input = `<input type="checkbox" ${attrs} ${cur ? "checked" : ""}>`;
  else if (f.kind === "enum") input = `<select ${attrs}>${f.options.map(o => `<option ${o === cur ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  else if (f.kind === "int" || f.kind === "float") input = `<input type="number" ${attrs} value="${esc(cur)}" step="${f.kind === "float" ? "any" : "1"}">`;
  else input = `<input ${attrs} value="${esc(cur)}">`;
  return `<label class="cfg-field${changed ? " changed" : ""}"><span class="lab">${esc(humanize(f.path))}<code>${breakable(sec + "." + f.path)}</code></span>${input}</label>`;
}

const SECTION_NOTES = {
  lora: "Region, modem preset and frequency must match the rest of your mesh, or this radio will stop hearing it. Hop limit above 3 adds airtime for everyone.",
  security: "These keys control who can administer this radio. A wrong admin key or ‘managed’ mode can lock you out of remote administration.",
  device: "Changing the role (e.g. to ROUTER) changes how this radio rebroadcasts; most nodes should stay CLIENT.",
  bluetooth: "Disabling Bluetooth means the phone app can no longer connect to this radio.",
  network: "Wi-Fi / Ethernet settings apply to boards that have them.",
  mqtt: "This is the radio's own MQTT gateway (over Wi-Fi), separate from MeshScope's MQTT tab.",
};

function renderConfigBody() {
  const body = $("#cfgBody");
  const s = CF.schema;
  if (CF.sel === "backups") { body.innerHTML = backupsHtml(); return; }
  if (!s) {
    body.innerHTML = `<div class="empty"><h2>Node configuration</h2><p>${esc(CF.err || "Loading…")}</p>
      <p class="muted small">Saved backups stay available under <b>Backups &amp; restore</b> while disconnected.</p></div>`;
    return;
  }
  const head = `<div class="cfg-node muted small">${esc(s.node.name)} · ${esc(s.node.id)}</div>`;
  if (CF.sel === "owner") {
    const o = { ...s.owner, ...CF.ch.owner };
    const ch = k => k in CF.ch.owner ? " changed" : "";
    body.innerHTML = `${head}<h2>Owner</h2><div class="cfg-fields">
      <label class="cfg-field${ch("long_name")}"><span class="lab">Long name</span><input data-owner="long_name" maxlength="39" value="${esc(o.long_name)}"></label>
      <label class="cfg-field${ch("short_name")}"><span class="lab">Short name<code>up to 4 characters</code></span><input data-owner="short_name" maxlength="4" value="${esc(o.short_name)}"></label>
      <label class="cfg-field${ch("is_licensed")}"><span class="lab">Licensed amateur operator<code>disables encryption on this radio</code></span><input type="checkbox" data-owner="is_licensed" ${o.is_licensed ? "checked" : ""}></label>
    </div>`;
    return;
  }
  if (CF.sel === "channels") { body.innerHTML = head + channelsHtml(); return; }
  const name = CF.sel.replace(/^sec:/, "");
  const sec = s.sections.find(x => x.name === name);
  if (!sec) { CF.sel = "owner"; return renderConfigBody(); }
  body.innerHTML = `${head}<h2>${esc(humanize(sec.name))} <span class="muted small">${sec.group === "module" ? "module" : "radio config"}${sec.reboots ? " · saving reboots the radio" : ""}</span></h2>
    ${SECTION_NOTES[sec.name] ? `<p class="warn small narrow">${esc(SECTION_NOTES[sec.name])}</p>` : ""}
    <div class="cfg-fields">${sec.fields.map(f => fieldInput(sec.name, f)).join("")}</div>`;
}

function randomPsk() {
  const b = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...b));
}
function channelsHtml() {
  const chans = CF.schema.channels;
  const rows = chans.map(c => {
    const e = { ...c, ...(CF.ch.channels[c.index] || {}) };
    const chg = k => CF.ch.channels[c.index] && k in CF.ch.channels[c.index] ? " changed" : "";
    const off = e.role === "DISABLED";
    let len = -1;
    try { len = atob(e.psk || "").length; } catch { /* still typing */ }
    const pskKind = e.psk === "" ? "no encryption" : e.psk === "AQ==" ? "default public key" : len < 0 ? "not valid base64"
      : len === 1 ? "simple key" : [16, 32].includes(len) ? `${len * 8}-bit private key` : `${len} bytes (must be 16 or 32)`;
    return `<tr class="${off ? "off" : ""}">
      <td>#${c.index}</td>
      <td class="${chg("role")}">${c.index === 0 ? "PRIMARY" : `<select data-ch="${c.index}" data-k="role">${["DISABLED", "SECONDARY"].map(r => `<option ${r === e.role ? "selected" : ""}>${r}</option>`).join("")}</select>`}</td>
      <td class="${chg("name")}"><input data-ch="${c.index}" data-k="name" maxlength="11" value="${esc(e.name)}" placeholder="${c.index === 0 ? "(preset name)" : ""}" ${off ? "disabled" : ""}></td>
      <td class="${chg("psk")}"><input class="psk" type="${len > 1 ? "password" : "text"}" title="Click to show" data-ch="${c.index}" data-k="psk" value="${esc(e.psk)}" ${off ? "disabled" : ""}>
        <div class="muted small">${esc(pskKind)} · <a href="#" data-pskset="${c.index}" data-v="AQ==">default</a> · <a href="#" data-pskset="${c.index}" data-v="random">random</a> · <a href="#" data-pskset="${c.index}" data-v="">none</a></div></td>
      <td class="${chg("uplink_enabled")}"><input type="checkbox" data-ch="${c.index}" data-k="uplink_enabled" ${e.uplink_enabled ? "checked" : ""} ${off ? "disabled" : ""}></td>
      <td class="${chg("downlink_enabled")}"><input type="checkbox" data-ch="${c.index}" data-k="downlink_enabled" ${e.downlink_enabled ? "checked" : ""} ${off ? "disabled" : ""}></td>
      <td class="${chg("position_precision")}"><input type="number" min="0" max="32" data-ch="${c.index}" data-k="position_precision" value="${esc(e.position_precision)}" ${off ? "disabled" : ""}></td>
    </tr>`;
  }).join("");
  return `<h2>Channels</h2>
    <p class="muted small narrow">Every radio that should talk on a channel needs the same name and key. Channel 0 is the primary channel.
      Enable a secondary channel by setting its role to SECONDARY. The MQTT uplink/downlink boxes are for this radio's own Wi-Fi MQTT gateway.
      Position precision: 0 = don't share position on this channel, 32 = full precision.</p>
    <div class="table-wrap"><table class="nodes cfg-ch"><thead><tr><th>#</th><th>Role</th><th>Name</th><th>Key (base64)</th><th>Uplink</th><th>Downlink</th><th>Position precision</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function backupsHtml() {
  const rows = CF.backups.map(b => `<tr>
      <td>${esc(b.label)}</td><td class="muted">${esc(b.node)}</td><td>${esc(b.created)}</td><td class="num muted">${(b.size / 1024).toFixed(1)} KB</td>
      <td><div class="acts">
        <button data-bk-dl="${esc(b.file)}">Download</button>
        <button data-bk-restore="${esc(b.file)}" ${realRadio() ? "" : "disabled title='Connect the radio to restore'"}>${CF.armed === "r:" + b.file ? "Click again to restore" : "Restore"}</button>
        <button class="danger" data-bk-del="${esc(b.file)}">${CF.armed === "d:" + b.file ? "Click again" : "Delete"}</button></div></td></tr>`).join("");
  return `<h2>Backups &amp; restore</h2>
    <p class="muted small narrow">A backup is the radio's full configuration (owner, channels and keys, radio and module settings) in the same YAML
      format as <code>meshtastic --export-config</code>. MeshScope saves one automatically before every change you apply here, before every restore,
      and the first time each day it connects to a radio. Restoring applies a backup to the connected radio with
      <code>meshtastic --configure</code>; the output appears in the Console tab and the radio reboots.</p>
    <p class="warn small narrow">Backups contain your channel keys and the radio's private key. Keep downloaded copies somewhere safe.</p>
    <div class="row-tools">
      <input id="bkLabel" placeholder="Label, e.g. working setup before firmware update" maxlength="60" style="flex:1;min-width:240px">
      <button class="primary" id="bkNow" ${realRadio() ? "" : "disabled title='Connect a radio first'"}>Back up now</button>
      <button id="bkImport">Import backup file…</button><input type="file" id="bkFile" accept=".yaml,.yml" hidden>
    </div>
    ${CF.backups.length ? `<div class="table-wrap"><table class="nodes"><thead><tr><th>Label</th><th>Node</th><th>Created</th><th class="num">Size</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<p class="muted">No backups yet.</p>`}
    <p class="muted small">Stored in ${esc(CF.dir)}</p>`;
}

function renderConfigBar() {
  const n = changeCount();
  const bar = $("#cfgBar");
  if (!n) { bar.hidden = true; CF.confirming = false; return; }
  bar.hidden = false;
  if (!CF.confirming) {
    bar.innerHTML = `<span><b>${n}</b> unsaved change${n > 1 ? "s" : ""}</span><span class="spacer"></span>
      <button id="cfgDiscard">Discard</button><button class="primary" id="cfgReview">Review &amp; apply</button>`;
    return;
  }
  const s = CF.schema;
  const lines = [];
  for (const [sec, fields] of Object.entries(CF.ch.sections)) {
    const def = s.sections.find(x => x.name === sec);
    for (const [p, v] of Object.entries(fields)) {
      const old = def?.fields.find(f => f.path === p)?.value;
      lines.push(`<li><code>${esc(sec)}.${esc(p)}</code>: ${esc(old)} → <b>${esc(v)}</b></li>`);
    }
  }
  for (const [k, v] of Object.entries(CF.ch.owner)) lines.push(`<li><code>owner.${esc(k)}</code>: ${esc(s.owner[k])} → <b>${esc(v)}</b></li>`);
  for (const [i, c] of Object.entries(CF.ch.channels)) {
    const old = s.channels.find(x => x.index === +i) || {};
    for (const [k, v] of Object.entries(c)) lines.push(`<li><code>channel ${i}.${esc(k)}</code>: ${esc(k === "psk" ? "•••" : old[k])} → <b>${esc(k === "psk" ? (v ? "new key" : "none") : v)}</b></li>`);
  }
  const reboots = Object.keys(CF.ch.sections).filter(sec => s.sections.find(x => x.name === sec)?.reboots);
  bar.innerHTML = `<div class="cfg-review"><b>Apply these changes to ${esc(s.node.name)}?</b><ul>${lines.join("")}</ul>
    <div class="small">A backup of the current configuration is saved first, so you can restore it from <b>Backups &amp; restore</b>.
      ${reboots.length ? `<span class="warn">Saving ${esc(reboots.join(", "))} makes the radio reboot; MeshScope reconnects when it comes back.</span>` : ""}</div></div>
    <span class="spacer"></span><button id="cfgCancel">Back</button><button class="primary" id="cfgApply">Apply to radio</button>`;
}

function coerce(el) {
  const k = el.dataset.kind;
  if (el.type === "checkbox") return el.checked;
  if (k === "int") return el.value === "" ? 0 : parseInt(el.value, 10);
  if (k === "float") return el.value === "" ? 0 : parseFloat(el.value);
  return el.value;
}

$("#cfgBody").addEventListener("input", e => {
  const el = e.target;
  const s = CF.schema;
  if (!s) return;
  if (el.dataset.sec) {
    const sec = el.dataset.sec, path = el.dataset.path;
    const orig = s.sections.find(x => x.name === sec).fields.find(f => f.path === path).value;
    const v = coerce(el);
    const m = CF.ch.sections[sec] || (CF.ch.sections[sec] = {});
    if (v === orig || (typeof v === "number" && Number.isNaN(v))) delete m[path]; else m[path] = v;
    if (!Object.keys(m).length) delete CF.ch.sections[sec];
    el.closest(".cfg-field").classList.toggle("changed", path in (CF.ch.sections[sec] || {}));
  } else if (el.dataset.owner) {
    const k = el.dataset.owner;
    const v = el.type === "checkbox" ? el.checked : el.value;
    if (v === s.owner[k]) delete CF.ch.owner[k]; else CF.ch.owner[k] = v;
    el.closest(".cfg-field").classList.toggle("changed", k in CF.ch.owner);
  } else if (el.dataset.ch) {
    setChannel(+el.dataset.ch, el.dataset.k, el.type === "checkbox" ? el.checked : el.type === "number" ? +el.value : el.value, el.dataset.k === "role");
    el.closest("td").classList.toggle("changed", !!(CF.ch.channels[el.dataset.ch] && el.dataset.k in CF.ch.channels[el.dataset.ch]));
  }
  CF.confirming = false;
  renderConfigBar();
  renderConfigNav();
});
function setChannel(idx, k, v, rerender) {
  const orig = CF.schema.channels.find(c => c.index === idx)[k];
  const m = CF.ch.channels[idx] || (CF.ch.channels[idx] = {});
  if (v === orig) delete m[k]; else m[k] = v;
  if (!Object.keys(m).length) delete CF.ch.channels[idx];
  if (rerender) renderConfigBody();
}

$("#cfgBody").addEventListener("click", async e => {
  const psk = e.target.closest("[data-pskset]");
  if (psk) {
    e.preventDefault();
    const v = psk.dataset.v === "random" ? randomPsk() : psk.dataset.v;
    setChannel(+psk.dataset.pskset, "psk", v, true);
    renderConfigBar(); renderConfigNav();
    return;
  }
  const dl = e.target.closest("[data-bk-dl]");
  if (dl) { try { await download(`/api/config/backup/download?file=${encodeURIComponent(dl.dataset.bkDl)}`); } catch (err) { toast(err.message); } return; }
  const rs = e.target.closest("[data-bk-restore]");
  if (rs && !rs.disabled) { armOr("r:" + rs.dataset.bkRestore, () => restoreBackup(rs.dataset.bkRestore)); return; }
  const del = e.target.closest("[data-bk-del]");
  if (del) {
    armOr("d:" + del.dataset.bkDel, async () => {
      try { await api("/api/config/backup/delete", { file: del.dataset.bkDel }); loadBackups(); } catch (err) { toast(err.message); }
    });
    return;
  }
  if (e.target.id === "bkNow") {
    e.target.disabled = true;
    try {
      const r = await api("/api/config/backup", { label: $("#bkLabel").value.trim() || "manual" });
      toast(`Backup saved: ${r.backup.file}`, true);
      loadBackups();
    } catch (err) { toast(err.message); e.target.disabled = false; }
  }
  if (e.target.id === "bkImport") $("#bkFile").click();
});
// private channel keys stay masked unless the field is being edited (screen sharing)
$("#cfgBody").addEventListener("focusin", e => { if (e.target.classList.contains("psk")) e.target.type = "text"; });
$("#cfgBody").addEventListener("focusout", e => {
  const el = e.target;
  if (!el.classList.contains("psk")) return;
  let n = 0;
  try { n = atob(el.value).length; } catch { /* invalid */ }
  if (n > 1) el.type = "password";
});
$("#cfgBody").addEventListener("change", async e => {
  if (e.target.id !== "bkFile") return;
  try {
    const [f] = await readFiles(e.target.files);
    const r = await api("/api/config/backup/upload", f);
    toast(`Imported ${r.backup.file}`, true);
    loadBackups();
  } catch (err) { toast("Import failed: " + err.message); }
});
function armOr(key, fn) {
  if (CF.armed === key) { CF.armed = null; fn(); renderConfigBody(); return; }
  CF.armed = key;
  renderConfigBody();
  setTimeout(() => { if (CF.armed === key) { CF.armed = null; if (CF.sel === "backups") renderConfigBody(); } }, 5000);
}
async function restoreBackup(file) {
  try {
    const r = await api("/api/config/restore", { file });
    toast(`Restoring ${file}… (safety backup ${r.safetyBackup.file} saved). Watch the Console tab for progress.`, true);
    loadBackups();
    setTimeout(() => setTab("console"), 600);
  } catch (err) { toast("Restore failed: " + err.message); }
}

$("#cfgNav").addEventListener("click", e => {
  const it = e.target.closest("[data-cfg]");
  if (!it) return;
  CF.sel = it.dataset.cfg;
  store("cfgSel", CF.sel);
  CF.armed = null;
  renderConfigNav();
  renderConfigBody();
  if (CF.sel === "backups") loadBackups();
});
$("#cfgBar").addEventListener("click", async e => {
  if (e.target.id === "cfgDiscard") { CF.ch = { sections: {}, owner: {}, channels: {} }; renderConfig(); }
  if (e.target.id === "cfgReview") { CF.confirming = true; renderConfigBar(); }
  if (e.target.id === "cfgCancel") { CF.confirming = false; renderConfigBar(); }
  if (e.target.id === "cfgApply") {
    e.target.disabled = true;
    const body = {
      sections: CF.ch.sections,
      owner: Object.keys(CF.ch.owner).length ? { ...CF.schema.owner, ...CF.ch.owner } : null,
      channels: Object.entries(CF.ch.channels).map(([i, c]) => ({ index: +i, ...c })),
    };
    try {
      const r = await api("/api/config/apply", body);
      toast(`Saved to the radio: ${r.written.join(", ")}.${r.reboot ? " The radio is rebooting." : ""} Backup: ${r.backup.file}`, true);
      CF.ch = { sections: {}, owner: {}, channels: {} };
      CF.confirming = false;
      loadBackups();
      setTimeout(loadConfig, 1500);
    } catch (err) {
      toast("Not applied: " + err.message);
      e.target.disabled = false;
    }
  }
});
