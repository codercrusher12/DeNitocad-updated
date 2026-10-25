/* NitoCAD Studio: front end only.
   Preview first, pay when you generate: every message builds a free preview, and a part's full
   files (STEP, IGES, DXF, PDF) unlock when the person presses Generate, which spends credits.
   The price depends on the AI tier that planned the version, and each tier has a daily allowance of messages.
   All backend calls go through the `Backend` object near the top. When the chat backend
   changes, replace `Backend.send` and `Backend.anchor` and leave the rest alone.
   Add ?demo=1 to the URL to explore the interface with sample parts and no sign-in. */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { applyRealisticLook, prepareStlGeometry, buildPartObject, materialFor, setWireframe, disposeObject } from "./viewer-look.js";

const Auth = window.NitoAuth;
const BASE = Auth.API_BASE;
const DEMO = new URLSearchParams(location.search).get("demo") === "1";
const $ = (id) => document.getElementById(id);
const esc = (v) => { const d = document.createElement("div"); d.textContent = v == null ? "" : String(v); return d.innerHTML; };
const uid = () => Math.random().toString(36).slice(2, 10);
const IS_TOUCH = matchMedia("(hover: none)").matches;

/* ============================================================ backend adapter */
const Backend = {
  /* One chat turn. The server reads the whole conversation and the current part's spec
     (POST /chats/{id}/messages), decides build, edit or answer, charges, builds and stores
     everything. Returns { reply, part?, needCredits?, balance?, authExpired?, error? }. */
  async send({ chat, text }) {
    if (DEMO) return Demo.send(text, lastPart(chat));
    const r = await api("POST", `/chats/${chat.id}/messages`, { content: text, model: S.model });
    if (r.authExpired) return { authExpired: true };
    if (!r.ok) {
      const msg = r.status === 409 ? "I am still working on your last message. Give me a moment."
        : r.status === 404 ? "I could not find this chat any more. Start a new design."
        : r.detail || "Something went wrong building that. Please try again.";
      return { reply: msg, error: true };
    }
    const d = r.data;
    return {
      reply: d.assistant_message.content, part: d.part ? fromServerPart(d.part) : null,
      needCredits: !!d.needs_credits, balance: d.balance,
      choices: (d.assistant_message.meta && d.assistant_message.meta.choices) || [],
      diff: d.diff || (d.assistant_message.meta && d.assistant_message.meta.diff) || null,
      requirements: d.requirements || [], newRequirements: d.new_requirements || []
    };
  },

  async anchor(part) {
    if (DEMO) { await sleep(1400); return { anchor_tx: "0x" + uid() + uid() + uid() + uid() + uid() + uid(), explorer_url: "https://scan.botchain.ai" }; }
    const session = Auth.getSession();
    if (!session) throw new Error("Your session expired. Sign in again.");
    const auth = { Authorization: "Bearer " + session.session_id };
    const prep = await fetch(`${BASE}/api/jobs/${part.jobId}/anchor/prepare`, { method: "POST", headers: auth });
    const p = await prep.json().catch(() => ({}));
    if (!prep.ok) throw new Error(p.detail || "Could not prepare the anchor.");
    if (p.already_anchored) return { anchor_tx: p.anchor_tx, explorer_url: p.explorer_url };
    if (!window.ethereum) throw new Error("No wallet found to sign the anchor.");
    try { await window.ethereum.request({ method: "wallet_switchEthereumChain", params: [{ chainId: p.tx.chainId }] }); } catch (e) { /* already on chain */ }
    const txHash = await window.ethereum.request({ method: "eth_sendTransaction", params: [p.tx] });
    for (let i = 0; i < 20; i++) {
      const r = await fetch(`${BASE}/api/jobs/${part.jobId}/anchor/confirm`, {
        method: "POST", headers: { ...auth, "Content-Type": "application/json" }, body: JSON.stringify({ tx_hash: txHash })
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "Anchor verification failed.");
      if (d.status === "anchored") return d;
      await sleep(2000);
    }
    throw new Error("The anchor was submitted but is not confirmed yet. Check again shortly.");
  },

  async exportFile(part, fmt) {
    if (DEMO) { await sleep(700); toast("Demo mode: no file is produced."); return; }
    const session = Auth.getSession();
    if (!session) throw new Error("Your session expired. Sign in again.");
    if (fmt === "stl") {
      const blob = await fetchStl(part, true);
      return blobDownload(blob, (part.title || "part").replace(/\s+/g, "_") + ".stl");
    }
    if (fmt.startsWith("pcb:")) {
      const kind = fmt.slice(4);
      const pr = await fetch(`${BASE}/pcb/jobs/${part.jobId}/link`, { method: "POST", headers: { Authorization: "Bearer " + session.session_id, "Content-Type": "application/json" }, body: JSON.stringify({ kind }) });
      if (!pr.ok) { const e = await pr.json().catch(() => ({})); throw new Error(e.detail || "Download failed (HTTP " + pr.status + ").") }
      const pinfo = await pr.json();
      return deliver(BASE + pinfo.url, pinfo.filename || `${part.jobId}-${kind}`);
    }
    const r = await fetch(`${BASE}/export/${fmt}/${part.jobId}/link`, { method: "POST", headers: { Authorization: "Bearer " + session.session_id } });
    if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || "Export failed (HTTP " + r.status + ").") }
    const info = await r.json();
    deliver(BASE + info.url, info.filename || `${part.jobId}.${fmt}`);
  },

  /* The template picker's cards. */
  async templates() {
    if (DEMO) return DEMO_TEMPLATES;
    const r = await api("GET", "/templates");
    return r.ok ? r.data : null;
  },

  /* Start a part from a template: a free preview, no AI. Same reply shape as send(). */
  async applyTemplate({ chat, id, text }) {
    if (DEMO) return Demo.send(text, lastPart(chat));
    const r = await api("POST", `/chats/${chat.id}/templates/${id}`);
    if (r.authExpired) return { authExpired: true };
    if (!r.ok) return { reply: r.detail || "I could not start that template. Please try again.", error: true };
    const d = r.data;
    return { reply: d.assistant_message.content, part: d.part ? fromServerPart(d.part) : null, balance: d.balance, choices: [],
      requirements: d.requirements || [], newRequirements: d.new_requirements || [] };
  },

  /* Manufacturability check for one process: paid once per version and process, then reopened free. */
  async check(chat, part, process) {
    if (DEMO) { await sleep(800); return { ok: true, charged: 5, balance: S.balance == null ? 19 : S.balance - 5, report: demoReport(process) }; }
    const r = await api("POST", `/chats/${chat.id}/parts/${part.id}/check`, { process });
    if (r.authExpired) return { authExpired: true };
    if (r.status === 402) return { needCredits: true, detail: r.detail };
    if (!r.ok) return { error: r.detail || "I could not run the check. Please try again." };
    return { ok: true, report: r.data.report, charged: r.data.charged, balance: r.data.balance };
  },

  /* Pay for this version and unlock its full files. Safe to repeat: a version that is
     already paid costs nothing. Returns { ok, balance } or { needCredits } or { authExpired }. */
  async generate(chat, part) {
    if (DEMO) { await sleep(900); return { ok: true, charged: 1, balance: S.balance == null ? 23 : S.balance - 1 }; }
    const r = await api("POST", `/chats/${chat.id}/parts/${part.id}/generate`);
    if (r.authExpired) return { authExpired: true };
    if (r.status === 402) return { needCredits: true, detail: r.detail };
    if (!r.ok) return { error: r.detail || "I could not build the files. Please try again." };
    return { ok: true, balance: r.data.balance, charged: r.data.charged };
  },

  async buyCredits(tier) {
    const cfg = await Auth.getChainConfig();
    const price = tier === "single" ? cfg.credit_price_bot : tier === "pack_1000" ? cfg.pack_1000_price_bot : cfg.pack_10000_price_bot;
    const session = Auth.getSession();
    if (!session) throw new Error("Sign in first.");
    const account = await Auth.connectWallet();
    const wei = BigInt(Math.round(price * 1e18));
    const txHash = await window.ethereum.request({
      method: "eth_sendTransaction",
      params: [{ from: account, to: cfg.treasury_address, value: "0x" + wei.toString(16) }]
    });
    const r = await fetch(BASE + "/credits/purchase", {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + session.session_id },
      body: JSON.stringify({ tx_hash: txHash, tier })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Credit purchase failed.");
    return d.balance;
  }
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function flatParams(p) {
  const out = {};
  Object.entries(p.parameters || {}).forEach(([k, v]) => { if (k !== "operations" && typeof v !== "object") out[k] = v; });
  return out;
}
function collectNotes(data) {
  const v = data.validation || {}, ws = (v.warnings || []).map(String), notes = [...new Set(ws)];
  const nums = (s) => (String(s).match(/-?\d+(?:\.\d+)?/g) || []).map(Number);
  const has = (n, x) => n.some((y) => Math.abs(y - Number(x)) < 1e-6);
  // a warning that already states the old and new value covers its correction, so say it once
  Object.entries(v.corrections || {}).forEach(([k, c]) => { if (!ws.some((w) => { const n = nums(w); return has(n, c.old) && has(n, c.new); })) notes.push(`${k}: ${c.old} to ${c.new}`); });
  return notes;
}
function titleFor(t) { return String(t || "part").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()); }
function replyFor(part, isEdit) {
  const n = Object.keys(part.params).length;
  let s = isEdit ? `Updated. This is a new free preview of the ${part.title} with your change applied.` : `Here is a free preview of your ${part.title}, built as a solid with ${n} parameter${n === 1 ? "" : "s"}.`;
  if (part.notes && part.notes.length) {
    const kept = part.notes.filter((n) => String(n).includes("Kept as you typed")).length;
    s += kept === part.notes.length ? `\n\nI kept the sizes you typed and flagged a few that may cause problems. The details are under Specs.`
      : kept ? `\n\nI adjusted some values and flagged others you typed. The details are under Specs.`
      : `\n\nI adjusted a few values to keep it manufacturable. The details are under Specs.`;
  }
  s += `\n\nTell me what to change, as often as you like. When it looks right, press Generate for the STEP, IGES, DXF, PDF, GLB and 3MF files.`;
  return s;
}

