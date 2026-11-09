/* Messages: the radio's channels, direct messages, and sending text through the radio.
   A message gets the "via MQTT" badge only when it reached MeshScope solely through the
   MQTT bridge; anything the USB radio heard over the air is shown as a radio packet. */
"use strict";

const MSG = { thread: store("msgThread") || "ch:0", read: store("msgRead") || {}, sig: "", lastSeq: null };
const MAX_BYTES = 200;
const enc = new TextEncoder();

VIEWS.messages = {
  show() { renderMessages(true); setTimeout(() => $("#msgText").focus(), 0); },
  render() { renderMessages(false); },
  openDm(num) {
    MSG.thread = `dm:${num}`;
    store("msgThread", MSG.thread);
    MSG.sig = "";
    setTab("messages");
  },
};

function threadOf(m) {
  if (m.dm) return `dm:${m.outgoing ? m.to : m.from}`;
  if (m.channel != null) return `ch:${m.channel}`;
  return `mq:${m.channelName || "?"}`;
}

function buildThreads(snap) {
  const T = new Map();
  const get = (key, init) => { if (!T.has(key)) T.set(key, { key, msgs: [], last: 0, unread: 0, ...init }); return T.get(key); };
  for (const c of snap.channels) {
    get(`ch:${c.index}`, { kind: "ch", index: c.index, name: c.name, live: true, primary: c.primary, defaultKey: c.defaultKey, uplink: c.uplinkEnabled });
  }
  for (const m of snap.messages) {
    const key = threadOf(m);
    const t = get(key, key.startsWith("ch:") ? { kind: "ch", index: m.channel, name: m.channelName || `Channel ${m.channel}`, live: false }
      : key.startsWith("mq:") ? { kind: "mq", name: m.channelName || "?" }
        : { kind: "dm", peer: +key.slice(3) });
    t.msgs.push(m);
    t.last = Math.max(t.last, m.t);
    if (!m.outgoing && m.t > (MSG.read[key] || 0)) t.unread++;
  }
  return T;
}

function markRead(key, t) {
  if (!t || (MSG.read[key] || 0) >= t) return;
  MSG.read[key] = t;
  store("msgRead", MSG.read);
}

/* unread badge + DM toasts, on every snapshot */
SNAP_HOOKS.push(snap => {
  const T = buildThreads(snap);
  if (S.tab === "messages" && T.has(MSG.thread)) markRead(MSG.thread, T.get(MSG.thread).last);
  let unread = 0;
  for (const t of T.values()) unread += t.kind === "mq" ? 0 : (S.tab === "messages" && t.key === MSG.thread ? 0 : t.unread);
  const b = $("#msgBadge");
  b.hidden = !unread;
  b.textContent = unread > 99 ? "99+" : unread;
  const top = snap.msgSeq || 0;
  if (MSG.lastSeq == null || top < MSG.lastSeq) { MSG.lastSeq = top; return; }
  for (const m of snap.messages) {
    if (m.seq > MSG.lastSeq && m.dm && !m.outgoing && !(S.tab === "messages" && MSG.thread === threadOf(m))) {
      toast(`💬 ${nodeName(m.from)}: ${m.text}`, true);
    }
  }
  MSG.lastSeq = top;
});

function threadLabel(t) {
  if (t.kind === "ch") return `<span class="chnum">#${t.index}</span>${esc(t.name)}`;
  if (t.kind === "mq") return `<span class="tag mqtt">MQTT</span> ${esc(t.name)}`;
  return `<span class="short" style="--c:${hopColor(S.byNum.get(t.peer))}">${esc(shortOf(t.peer))}</span>${esc(nodeName(t.peer))}`;
}

const STATUS_TXT = {
  sending: ["…", "sending"], heard: ["✓", "sent - a neighbour repeated it"], delivered: ["✓✓", "delivered"],
  failed: ["✗", "not delivered"], noack: ["?", "no acknowledgement"],
};

