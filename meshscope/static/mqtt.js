/* MQTT bridge settings and status. */
"use strict";

const MQ = { timer: 0, info: null, formDirty: false };
const MQ_STATS = [
  ["received", "messages"], ["decoded", "decoded"], ["encrypted", "undecryptable"], ["duplicates", "already seen"],
  ["filtered", "too far away"], ["ignored", "ignored"], ["published", "uplinked"], ["uplinkSkippedSeen", "uplink skipped: on MQTT"],
  ["uplinkSkippedFlag", "uplink skipped: not OK to MQTT"],
];

VIEWS.mqtt = {
  show() { loadMqtt(true); clearInterval(MQ.timer); MQ.timer = setInterval(() => loadMqtt(false), 2000); },
  hide() { clearInterval(MQ.timer); },
};

SNAP_HOOKS.push(snap => {
  const dot = $("#mqttDot");
  const st = snap.mqttStatus;
  dot.hidden = !st || st === "stopped";
  dot.className = "dot" + (st === "error" ? " err" : st === "connecting" ? " wait" : "");
  dot.title = `MQTT ${st}`;
});

async function loadMqtt(fill) {
  try {
    const d = await api("/api/mqtt");
    MQ.info = d;
    const pill = $("#mqttStatus");
    pill.className = "pill " + ({ connected: "connected", connecting: "connecting", error: "error" }[d.status] || "");
    pill.textContent = d.status;
    $("#mqttDetail").textContent = d.detail || "";
    $("#mqttStart").textContent = d.status === "stopped" ? "Start" : "Reconnect";
    $("#mqttStop").disabled = d.status === "stopped";
    $("#mqttStats").innerHTML = MQ_STATS.map(([k, label]) =>
      `<div class="stat"><div class="v">${(d.stats[k] ?? 0).toLocaleString()}</div><div class="k">${esc(label)}</div></div>`).join("");
    if (fill || !MQ.formDirty) fillMqttForm(d);
  } catch (e) { console.error(e); }
}

function fillMqttForm(d) {
  const s = d.settings;
  $("#mqHost").value = s.host || "";
  $("#mqPort").value = s.port || 1883;
  $("#mqUser").value = s.username || "";
  $("#mqPass").value = s.password || "";
  $("#mqTls").checked = !!s.tls;
  $("#mqTopics").value = (s.topics || []).join("\n");
  $("#mqDist").value = s.maxDistanceKm ?? 0;
  $("#mqStrict").checked = !!s.strictDistance;
  $("#mqKeys").value = (s.channelKeys || []).map(k => `${k.name}=${k.psk}`).join("\n");
  $("#mqUplink").checked = !!s.uplink;
  $("#mqRoot").value = s.uplinkRoot || "";
  $("#mqDelay").value = s.uplinkDelay ?? 5;
  const allowed = new Set(s.uplinkChannels || []);
  const names = [...new Set([...d.radioChannels.map(c => c.name), ...allowed])];
  $("#mqChannels").innerHTML = names.length
    ? names.map(n => {
      const rc = d.radioChannels.find(c => c.name === n);
      return `<label class="chk"><input type="checkbox" data-upch="${esc(n)}" ${allowed.has(n) ? "checked" : ""}> ${esc(n)}${rc ? (rc.defaultKey ? ' <span class="note">public key</span>' : "") : ' <span class="note">not on radio</span>'}</label>`;
    }).join("")
    : `<span class="muted small">Connect your radio to choose which of its channels may be uplinked.</span>`;
  MQ.formDirty = false;
}

function mqttFormValues() {
  const keys = $("#mqKeys").value.split("\n").map(l => l.trim()).filter(Boolean).map(l => {
    const i = l.indexOf("=");
    if (i < 1) throw new Error(`Channel key line "${l}" should look like Name=base64key`);
    return { name: l.slice(0, i).trim(), psk: l.slice(i + 1).trim() };
  });
  return {
    host: $("#mqHost").value.trim(), port: +$("#mqPort").value || 1883,
    username: $("#mqUser").value, password: $("#mqPass").value, tls: $("#mqTls").checked,
    topics: $("#mqTopics").value.split("\n").map(t => t.trim()).filter(Boolean),
    maxDistanceKm: +$("#mqDist").value || 0, strictDistance: $("#mqStrict").checked, channelKeys: keys,
    uplink: $("#mqUplink").checked, uplinkRoot: $("#mqRoot").value.trim() || "msh/US",
    uplinkDelay: Math.max(1, Math.min(60, +$("#mqDelay").value || 5)),
    uplinkChannels: $$("#mqChannels input:checked").map(i => i.dataset.upch),
  };
}

async function saveMqtt() {
  try {
    await api("/api/mqtt/settings", mqttFormValues());
    MQ.formDirty = false;
    toast("MQTT settings saved", true);
    loadMqtt(true);
    return true;
  } catch (e) { toast(e.message); return false; }
}

$("#view-mqtt").addEventListener("input", e => { if (e.target.closest(".mqtt-grid")) MQ.formDirty = true; });
$("#mqttSave").onclick = saveMqtt;
$("#mqttStart").onclick = async () => {
  if (MQ.formDirty && !(await saveMqtt())) return;
  try { await api("/api/mqtt/start", {}); loadMqtt(false); } catch (e) { toast(e.message); }
};
$("#mqttStop").onclick = async () => {
  try { await api("/api/mqtt/stop", {}); loadMqtt(false); } catch (e) { toast(e.message); }
};
$("#mqUplink").addEventListener("input", e => {
  if (e.target.checked) toast("Uplink publishes packets your radio hears to the broker. Only broadcasts marked OK-to-MQTT on the ticked channels are sent.", true);
});
