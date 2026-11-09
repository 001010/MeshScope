/* Addons: create, edit, enable, import and export Python scripts that react to inbound packets. */
"use strict";

const AD = { list: [], sel: store("addonSel"), code: "", dirty: false, outSeq: 0, timer: 0, armed: false, loadedSlug: null };
const TEMPLATE_LABELS = {
  blank: "Blank", keyword_alert: "Keyword alert (pop-up)", low_battery: "Low battery watch",
  traffic_stats: "Traffic stats (per minute)", auto_reply: "Ping auto-reply (transmits!)",
};

VIEWS.addons = {
  show() { loadAddons(true); clearInterval(AD.timer); AD.timer = setInterval(() => loadAddons(false), 1500); },
  hide() { clearInterval(AD.timer); },
};

async function loadAddons(full) {
  try {
    const d = await api("/api/addons");
    AD.list = d.addons;
    if (full) {
      $("#addonDir").textContent = `Stored in ${d.dir}`;
      $("#addonDoc").textContent = d.doc;
      $("#addonNewTpl").innerHTML = d.templates.map(t => `<option value="${t}">${esc(TEMPLATE_LABELS[t] || t)}</option>`).join("");
    }
    if (AD.sel && !AD.list.some(a => a.slug === AD.sel)) selectAddon(null);
    renderAddonList();
    if (AD.sel) await loadAddonDetail(full || AD.loadedSlug !== AD.sel);
  } catch (e) { console.error(e); }
}

function renderAddonList() {
  const checked = new Set($$("#addonItems input:checked").map(i => i.dataset.slug));
  $("#addonItems").innerHTML = AD.list.length ? AD.list.map(a => `
    <div class="addon-item${a.slug === AD.sel ? " sel" : ""}" data-slug="${esc(a.slug)}">
      <input type="checkbox" data-slug="${esc(a.slug)}" ${checked.has(a.slug) ? "checked" : ""} title="Select for export">
      <span class="st ${esc(a.status)}" title="${esc(a.status)}"></span>
      <span class="nm" title="${esc(a.slug)}.py">${esc(a.name)}</span>
      <span class="muted small">${a.enabled ? (a.packets ? a.packets : "on") : "off"}</span>
    </div>`).join("") : `<p class="muted small">No addons yet. Press <b>New</b> to start from a template.</p>`;
}

function selectAddon(slug) {
  if (AD.dirty && slug !== AD.sel && !confirm("Discard unsaved changes to this addon?")) return false;
  AD.sel = slug;
  store("addonSel", slug);
  AD.dirty = false;
  AD.outSeq = 0;
  AD.loadedSlug = null;
  AD.armed = false;
  $("#addonOut").innerHTML = "";
  $("#addonEditor").hidden = !slug;
  $("#addonEmpty").hidden = !!slug;
  renderAddonList();
  if (slug) loadAddonDetail(true);
  return true;
}

async function loadAddonDetail(withCode) {
  const slug = AD.sel;
  const d = await api(`/api/addon?slug=${encodeURIComponent(slug)}&since=${AD.outSeq}`);
  if (slug !== AD.sel) return;
  $("#addonEditor").hidden = false;
  $("#addonEmpty").hidden = true;
  if (withCode && !AD.dirty) {
    $("#addonCode").value = d.code;
    AD.code = d.code;
    AD.loadedSlug = slug;
  }
  $("#addonTitle").textContent = `${d.name}  `;
  $("#addonTitle").title = `${d.slug}.py`;
  const pill = $("#addonStatus");
  pill.className = "pill " + d.status;
  pill.textContent = d.status;
  $("#addonEnabled").checked = d.enabled;
  $("#addonCounts").textContent = `${d.packets} packets · ${d.errors} errors${d.dropped ? ` · ${d.dropped} dropped (too slow)` : ""}${d.lastActivity ? ` · active ${ago(d.lastActivity)}` : ""}`;
  $("#addonDirty").textContent = AD.dirty ? "unsaved changes" : "";
  $("#addonDelete").textContent = AD.armed ? "Click again to delete" : "Delete";
  if (d.output.length) {
    const out = $("#addonOut");
    const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
    for (const l of d.output) {
      const div = document.createElement("div");
      div.className = l.kind;
      div.textContent = `${clock(l.t)}  ${l.text}`;
      out.appendChild(div);
      AD.outSeq = Math.max(AD.outSeq, l.seq);
    }
    while (out.childElementCount > 1000) out.firstElementChild.remove();
    if (atBottom) out.scrollTop = out.scrollHeight;
  }
}

async function saveAddon() {
  if (!AD.sel) return;
  try {
    const r = await api("/api/addons/save", { slug: AD.sel, code: $("#addonCode").value });
    AD.code = $("#addonCode").value;
    AD.dirty = false;
    toast(r.status === "error" ? "Saved, but the addon failed to start - see Output" : "Saved" + (r.status === "running" ? " and restarted" : ""), r.status !== "error");
    loadAddons(false);
  } catch (e) { toast("Not saved: " + e.message); }
}