/* ============================================================ server api */
async function api(method, path, body) {
  const s = Auth.getSession();
  if (!s) return { authExpired: true };
  let res;
  try {
    res = await fetch(BASE + path, {
      method, body: body ? JSON.stringify(body) : undefined,
      headers: { Authorization: "Bearer " + s.session_id, ...(body ? { "Content-Type": "application/json" } : {}) }
    });
  } catch (e) { return { ok: false, status: 0, detail: "I could not reach the server. Check your connection and try again." }; }
  if (res.status === 401) return { authExpired: true };
  let data = null; try { data = await res.json(); } catch (e) { /* empty body */ }
  const d = data && (data.detail || data.error);
  return { ok: res.ok, status: res.status, data, detail: typeof d === "string" ? d : (d ? "Please check your message and try again." : "") };
}
function fromServerPart(p) {
  const spec = p.spec || {};
  const tx = p.anchor_tx || null;
  return {
    id: p.id, version: p.version, jobId: p.job_id, title: p.title, partType: p.part_type, paid: p.paid !== false, checks: p.checks || {}, generateCredits: p.generate_credits || 10, aiTier: p.ai_tier || "basic",
    params: flatParams({ parameters: spec.parameters || {} }), material: spec.material || "", notes: p.notes || [],
    description: "", spec, anchor: tx ? { anchor_tx: tx, explorer_url: Auth.PINNED_CHAIN.explorer_url + "/tx/" + tx } : null,
    facts: p.facts || {}, reqResults: p.requirement_results || []
  };
}
function fromServerChat(c) {
  const parts = {};
  (c.parts || []).forEach((p) => { parts[p.id] = fromServerPart(p); });
  return {
    id: c.id, title: c.title, updatedAt: Date.parse(c.updated_at) || Date.now(), remote: true, loaded: true, parts, requirements: c.requirements || [],
    messages: (c.messages || []).map((m) => ({ id: m.id, role: m.role, text: m.content, partId: m.part_id || undefined, needCredits: !!(m.meta && m.meta.needs_credits), choices: (m.meta && m.meta.choices) || [], diff: (m.meta && m.meta.diff) || null }))
  };
}

/* ============================================================ demo backend */
const Demo = {
  async send(text, lastPart) {
    await sleep(1800);
    const t = (lastPart ? lastPart.description + " " : "") + text;
    const low = t.toLowerCase();
    const kind = /gear/.test(low) ? "gear" : /shaft|coupling|rod/.test(low) ? "shaft" : /box|enclosure|case/.test(low) ? "box" : "bracket";
    const num = (re, d) => { const m = low.match(re); return m ? parseFloat(m[1]) : d; };
    const thick = /thicker/.test(low) && lastPart ? Math.round(lastPart.params.thickness_mm * 1.2 * 10) / 10 : num(/(\d+(?:\.\d+)?)\s*mm thick/, 3);
    const defs = {
      bracket: { width_mm: num(/(\d+)\s*mm wide/, 50), height_mm: num(/(\d+)\s*mm tall/, 60), thickness_mm: thick, holes: num(/(\d+)\s*holes/, 4), fillet_mm: 5 },
      gear: { teeth: num(/(\d+)\s*teeth/, 20), module: num(/module\s*(\d+(?:\.\d+)?)/, 2), thickness_mm: num(/(\d+)\s*mm thick/, 10), bore_mm: num(/(\d+)\s*mm bore/, 5) },
      shaft: { diameter_mm: num(/(\d+)\s*mm diameter/, 10), length_mm: num(/(\d+)\s*mm long/, 50), chamfer_mm: 0.5 },
      box: { length_mm: 100, width_mm: 80, height_mm: 50, wall_mm: 3, lid: true }
    };
    const part = {
      id: uid(), description: t, jobId: "demo-" + uid(), demoKind: kind, partType: kind === "bracket" ? "l_bracket" : kind,
      params: defs[kind], material: "6061 aluminum", notes: [], createdAt: Date.now(), paid: false
    };
    part.title = titleFor(part.partType);
    return { reply: replyFor(part, !!lastPart), part };
  },
  geometry(kind, p) {
    const g = new THREE.Group();
    const mat = materialFor("aluminum");
    const add = (geo, x = 0, y = 0, z = 0) => { const m = new THREE.Mesh(geo, mat); m.position.set(x, y, z); g.add(m); return m; };
    if (kind === "gear") {
      const N = Math.max(6, Math.round(p.teeth || 20)), ro = (p.module || 2) * N / 2 + (p.module || 2), ri = ro - (p.module || 2) * 2.2;
      const sh = new THREE.Shape();
      for (let i = 0; i < N; i++) {
        const a = (i / N) * Math.PI * 2, w = Math.PI / N;
        const pts = [[ri, a], [ri, a + w * 0.3], [ro, a + w * 0.55], [ro, a + w * 0.95], [ri, a + w * 1.2]];
        pts.forEach(([r, ang], j) => { const x = Math.cos(ang) * r, y = Math.sin(ang) * r; (i === 0 && j === 0) ? sh.moveTo(x, y) : sh.lineTo(x, y); });
      }
      const hole = new THREE.Path(); hole.absarc(0, 0, (p.bore_mm || 5) / 2, 0, Math.PI * 2, true); sh.holes.push(hole);
      const geo = new THREE.ExtrudeGeometry(sh, { depth: p.thickness_mm || 10, bevelEnabled: false });
      geo.rotateX(-Math.PI / 2); add(geo);
    } else if (kind === "shaft") {
      const r = (p.diameter_mm || 10) / 2, L = p.length_mm || 50, c = p.chamfer_mm || 0.5;
      const geo = new THREE.LatheGeometry([[0, 0], [r - c, 0], [r, c], [r, L - c], [r - c, L], [0, L]].map(([x, y]) => new THREE.Vector2(x, y)), 48);
      geo.rotateZ(Math.PI / 2); add(geo);
    } else if (kind === "box") {
      const L = p.length_mm, W = p.width_mm, H = p.height_mm, t = p.wall_mm;
      add(new THREE.BoxGeometry(L, t, W), 0, t / 2, 0);
      add(new THREE.BoxGeometry(L, H, t), 0, H / 2, W / 2 - t / 2);
      add(new THREE.BoxGeometry(L, H, t), 0, H / 2, -W / 2 + t / 2);
      add(new THREE.BoxGeometry(t, H, W - 2 * t), L / 2 - t / 2, H / 2, 0);
      add(new THREE.BoxGeometry(t, H, W - 2 * t), -L / 2 + t / 2, H / 2, 0);
    } else {
      const w = p.width_mm || 50, h = p.height_mm || 60, t = p.thickness_mm || 3, d = 40;
      const base = new THREE.Shape(); base.moveTo(0, 0); base.lineTo(w, 0); base.lineTo(w, d); base.lineTo(0, d); base.closePath();
      const n = Math.min(6, Math.max(0, Math.round(p.holes || 4)));
      for (let i = 0; i < n; i++) { const hp = new THREE.Path(); hp.absarc(w * (0.2 + 0.6 * (i % 2)), d * (0.28 + 0.44 * Math.floor(i / 2) / Math.max(1, Math.ceil(n / 2) - 1 || 1)), 2.6, 0, Math.PI * 2, true); base.holes.push(hp); }
      const g1 = new THREE.ExtrudeGeometry(base, { depth: t, bevelEnabled: false }); g1.rotateX(-Math.PI / 2); g1.translate(-w / 2, 0, d / 2);
      add(g1);
      add(new THREE.BoxGeometry(w, h, t), 0, h / 2, -d / 2 + t / 2);
    }
    const b = new THREE.Box3().setFromObject(g), c = b.getCenter(new THREE.Vector3());
    g.children.forEach((m) => { m.position.sub(new THREE.Vector3(c.x, 0, c.z)); });
    return g;
  }
};

/* ============================================================ state + storage */
const S = { chats: [], activeId: null, activePartId: null, busy: false, balance: null, tab: "specs" };
let storeKey = "nitocad_studio_v1:guest";
function userKey() { const s = Auth.getSession(); return DEMO ? "demo" : (s ? (s.email || s.wallet_address || "user") : "guest"); }
async function load() {
  S.activeId = null;
  storeKey = "nitocad_studio_v1:" + userKey();
  if (DEMO) {
    try { S.chats = JSON.parse(localStorage.getItem(storeKey) || "[]"); } catch (e) { S.chats = []; }
    S.chats.sort((a, b) => b.updatedAt - a.updatedAt);
    return;
  }
  await migrateLocal();
  const r = await api("GET", "/chats");
  if (r.authExpired) { Auth.clearSession(); return; }
  S.chats = r.ok ? r.data.chats.map((c) => ({
    id: c.id, title: c.title, updatedAt: Date.parse(c.updated_at) || Date.now(), remote: true, loaded: false,
    count: c.message_count, messages: [], parts: {}
  })) : [];
  if (!r.ok) toast(r.detail || "Could not load your chats.");
}
/* Chats saved in this browser before server storage existed are uploaded once, then set aside. */
async function migrateLocal() {
  let raw = null;
  try { raw = localStorage.getItem(storeKey); } catch (e) { return; }
  if (!raw) return;
  let chats = [];
  try { chats = JSON.parse(raw); } catch (e) { return; }
  chats = (chats || []).filter((c) => c && c.messages && c.messages.length).slice(0, 50);
  if (!chats.length) { try { localStorage.removeItem(storeKey); } catch (e) { /* ignore */ } return; }
  const r = await api("POST", "/chats/import", { chats });
  if (r.ok) {
    try { localStorage.setItem(storeKey + ":imported", raw); localStorage.removeItem(storeKey); } catch (e) { /* ignore */ }
    toast(`Moved ${r.data.imported} chat${r.data.imported === 1 ? "" : "s"} to your account.`);
  }
}
async function loadChat(c) {
  if (DEMO || c.loaded || !c.remote) return true;
  const r = await api("GET", `/chats/${c.id}`);
  if (r.authExpired) { Auth.clearSession(); return false; }
  if (!r.ok) { toast(r.detail || "Could not open that chat."); return false; }
  Object.assign(c, fromServerChat(r.data));
  return true;
}
let saveT = 0;
function save() {   /* demo mode only: real accounts are stored on the server */
  if (!DEMO) return;
  clearTimeout(saveT);
  saveT = setTimeout(() => { try { localStorage.setItem(storeKey, JSON.stringify(S.chats.slice(0, 60))); } catch (e) { /* ignore */ } }, 150);
}
const chat = () => S.chats.find((c) => c.id === S.activeId) || null;
function newChat() { const c = { id: uid(), title: "New design", createdAt: Date.now(), updatedAt: Date.now(), messages: [], parts: {}, remote: false, loaded: true }; S.chats.unshift(c); return c; }
function partOf(c, id) { return c && c.parts[id]; }
function lastPart(c) { if (!c) return null; for (let i = c.messages.length - 1; i >= 0; i--) { const m = c.messages[i]; if (m.partId && c.parts[m.partId]) return c.parts[m.partId]; } return null; }
function versionsOf(c, part) { const all = []; c.messages.forEach((m) => { if (m.partId && c.parts[m.partId]) all.push(c.parts[m.partId]); }); return all; }

