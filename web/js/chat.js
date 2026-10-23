/* 聊天页逻辑：会话列表 / 消息渲染 / 流式 / 图片 / 朗读 / 收藏 */
const S = { convs: [], current: null, pendingImages: [], typing: null, toolHint: null, urlCache: {} };

/* ---------------- 启动 ---------------- */
(async function init() {
  try {
    const info = await api("/api/system/info");
    $("#ver").textContent = "v" + info.version;
  } catch {}
  await loadConvs();
  const last = localStorage.getItem("lastConv");
  if (last && S.convs.some((c) => c.id === last)) selectConv(last);
  else if (S.convs.length) selectConv(S.convs[0].id);
  else await newConv();
  loadMood();
  setInterval(loadMood, 45000);
})();

connectWS(onEvent, (ok) => { $("#wsDot").style.color = ok ? "#7bd88f" : "#e6685a"; });

/* ---------------- 会话 ---------------- */
async function loadConvs() {
  const r = await api("/api/conversations");
  S.convs = r.conversations || [];
  renderConvs();
}

function renderConvs() {
  const box = $("#convList");
  box.innerHTML = "";
  for (const c of S.convs) {
    const el = document.createElement("div");
    el.className = "conv-item" + (c.id === S.current ? " active" : "");
    el.dataset.id = c.id;
    el.innerHTML = `
      <div class="t">
        <b>${esc(c.title || "（还没起名）")}</b>
        <span class="channel-tag">${c.channel === "qq" ? qqTag(c.id) : "web"}</span>
      </div>
      <div class="s">${esc(c.last || "")}</div>`;
    el.onclick = () => selectConv(c.id);
    box.appendChild(el);
  }
}

function qqTag(id) {
  if (id.startsWith("qq:private:")) return "QQ私聊";
  if (id.startsWith("qq:group:")) return "QQ群";
  return "QQ";
}

async function newConv() {
  const r = await api("/api/conversations", { method: "POST" });
  await loadConvs();
  await selectConv(r.id);
}

async function selectConv(id) {
  S.current = id;
  localStorage.setItem("lastConv", id);
  const conv = S.convs.find((c) => c.id === id);
  $("#convTitle").textContent = conv ? (conv.title || "（还没起名）") : id;
  $("#convChannel").style.display = conv && conv.channel === "qq" ? "" : "none";
  if (conv) $("#convChannel").textContent = qqTag(id);
  renderConvs();
  clearTyping();
  await reloadMessages();
  $("#input").focus();
}

/* ---------------- 消息 ---------------- */
async function reloadMessages() {
  if (!S.current) return;
  const r = await api(`/api/conversations/${encodeURIComponent(S.current)}/messages?limit=300`);
  renderMessages(r.messages || []);
}

async function renderMessages(msgs) {
  const box = $("#messages");
  box.innerHTML = "";
  for (const m of msgs) box.appendChild(await renderMessage(m));
  box.scrollTop = box.scrollHeight;
}

async function renderMessage(m) {
  const el = document.createElement("div");
  const role = m.role === "assistant" ? "ai" : (m.source === "speak-up" ? "ai" : "user");
  el.className = "msg " + role;
  const avatar = role === "ai" ? "🐟" : "🧑";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = miniMd(m.content || "");

  if (m.images && m.images.length) {
    const imgs = document.createElement("div");
    imgs.className = "imgs";
    for (const p of m.images) {
      const url = await mediaUrl(p);
      if (!url) continue;
      const img = document.createElement("img");
      img.src = url;
      img.onclick = () => window.open(url, "_blank");
      imgs.appendChild(img);
    }
    bubble.appendChild(imgs);
  }

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.innerHTML = `<span>${fmtTime(m.created_at)}</span>` +
    (m.source === "speak-up" ? `<span class="tag">主动开口</span>` : "");
  if (m.role === "assistant" && (m.content || "").trim()) {
    const bPlay = document.createElement("button");
    bPlay.textContent = "▶ 朗读";
    bPlay.onclick = () => speakText(m.content, bPlay);
    const bFav = document.createElement("button");
    bFav.textContent = "♡ 收藏";
    bFav.onclick = async () => {
      try { await api("/api/tts/favorites", { method: "POST", body: { text: m.content } }); toast("收藏了"); }
      catch (e) { toast("收藏失败：" + e.message, true); }
    };
    meta.appendChild(bPlay);
    meta.appendChild(bFav);
  }
  bubble.appendChild(meta);

  el.innerHTML = `<div class="avatar">${avatar}</div>`;
  el.appendChild(bubble);
  return el;
}

async function mediaUrl(path) {
  if (S.urlCache[path] !== undefined) return S.urlCache[path];
  try {
    const r = await api("/api/media_url?path=" + encodeURIComponent(path));
    S.urlCache[path] = r.url;
    return r.url;
  } catch { return null; }
}

async function speakText(text, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "…合成中"; }
  try {
    const r = await api("/api/tts/speak", { method: "POST", body: { text, conv_id: S.current || "" } });
    if (!r.ok) throw new Error(r.message || "合成失败");
    playAudio(r.url);
  } catch (e) {
    toast("朗读失败：" + e.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "▶ 朗读"; }
  }
}

/* ---------------- 流式 ---------------- */
function clearTyping() {
  if (S.typing) { S.typing.remove(); S.typing = null; }
  if (S.toolHint) { S.toolHint.remove(); S.toolHint = null; }
}

