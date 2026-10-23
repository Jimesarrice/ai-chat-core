/* 共用小工具（无依赖） */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body !== undefined && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!r.ok) {
    const msg = (data && (data.detail?.message || data.detail || data.message)) || `HTTP ${r.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

function toast(msg, bad = false) {
  let box = document.getElementById("toastBox");
  if (!box) {
    box = document.createElement("div");
    box.id = "toastBox";
    document.body.appendChild(box);
  }
  // 最多同时留 3 条，旧的先走
  while (box.children.length >= 3) box.firstChild.remove();
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* 极简 markdown：代码块 / 行内代码 / 粗体 / 链接（先转义，保安全） */
function miniMd(text) {
  let s = esc(text);
  s = s.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, body) => `<pre>${body.trimEnd()}</pre>`);
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  return s;
}

function fmtTime(s) {
  if (!s) return "";
  const d = new Date(String(s).replace(" ", "T"));
  if (isNaN(d)) return String(s);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return sameDay ? hm : `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")} ${hm}`;
}

function connectWS(onEvent, onStatus) {
  let ws = null, closed = false, retry = 1;
  function open() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/api/ws`);
    ws.onopen = () => { retry = 1; onStatus && onStatus(true); };
    ws.onclose = () => { onStatus && onStatus(false); if (!closed) setTimeout(open, Math.min(retry *= 1.7, 15000)); };
    ws.onerror = () => { try { ws.close(); } catch {} };
    ws.onmessage = (e) => { try { onEvent(JSON.parse(e.data)); } catch {} };
  }
  open();
  return { close() { closed = true; try { ws.close(); } catch {} } };
}

function moodFace(v) {
  if (v >= 80) return "😊"; if (v >= 62) return "🙂"; if (v >= 42) return "😌"; if (v >= 25) return "🥺"; return "😢";
}

/* 音频播放器（单例，播新的停旧的） */
let _audio = null;
function playAudio(url) {
  if (_audio) { try { _audio.pause(); } catch {} }
  _audio = new Audio(url);
  _audio.play().catch(() => toast("浏览器拦了自动播放，点一下页面再试", true));
}