/* ============================================================ toast + downloads */
let toastT;
function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("on"), 3200); }
const IS_MOBILE = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
function deliver(url, filename) {
  const a = document.createElement("a"); a.href = url; a.rel = "noopener"; a.style.display = "none";
  document.body.appendChild(a); a.click(); setTimeout(() => a.remove(), 1000);
  if (IS_MOBILE) saveBar(url, filename);
}
function saveBar(url, filename) {
  const old = $("saveBar"); if (old) old.remove();
  const bar = document.createElement("div"); bar.id = "saveBar";
  bar.style.cssText = "position:fixed;left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom,0px));z-index:200;background:#1C1C1C;color:#fff;border-radius:14px;padding:12px 14px;display:flex;gap:10px;align-items:center;font-size:.85rem;";
  bar.innerHTML = `<span style="flex:1;min-width:0;word-break:break-all">Download started. Not saving? ${esc(filename)}</span>`;
  const a = document.createElement("a"); a.href = url; a.textContent = "Tap to save"; a.setAttribute("download", filename);
  a.style.cssText = "background:#13A57D;color:#fff;padding:9px 14px;border-radius:999px;font-weight:600;text-decoration:none;white-space:nowrap";
  const x = document.createElement("button"); x.textContent = "\u2715"; x.setAttribute("aria-label", "Dismiss"); x.style.cssText = "background:none;border:0;color:#A6A6A6;font-size:18px";
  x.onclick = () => bar.remove(); a.onclick = () => setTimeout(() => bar.remove(), 1500);
  bar.append(a, x); document.body.appendChild(bar); setTimeout(() => bar.remove(), 25000);
}
async function blobDownload(blob, filename) {
  if (IS_MOBILE && navigator.canShare && navigator.share) {
    try { const f = new File([blob], filename, { type: blob.type || "application/octet-stream" }); if (navigator.canShare({ files: [f] })) { await navigator.share({ files: [f], title: filename }); return; } }
    catch (e) { if (e && e.name === "AbortError") return; }
  }
  const url = URL.createObjectURL(blob);
  if (IS_MOBILE) { saveBar(url, filename); return; }
  const a = document.createElement("a"); a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
async function fetchStl(part, asBlob) {
  const url = `${BASE}/download/job/${part.jobId}/stl`;
  const r = await fetch(url, { headers: Auth.authHeader() });
  if (!r.ok) throw new Error("Could not load the mesh (HTTP " + r.status + ").");
  return asBlob ? r.blob() : r.arrayBuffer();
}

/* ============================================================ viewer */
const V = { ready: false, running: false, mesh: null, wire: false, grid: true, token: 0 };
function viewerInit() {
  if (V.ready) return;
  const el = $("cvStage");
  V.scene = new THREE.Scene(); V.scene.background = new THREE.Color(0xEDF2F4);
  V.camera = new THREE.PerspectiveCamera(40, 1, 0.1, 2000);
  V.renderer = new THREE.WebGLRenderer({ antialias: true });
  V.renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  el.insertBefore(V.renderer.domElement, el.firstChild);
  V.controls = new OrbitControls(V.camera, V.renderer.domElement);
  V.controls.enableDamping = true;
  // the studio environment does most of the lighting, so these two only add shape and a shadow
  V.scene.add(new THREE.HemisphereLight(0xffffff, 0xb7c3d1, 0.35));
  const dl = new THREE.DirectionalLight(0xffffff, 1.6); dl.position.set(60, 90, 50); V.scene.add(dl);
  applyRealisticLook(V.scene, V.renderer, dl);
  V.gridObj = new THREE.GridHelper(160, 16, 0xb7c3d1, 0xd5dde6); V.scene.add(V.gridObj);
  new ResizeObserver(viewerFit).observe(el);
  V.ready = true;
}
function viewerFit() {
  if (!V.ready) return;
  const el = $("cvStage"), w = el.clientWidth, h = el.clientHeight;
  if (!w || !h) return;
  V.renderer.setSize(w, h); V.camera.aspect = w / h; V.camera.updateProjectionMatrix();
}
function viewerLoop() { if (!V.running) return; requestAnimationFrame(viewerLoop); V.controls.update(); V.renderer.render(V.scene, V.camera); }
function viewerReset() {
  if (!V.ready) return;
  const y = V.mesh ? V.mesh.userData.midY || 0 : 0;
  V.camera.position.set(95, 80 + y, 95); V.controls.target.set(0, y, 0); V.controls.update();
}
function viewerStatus(msg, loading) {
  const stage = $("cvStage"); let el = stage.querySelector(".cv-load");
  if (!msg) { if (el) el.remove(); return; }
  if (!el) { el = document.createElement("div"); el.className = "cv-load"; el.innerHTML = "<i></i><span></span>"; stage.appendChild(el); }
  el.querySelector("i").style.display = loading ? "" : "none";
  el.querySelector("span").textContent = msg;
}
function applyWire() { if (V.mesh) setWireframe(V.mesh, V.wire); }
function viewerPlace(obj) {
  if (V.mesh) { V.scene.remove(V.mesh); disposeObject(V.mesh); }
  const wrap = new THREE.Group(); wrap.add(obj);
  const b0 = new THREE.Box3().setFromObject(wrap), size = b0.getSize(new THREE.Vector3());
  const k = 70 / (Math.max(size.x, size.y, size.z) || 1);
  wrap.scale.setScalar(k);
  const b1 = new THREE.Box3().setFromObject(wrap), c = b1.getCenter(new THREE.Vector3());
  wrap.position.set(-c.x, -b1.min.y, -c.z);
  wrap.userData.midY = (b1.max.y - b1.min.y) / 2;
  V.mesh = wrap; V.scene.add(wrap); applyWire(); viewerReset(); viewerStatus("");
  const d = $("cvDim"); d.hidden = false; const big = Math.max(size.x, size.y, size.z) >= 3000;   /* civil parts read better in metres */
  const fmt = (v) => big ? (v / 1000).toFixed(2) : v.toFixed(1);
  d.textContent = `${fmt(size.x)} x ${fmt(size.z)} x ${fmt(size.y)} ${big ? "m" : "mm"}`;
}
async function viewerShow(part) {
  $("cvEmpty").hidden = !!part;
  if (!part) { if (V.mesh) { V.scene.remove(V.mesh); V.mesh = null; } $("cvDim").hidden = true; return; }
  viewerInit(); viewerFit();
  if (!V.running) { V.running = true; viewerLoop(); }
  const token = ++V.token;
  viewerStatus("Loading part", true);
  try {
    let obj;
    if (part.demoKind) { await sleep(250); obj = Demo.geometry(part.demoKind, part.params); }
    else {
      const geo = prepareStlGeometry(new STLLoader().parse(await fetchStl(part, false)));
      geo.center(); geo.rotateX(-Math.PI / 2);
      obj = buildPartObject(geo, part.material);
    }
    if (token !== V.token) return;
    viewerPlace(obj);
  } catch (e) { if (token === V.token) viewerStatus(e.message || "Could not load the part.", false); }
}

/* ============================================================ rendering: sidebar */
function groupLabel(ts) {
  const d = (Date.now() - ts) / 864e5;
  return d < 1 ? "Today" : d < 2 ? "Yesterday" : d < 8 ? "Previous 7 days" : "Earlier";
}
function renderSide() {
  const list = $("chatList");
  const real = S.chats.filter((c) => c.messages.length || c.count);
  if (!real.length) { list.innerHTML = '<div class="chat-empty">Your designs will show up here. Start one with a message.</div>'; return; }
  let html = "", last = "";
  real.forEach((c) => {
    const g = groupLabel(c.updatedAt);
    if (g !== last) { html += `<div class="chat-group">${g}</div>`; last = g; }
    html += `<div class="chat-item${c.id === S.activeId ? " active" : ""}"><button class="chat-open" data-open="${c.id}">${esc(c.title)}</button>` +
      `<button class="chat-del" data-del="${c.id}" aria-label="Delete ${esc(c.title)}"><svg class="icon" viewBox="0 0 24 24"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13"/></svg></button></div>`;
  });
  list.innerHTML = html;
}

/* ============================================================ rendering: thread */
const STARTERS = [
  ["Motor mount", "Mounting bracket for a 50mm stepper motor, 4 holes, 5mm fillets, 3mm thick"],
  ["Spur gear", "Gear with 24 teeth, module 2, 10mm thick, 6mm bore"],
  ["Enclosure", "Box enclosure 100x80x50mm, 3mm walls, with lid"],
  ["Drive shaft", "Shaft 12mm diameter, 120mm long, 1mm chamfer"]
];
function thumbSvg(kind) {
  const s = '<svg viewBox="0 0 84 68" fill="none" stroke="#1C1C1E" stroke-width="1.4" stroke-linejoin="round">';
  const shapes = {
    gear: '<circle cx="42" cy="34" r="20" fill="#fff"/><circle cx="42" cy="34" r="6"/><path d="M42 10v6M42 52v6M18 34h6M60 34h6M25 17l4 4M55 47l4 4M59 17l-4 4M29 47l-4 4"/>',
    shaft: '<rect x="8" y="27" width="68" height="14" rx="2" fill="#fff"/><path d="M14 27v14M70 27v14"/>',
    box: '<path d="M14 26l28-12 28 12v22L42 60 14 48z" fill="#fff"/><path d="M14 26l28 12 28-12M42 38v22"/>',
    assembly: '<rect x="10" y="46" width="64" height="10" rx="1" fill="#fff"/><path d="M24 46V24h12v12h22v10" fill="#fff"/><circle cx="30" cy="31" r="2"/><path d="M58 36v-8l8 4z"/>',
    bracket: '<path d="M18 18h14v28h34v10H18z" fill="#fff"/><circle cx="25" cy="27" r="2"/><circle cx="25" cy="42" r="2"/><circle cx="48" cy="51" r="2"/><circle cx="60" cy="51" r="2"/>'
  };
  return s + (shapes[kind] || shapes.bracket) + "</svg>";
}
function kindOf(part) { return part.partType === "assembly" ? "assembly" : part.demoKind || (/gear/.test(part.partType) ? "gear" : /shaft/.test(part.partType) ? "shaft" : /box|enclosure/.test(part.partType) ? "box" : "bracket"); }
function para(text) { return String(text).split(/\n\n+/).map((t) => "<p>" + esc(t).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\n/g, "<br>") + "</p>").join(""); }
const ASSIST_AVA = '<div class="ava"><img src="logo-mark.png" alt=""></div>';

