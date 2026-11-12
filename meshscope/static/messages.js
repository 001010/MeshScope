/* Messages: the radio's channels, direct messages, and sending text through the radio.
   A message gets the "via MQTT" badge only when it reached MeshScope solely through the
   MQTT bridge; anything the USB radio heard over the air is shown as a radio packet. */
"use strict";

const MSG = { thread: store("msgThread") || "ch:0", read: store("msgRead") || {}, sig: "", lastSeq: null,
  sel: null, reply: null, pickFor: null };  // selected message (seq), message being replied to, picker target
const QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏", "🔥", "✅"];
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
    if (!m.outgoing && !m.emoji && m.t > (MSG.read[key] || 0)) t.unread++;  // reactions aren't new messages
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
      toast(m.emoji ? `${nodeName(m.from)} reacted ${m.text}` : `💬 ${nodeName(m.from)}: ${m.text}`, true);
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

/** Emoji reactions are folded into the message they react to (shown as chips under it, like Slack);
 *  text replies stay in the thread with a muted quote of the message they answer. */
function foldReplies(msgs) {
  const byPid = new Map();
  for (const m of S.snap.messages) if (m.packetId != null && !m.emoji) byPid.set(m.packetId, m);
  const reacts = new Map(); // seq of the target message -> Map(emoji -> [node nums])
  const shown = [];
  for (const m of msgs) {
    const target = m.emoji && m.replyId != null ? byPid.get(m.replyId) : null;
    if (target && threadOf(target) === threadOf(m)) {
      const r = reacts.get(target.seq) || new Map();
      reacts.set(target.seq, r);
      const who = r.get(m.text) || [];
      if (!who.includes(m.from)) who.push(m.from);
      r.set(m.text, who);
    } else {
      shown.push(m);
    }
  }
  return { shown, reacts, byPid };
}
function quoteHtml(m, ctx) {
  if (m.replyId == null) return "";
  const q = ctx.byPid.get(m.replyId);
  const verb = m.emoji ? "reacted to" : "replying to";
  if (!q) return `<div class="quote">↪ ${verb} a message MeshScope didn't receive</div>`;
  const who = q.outgoing ? "You" : nodeName(q.from);
  return `<div class="quote" data-jump="${q.seq}" title="${esc(verb)} ${esc(who)}: ${esc(q.text)}">↪ <b>${esc(who)}</b> ${esc(q.text)}</div>`;
}
function reactsHtml(m, ctx) {
  const r = ctx.reacts.get(m.seq);
  if (!r) return "";
  return `<div class="reacts">${[...r].map(([e, who]) => `<span class="react" title="${esc(who.map(nodeName).join(", "))}">${esc(e)}`
    + `${who.length > 1 ? `<b>${who.length}</b>` : ""}</span>`).join("")}</div>`;
}

/** Reply / React buttons: shown on hover, and kept open on the selected message */
function actsHtml(m, ctx) {
  if (!ctx.canAct || m.packetId == null || m.emoji) return "";
  return `<div class="msg-acts"><button type="button" data-reply="${m.seq}" title="Reply to this message">↩ Reply</button>`
    + `<button type="button" data-react="${m.seq}" title="React with an emoji">☺ React</button></div>`;
}