function msgHtml(m, t) {
  const when = new Date(m.t * 1000);
  const time = when.toDateString() === new Date().toDateString() ? when.toLocaleTimeString() : when.toLocaleString();
  if (m.outgoing) {
    const [ic, txt] = STATUS_TXT[m.status] || ["", m.status || ""];
    const err = m.status === "failed" && m.error ? `: ${m.error.replaceAll("_", " ").toLowerCase()}` : "";
    return `<div class="msg out ${esc(m.status || "")}"><div class="body">${esc(m.text)}</div>
      <div class="meta">${esc(time)}${m.src === "demo" ? " · demo (not transmitted)" : ""} · <span class="st" title="${esc(txt + err)}">${ic} ${esc(txt + err)}</span></div></div>`;
  }
  const n = S.byNum.get(m.from);
  const bits = [esc(time)];
  if (m.hops != null) bits.push(m.hops === 0 ? "direct" : `${m.hops} hop${m.hops > 1 ? "s" : ""}`);
  if (m.snr != null) bits.push(`${m.snr} dB`);
  if (t.kind !== "dm" && m.to != null && m.to !== BCAST) bits.push(`to ${esc(nodeName(m.to))}`);
  const viaMqtt = m.src === "mqtt"
    ? `<span class="tag mqtt" title="Received only through the MQTT bridge, not over the air by your radio">via MQTT${m.gateway != null ? ` · gateway ${esc(shortOf(m.gateway))}` : ""}</span>` : "";
  return `<div class="msg in"><div class="meta"><span class="short" style="--c:${hopColor(n)}">${esc(shortOf(m.from))}</span>
      <b class="who" data-select="${m.from}">${esc(nodeName(m.from))}</b><span class="muted">${bits.join(" · ")}</span>${viaMqtt}</div>
    <div class="body">${esc(m.text)}</div></div>`;
}

function renderMessages(force) {
  if (!S.snap) return;
  const T = buildThreads(S.snap);
  if (!T.has(MSG.thread)) {
    const first = [...T.values()].find(t => t.kind === "ch" && t.live) || [...T.values()][0];
    if (first && !MSG.thread.startsWith("dm:")) MSG.thread = first.key;
  }
  // side list
  const chans = [...T.values()].filter(t => t.kind === "ch").sort((a, b) => a.index - b.index);
  const mqs = [...T.values()].filter(t => t.kind === "mq").sort((a, b) => b.last - a.last);
  const dms = [...T.values()].filter(t => t.kind === "dm").sort((a, b) => b.last - a.last);
  if (MSG.thread.startsWith("dm:") && !T.has(MSG.thread)) dms.unshift({ key: MSG.thread, kind: "dm", peer: +MSG.thread.slice(3), msgs: [], unread: 0, last: 0 });
  const item = t => `<div class="th${t.key === MSG.thread ? " sel" : ""}${t.kind === "ch" && !t.live ? " stale" : ""}" data-thread="${esc(t.key)}">
    <span class="nm">${threadLabel(t)}</span>${t.unread && t.key !== MSG.thread ? `<span class="badge">${t.unread}</span>` : ""}</div>`;
  $("#msgChannels").innerHTML = (chans.length ? chans.map(item).join("")
    : `<p class="muted small">${connected() ? "This radio reports no channels." : "Connect a radio to see its channels."}</p>`)
    + (mqs.length ? `<div class="subhead">Heard via MQTT only</div>` + mqs.map(item).join("") : "");
  $("#msgDms").innerHTML = dms.length ? dms.map(item).join("") : `<p class="muted small">None yet. Start one below, or use “Message” on any node.</p>`;
  const opts = [...S.snap.nodes].filter(n => !n.isLocal)
    .sort((a, b) => (b.favorite - a.favorite) || (b.lastHeard || 0) - (a.lastHeard || 0)).slice(0, 300);
  const sig = opts.map(n => n.num + (n.favorite ? "*" : "")).join(",");
  if ($("#msgNewDm").dataset.sig !== sig) {
    $("#msgNewDm").innerHTML = `<option value="">New direct message…</option>` + opts.map(n => `<option value="${n.num}">${n.favorite ? "★ " : ""}${esc(n.longName || n.id)} (${esc(shortName(n))})</option>`).join("");
    $("#msgNewDm").dataset.sig = sig;
  }

  // header + thread
  const t = T.get(MSG.thread) || dms.find(d => d.key === MSG.thread);
  let head = "", canSend = false, why = "";
  if (!t) {
    head = `<h3>Messages</h3>`;
    why = "Pick a channel or conversation";
  } else if (t.kind === "ch") {
    canSend = connected() && t.live;
    why = !connected() ? "Connect a radio to send" : !t.live ? "This channel isn't on the connected radio" : "";
    head = `<h3>#${t.index} ${esc(t.name)}</h3><span class="muted">${t.live ? (t.primary ? "primary channel" : "secondary channel") : "not on this radio"}${t.live ? ` · ${t.defaultKey ? "default (public) key" : "private key"}` : ""}${t.uplink ? " · radio uplinks to MQTT" : ""}</span>
      <span class="spacer"></span><button id="msgCfg" title="Add, rename or re-key channels">Edit channels</button>`;
  } else if (t.kind === "mq") {
    why = "Your radio isn't on this channel, so you can only read it";
    head = `<h3><span class="tag mqtt">MQTT</span> ${esc(t.name)}</h3><span class="muted">heard through the MQTT bridge only</span>`;
  } else {
    const n = S.byNum.get(t.peer);
    canSend = connected();
    why = connected() ? "" : "Connect a radio to send";
    head = `<h3><span class="short" style="--c:${hopColor(n)}">${esc(shortOf(t.peer))}</span>${esc(nodeName(t.peer))}</h3>
      <span class="muted">direct message · ${esc(hexId(t.peer))}${n?.hopsAway != null ? ` · ${n.hopsAway} hop${n.hopsAway === 1 ? "" : "s"} away` : ""}${n?.lastHeard ? ` · heard ${ago(n.lastHeard)}` : ""}</span>
      <span class="spacer"></span><button data-select="${t.peer}">Node details</button>`;
  }
  const msgs = t ? t.msgs : [];
  const sig2 = MSG.thread + "|" + msgs.length + "|" + msgs.map(m => m.seq + (m.status || "") + m.src).join(",") + "|" + connected();
  if (force || sig2 !== MSG.sig) {
    MSG.sig = sig2;
    $("#msgHead").innerHTML = head;
    const box = $("#msgThread");
    const atBottom = force || box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    box.innerHTML = msgs.length ? msgs.map(m => msgHtml(m, t)).join("")
      : `<div class="empty small"><p>${t?.kind === "dm" ? "No messages with this node yet." : "No messages on this channel yet."}</p></div>`;
    if (atBottom) box.scrollTop = box.scrollHeight;
    $("#msgCfg")?.addEventListener("click", () => VIEWS.config?.open?.("channels"));
  }
  if (t) markRead(t.key, t.last);
  const ta = $("#msgText");
  ta.disabled = !canSend;
  ta.placeholder = canSend ? (t.kind === "dm" ? `Message ${nodeName(t.peer)}… (Enter to send, Shift+Enter for a new line)`
    : `Message #${t.index} ${t.name}… (Enter to send, Shift+Enter for a new line)`) : why;
  updateBytes();
}