function partCardHtml(c, part, isLatest) {
  const vers = versionsOf(c, part), v = vers.findIndex((p) => p.id === part.id) + 1;
  const chips = part.partType === "assembly" ? assemblyChips(part) : Object.entries(part.params).slice(0, 4).map(([k, val]) => `<span class="spec-chip">${esc(k.replace(/_mm$/, ""))} ${esc(val)}${/_mm$/.test(k) ? " mm" : ""}</span>`).join("");
  const anchored = part.anchor ? '<span class="ver anchored">anchored</span>' : "";
  return `<div class="part"><button class="part-top" data-part="${part.id}" aria-label="Open ${esc(part.title)} in viewer">` +
    `<div class="part-thumb">${thumbSvg(kindOf(part))}</div>` +
    `<div class="part-meta"><div class="part-title">${esc(part.title)} <span class="ver">v${v}</span>${anchored}</div><div class="part-specs">${chips}</div></div>` +
    `<span class="part-open">Open <svg class="icon" viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M9 6l6 6-6 6"/></svg></span></button>` +
    `<div class="part-actions">` + (part.paid
      ? `<button class="chip" data-export="${part.id}">Download files</button><button class="chip" data-part="${part.id}" data-tab="chain">Anchor on-chain</button>`
      : `<button class="btn btn-primary btn-sm" data-generate="${part.id}">Generate files · ${part.generateCredits || 10} credits</button><span class="preview-tag">Free preview</span>`) + `</div></div>` +
    (isLatest ? followupsHtml(part) : "");
}
function followupsHtml(part) {
  const k = kindOf(part);
  const set = { assembly: ["Make the base plate thicker", "Add another part on top", "Move the second part to the edge"], gear: ["Add 4 lightening holes", "Make the bore 8 mm", "Use 30 teeth"], shaft: ["Add a keyway", "Make it 150 mm long", "Add a 2 mm chamfer"], box: ["Add 4 corner screw posts", "Make the walls 4 mm", "Add a vent slot"], bracket: ["Make it thicker", "Add 2 more holes", "Use 8 mm fillets"] }[k];
  return '<div class="followups">' + set.map((t) => `<button class="chip" data-say="${esc(t)}">${esc(t)}</button>`).join("") + "</div>";
}
function renderThread(animateLast) {
  const inner = $("threadInner"), c = chat();
  if (!c || !c.messages.length) {
    inner.innerHTML = `<div class="hello"><div class="mark"><img src="logo-mark.png" alt=""></div><h1>What are we building?</h1>` +
      `<p>Describe a part in plain words. Then keep talking to change it. Every version stays in this chat.</p>` +
      `<div class="starters">${STARTERS.map(([t, p]) => `<button class="starter" data-say="${esc(p)}"><b>${esc(t)}</b><span>${esc(p)}</span></button>`).join("")}</div></div>`;
    return;
  }
  const latest = lastPart(c);
  inner.innerHTML = c.messages.map((m) => {
    if (m.role === "user") return `<div class="msg user"><div class="bubble">${esc(m.text)}</div></div>`;
    if (m.fileCard) return `<div class="msg">${ASSIST_AVA}<div class="reply" data-mid="${m.id}">${fileCardHtml(m)}</div></div>`;
    const part = m.partId ? partOf(c, m.partId) : null;
    let extra = "";
    if (part) extra = partCardHtml(c, part, latest && latest.id === part.id && m === c.messages[c.messages.length - 1]);
    if (m.diff) extra = diffHtml(m.diff) + extra;
    if (m.needCredits) extra = needCreditsHtml();
    if (m.choices && m.choices.length && m === c.messages[c.messages.length - 1]) extra += choicesHtml(m.choices);
    return `<div class="msg">${ASSIST_AVA}<div class="reply" data-mid="${m.id}">${para(m.text)}${extra}</div></div>`;
  }).join("");
}
/* What the last edit changed, measured on the build. Rows the server marks worse are highlighted. */
function diffHtml(d) {
  if (!d) return "";
  const li = (cls, html) => `<li${cls ? ` class="${cls}"` : ""}>${html}</li>`;
  const rows = [];
  (d.spec || []).forEach((t) => rows.push(li("", esc(t))));
  (d.facts || []).forEach((r) => rows.push(li(r.direction ? "d-" + r.direction : "", `<b>${esc(r.label)}</b> ${esc(String(r.before))} to ${esc(String(r.after))}`)));
  (d.requirements || []).forEach((r) => rows.push(li(r.direction ? "d-" + r.direction : "", `<b>${esc(r.text)}</b> ${esc(String(r.before))} to ${esc(String(r.after))}`)));
  if (!rows.length) return "";
  const head = d.from_version && d.to_version ? `What changed, version ${d.from_version} to ${d.to_version}` : "What changed";
  return `<div class="edit-diff${d.worse ? " has-worse" : ""}"><div class="edit-diff-h">${esc(head)}</div><ul>${rows.join("")}</ul></div>`;
}
/* Guidance from the AI as tappable questions. A tap sends the question and the answer together. */
function choicesHtml(choices) {
  return '<div class="choices">' + choices.map((q) => `<div class="choice-q"><p>${esc(q.question)}</p><div class="followups">` +
    q.options.map((o) => `<button class="chip" data-say="${esc(q.question + " " + o)}">${esc(o)}</button>`).join("") + "</div></div>").join("") + "</div>";
}
function needCreditsHtml() {
  return `<div class="need-credits"><b>You are out of credits</b><p>Generating the full files costs credits, and the price depends on the AI you used. Messages are free within each AI's daily allowance.</p><button class="btn btn-primary btn-sm" data-buy="open">Get credits</button></div>`;
}
function scrollEnd(smooth) { const t = $("thread"); t.scrollTo({ top: t.scrollHeight, behavior: smooth ? "smooth" : "auto" }); }

/* working indicator */
let workTimer = 0;
function showWorking() {
  const inner = $("threadInner");
  const steps = ["Reading your request", "Checking the dimensions", "Building the solid", "Preparing the preview"];
  const el = document.createElement("div"); el.className = "msg"; el.id = "working";
  el.innerHTML = ASSIST_AVA + '<div class="reply"><div class="working">' + steps.map((s, i) => `<div class="work-row ${i === 0 ? "live" : ""}" data-i="${i}"><span class="tick"></span><span>${s}</span></div>`).join("") + "</div></div>";
  inner.appendChild(el); scrollEnd(true);
  let i = 0;
  workTimer = setInterval(() => {
    const rows = el.querySelectorAll(".work-row");
    if (i < rows.length) { rows[i].className = "work-row done"; rows[i].querySelector(".tick").innerHTML = '<svg viewBox="0 0 12 12"><path d="M2 6.5l2.5 2.5L10 3.5"/></svg>'; }
    i++;
    if (i < rows.length) rows[i].className = "work-row live"; else clearInterval(workTimer);
  }, 1100);
}
function hideWorking() { clearInterval(workTimer); const w = $("working"); if (w) w.remove(); }

/* ============================================================ rendering: canvas */
function renderCanvas() {
  const c = chat(), part = c && S.activePartId ? partOf(c, S.activePartId) : null;
  $("cvTitle").textContent = part ? part.title : "Part viewer";
  const sel = $("cvVersion");
  if (part) {
    const vers = versionsOf(c, part);
    sel.hidden = vers.length < 2;
    sel.innerHTML = vers.map((p, i) => `<option value="${p.id}"${p.id === part.id ? " selected" : ""}>v${i + 1}</option>`).join("");
  } else sel.hidden = true;
  document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", t.dataset.tab === S.tab ? "true" : "false"));
  const panel = $("panel");
  if (!part) { panel.innerHTML = '<div class="chat-empty">Build a part to see its specs, files and on-chain record here.</div>'; return; }
  if (S.tab === "specs") {
    let rows = `<div class="row"><span>part_type</span><span>${esc(part.partType)}</span></div>`;
    Object.entries(part.params).forEach(([k, v]) => { rows += `<div class="row"><span>${esc(k)}</span><span>${esc(v)}</span></div>`; });
    if (part.material) rows += `<div class="row"><span>material</span><span>${esc(part.material)}</span></div>`;
    let notes = "";
    if (part.notes && part.notes.length) notes = `<div class="note"><b>Notes on this part</b><ul>${part.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>`;
    panel.innerHTML = part.partType === "assembly" ? `${assemblyHtml(part)}${requirementsHtml(part)}` : `<div class="kv">${rows}</div>${notes}${requirementsHtml(part)}`;
  } else if (S.tab === "files") {
    const F = [["STEP", "step", "3D solid for CAD and CAM"], ["IGES", "iges", "3D solid, older systems"], ["DXF", "dxf", "2D cut layers for laser and CNC"], ["PDF", "pdf", "1:1 drawing"], ["GLB", "glb", "3D model for web viewers, AR and game engines"], ["3MF", "3mf", "3D model for slicers and print services"], ["URDF", "urdf", "Robot description (zip) for ROS and simulators"], ["STL", "stl", "Mesh for 3D printing"]];
    const lock = !part.paid;
    const banner = lock ? `<div class="note"><b>Free preview</b><p>You can look at this version and download the STL mesh for free. Press Generate to unlock the STEP, IGES, DXF, PDF, GLB and 3MF files. That costs ${part.generateCredits || 10} credits, once for this version.</p><button class="btn btn-primary btn-sm" data-generate="${part.id}">Generate files · ${part.generateCredits || 10} credits</button></div>` : "";
    const rowsF = part.partType === "assembly" ? F.filter((r) => r[1] !== "urdf") : F;   // a URDF is one rigid link
    panel.innerHTML = banner + '<div class="files">' + rowsF.map(([n, f, d]) => {
      const locked = lock && f !== "stl";
      return `<div class="file${locked ? " locked" : ""}"><span class="ext">${n}</span><div class="what"><b>${n} file</b><span>${locked ? "Unlocks when you press Generate" : d}</span></div><button class="btn btn-ghost btn-sm" ${locked ? "disabled" : `data-dl="${f}"`}>${locked ? "Locked" : "Download"}</button></div>`;
    }).join("") + (part.partType === "pcb_board" ? [["KICAD", "pcb:kicad", "KiCad project", "Open and edit in KiCad"], ["GERBER", "pcb:gerbers", "Gerber and drill files", "Send to a circuit board maker"], ["BOM", "pcb:bom", "Parts list", "CSV of every part"], ["POS", "pcb:pos", "Part positions", "For assembly machines"]].map(([n, f, t, d]) =>
      `<div class="file${lock ? " locked" : ""}"><span class="ext">${n}</span><div class="what"><b>${t}</b><span>${lock ? "Unlocks when you press Generate" : d}</span></div><button class="btn btn-ghost btn-sm" ${lock ? "disabled" : `data-dl="${f}"`}>${lock ? "Locked" : "Download"}</button></div>`).join("") : "") + "</div>" + (part.partType === "pcb_board" && !lock ? '<div class="note"><p>Board files are built when you download them and can take up to a minute. Gerber, drill and part-position files need KiCad on the server.</p></div>' : "") + (lock ? "" : checkCardHtml(part));
  } else {
    if (part.anchor) {
      const tx = part.anchor.anchor_tx || "";
      panel.innerHTML = `<div class="chain"><div class="kick">Anchored on BOT Chain</div><b>This version has a permanent record</b><div class="tx">Tx ${esc(tx.slice(0, 12))}...${esc(tx.slice(-8))}</div><a class="btn btn-primary btn-sm" target="_blank" rel="noopener" href="${esc(part.anchor.explorer_url || "https://scan.botchain.ai")}">View on explorer</a></div>`;
    } else {
      panel.innerHTML = `<div class="chain"><div class="kick">Optional · BOT Chain</div><b>Anchor this version on-chain</b><ul>` +
        `<li>A timestamped record nobody can edit later, including us.</li><li>Your wallet is stored as the submitter, so you can show you registered it first.</li>` +
        `<li>Anyone can check the parameters and STEP file hashes on the explorer.</li></ul>` +
        `<div class="fine">Costs a small network fee from your wallet. Separate from downloading.</div><button class="btn btn-primary" id="anchorBtn">Anchor on BOT Chain</button></div>`;
    }
  }
}
/* ============================================================ manufacturability check */
const CHECK_PROCESSES = [["cnc", "CNC", "CNC machining"], ["sheet_metal", "Sheet metal", "sheet metal"], ["injection_molding", "Molding", "injection molding"], ["fdm", "FDM print", "FDM 3D printing"], ["resin", "Resin print", "resin 3D printing"]];
const CHECK_TAG = { pass: ["OK", "ok"], warn: ["Review", "warn"], fail: ["Problem", "bad"], not_checked: ["Not checked", "nc"], info: ["Note", "info"] };
function reportHtml(r) {
  const rows = (r.checks || []).map((c) => {
    const [label, cls] = CHECK_TAG[c.status] || CHECK_TAG.info;
    const meta = c.measured ? `<small>${esc(c.measured)}${c.limit ? " · limit " + esc(c.limit) : ""}</small>` : "";
    return `<div class="chk"><span class="chk-tag ${cls}">${label}</span><div><b>${esc(c.title)}</b><p>${esc(c.detail)}</p>${meta}</div></div>`;
  }).join("");
  const notes = (r.assumptions || []).concat(r.disclaimer ? [r.disclaimer] : []).map((a) => `<p class="rnote">${esc(a)}</p>`).join("");
  return `<div class="report"><p class="rhead">${esc(r.headline || "")}</p>${rows}${notes}</div>`;
}
const NO_CHECK = new Set(["pcb_board", "building", "road_segment", "box_culvert", "retaining_wall"]);   // the server refuses these too
function checkCardHtml(part) {
  if (NO_CHECK.has(part.partType)) return "";
  if (part.partType === "assembly") return `<p class="rnote">The manufacturability check runs on single parts. Assemblies are not checked yet.</p>`;
  const cur = S.checkProc || "cnc", saved = part.checks || {};
  const price = (S.ai && S.ai.check_credits) || 5;
  const label = (CHECK_PROCESSES.find((p) => p[0] === cur) || CHECK_PROCESSES[0])[2];
  const chips = CHECK_PROCESSES.map(([id, short]) => `<button class="chip${id === cur ? " on" : ""}" data-proc="${id}">${short}${saved[id] ? " · done" : ""}</button>`).join("");
  const body = saved[cur] ? reportHtml(saved[cur])
    : `<p>Checks this version against general rules for ${label}, such as wall thickness, corners, holes, draft or overhangs. It runs no AI and reads the part's mesh and dimensions. Guidance only, not a certified review.</p><button class="btn btn-primary btn-sm" data-check="${cur}">Run check · ${price} credits</button>`;
  return `<div class="check-card"><div class="kick">Manufacturability check</div><div class="followups">${chips}</div>${body}</div>`;
}
/* The Specs-tab section for an assembly: its parts with positions and mass, then the findings.
   Positions come from the stored parts list, mass and size from the report the build produced. */