function msgHtml(m, t, ctx) {
  const when = new Date(m.t * 1000);
  const time = when.toDateString() === new Date().toDateString() ? when.toLocaleTimeString() : when.toLocaleString();
  if (m.outgoing) {
    const [ic, txt] = STATUS_TXT[m.status] || ["", m.status || ""];
    const err = m.status === "failed" && m.error ? `: ${m.error.replaceAll("_", " ").toLowerCase()}` : "";
    return `<div class="msg out ${esc(m.status || "")}${MSG.sel === m.seq ? " sel" : ""}" id="msg-${m.seq}" data-seq="${m.seq}">${actsHtml(m, ctx)}${quoteHtml(m, ctx)}<div class="body">${esc(m.text)}</div>${reactsHtml(m, ctx)}
      <div class="meta">${esc(time)}${m.src === "demo" ? " · demo (not transmitted)" : ""} · <span class="st" title="${esc(txt + err)}">${ic} ${esc(txt + err)}</span></div></div>`;
  }
  const n = S.byNum.get(m.from);
  const bits = [esc(time)];
  if (m.hops != null) bits.push(m.hops === 0 ? "direct" : `${m.hops} hop${m.hops > 1 ? "s" : ""}`);
  if (m.snr != null) bits.push(`${m.snr} dB`);
  if (t.kind !== "dm" && m.to != null && m.to !== BCAST) bits.push(`to ${esc(nodeName(m.to))}`);
  const viaMqtt = m.src === "mqtt"
    ? `<span class="tag mqtt" title="Received only through the MQTT bridge, not over the air by your radio">via MQTT${m.gateway != null ? ` · gateway ${esc(shortOf(m.gateway))}` : ""}</span>` : "";
  return `<div class="msg in${m.emoji ? " emoji" : ""}${MSG.sel === m.seq ? " sel" : ""}" id="msg-${m.seq}" data-seq="${m.seq}">${actsHtml(m, ctx)}<div class="meta"><span class="short" style="--c:${hopColor(n)}">${esc(shortOf(m.from))}</span>
      <b class="who" data-select="${m.from}">${esc(nodeName(m.from))}</b><span class="muted">${bits.join(" · ")}</span>${viaMqtt}</div>
    ${quoteHtml(m, ctx)}<div class="body">${esc(m.text)}</div>${reactsHtml(m, ctx)}</div>`;
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
  const sig2 = MSG.thread + "|" + msgs.length + "|" + msgs.map(m => m.seq + (m.status || "") + m.src).join(",") + "|" + connected() + "|" + canSend;
  if (MSG.reply && threadOf(MSG.reply) !== MSG.thread) setReply(null);  // a reply belongs to its own conversation
  if (!canSend) { setReply(null); closePicker(); }
  if (force || sig2 !== MSG.sig) {
    MSG.sig = sig2;
    $("#msgHead").innerHTML = head;
    const box = $("#msgThread");
    const atBottom = force || box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    const ctx = foldReplies(msgs);
    ctx.canAct = canSend && t.kind !== "mq";
    box.innerHTML = msgs.length ? ctx.shown.map(m => msgHtml(m, t, ctx)).join("")
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
  const body = { ...threadTarget(key), text, replyId: MSG.reply?.packetId ?? null };
  $("#msgSend").disabled = true;
  try {
    await api("/api/send", body);
    $("#msgText").value = "";
    setReply(null);
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

/* clicking a quote scrolls to the message it quotes */
$("#msgThread").addEventListener("click", e => {
  const q = e.target.closest(".quote[data-jump]");
  if (!q) return;
  const el = document.getElementById("msg-" + q.dataset.jump);
  if (!el) return;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
});

/* ---- reply / react */
function threadTarget(key) {
  return key.startsWith("dm:") ? { to: +key.slice(3), channel: 0 } : { channel: +key.slice(3) };
}
const msgBySeq = seq => S.snap.messages.find(m => m.seq === seq);

function setReply(m) {
  MSG.reply = m || null;
  const bar = $("#msgReply");
  if (!m) { bar.hidden = true; bar.innerHTML = ""; return; }
  bar.innerHTML = `<span class="lab">↩ Replying to</span> <b>${esc(m.outgoing ? "yourself" : nodeName(m.from))}</b>`
    + `<span class="txt">${esc(m.text)}</span><button type="button" class="x" title="Cancel reply (Esc)">×</button>`;
  bar.hidden = false;
  $("#msgText").focus();
}

function selectMsg(seq) {
  MSG.sel = MSG.sel === seq ? null : seq;
  $$("#msgThread .msg.sel").forEach(el => el.classList.remove("sel"));
  if (MSG.sel != null) document.getElementById("msg-" + MSG.sel)?.classList.add("sel");
}

function openPicker(btn, m) {
  MSG.pickFor = m;
  const p = $("#emojiPick");
  p.innerHTML = QUICK_REACTIONS.map(e => `<button type="button" data-emoji="${e}" title="React ${e}">${e}</button>`).join("")
    + `<input type="text" id="emojiOther" maxlength="16" placeholder="other…" title="Type or paste any emoji, then Enter">`;
  p.hidden = false;
  const r = btn.getBoundingClientRect(), w = p.offsetWidth, h = p.offsetHeight;
  p.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left)) + "px";
  p.style.top = (r.top - h - 6 > 8 ? r.top - h - 6 : r.bottom + 6) + "px";
  p.querySelector("button")?.focus();
}
function closePicker() { MSG.pickFor = null; const p = $("#emojiPick"); if (p) p.hidden = true; }

async function sendReaction(m, emoji) {
  emoji = (emoji || "").trim();
  closePicker();
  if (!emoji || !m) return;
  try {
    await api("/api/send", { ...threadTarget(threadOf(m)), text: emoji, replyId: m.packetId, emoji: true });
  } catch (e) { toast("Reaction not sent: " + e.message); }
}

$("#msgThread").addEventListener("click", e => {
  const rb = e.target.closest("[data-reply]");
  if (rb) { setReply(msgBySeq(+rb.dataset.reply)); return; }
  const xb = e.target.closest("[data-react]");
  if (xb) { e.stopPropagation(); openPicker(xb, msgBySeq(+xb.dataset.react)); return; }
  if (e.target.closest(".quote, .who, a, button")) return;
  const el = e.target.closest(".msg[data-seq]");
  if (el && el.querySelector(".msg-acts")) selectMsg(+el.dataset.seq);
});
$("#msgReply").addEventListener("click", e => { if (e.target.closest(".x")) setReply(null); });
$("#emojiPick").addEventListener("click", e => {
  e.stopPropagation();
  const b = e.target.closest("[data-emoji]");
  if (b) sendReaction(MSG.pickFor, b.dataset.emoji);
});
$("#emojiPick").addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.id === "emojiOther") { e.preventDefault(); sendReaction(MSG.pickFor, e.target.value); }
});
document.addEventListener("click", e => { if (!$("#emojiPick").hidden && !e.target.closest("#emojiPick")) closePicker(); });
document.addEventListener("keydown", e => {
  if (e.key !== "Escape" || S.tab !== "messages") return;
  if (!$("#emojiPick").hidden) closePicker();
  else if (MSG.reply) setReply(null);
  else if (MSG.sel != null) selectMsg(MSG.sel);
});