function updateBytes() {
  const n = enc.encode($("#msgText").value.trim()).length;
  const el = $("#msgBytes");
  el.textContent = `${n}/${MAX_BYTES}`;
  el.classList.toggle("over", n > MAX_BYTES);
  $("#msgSend").disabled = $("#msgText").disabled || !n || n > MAX_BYTES;
}

async function sendMessage() {
  const text = $("#msgText").value.trim();
  if (!text || $("#msgSend").disabled) return;
  const key = MSG.thread;
  const body = key.startsWith("dm:") ? { text, to: +key.slice(3), channel: 0 } : { text, channel: +key.slice(3) };
  $("#msgSend").disabled = true;
  try {
    await api("/api/send", body);
    $("#msgText").value = "";
    updateBytes();
  } catch (e) { toast("Not sent: " + e.message); updateBytes(); }
}

$("#msgChannels").addEventListener("click", e => pickThread(e));
$("#msgDms").addEventListener("click", e => pickThread(e));
function pickThread(e) {
  const t = e.target.closest("[data-thread]");
  if (!t) return;
  MSG.thread = t.dataset.thread;
  store("msgThread", MSG.thread);
  renderMessages(true);
  $("#msgText").focus();
}
$("#msgNewDm").addEventListener("change", e => { if (e.target.value) VIEWS.messages.openDm(+e.target.value); e.target.value = ""; });
$("#msgText").addEventListener("input", updateBytes);
$("#msgText").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); } });
$("#msgForm").addEventListener("submit", e => { e.preventDefault(); sendMessage(); });