function demoReport(process) {
  return { process, process_label: process, headline: "0 problems, 1 to review, 2 passed, 1 not checked.", disclaimer: "Demo data, not a real check.",
    checks: [{ id: "closed", title: "Mesh is closed", status: "pass", detail: "Every edge is shared by two triangles." },
             { id: "walls", title: "Thin walls", status: "warn", detail: "Some spots are thinner than the general minimum.", measured: "thinnest 0.7 mm", limit: "0.8 mm" },
             { id: "holes", title: "Hole depth to diameter", status: "pass", detail: "A standard drill reaches this depth." },
             { id: "access", title: "Tool access, setups and tolerances", status: "not_checked", detail: "These need a machining plan." }] };
}
async function runCheck(btn, part, proc) {
  const c = chat();
  if (!c || S.checking) return;
  S.checking = true; const label = btn.textContent; btn.disabled = true; btn.textContent = "Checking";
  try {
    const r = await Backend.check(c, part, proc);
    if (r.authExpired) { toast("Your session expired. Sign in again."); return; }
    if (r.needCredits) { toast(r.detail || "You are out of credits."); openCredits(); return; }
    if (r.error) { toast(r.error); return; }
    part.checks = part.checks || {}; part.checks[proc] = r.report; save();
    if (r.balance != null) setBalance(r.balance); else refreshBalance();
    toast(r.charged ? `Check done. ${r.charged} credits used.` : "Check opened.");
    renderCanvas();
  } catch (err) { toast(err.message || "Could not run the check."); }
  finally { S.checking = false; if (btn.isConnected) { btn.disabled = false; btn.textContent = label; } }
}

/* Assemblies: the parts list, what the overlap and floating-part checks found, and the mass. */
function assemblyChips(part) {
  const a = (part.facts && part.facts.assembly) || {}, bom = a.bom || {}, mp = a.mass_properties || {};
  const chips = [`${bom.part_count || 0} parts`];
  if (mp.complete) chips.push(`${mp.mass_g} g`);
  const inter = (a.interferences || []).length, groups = (a.disconnected_groups || []).length;
  chips.push(inter ? `${inter} overlap${inter === 1 ? "" : "s"}` : groups ? "floating parts" : "no overlaps");
  return chips.map((c) => `<span class="spec-chip">${esc(c)}</span>`).join("");
}
function assemblyHtml(part) {
  const a = (part.facts && part.facts.assembly) || {}, bom = a.bom || {}, mp = a.mass_properties || {};
  const size = a.overall_size_mm, cg = mp.center_of_mass_mm;
  let rows = `<div class="row"><span>parts</span><span>${bom.part_count || 0} (${bom.line_count || 0} on the list)</span></div>`;
  if (size) rows += `<div class="row"><span>overall size</span><span>${size.map((v) => Number(v).toFixed(1)).join(" x ")} mm</span></div>`;
  rows += `<div class="row"><span>mass</span><span>${mp.parts_counted ? esc(mp.mass_g) + " g" + (mp.complete ? "" : " (partial)") : "unknown"}</span></div>`;
  if (cg && mp.complete) rows += `<div class="row"><span>centre of mass</span><span>${cg.map((v) => Number(v).toFixed(1)).join(", ")} mm</span></div>`;

  const inter = a.interferences || [], groups = a.disconnected_groups || [], skipped = a.unchecked_pairs || [];
  let found = "";
  inter.forEach((i) => { found += `<li class="${i.severity === "major" ? "bad" : "warn"}"><b>${esc(i.a)}</b> and <b>${esc(i.b)}</b> overlap by ${esc(i.volume_mm3)} mm\u00b3${i.severity === "major" ? " (serious)" : ""}</li>`; });
  groups.forEach((g) => { found += `<li class="warn">Not touching the rest: ${g.map(esc).join(", ")}</li>`; });
  skipped.forEach((s) => { found += `<li class="warn">Could not check <b>${esc(s.a)}</b> against <b>${esc(s.b)}</b>: ${esc(s.reason)}</li>`; });
  const checks = found
    ? `<ul class="asm-find">${found}</ul>`
    : '<p class="asm-ok">No overlaps or floating parts found.</p>';

  /* One row per part (where it sits, how big, what it weighs) when the server sent them. Older saved
     versions have only the grouped parts list, so that is the fallback. */
  const num = (v) => +(+v).toFixed(1);
  const per = (a.parts || []).map((p) => `<tr><td>${esc(p.name)}<small>${esc(p.part_type || "")}</small></td><td>${esc((p.position_mm || []).map(num).join(", "))}</td>` +
    `<td>${p.size_mm ? esc(p.size_mm.map(num).join(" x ")) : ""}</td><td>${p.mass_g != null ? esc(p.mass_g) : "-"}</td></tr>`).join("");
  const list = (bom.rows || []).map((r) => `<tr><td>${esc(r.item)}</td><td>${esc(r.quantity)}</td><td>${esc(r.names.length > 1 ? r.names[0] + " +" + (r.names.length - 1) : r.names[0])}<small>${esc(r.part_type)}</small></td>` +
    `<td>${esc(r.material || "-")}</td><td>${r.total_mass_g == null ? "-" : esc(r.total_mass_g)}</td></tr>`).join("");
  const table = per
    ? `<div class="asm-scroll"><table class="asm-table"><thead><tr><th>Part</th><th>Position mm</th><th>Size mm</th><th>g</th></tr></thead><tbody>${per}</tbody></table></div>`
    : list ? `<div class="asm-scroll"><table class="asm-table"><thead><tr><th>#</th><th>Qty</th><th>Part</th><th>Material</th><th>g</th></tr></thead><tbody>${list}</tbody></table></div>` : "";
  const notes = (part.notes || []).length ? `<div class="note"><b>Notes</b><ul>${part.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>` : "";
  return `<div class="kv">${rows}</div><div class="asm"><h4>Checks</h4>${checks}` +
    `<p class="asm-fine">Geometric checks only. Not a fit, clearance or tolerance analysis.</p>` +
    `<h4>Parts list</h4>${table}` +
    (a.bom_csv ? `<button type="button" class="btn btn-ghost btn-sm" data-bom-csv>Download parts list (CSV)</button>` : "") + `</div>${notes}`;
}
/* Requirements: said once in the chat ("must fit in 100 x 80 x 60 mm"), re-checked on every version. */
function requirementsHtml(part) {
  if (DEMO) return "";
  const rows = (part.reqResults || []).map((r) => {
    const label = { pass: "Meets", fail: "Breaks", unknown: "Not measured" }[r.status] || r.status;
    return `<div class="req ${esc(r.status)}"><div><b>${esc(r.text)}</b><span>${esc(label)}${r.detail ? " · " + esc(r.detail) : ""}</span></div>` +
      `<button type="button" class="req-x" data-req-remove="${esc(r.id)}" aria-label="Remove requirement">\u00d7</button></div>`;
  }).join("");
  return `<div class="reqs"><h4>Requirements</h4>${rows || '<p class="req-none">None yet. Add one and every version is checked against it.</p>'}` +
    `<div class="req-add"><input id="reqInput" type="text" maxlength="300" placeholder="e.g. must fit in 100 x 80 x 60 mm" aria-label="Add a requirement"><button type="button" class="btn btn-ghost btn-sm" data-req-add>Add</button></div></div>`;
}
async function changeRequirements(c, action) {
  const r = await action();
  if (r.authExpired) { Auth.clearSession(); route(); return; }
  if (!r.ok) { toast(r.detail || "Could not change the requirement."); return; }
  c.loaded = false; await loadChat(c); renderCanvas();
}
/* Press Generate: pay once for this version and unlock its full files. */
async function generateFiles(btn, partId) {
  const c = chat(), part = c && partOf(c, partId);
  if (!part || part.paid || S.generating) return;
  S.generating = true; const label = btn.textContent; btn.disabled = true; btn.textContent = "Generating";
  try {
    const r = await Backend.generate(c, part);
    if (r.authExpired) { toast("Your session expired. Sign in again."); return; }
    if (r.needCredits) { toast(r.detail || "You are out of credits."); openCredits(); return; }
    if (r.error) { toast(r.error); return; }
    part.paid = true; save();
    if (r.balance != null) setBalance(r.balance); else refreshBalance();
    toast(r.charged === 0 ? "Files unlocked." : `Files unlocked. ${r.charged} credit${r.charged === 1 ? "" : "s"} used.`);
    renderThread(); renderCanvas();
  } catch (err) { toast(err.message || "Could not generate the files."); }
  finally { S.generating = false; if (btn.isConnected) { btn.disabled = false; btn.textContent = label; } }
}
function openCanvas(partId, tab) {
  const c = chat(); if (!c) return;
  if (partId) S.activePartId = partId;
  if (tab) S.tab = tab;
  $("app").classList.add("canvas-open");
  renderCanvas();
  const part = partOf(c, S.activePartId);
  viewerShow(part);
  if (innerWidth <= 1180) $("scrim").classList.add("on");
}
function closeCanvas() { $("app").classList.remove("canvas-open"); $("scrim").classList.remove("on"); }

