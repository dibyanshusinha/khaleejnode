/* KhaleejNode — presentation layer controller.
 *
 * This script is intentionally "dumb": it renders state and posts intents to
 * the loopback core bridge (server/app.py). It performs NO licensing, NO vault
 * math, and holds NO secrets. Every security decision happens in the core.
 */

const API = ""; // same-origin loopback (browser/dev-server mode)
let selectedFile = null;
let maxSeenBalance = 10; // for the progress bar scale

// Detect the Tauri desktop shell. When present we call native Rust commands via
// invoke(); otherwise we fall back to the HTTP bridge so the browser demo works.
const TAURI = typeof window !== "undefined" && !!window.__TAURI__;
function invoke(cmd, args) {
  return window.__TAURI__.core.invoke(cmd, args);
}

// Maps the two REST routes to Tauri command names + arg shapes.
const CMD = {
  "/api/status": { name: "status", args: () => ({}) },
  "/api/refill": { name: "refill_token", args: (b) => ({ token: b.token }) },
  "/api/process": {
    name: "process",
    args: (b) => ({ filename: b.filename, contentBase64: b.content_base64 }),
  },
};

// ---------- transport helpers (Tauri invoke, else fetch) ----------
async function apiGet(path) {
  if (TAURI && CMD[path]) return invoke(CMD[path].name, CMD[path].args({}));
  const res = await fetch(API + path, { cache: "no-store" });
  return res.json();
}
async function apiPost(path, body) {
  if (TAURI && CMD[path]) {
    try {
      const data = await invoke(CMD[path].name, CMD[path].args(body || {}));
      return { ok: true, data };
    } catch (e) {
      // Rust commands reject with a string error message.
      return { ok: false, data: { error: String(e) } };
    }
  }
  const res = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return { ok: res.ok, data: await res.json() };
}
function $(id) { return document.getElementById(id); }
function fmtNum(n) { return Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 }); }
function setDot(itemId, ok) {
  const el = $(itemId);
  if (!el) return;
  const dot = el.querySelector(".dot");
  if (dot) dot.className = "dot " + (ok ? "on" : "bad");
}
const EMPTY_LOG_HTML =
  '<div class="log-empty">' +
  '<div class="log-empty-icon">🗂️</div>' +
  '<div class="log-empty-title">No checks run yet</div>' +
  '<div class="log-empty-sub">Drop a shipping PDF into the panel on the left to run ' +
  'your first customs validation. Extraction results and adversarial flags appear here.</div>' +
  "</div>";
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- status / routing ----------
async function refreshStatus() {
  const s = await apiGet("/api/status");

  $("edition").textContent = s.product.edition + " · v" + s.product.version;
  $("node-pill").textContent = "NODE " + (s.hardware.hardware_id_short || "—");

  if (!s.activated) {
    $("locked-view").classList.remove("hidden");
    $("dashboard-view").classList.add("hidden");
    $("lock-reason").textContent = s.license_reason || "This node is not activated.";
    $("lock-hwid").textContent = s.hardware.hardware_id || "—";
    return;
  }

  $("locked-view").classList.add("hidden");
  $("dashboard-view").classList.remove("hidden");

  // Security status bar
  setDot("st-license", s.activated);
  setDot("st-vault", s.vault_ok);
  if (s.product && s.product.region) $("st-region").textContent = s.product.region;

  if (!s.vault_ok) {
    $("token-count").textContent = "⚠";
    $("refill-msg").textContent = s.vault_message || "Vault integrity error.";
    $("refill-msg").className = "refill-msg err";
    return;
  }

  const bal = s.balance == null ? 0 : s.balance;
  if (bal > maxSeenBalance) maxSeenBalance = bal;
  $("token-count").textContent = bal;
  const pct = maxSeenBalance > 0 ? Math.min(100, (bal / maxSeenBalance) * 100) : 0;
  $("token-bar-fill").style.width = pct + "%";
  $("process-btn").disabled = !selectedFile || bal < 1;
}

// ---------- refill ----------
async function redeemToken() {
  const token = $("refill-input").value.trim();
  const msg = $("refill-msg");
  if (!token) {
    msg.textContent = "Paste a token first.";
    msg.className = "refill-msg err";
    return;
  }
  $("refill-btn").disabled = true;
  const { ok, data } = await apiPost("/api/refill", { token });
  $("refill-btn").disabled = false;

  if (ok && !data.error) {
    msg.textContent = `+${data.credits_added} credits added — new balance ${data.new_balance}.`;
    msg.className = "refill-msg ok";
    $("refill-input").value = "";
    await refreshStatus();
  } else {
    msg.textContent = data.error || "Token rejected.";
    msg.className = "refill-msg err";
  }
}

// ---------- file selection ----------
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const res = reader.result || "";
      const comma = String(res).indexOf(",");
      resolve(comma >= 0 ? String(res).slice(comma + 1) : "");
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}
function setFile(file) {
  selectedFile = file;
  $("file-name").textContent = file ? `Selected: ${file.name} (${file.size} bytes)` : "";
  refreshStatus();
}