$("#addonItems").addEventListener("click", e => {
  if (e.target.matches("input[type=checkbox]")) return;
  const it = e.target.closest(".addon-item");
  if (it) selectAddon(it.dataset.slug);
});
$("#addonSelAll").addEventListener("input", e => $$("#addonItems input[type=checkbox]").forEach(i => { i.checked = e.target.checked; }));
$("#addonCode").addEventListener("input", () => {
  AD.dirty = $("#addonCode").value !== AD.code;
  $("#addonDirty").textContent = AD.dirty ? "unsaved changes" : "";
});
$("#addonCode").addEventListener("keydown", e => {
  const ta = e.target;
  if (e.key === "Tab") { // indent / outdent with 4 spaces
    e.preventDefault();
    const { selectionStart: s, selectionEnd: en, value: v } = ta;
    if (s === en && !e.shiftKey) {
      ta.setRangeText("    ", s, en, "end");
    } else {
      const ls = v.lastIndexOf("\n", s - 1) + 1;
      const block = v.slice(ls, en);
      const out = e.shiftKey ? block.replace(/^ {1,4}/gm, "") : block.replace(/^/gm, "    ");
      ta.setRangeText(out, ls, en, "select");
    }
    ta.dispatchEvent(new Event("input"));
  } else if (e.key === "Enter") { // keep indentation, add a level after ':'
    const { selectionStart: s, value: v } = ta;
    const line = v.slice(v.lastIndexOf("\n", s - 1) + 1, s);
    let ind = (/^\s*/.exec(line) || [""])[0];
    if (/:\s*$/.test(line)) ind += "    ";
    e.preventDefault();
    ta.setRangeText("\n" + ind, s, ta.selectionEnd, "end");
    ta.dispatchEvent(new Event("input"));
  }
});
document.addEventListener("keydown", e => {
  if (S.tab === "addons" && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveAddon(); }
});
$("#addonSave").onclick = saveAddon;
$("#addonEnabled").addEventListener("input", async e => {
  try {
    if (e.target.checked && AD.dirty) await saveAddon();
    await api("/api/addons/enable", { slug: AD.sel, enabled: e.target.checked });
    loadAddons(false);
  } catch (err) { toast(err.message); }
});
$("#addonDelete").onclick = async () => {
  if (!AD.armed) {
    AD.armed = true;
    $("#addonDelete").textContent = "Click again to delete";
    setTimeout(() => { AD.armed = false; $("#addonDelete").textContent = "Delete"; }, 5000);
    return;
  }
  try {
    await api("/api/addons/delete", { slug: AD.sel });
    AD.dirty = false;
    selectAddon(null);
    loadAddons(false);
  } catch (e) { toast(e.message); }
};
$("#addonNew").onclick = () => { $("#addonNewForm").hidden = false; $("#addonNewName").focus(); };
$("#addonCancel").onclick = () => { $("#addonNewForm").hidden = true; };
$("#addonCreate").onclick = async () => {
  try {
    const r = await api("/api/addons/create", { name: $("#addonNewName").value.trim() || "New addon", template: $("#addonNewTpl").value });
    $("#addonNewForm").hidden = true;
    $("#addonNewName").value = "";
    await loadAddons(false);
    selectAddon(r.slug);
  } catch (e) { toast(e.message); }
};
$("#addonNewName").addEventListener("keydown", e => { if (e.key === "Enter") $("#addonCreate").click(); });
$("#addonImport").onclick = () => $("#addonFile").click();
$("#addonFile").addEventListener("change", async e => {
  try {
    const files = await readFiles(e.target.files);
    const r = await api("/api/addons/import", { files });
    toast(`Imported ${r.created.length} addon${r.created.length === 1 ? "" : "s"} (disabled until you enable them)`, true);
    await loadAddons(false);
    if (r.created.length) selectAddon(r.created[0]);
  } catch (err) { toast("Import failed: " + err.message); }
  e.target.value = "";
});
$("#addonExportSel").onclick = async () => {
  const slugs = $$("#addonItems input[type=checkbox]:checked").map(i => i.dataset.slug);
  try {
    const name = await download(`/api/addons/export?slugs=${encodeURIComponent(slugs.join(","))}`);
    toast(`Exported ${slugs.length || "all"} addon${slugs.length === 1 ? "" : "s"} as ${name}`, true);
  } catch (e) { toast(e.message); }
};
$("#addonExportOne").onclick = async () => {
  try { await download(`/api/addons/export?slugs=${encodeURIComponent(AD.sel)}`); } catch (e) { toast(e.message); }
};