/* ============================================================ sending */
async function send(text, templateId) {
  text = (text || "").trim();
  if (!text || S.busy) return;
  let c = chat();
  if (!c) { c = newChat(); S.activeId = c.id; }
  c.messages.push({ id: uid(), role: "user", text });
  if (c.title === "New design") c.title = text.length > 44 ? text.slice(0, 42).trim() + "..." : text;
  c.updatedAt = Date.now();
  if (!templateId) { $("input").value = ""; autosize(); }
  $("send").disabled = true;
  S.busy = true; renderThread(); renderSide(); $("chatTitle").textContent = c.title; scrollEnd(false); showWorking();
  let out;
  try {
    if (!DEMO && !c.remote) {   // the first message creates the chat on the server
      const created = await api("POST", "/chats", {});
      if (created.authExpired) { out = { authExpired: true }; }
      else if (!created.ok) { out = { reply: created.detail || "I could not start a new chat. Please try again.", error: true }; }
      else { c.id = created.data.id; c.remote = true; S.activeId = c.id; }
    }
    if (!out) out = templateId ? await Backend.applyTemplate({ chat: c, id: templateId, text }) : await Backend.send({ chat: c, text });
  } catch (e) { out = { reply: "Something went wrong on my side. Please try again.", error: true }; }
  hideWorking(); S.busy = false;
  if (out.authExpired) { Auth.clearSession(); c.messages.pop(); toast("Your session expired. Sign in again."); save(); route(); return; }
  const msg = { id: uid(), role: "assistant", text: out.reply || "" };
  if (out.needCredits) msg.needCredits = true;
  if (out.choices && out.choices.length) msg.choices = out.choices;
  if (out.diff) msg.diff = out.diff;
  if (out.part) { c.parts[out.part.id] = out.part; msg.partId = out.part.id; S.activePartId = out.part.id; out.part.reqResults = out.requirements || []; }
  else if (out.requirements && out.requirements.length) { const lp = lastPart(c); if (lp) lp.reqResults = out.requirements; }
  if (out.newRequirements && out.newRequirements.length) c.requirements = [...(c.requirements || []), ...out.newRequirements];
  c.messages.push(msg); c.updatedAt = Date.now(); save();
  if (out.balance != null) setBalance(out.balance);
  refreshAi();
  renderThread(); renderSide(); scrollEnd(true);
  if (out.part) { openCanvas(out.part.id, "specs"); if (innerWidth <= 1180) closeCanvas(); }
  $("send").disabled = !$("input").value.trim();
}


/* ============================================================ attached files */
/* Mesh files (STL, OBJ, GLB, 3MF) open right here in the browser and are never uploaded.
   STEP, IGES, BREP, PDF and images go to POST /files/analyze, which reads them for free and
   hands back sizes and a plain description. Nothing is built until the person sends a message,
   so a misread number is caught by a human. File cards live in this browser session only. */
const MAX_UPLOAD_MB = 15;
const MESH_EXT = new Set(["stl", "obj", "glb", "gltf", "3mf"]);
const SERVER_EXT = new Set(["step", "stp", "iges", "igs", "brep", "pdf", "png", "jpg", "jpeg", "webp",
  /* 2D drawings and electronics */
  "dxf", "svg", "kicad_pcb", "zip", "gbr", "ger", "gtl", "gbl", "gts", "gbs", "gto", "gbo", "gtp", "gbp", "gko", "gm1", "gm2", "gml", "g1", "g2", "g3", "g4", "drl", "xln", "exc"]);
const extOf = (n) => (String(n).split(".").pop() || "").toLowerCase();
const fmtBytes = (n) => n < 1048576 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB";
const num2 = (v) => String(Math.round(v * 100) / 100);
const fileStore = {};   /* message id -> { buffer?: ArrayBuffer, file?: File, description?: string } */
const fileMat = () => materialFor("");   // machined metal, not a flat colour

function stlObject(buf) {
  const geo = new STLLoader().parse(buf); geo.computeVertexNormals(); geo.center(); geo.rotateX(-Math.PI / 2);
  return new THREE.Mesh(geo, fileMat());
}
async function meshObject(file) {
  const ext = extOf(file.name), buf = await file.arrayBuffer();
  if (ext === "stl") return { obj: stlObject(buf), buf };
  let obj;
  if (ext === "obj") { const { OBJLoader } = await import("three/addons/loaders/OBJLoader.js"); obj = new OBJLoader().parse(new TextDecoder().decode(buf)); obj.traverse((o) => { if (o.isMesh) o.material = fileMat(); }); }
  else if (ext === "glb" || ext === "gltf") { const { GLTFLoader } = await import("three/addons/loaders/GLTFLoader.js"); const g = await new GLTFLoader().parseAsync(ext === "gltf" ? new TextDecoder().decode(buf) : buf, ""); obj = g.scene; }
  else { const { ThreeMFLoader } = await import("three/addons/loaders/3MFLoader.js"); obj = new ThreeMFLoader().parse(buf); }
  return { obj, buf };
}
function showInViewer(obj, title) {
  $("app").classList.add("canvas-open"); S.activePartId = null; renderCanvas();
  $("cvTitle").textContent = title; $("cvEmpty").hidden = true;
  viewerInit(); viewerFit(); if (!V.running) { V.running = true; viewerLoop(); }
  V.token++; viewerPlace(obj);
  if (innerWidth <= 1180) $("scrim").classList.add("on");
}
function b64ToBuf(b64) { const bin = atob(b64), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return u.buffer; }