// ---------- process ----------
async function processDocument() {
  if (!selectedFile) return;
  $("process-btn").disabled = true;
  $("process-btn").textContent = "Analyzing…";

  const content_base64 = await fileToBase64(selectedFile);
  const { ok, data } = await apiPost("/api/process", {
    filename: selectedFile.name,
    content_base64,
  });

  $("process-btn").textContent = "Run Customs Check";
  if (ok && !data.error) {
    renderResult(data);
    await refreshStatus();
  } else {
    renderError(data.error || "Processing failed.");
    await refreshStatus();
  }
}

// ---------- log rendering ----------
function renderError(message) {
  const log = $("log");
  clearEmpty();
  const el = document.createElement("div");
  el.className = "log-entry";
  el.innerHTML = `<div class="log-head"><span class="log-file">Error</span>
    <span class="badge held">FAILED</span></div>
    <div class="flag-msg">${esc(message)}</div>`;
  log.prepend(el);
}

function renderResult(data) {
  const log = $("log");
  clearEmpty();

  const v = data.validation;
  const sub = data.customs_submission;
  const badge = { READY: "ready", REVIEW: "review", HELD: "held" }[sub.status] || "held";
  const badgeText = sub.status;

  const flagsHtml = v.flags.length
    ? v.flags
        .map(
          (f) => `<div class="flag">
            <span class="sev ${f.severity}">${f.severity}</span>
            <span><span class="flag-msg">${esc(f.message)}</span>
            ${f.field ? `<span class="flag-field"> [${esc(f.field)}]</span>` : ""}</span>
          </div>`
        )
        .join("")
    : `<div class="flag"><span class="sev INFO">CLEAN</span>
       <span class="flag-msg">No adversarial flags raised.</span></div>`;

  // Compliance summary strip (risk score, duty, screening).
  const c = v.compliance || {};
  const duty = c.duty ? c.duty.total_duty_aed : 0;
  const value = c.duty ? c.duty.total_declared_value_aed : 0;
  const risk = c.risk_score != null ? c.risk_score : "—";
  const band = (c.risk_band || "n/a");
  const hits = c.screening && c.screening.denied_party_hits ? c.screening.denied_party_hits.length : 0;
  const sample = (sub.data_mode || c.data_mode) !== "OFFICIAL";
  const complianceHtml = `
    <div class="compliance-bar">
      <span class="risk-chip risk-${esc(band)}">RISK ${esc(String(risk))} · ${esc(band.toUpperCase())}</span>
      <span class="cmeta">Duty <b>AED ${fmtNum(duty)}</b></span>
      <span class="cmeta">Value <b>AED ${fmtNum(value)}</b></span>
      ${hits ? `<span class="risk-chip risk-critical">⚠ ${hits} sanctions match</span>` : ""}
      ${sample ? `<span class="risk-chip sample-chip" title="Screening ran on non-authoritative sample lists">⚠ SAMPLE DATA</span>` : ""}
      ${sub.human_review_required ? `<span class="risk-chip risk-high">HUMAN REVIEW REQUIRED</span>` : ""}
    </div>`;

  const jsonId = "json-" + Math.random().toString(36).slice(2, 8);
  const entry = document.createElement("div");
  entry.className = "log-entry";
  entry.innerHTML = `
    <div class="log-head">
      <span class="log-file">${esc(data.filename)}</span>
      <span class="badge ${badge}">${esc(badgeText)}</span>
    </div>
    <div class="muted small">
      BL ${esc(data.manifest.bill_of_lading)} ·
      ${esc(data.manifest.port_of_loading)} → ${esc(data.manifest.port_of_discharge)} ·
      ${data.manifest.items.length} line item(s) ·
      balance now ${data.new_balance}
    </div>
    ${complianceHtml}
    <div style="margin-top:10px">${flagsHtml}</div>
    <div class="muted small" style="margin-top:8px">${esc(sub.note)}</div>
    ${sub.disclaimer ? `<div class="disclaimer">${esc(sub.disclaimer)}</div>` : ""}
    <div class="toggle-json" onclick="document.getElementById('${jsonId}').classList.toggle('hidden')">
      ▸ Toggle structured JSON extraction
    </div>
    <pre id="${jsonId}" class="json-block hidden">${esc(JSON.stringify(data.manifest, null, 2))}</pre>
  `;
  log.prepend(entry);
}

function clearEmpty() {
  const empty = document.querySelector(".log-empty");
  if (empty) empty.remove();
}

// ---------- wiring ----------
function initDropzone() {
  const dz = $("dropzone");
  const input = $("file-input");
  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", (e) => {
    if (e.target.files && e.target.files[0]) setFile(e.target.files[0]);
  });
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.remove("dragover");
    })
  );
  dz.addEventListener("drop", (e) => {
    if (e.dataTransfer.files && e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initDropzone();
  $("refill-btn").addEventListener("click", redeemToken);
  $("process-btn").addEventListener("click", processDocument);
  $("clear-log").addEventListener("click", () => {
    $("log").innerHTML = EMPTY_LOG_HTML;
  });
  refreshStatus();
  setInterval(refreshStatus, 5000);
});