function ensureTyping() {
  if (S.typing && document.body.contains(S.typing)) return;
  clearTyping();
  const el = document.createElement("div");
  el.className = "typing";
  el.innerHTML = `🐟 <span class="dot">·</span><span class="dot">·</span><span class="dot">·</span>`;
  $("#messages").appendChild(el);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  S.typing = el;
}

function onEvent(ev) {
  const mine = ev.conv_id === S.current;
  switch (ev.type) {
    case "start":
      if (mine) ensureTyping();
      break;
    case "delta":
      if (!mine) return;
      ensureTyping();
      if (!S.typing.dataset.raw) S.typing.dataset.raw = "";
      S.typing.dataset.raw += ev.text;
      S.typing.innerHTML = `🐟 <span class="stream">${esc(S.typing.dataset.raw.slice(-400))}</span>`;
      $("#messages").scrollTop = $("#messages").scrollHeight;
      break;
    case "tool":
      if (!mine) return;
      if (!S.toolHint) {
        S.toolHint = document.createElement("div");
        S.toolHint.className = "tool-hint";
        $("#messages").appendChild(S.toolHint);
      }
      S.toolHint.textContent = `🔍 正在用「${ev.name}」查…`;
      break;
    case "done":
      if (!mine) return;
      clearTyping();
      reloadMessages();
      refreshConvMeta();
      break;
    case "message":  /* 主动搭话 / 其它通道来的消息 */
      if (mine) reloadMessages();
      markUnread(ev.conv_id);
      refreshConvMeta();
      break;
    case "title":
      refreshConvMeta();
      break;
    case "voice":
      if (mine) playAudio(ev.path);
      break;
    case "voice_stop":
      if (_audio) { try { _audio.pause(); } catch {} }
      break;
    case "error":
      if (mine) {
        clearTyping();
        const el = document.createElement("div");
        el.className = "msg system";
        el.innerHTML = `<div class="avatar">⚠</div><div class="bubble">${esc(ev.text || "出错了")}</div>`;
        $("#messages").appendChild(el);
        $("#messages").scrollTop = $("#messages").scrollHeight;
      }
      toast(ev.text || "出错了", true);
      break;
  }
}

function markUnread(convId) {
  const item = $(`.conv-item[data-id="${CSS.escape(convId)}"]`);
  if (item && convId !== S.current && !item.querySelector(".unread")) {
    const dot = document.createElement("span");
    dot.className = "unread";
    item.querySelector(".t").appendChild(dot);
  }
}

async function refreshConvMeta() {
  await loadConvs();
  const conv = S.convs.find((c) => c.id === S.current);
  if (conv) $("#convTitle").textContent = conv.title || "（还没起名）";
}

/* ---------------- 心情 ---------------- */
async function loadMood() {
  try {
    const r = await api("/api/mood");
    const chip = $("#moodChip");
    chip.hidden = false;
    chip.textContent = `${r.emoji || moodFace(r.mood)} ${r.label} ${Math.round(r.mood)}`;
    chip.title = (r.last_reason ? r.last_reason + "\n" : "") + "点这是配置页（详情在「心情」里）";
    chip.onclick = () => location.href = "/admin.html#mood";
  } catch {}
}

/* ---------------- 输入 ---------------- */
const input = $("#input");
const sendBtn = $("#sendBtn");

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 200) + "px";
  updateSendBtn();
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});

$("#attachBtn").onclick = () => $("#fileInput").click();
$("#fileInput").onchange = async () => {
  for (const f of $("#fileInput").files) {
    const fd = new FormData();
    fd.append("file", f);
    try {
      const r = await fetch("/api/upload", { method: "POST", body: fd }).then((x) => x.json());
      if (r.ok) S.pendingImages.push({ path: r.path, url: r.url });
    } catch { toast("上传失败：" + f.name, true); }
  }
  $("#fileInput").value = "";
  renderAttachments();
  updateSendBtn();
};

function renderAttachments() {
  const box = $("#attachments");
  box.innerHTML = "";
  S.pendingImages.forEach((im, i) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `<img src="${im.url}" style="height:26px;border-radius:4px"> <a href="#" data-i="${i}">✕</a>`;
    chip.querySelector("a").onclick = (e) => { e.preventDefault(); S.pendingImages.splice(i, 1); renderAttachments(); updateSendBtn(); };
    box.appendChild(chip);
  });
}

function updateSendBtn() {
  sendBtn.disabled = !(input.value.trim() || S.pendingImages.length) || !S.current;
}

async function send() {
  const text = input.value.trim();
  if ((!text && !S.pendingImages.length) || !S.current) return;
  const images = S.pendingImages.map((x) => x.path);
  input.value = "";
  S.pendingImages = [];
  renderAttachments();
  input.style.height = "auto";
  updateSendBtn();
  try {
    await api(`/api/conversations/${encodeURIComponent(S.current)}/messages`, {
      method: "POST", body: { text, images },
    });
    reloadMessages();  /* 先把自己的消息显出来；回复走 WS */
  } catch (e) {
    toast("发送失败：" + e.message, true);
  }
}
sendBtn.onclick = send;
$("#newConv").onclick = newConv;
$("#refreshConvs").onclick = loadConvs;