function fileCardFromServer(file, r) {
  const a = r.analysis || {}, card = { name: r.filename || file.name, kind: r.kind, size: fmtBytes(r.size_bytes || file.size), rows: [], dims: [], features: [], missing: [], notes: [], warnings: r.warnings || [], description: r.description || "", text: "", numbers: [], mesh: !!r.stl_base64 };
  if (a.bbox_mm) {
    card.rows.push(["Overall size", `${num2(a.bbox_mm.x)} x ${num2(a.bbox_mm.y)} x ${num2(a.bbox_mm.z)} mm`]);
    if (a.volume_mm3 != null) card.rows.push(["Volume", `${Math.round(a.volume_mm3).toLocaleString("en-US")} mm3`]);
    card.rows.push(["Solids / faces", `${a.solids} / ${a.faces}`]);
    (a.round_holes || []).forEach((h) => card.rows.push(["Round hole", `${h.count} x ${num2(h.diameter_mm)} mm diameter`]));
    (a.round_bosses || []).forEach((h) => card.rows.push(["Round boss or shaft", `${h.count} x ${num2(h.diameter_mm)} mm diameter`]));
  }
  const v = a.vision;
  if (v) {
    card.summary = v.summary; card.guess = v.part_type_guess;
    card.dims = v.dimensions || []; card.features = v.features || []; card.missing = v.missing || []; card.notes = v.notes || [];
  }
  if (a.text_excerpt) card.text = a.text_excerpt;
  if (a.numbers_found) card.numbers = a.numbers_found;
  return card;
}
function fileCardHtml(m) {
  const k = m.fileCard;
  if (k.error) return `<div class="filecard"><div class="fc-head"><b>${esc(k.name)}</b></div><div class="fc-warn">${esc(k.error)}</div></div>`;
  let h = `<div class="filecard"><div class="fc-head"><b>${esc(k.name)}</b><span class="fc-kind">${esc(String(k.kind).toUpperCase())}</span></div><div class="fc-sub">${esc(k.size)}${k.local ? " · opened in your browser, not uploaded" : ""}</div>`;
  if (k.summary) h += `<div>${esc(k.summary)}</div>`;
  if (k.rows.length) h += "<dl>" + k.rows.map(([a, b]) => `<dt>${esc(a)}</dt><dd>${esc(b)}</dd>`).join("") + "</dl>";
  if (k.dims.length) h += '<div class="fc-h">Dimensions read from the file</div><ul>' + k.dims.map((d) => `<li>${esc(d.label)}: ${d.value_mm == null ? "not readable" : esc(num2(d.value_mm)) + " mm"}${d.raw ? ` <span class="fc-sub">(${esc(d.raw)})</span>` : ""}<span class="conf ${esc(d.confidence)}">${esc(d.confidence)}</span></li>`).join("") + "</ul>";
  if (k.features.length) h += '<div class="fc-h">Features</div><ul>' + k.features.map((f) => `<li>${esc(f)}</li>`).join("") + "</ul>";
  if (k.missing.length) h += '<div class="fc-h">Not shown in the file</div><ul>' + k.missing.map((f) => `<li>${esc(f)}</li>`).join("") + "</ul>";
  if (k.numbers && k.numbers.length) h += `<div class="fc-h">Numbers with units found in the text</div><div class="fc-sub">${k.numbers.map(esc).join(", ")}</div>`;
  if (k.text) h += `<details><summary>Text found in the PDF</summary><pre>${esc(k.text)}</pre></details>`;
  (k.notes || []).concat(k.warnings || []).forEach((w) => { h += `<div class="fc-warn">${esc(w)}</div>`; });
  if (k.description) h += `<div class="fc-desc">${esc(k.description)}</div>`;
  h += '<div class="fc-actions">';
  if (k.description) h += `<button class="btn btn-primary btn-sm" data-fuse="${m.id}">Use as my description</button>`;
  if (k.mesh) h += `<button class="btn btn-ghost btn-sm" data-fview="${m.id}">View in 3D</button>`;
  h += "</div>";
  if (k.description) h += '<div class="fc-sub" style="margin-top:8px">Check every number against your file, edit the text, then send it to build.</div>';
  return h + "</div>";
}
async function attachFile(file) {
  if (!file) return;
  if (DEMO) { toast("Sign in to attach files."); return; }
  if (S.busy) { toast("Wait for the current message to finish."); return; }
  const ext = extOf(file.name);
  let c = chat(); if (!c) { c = newChat(); S.activeId = c.id; }
  const push = (m) => { c.messages.push({ id: uid(), ...m }); c.updatedAt = Date.now(); };
  const bad = (msg) => { push({ role: "assistant", text: "", fileCard: { name: file.name, kind: ext || "file", size: fmtBytes(file.size), error: msg } }); renderThread(); scrollEnd(true); };
  push({ role: "user", text: `Attached ${file.name} (${fmtBytes(file.size)})` });
  if (c.title === "New design") c.title = "File: " + file.name.slice(0, 36);
  renderThread(); renderSide(); $("chatTitle").textContent = c.title; scrollEnd(false);
  if (file.size > MAX_UPLOAD_MB * 1048576) return bad(`That file is over ${MAX_UPLOAD_MB} MB.`);
  if (!file.size) return bad("The file is empty.");
  S.busy = true; showWorking();
  try {
    if (MESH_EXT.has(ext)) {
      const { obj, buf } = await meshObject(file);
      const id = uid(); fileStore[id] = { buffer: buf };
      c.messages.push({ id, role: "assistant", text: "", fileCard: { name: file.name, kind: ext, size: fmtBytes(file.size), rows: [], dims: [], features: [], missing: [], notes: [], warnings: ["Mesh files show shape only. To build a part from it, describe it in the chat."], description: "", local: true, mesh: ext === "stl" } });
      hideWorking(); S.busy = false; renderThread(); scrollEnd(true);
      showInViewer(obj, file.name);
      return;
    }
    if (!SERVER_EXT.has(ext)) { hideWorking(); S.busy = false; return bad("This file type is not supported. Use STEP, IGES, BREP, DXF, SVG, KiCad (.kicad_pcb), Gerber (a .zip or single layer), PDF, PNG, JPG, WEBP, STL, OBJ, GLB or 3MF."); }
    const fd = new FormData(); fd.append("file", file, file.name);
    let res;
    try { res = await fetch(`${BASE}/files/analyze`, { method: "POST", headers: Auth.authHeader(), body: fd }); }
    catch (e) { hideWorking(); S.busy = false; return bad("I could not reach the server. Check your connection and try again."); }
    if (res.status === 401) { hideWorking(); S.busy = false; Auth.clearSession(); toast("Your session expired. Sign in again."); route(); return; }
    let data = null; try { data = await res.json(); } catch (e) { /* empty */ }
    hideWorking(); S.busy = false;
    if (!res.ok) {
      const d = data && data.detail;
      return bad(res.status === 429 ? (typeof d === "string" ? d : "Too many files. Try again later.") : typeof d === "string" ? d : "That file could not be read.");
    }
    const id = uid(); const card = fileCardFromServer(file, data);
    fileStore[id] = { description: card.description, buffer: data.stl_base64 ? b64ToBuf(data.stl_base64) : null };
    c.messages.push({ id, role: "assistant", text: "", fileCard: card }); c.updatedAt = Date.now();
    renderThread(); scrollEnd(true);
    if (fileStore[id].buffer) showInViewer(stlObject(fileStore[id].buffer), card.name);
  } catch (e) {
    hideWorking(); S.busy = false; bad("I could not open that file. It may be damaged or in a variant I cannot read.");
  }
  $("send").disabled = !$("input").value.trim();
}
$("attach").addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", (e) => { const f = e.target.files && e.target.files[0]; e.target.value = ""; attachFile(f); });
["dragenter", "dragover"].forEach((ev) => $("composer").addEventListener(ev, (e) => { if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); $("composer").classList.add("drag"); } }));
["dragleave", "drop"].forEach((ev) => $("composer").addEventListener(ev, () => $("composer").classList.remove("drag")));
$("composer").addEventListener("drop", (e) => { const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]; if (f) { e.preventDefault(); attachFile(f); } });
try {   // a prompt sent from the landing page preview
  const pre = sessionStorage.getItem("nito_prefill");
  if (pre) { $("input").value = pre; sessionStorage.removeItem("nito_prefill"); $("input").dispatchEvent(new Event("input")); }
} catch (e) { /* storage blocked */ }
$("input").addEventListener("paste", (e) => { const f = [...((e.clipboardData && e.clipboardData.files) || [])][0]; if (f) { e.preventDefault(); attachFile(f); } });
$("threadInner").addEventListener("click", (e) => {
  const use = e.target.closest("[data-fuse]");
  if (use) { const f = fileStore[use.dataset.fuse]; if (f && f.description) { $("input").value = f.description; autosize(); $("send").disabled = false; $("input").focus(); toast("Edit the description if needed, then send it."); } return; }
  const view = e.target.closest("[data-fview]");
  if (view) { const f = fileStore[view.dataset.fview]; if (f && f.buffer) showInViewer(stlObject(f.buffer), "File preview"); }
});

/* ============================================================ credits */
function setBalance(b) {
  S.balance = b;
  $("creditNum").textContent = b == null ? "-" : b.toLocaleString();
  $("creditChip").classList.toggle("low", b != null && b <= 2);
}
async function refreshBalance() { setBalance(DEMO ? 24 : await Auth.fetchBalance()); }
/* ============================================================ templates */
const DEMO_TEMPLATES = { categories: [{ id: "standard_parts", label: "Standard parts" }, { id: "assemblies", label: "Assemblies" }],
  templates: [{ id: "bearing-608", title: "608 ball bearing", category: "standard_parts", category_label: "Standard parts", summary: "8 mm bore, 22 mm outside, 7 mm wide.", tags: ["Standard size"], hint: "Try: make the bore 10 mm", part_type: "bearing", parts: 1 },
    { id: "asm-gear-shaft", title: "Gear on a shaft", category: "assemblies", category_label: "Assemblies", summary: "A 20 tooth gear fitted on an 8 mm shaft.", tags: ["2 parts"], hint: "Try: use 30 teeth", part_type: "assembly", parts: 2 }] };
async function openTemplates() {
  const data = await Backend.templates();
  if (!data) { toast("I could not load the templates. Try again."); return; }
  const m = document.createElement("div"); m.className = "modal"; m.id = "tplModal";
  let cat = "all", q = "";
  const draw = () => {
    const cards = data.templates.filter((t) => (cat === "all" || t.category === cat) && (!q || (t.title + " " + t.summary + " " + t.tags.join(" ")).toLowerCase().includes(q)));
    m.querySelector(".tpl-grid").innerHTML = cards.length ? cards.map((t) =>
      `<div class="tpl"><b>${esc(t.title)}</b><span class="muted">${esc(t.summary)}</span><div class="followups">` +
      `<span class="tag">${esc(t.category_label)}</span>${t.tags.map((g) => `<span class="tag">${esc(g)}</span>`).join("")}</div>` +
      `<button class="btn btn-primary btn-sm" data-tpl="${esc(t.id)}" data-title="${esc(t.title)}">Use template</button></div>`).join("") : "<p class=\"muted\">No templates match.</p>";
    m.querySelectorAll("[data-cat]").forEach((b) => b.classList.toggle("on", b.dataset.cat === cat));
  };
  m.innerHTML = `<div class="modal-card tpl-card"><h3>Templates</h3><p>Start from a known part, then change it by chat. A template is a free preview, and no AI is used.</p>` +
    `<input type="search" id="tplSearch" placeholder="Search, for example bracket" aria-label="Search templates"><div class="followups"><button class="chip" data-cat="all">All</button>` +
    data.categories.map((c) => `<button class="chip" data-cat="${esc(c.id)}">${esc(c.label)}</button>`).join("") + `</div><div class="tpl-grid"></div>` +
    `<div class="modal-foot"><button class="btn btn-ghost btn-sm" data-close>Close</button></div></div>`;
  document.body.appendChild(m); draw();
  m.addEventListener("input", (e) => { if (e.target.id === "tplSearch") { q = e.target.value.trim().toLowerCase(); draw(); } });
  m.addEventListener("click", async (e) => {
    if (e.target === m || e.target.closest("[data-close]")) { m.remove(); return; }
    const c = e.target.closest("[data-cat]"); if (c) { cat = c.dataset.cat; draw(); return; }
    const t = e.target.closest("[data-tpl]"); if (t) { m.remove(); await send("Start from the template: " + t.dataset.title, t.dataset.tpl); }
  });
}

async function openCredits() {
  let cfg = null;
  try { cfg = DEMO ? { credit_price_bot: 0.01, pack_1000_price_bot: 1, pack_1000_credits: 1000, pack_10000_price_bot: 10, pack_10000_credits: 10000 } : await Auth.getChainConfig(); } catch (e) { toast("Could not load pricing. Try again shortly."); return; }
  const tiers = [["single", "1 credit", cfg.credit_price_bot], ["pack_1000", (cfg.pack_1000_credits || 1000).toLocaleString() + " credits", cfg.pack_1000_price_bot], ["pack_10000", (cfg.pack_10000_credits || 10000).toLocaleString() + " credits", cfg.pack_10000_price_bot]];
  const m = document.createElement("div"); m.className = "modal"; m.setAttribute("role", "dialog"); m.setAttribute("aria-modal", "true");
  m.innerHTML = `<div class="modal-card"><h3>Get credits</h3><p>Paid in native BOT on BOT Chain. Generating the files for a version costs 2 to 40 credits depending on the AI, and every export format is included. Messages are free within each AI's daily allowance.</p>` +
    tiers.map(([id, label, price]) => `<div class="tier"><div><b>${label}</b><span>${price} BOT</span></div><button class="btn btn-primary btn-sm" data-tier="${id}">Buy</button></div>`).join("") +
    `<div class="modal-foot"><button class="btn btn-ghost btn-sm" data-close>Close</button></div></div>`;
  document.body.appendChild(m);
  m.addEventListener("click", async (e) => {
    if (e.target === m || e.target.closest("[data-close]")) { m.remove(); return; }
    const b = e.target.closest("[data-tier]"); if (!b) return;
    b.disabled = true; const t = b.textContent; b.textContent = "Confirm in wallet";
    try { if (DEMO) { await sleep(900); } else { await Backend.buyCredits(b.dataset.tier); } await refreshBalance(); toast("Credits added."); m.remove(); }
    catch (err) { toast(err.message || "Purchase failed."); b.disabled = false; b.textContent = t; }
  });
}

