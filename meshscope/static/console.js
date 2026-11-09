/* Console: run `meshtastic` CLI commands on the connected radio, and watch the device log. */
"use strict";

const CLI = { seq: 0, timer: 0, hist: store("cliHistory") || [], hpos: -1, pending: null };

VIEWS.console = {
  show() {
    pollConsole();
    clearInterval(CLI.timer);
    CLI.timer = setInterval(pollConsole, 700);
    setTimeout(() => $("#cliInput").focus(), 0);
  },
  hide() { clearInterval(CLI.timer); },
};

async function pollConsole() {
  try {
    const d = await api(`/api/console?since=${CLI.seq}`);
    if (d.seq < CLI.seq) { CLI.seq = 0; $("#cliOut").innerHTML = ""; return; } // app restarted
    const out = $("#cliOut");
    const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
    if (d.lines.length) {
      const frag = document.createDocumentFragment();
      for (const l of d.lines) {
        const div = document.createElement("div");
        div.className = l.kind;
        div.textContent = l.kind === "cmd" ? `> ${l.text}` : l.text;
        if (l.kind === "device") div.title = clock(l.t);
        frag.appendChild(div);
      }
      out.appendChild(frag);
      while (out.childElementCount > 5000) out.firstElementChild.remove();
      if (atBottom) out.scrollTop = out.scrollHeight;
    }
    CLI.seq = d.seq;
    $("#cliForm button").disabled = d.busy;
    $("#cliForm button").textContent = d.busy ? "Running…" : "Run";
    if (!out.childElementCount) {
      out.innerHTML = `<div class="sys">${realRadio()
        ? "Type CLI arguments below, e.g. --info, --get lora, --set lora.hop_limit 4. The device log streams here too."
        : "Connect to a real radio to use the CLI (it isn't available in demo mode)."}</div>`;
    }
  } catch (e) { console.error(e); }
}

async function runCli(args, confirmed = false) {
  args = args.trim();
  if (!args) return;
  try {
    const r = await api("/api/console/run", { args, confirmed });
    if (r.confirm) {
      CLI.pending = args;
      const bar = $("#cliConfirm");
      bar.hidden = false;
      bar.innerHTML = `<span>⚠ <b>${esc(r.flags.join(", "))}</b> can change, reboot or wipe the radio. Run <code>${esc(args)}</code>?</span>
        <span class="spacer"></span><button class="danger" id="cliYes">Run it</button><button id="cliNo">Cancel</button>`;
      $("#cliYes").onclick = () => { bar.hidden = true; runCli(CLI.pending, true); };
      $("#cliNo").onclick = () => { bar.hidden = true; CLI.pending = null; };
      return;
    }
    CLI.hist = [args, ...CLI.hist.filter(h => h !== args)].slice(0, 100);
    store("cliHistory", CLI.hist);
    CLI.hpos = -1;
    $("#cliInput").value = "";
    pollConsole();
  } catch (e) {
    const div = document.createElement("div");
    div.className = "err";
    div.textContent = e.message;
    $("#cliOut").appendChild(div);
    $("#cliOut").scrollTop = $("#cliOut").scrollHeight;
  }
}

$("#cliForm").addEventListener("submit", e => { e.preventDefault(); runCli($("#cliInput").value); });
$("#cliInput").addEventListener("keydown", e => {
  if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
  e.preventDefault();
  CLI.hpos = Math.max(-1, Math.min(CLI.hist.length - 1, CLI.hpos + (e.key === "ArrowUp" ? 1 : -1)));
  $("#cliInput").value = CLI.hpos < 0 ? "" : CLI.hist[CLI.hpos];
});
$$("[data-cli]").forEach(b => b.addEventListener("click", () => runCli(b.dataset.cli)));
$("#cliClear").onclick = async () => { await api("/api/console/clear", {}).catch(() => {}); $("#cliOut").innerHTML = ""; pollConsole(); };
$("#cliDevice").checked = store("cliDevice") ?? true;
const applyDeviceToggle = () => { $("#cliOut").classList.toggle("nodev", !$("#cliDevice").checked); store("cliDevice", $("#cliDevice").checked); };
$("#cliDevice").addEventListener("input", applyDeviceToggle);
applyDeviceToggle();
