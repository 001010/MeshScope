/* My Nodes: radios you've connected to, remembered by node ID (not COM port). */
"use strict";

const MY = { timer: 0, list: [], armed: null };

VIEWS.mynodes = {
  show() { loadMyNodes(); clearInterval(MY.timer); MY.timer = setInterval(loadMyNodes, 4000); },
  hide() { clearInterval(MY.timer); },
  render() { if (S.prevMyStatus !== S.snap.status) { S.prevMyStatus = S.snap.status; loadMyNodes(); } },
};

async function loadMyNodes() {
  try {
    const d = await api("/api/mynodes");
    MY.list = d.myNodes;
    renderMyNodes();
  } catch (e) { console.error(e); }
}

const MATCH_TXT = {
  "usb-serial": "found by its USB serial number",
  "last-port": "on the COM port it last used (node ID is checked on connect)",
};

function renderMyNodes() {
  const box = $("#myNodesList");
  if (box.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return; // typing a nickname
  if (!MY.list.length) {
    box.innerHTML = `<div class="empty" style="grid-column:1/-1"><h2>No My Nodes yet</h2>
      <p>Connect to a radio over USB once and it's remembered here by its node ID.</p></div>`;
    return;
  }
  const busy = S.snap && (S.snap.status === "connecting");
  box.innerHTML = MY.list.map(m => {
    const name = m.nickname || m.longName || m.id;
    const presence = m.connected
      ? `<span class="presence yes">● connected on ${esc(S.snap?.port || m.lastPort || "")}</span>`
      : m.presentPort
        ? `<span class="presence yes">● plugged in on ${esc(m.presentPort)}</span>`
        : `<span class="presence no">○ not plugged in</span>`;
    const usb = [m.usbDescription, m.usbSerial ? `serial ${m.usbSerial}` : null].filter(Boolean).join(" · ");
    return `<div class="card${m.connected ? " connected" : ""}">
      <div class="hd"><span class="short" style="--c:var(--local)">${esc(m.shortName || m.id.slice(-4))}</span><h3>${esc(name)}</h3><span class="spacer"></span>${presence}</div>
      <div class="meta">
        <span>Node ID</span><span>${esc(m.id)}</span>
        ${m.nickname && m.longName ? `<span>Radio name</span><span>${esc(m.longName)}</span>` : ""}
        <span>Hardware</span><span>${esc(m.hwModel || "?")}</span>
        <span>Last port</span><span>${esc(m.lastPort || "?")}${m.presentPort && m.match ? ` <span class="muted">(${esc(MATCH_TXT[m.match] || m.match)})</span>` : ""}</span>
        ${usb ? `<span>USB</span><span>${esc(usb)}</span>` : ""}
        <span>Last connected</span><span>${m.lastConnected ? ago(m.lastConnected) : "never"}</span>
      </div>
      <label class="field">Nickname<input data-nick="${m.num}" maxlength="40" value="${esc(m.nickname || "")}" placeholder="${esc(m.longName || "")}"></label>
      <div class="btns">
        ${m.connected ? `<span class="pill connected">Connected</span>`
          : `<button class="primary" data-myconnect="${m.num}" ${busy ? "disabled" : ""}>${m.presentPort ? "Connect" : "Find & connect"}</button>`}
        <label class="chk" title="Connect to this radio automatically when MeshScope starts"><input type="checkbox" data-auto="${m.num}" ${m.autoConnect ? "checked" : ""}> Auto-connect at startup</label>
        <span class="spacer"></span>
        <button class="danger" data-myremove="${m.num}">${MY.armed === m.num ? "Click again to remove" : "Remove"}</button>
      </div></div>`;
  }).join("");
}

$("#myNodesList").addEventListener("click", async e => {
  const c = e.target.closest("[data-myconnect]");
  if (c) {
    try {
      const r = await api("/api/connect", { myNode: +c.dataset.myconnect });
      toast(`Looking for the radio on ${r.trying.join(", ")}…`, true);
    } catch (err) { toast(err.message); }
    return;
  }
  const rm = e.target.closest("[data-myremove]");
  if (rm) {
    const num = +rm.dataset.myremove;
    if (MY.armed !== num) {
      MY.armed = num;
      renderMyNodes();
      setTimeout(() => { if (MY.armed === num) { MY.armed = null; renderMyNodes(); } }, 5000);
      return;
    }
    MY.armed = null;
    try { await api("/api/mynodes/remove", { num }); await loadMyNodes(); loadPorts(); } catch (err) { toast(err.message); }
  }
});
$("#myNodesList").addEventListener("change", async e => {
  const t = e.target;
  try {
    if (t.dataset.nick) {
      await api("/api/mynodes/update", { num: +t.dataset.nick, nickname: t.value.trim() });
      t.blur();
      await loadMyNodes();
      loadPorts();
    } else if (t.dataset.auto) {
      await api("/api/mynodes/update", { num: +t.dataset.auto, autoConnect: t.checked });
      await loadMyNodes();
    }
  } catch (err) { toast(err.message); }
});
$("#myNodesList").addEventListener("keydown", e => { if (e.key === "Enter" && e.target.dataset.nick) e.target.blur(); });