/* ============================================================ composer */
function autosize() { const t = $("input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 180) + "px"; }
$("input").addEventListener("input", () => { autosize(); $("send").disabled = S.busy || !$("input").value.trim(); });
$("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !IS_TOUCH) { e.preventDefault(); send($("input").value); } });
$("send").addEventListener("click", () => send($("input").value));

/* ============================================================ events */
function openSide() { $("side").classList.add("open"); $("scrim").classList.add("on"); }
function closeSide() { $("side").classList.remove("open"); if (!$("app").classList.contains("canvas-open") || innerWidth > 1180) $("scrim").classList.remove("on"); }
$("menuBtn").addEventListener("click", openSide);
$("sideClose").addEventListener("click", closeSide);
$("scrim").addEventListener("click", () => { closeSide(); if (innerWidth <= 1180) closeCanvas(); $("scrim").classList.remove("on"); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeSide(); if (innerWidth <= 1180) closeCanvas(); } });
$("cvClose").addEventListener("click", closeCanvas);
$("canvasBtn").addEventListener("click", () => { const c = chat(), p = c && lastPart(c); if (p) openCanvas(S.activePartId || p.id); else toast("Build a part first."); });
$("creditChip").addEventListener("click", openCredits);
$("newChat").addEventListener("click", () => { S.activeId = null; S.activePartId = null; closeCanvas(); closeSide(); renderAll(); $("input").focus(); });
$("signOut").addEventListener("click", async () => { await Auth.signOut(); if (DEMO) location.href = "studio.html"; else route(); });
$("tReset").addEventListener("click", viewerReset);
$("tWire").addEventListener("click", (e) => { V.wire = !V.wire; e.currentTarget.setAttribute("aria-pressed", V.wire); applyWire(); });
$("tGrid").addEventListener("click", (e) => { V.grid = !V.grid; e.currentTarget.setAttribute("aria-pressed", V.grid); if (V.gridObj) V.gridObj.visible = V.grid; });
$("cvVersion").addEventListener("change", (e) => { S.activePartId = e.target.value; renderCanvas(); viewerShow(partOf(chat(), S.activePartId)); });
document.querySelector(".tabs").addEventListener("click", (e) => { const t = e.target.closest(".tab"); if (!t) return; S.tab = t.dataset.tab; renderCanvas(); });

$("chatList").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    if (!confirm("Delete this design chat?")) return;
    const id = del.dataset.del, c = S.chats.find((x) => x.id === id);
    if (c && c.remote) { const r = await api("DELETE", `/chats/${id}`); if (!r.ok && !r.authExpired) { toast(r.detail || "Could not delete that chat."); return; } }
    S.chats = S.chats.filter((x) => x.id !== id);
    if (S.activeId === id) { S.activeId = null; S.activePartId = null; closeCanvas(); }
    save(); renderAll(); return;
  }
  const open = e.target.closest("[data-open]");
  if (open) {
    const c = S.chats.find((x) => x.id === open.dataset.open); if (!c) return;
    closeSide(); closeCanvas();
    if (!c.loaded) { S.activeId = c.id; $("threadInner").innerHTML = '<div class="chat-empty">Opening chat...</div>'; if (!(await loadChat(c))) { S.activeId = null; renderAll(); route(); return; } }
    S.activeId = c.id; const p = lastPart(c); S.activePartId = p ? p.id : null;
    renderAll(); scrollEnd(false);
  }
});
$("threadInner").addEventListener("click", async (e) => {
  const say = e.target.closest("[data-say]"); if (say) { send(say.dataset.say); return; }
  const buy = e.target.closest("[data-buy]"); if (buy) { openCredits(); return; }
  const gen = e.target.closest("[data-generate]"); if (gen) { await generateFiles(gen, gen.dataset.generate); return; }
  const ex = e.target.closest("[data-export]");
  if (ex) { S.activePartId = ex.dataset.export; openCanvas(ex.dataset.export, "files"); return; }
  const pt = e.target.closest("[data-part]"); if (pt) openCanvas(pt.dataset.part, pt.dataset.tab || "specs");
});
$("panel").addEventListener("click", async (e) => {
  const c = chat(), part = c && partOf(c, S.activePartId); if (!part) return;
  const gen = e.target.closest("[data-generate]"); if (gen) { await generateFiles(gen, part.id); return; }
  const proc = e.target.closest("[data-proc]"); if (proc) { S.checkProc = proc.dataset.proc; renderCanvas(); return; }
  const chk = e.target.closest("[data-check]"); if (chk) { await runCheck(chk, part, chk.dataset.check); return; }
  if (e.target.closest("[data-bom-csv]")) {
    const csv = part.facts && part.facts.assembly && part.facts.assembly.bom_csv;
    if (csv) await blobDownload(new Blob([csv], { type: "text/csv" }), "parts_list.csv");
    return;
  }
  if (e.target.closest("[data-req-add]")) {
    const input = $("reqInput"), text = input ? input.value.trim() : "";
    if (text) await changeRequirements(c, () => api("POST", `/chats/${c.id}/requirements`, { text }));
    return;
  }
  const rm = e.target.closest("[data-req-remove]");
  if (rm) { await changeRequirements(c, () => api("DELETE", `/chats/${c.id}/requirements/${rm.dataset.reqRemove}`)); return; }
  const dl = e.target.closest("[data-dl]");
  if (dl) {
    dl.disabled = true; const t = dl.textContent; dl.textContent = "Building";
    try { await Backend.exportFile(part, dl.dataset.dl); } catch (err) { toast(err.message || "Download failed."); }
    dl.disabled = false; dl.textContent = t; return;
  }
  if (e.target.closest("#anchorBtn")) {
    const b = $("anchorBtn"); b.disabled = true; b.textContent = "Confirm in your wallet";
    try { part.anchor = await Backend.anchor(part); save(); toast("Anchored on BOT Chain."); renderCanvas(); renderThread(); }
    catch (err) { toast(err.message || "Anchor not completed."); b.disabled = false; b.textContent = "Anchor on BOT Chain"; }
  }
});


/* ============================================================ gate + routing */
function showGate(show) { $("gate").hidden = !show; $("app").hidden = show; }
async function gateWallet() {
  const b = $("gateWallet"), err = $("gateErr"); err.textContent = ""; b.disabled = true; const t = b.textContent; b.textContent = "Waiting for your wallet";
  try { await Auth.signInWithWallet(); } catch (e) { err.textContent = e.message || "Sign-in failed."; }
  b.disabled = false; b.textContent = t;
}
$("gateWallet").addEventListener("click", gateWallet);
Auth.onError = (e) => { $("gateErr").textContent = (e && e.message) || "Sign-in failed."; };

function renderAll() {
  const c = chat();
  $("chatTitle").textContent = c ? c.title : "New design";
  renderSide(); renderThread(); renderCanvas();
}
function fillAccount() {
  const s = Auth.getSession(), name = DEMO ? "Demo account" : Auth.displayName(s);
  $("acctName").textContent = name; $("avatar").textContent = (name.replace(/^0x/, "")[0] || "N");
  $("acctSub").textContent = DEMO ? "Sample data" : (s && s.email ? "Google" : "Wallet");
}
/* ============================================================ AI picker */
const DEMO_AI = { default: "basic:deepseek", check_credits: 5, tiers: [
  { id: "engine", label: "Engine", messages_per_day: null, messages_left: null, generate_credits: 2, models: [{ id: "engine:rules", label: "No AI (engine only)", available: true }] },
  { id: "basic", label: "Basic", messages_per_day: 40, messages_left: 40, generate_credits: 10, models: [{ id: "basic:deepseek", label: "DeepSeek", available: true }] },
  { id: "plus", label: "Plus", messages_per_day: 15, messages_left: 15, generate_credits: 25, models: [{ id: "plus:gpt", label: "GPT", available: true }, { id: "plus:qwen", label: "Qwen", available: true }] },
  { id: "advanced", label: "Advanced", messages_per_day: 4, messages_left: 4, generate_credits: 40, models: [{ id: "advanced:claude", label: "Claude", available: true }] }
] };
try { S.model = localStorage.getItem("nitocad_studio_model") || "basic:deepseek"; } catch (e) { S.model = "basic:deepseek"; }
let aiBound = false;
async function loadAi() {
  if (DEMO) S.ai = DEMO_AI;
  else {
    if (!Auth.getSession()) return;
    const r = await api("GET", "/ai/models");
    if (!r.ok) return;
    S.ai = r.data;
  }
  const usable = S.ai.tiers.flatMap((t) => t.models.filter((m) => m.available).map((m) => m.id));
  if (!usable.includes(S.model)) S.model = S.ai.default;
  if (!aiBound) {
    aiBound = true;
    const tb = $("tplBtn"); if (tb) tb.addEventListener("click", openTemplates);
    $("aiModel").addEventListener("change", (e) => {
      S.model = e.target.value;
      try { localStorage.setItem("nitocad_studio_model", S.model); } catch (err) { /* private mode */ }
      renderAiBar();
    });
  }
  renderAiBar();
}
function refreshAi() { loadAi().catch(() => {}); }
function renderAiBar() {
  if (!S.ai) return;
  const sel = $("aiModel");
  sel.innerHTML = S.ai.tiers.map((t) => `<optgroup label="${esc(t.label)} · Generate ${t.generate_credits} credits">` +
    t.models.map((m) => `<option value="${esc(m.id)}"${m.available ? "" : " disabled"}${m.id === S.model ? " selected" : ""}>${esc(m.label)}${m.available ? "" : " (not set up yet)"}</option>`).join("") + "</optgroup>").join("");
  const t = S.ai.tiers.find((x) => x.models.some((m) => m.id === S.model)) || S.ai.tiers[0];
  const left = t.messages_per_day == null ? "No daily limit" : `${t.messages_left} of ${t.messages_per_day} messages left today`;
  $("aiNote").textContent = `${left} · Generate ${t.generate_credits} credits`;
}

async function loadConfig() {
  document.querySelector(".fineprint").textContent = "Messages are free within each AI's daily allowance. Generating the full files costs credits, shown on the button. Check dimensions before you manufacture.";
}
let booted = false;
function route() {
  const signed = DEMO || !!Auth.getSession();
  if (!signed) { showGate(true); booted = false; Auth.initGoogle([$("gateGoogle")]); return; }
  showGate(false);
  if (!booted) { booted = true; fillAccount(); refreshBalance(); loadConfig(); loadAi(); load().then(() => { if (!Auth.getSession() && !DEMO) return route(); renderAll(); if (!IS_TOUCH) $("input").focus(); }); }
}
Auth.onChange(() => route());
route();
