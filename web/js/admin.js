/* 配置与日志页：schema 驱动表单 + 各能力面板。 */
const A = { schema: null, config: null, activeTab: "persona" };

(async function initAdmin() {
  try { $("#ver").textContent = "v" + (await api("/api/system/info")).version; } catch {}
  await loadConfig();
  buildTabs();
  gotoHashTab();
  renderGeneric("persona");
  renderLLM();
  renderGeneric("speakup"); renderSpeakupExtra();
  renderGeneric("qq"); renderQQExtra();
  renderGeneric("tts"); renderTTSExtra();
  renderGeneric("search"); renderSearchExtra();
  renderGeneric("system"); renderSysExtra();
  renderMemory();
  renderMood();
  renderLogs();
  window.addEventListener("hashchange", gotoHashTab);
})();

function gotoHashTab() {
  const h = (location.hash || "").replace("#", "");
  if ($(`#tabs button[data-tab="${h}"]`)) switchTab(h);
}

function buildTabs() {
  $$("#tabs button").forEach((b) => {
    b.onclick = () => switchTab(b.dataset.tab);
  });
}

function switchTab(name) {
  A.activeTab = name;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + name));
  location.hash = name;
}

async function loadConfig() {
  A.schema = await api("/api/config");
  A.config = A.schema.current;
  if (A.schema.error) toast("配置有问题：" + A.schema.error, true);
}

/* ---------------- 通用表单 ---------------- */
function sectionOf(key) { return (A.schema.sections || []).find((s) => s.key === key); }

function renderGeneric(key) {
  const sec = sectionOf(key);
  const box = $("#cfg-" + key);
  if (!sec || !box) return;
  box.innerHTML = "";
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `<h3>${esc(sec.label)}</h3><div class="desc">${esc(sec.desc || "")}</div>`;
  const grid = document.createElement("div");
  grid.className = "grid";
  for (const f of sec.fields) grid.appendChild(fieldEl(key, f, A.config[key][f.name]));
  card.appendChild(grid);
  const actions = document.createElement("div");
  actions.className = "row-actions";
  const save = document.createElement("button");
  save.className = "primary";
  save.textContent = "保存这一节";
  save.onclick = () => saveSection(key, grid, sec.fields);
  actions.appendChild(save);
  card.appendChild(actions);
  box.appendChild(card);
}

function fieldEl(sectionKey, f, value) {
  const wrap = document.createElement("div");
  wrap.className = "field" + (f.ui === "textarea" || f.ui === "json" ? " full" : "");
  wrap.dataset.name = f.name;
  const lab = document.createElement("div");
  lab.className = "flabel";
  lab.innerHTML = `<label>${esc(f.label)}</label>`;
  wrap.appendChild(lab);

  let ctl;
  let intoLabelRow = false;   // 开关跟标签同一行（label 左、开关右）
  const ui = f.ui || "text";
  if (ui === "switch") {
    // 用 <label> 包住隐藏 checkbox：点哪都能切换（span 不会转发点击，这是之前"点了没反应"的根因）
    ctl = document.createElement("label");
    ctl.className = "switch";
    ctl.innerHTML = `<input type="checkbox"><i></i>`;
    ctl.querySelector("input").checked = !!value;
    intoLabelRow = true;
  } else if (ui === "select") {
    ctl = document.createElement("select");
    for (const ch of f.choices || []) {
      const o = document.createElement("option");
      o.value = ch; o.textContent = ch;
      ctl.appendChild(o);
    }
    ctl.value = value ?? "";
  } else if (ui === "number") {
    ctl = document.createElement("input");
    ctl.type = "number";
    ctl.step = f.type === "integer" ? "1" : "any";
    ctl.value = value ?? "";
  } else if (ui === "textarea") {
    ctl = document.createElement("textarea");
    ctl.value = value ?? "";
  } else if (ui === "json" || ui === "targets" || ui === "providers" || ui === "tasks") {
    if (ui === "targets") {
      ctl = targetsEl(value);
    } else {
      ctl = document.createElement("textarea");
      ctl.className = "mono";
      ctl.value = JSON.stringify(value ?? (f.type === "array" ? [] : {}), null, 2);
    }
  } else if (ui === "password") {
    ctl = document.createElement("input");
    ctl.type = "password";
    ctl.value = value ?? "";
    ctl.autocomplete = "off";
  } else {
    ctl = document.createElement("input");
    ctl.type = "text";
    ctl.value = value ?? "";
  }
  ctl.dataset.role = "ctl";
  if (intoLabelRow) {
    lab.style.justifyContent = "space-between";
    lab.appendChild(ctl);      // 标签左、开关右，同一行
  } else {
    wrap.appendChild(ctl);
  }
  if (f.desc) {
    const d = document.createElement("div");
    d.className = "fdesc";
    d.textContent = f.desc;
    wrap.appendChild(d);
  }
  return wrap;
}

function targetsEl(value) {
  const box = document.createElement("div");
  box.style.border = "1px solid var(--line)";
  box.style.borderRadius = "8px";
  box.style.padding = "8px 10px";
  box.dataset.role = "ctl";
  box.dataset.kind = "targets";
  box.innerHTML = `<div class="hint" id="tgHint">加载会话清单里…</div>`;
  api("/api/conversations").then((r) => {
    const chosen = new Set(value || []);
    box.innerHTML = "";
    for (const c of r.conversations || []) {
      const row = document.createElement("label");
      row.style.display = "flex"; row.style.gap = "8px"; row.style.marginBottom = "3px"; row.style.color = "var(--text)";
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.style.width = "auto";
      cb.checked = chosen.has(c.id);
      cb.dataset.convId = c.id;
      row.appendChild(cb);
      const span = document.createElement("span");
      span.textContent = `${c.title || "（还没起名）"}（${c.id}）`;
      row.appendChild(span);
      box.appendChild(row);
    }
    if (!box.children.length) box.innerHTML = `<div class="hint">还没有任何会话（先去聊天页聊两句）</div>`;
  });
  return box;
}

function collectFields(container, fields) {
  const patch = {};
  for (const f of fields) {
    const wrap = container.querySelector(`.field[data-name="${f.name}"]`);
    if (!wrap) continue;
    const ctl = wrap.querySelector('[data-role="ctl"]');
    const ui = f.ui || "text";
    if (ui === "switch") patch[f.name] = ctl.querySelector("input").checked;
    else if (ui === "select") patch[f.name] = ctl.value;
    else if (ui === "number") {
      const v = ctl.value === "" ? null : Number(ctl.value);
      patch[f.name] = f.type === "integer" ? Math.round(v) : v;
    } else if (ui === "targets") {
      patch[f.name] = [...ctl.querySelectorAll("input[type=checkbox]:checked")].map((x) => x.dataset.convId);
    } else if (ui === "json") {
      try { patch[f.name] = JSON.parse(ctl.value || (f.type === "array" ? "[]" : "{}")); }
      catch { throw new Error(`「${f.label}」不是合法 JSON`); }
    } else patch[f.name] = ctl.value;
  }
  return patch;
}

async function saveSection(key, grid, fields) {
  let patch;
  try { patch = collectFields(grid, fields); }
  catch (e) { toast(e.message, true); return; }
  await doSave({ [key]: patch });
  if (["qq", "tts", "search"].includes(key)) {
    if (key === "qq") renderQQExtra();
    if (key === "tts") renderTTSExtra();
    if (key === "search") renderSearchExtra();
  }
}

async function doSave(patch, quiet = false) {
  try {
    const r = await api("/api/config", { method: "POST", body: { patch } });
    $("#saveState").textContent = "已保存 " + new Date().toLocaleTimeString();
    if (!quiet) toast(r.message || "存好了");
    A.config = (await api("/api/config")).current;
  } catch (e) {
    toast("保存失败：" + e.message, true);
  }
}

/* ---------------- 模型接入 ---------------- */
function renderLLM() {
  const sec = sectionOf("llm");
  const box = $("#llmBody");
  if (!sec) return;
  box.innerHTML = "";

  // ① 本地 llama.cpp（引擎已内置，模型从 Models/ 自动识别）
  const lcard = card("本地 llama.cpp（内置 Vulkan 引擎，模型自动识别）",
    "引擎在 engines/llama.cpp/；模型从项目 Models/ 目录自动识别。选择模型 →「用这个模型」→「生成启动命令」→「启动本地服务」，任务分工里 chat 指向 local 即可本地推理。");
  lcard.innerHTML += `<div class="grid">
      <div class="field"><label>检测到的模型（Models/）</label><select id="llmLocalModel"></select></div>
      <div class="field"><label>&nbsp;</label><button id="llmUseModel">用这个模型</button></div>
    </div>
    <div class="hint" id="llmModelNote"></div>
    <div class="hint" style="color:var(--warn)">⚠ 本地部署大语言模型时可能会抢占显存（和本地 TTS 共用一块显卡时尤其明显）。</div>`;
  const ldefs = (sec.fields || []).filter((f) => ["local_command", "local_auto_start"].includes(f.name));
  const lgrid = document.createElement("div");
  lgrid.className = "grid";
  lgrid.style.marginTop = "8px";
  for (const f of ldefs) lgrid.appendChild(fieldEl("llm", f, A.config.llm[f.name]));
  lcard.appendChild(lgrid);
  const lsave = document.createElement("div");
  lsave.className = "row-actions";
  const saveLocalBtn = document.createElement("button");
  saveLocalBtn.textContent = "保存本地引擎配置";
  saveLocalBtn.onclick = async () => { await doSave({ llm: collectFields(lgrid, ldefs) }); loadLlamaStatus(); };
  lsave.appendChild(saveLocalBtn);
  const acts = document.createElement("div");
  acts.className = "row-actions";
  acts.appendChild(btn("🧩 生成启动命令（选中的模型）", genLlamaCommand, "primary"));
  acts.appendChild(btn("▶ 启动本地服务", async () => { const r = await api("/api/llm/local/start", { method: "POST", body: {} }); toast(r.message || "", !r.ok); loadLlamaStatus(); }));
  acts.appendChild(btn("■ 停止", async () => { const r = await api("/api/llm/local/stop", { method: "POST" }); toast(r.message || "", !r.ok); loadLlamaStatus(); }));
  acts.appendChild(btn("🔌 检测本地服务", async () => { const r = await api("/api/llm/local/probe", { method: "POST" }); toast(r.message, !r.ok); }));
  acts.appendChild(btn("刷新", () => { loadLlamaModels(true); loadLlamaStatus(); }));
  lcard.appendChild(lsave);
  lcard.appendChild(acts);
  const lstat = document.createElement("div");
  lstat.id = "llmLocalStatus";
  lstat.className = "hint";
  lstat.style.marginTop = "8px";
  lstat.textContent = "查询中…";
  lcard.appendChild(lstat);
  box.appendChild(lcard);

  const pcard = document.createElement("div");
  pcard.className = "card";
  pcard.innerHTML = `<h3>接入点（两条路径）</h3>
    <div class="desc">本地 llama.cpp（Vulkan 后端，llama-server 默认 127.0.0.1:8080）与云端 OpenAI 兼容端点都填在这里。改完点最下面「保存模型接入」。</div>`;
  const pdefs = A.schema.defs["ProviderConfig"] || [];
  const providersBox = document.createElement("div");
  providersBox.id = "providersBox";
  for (const [pid, prov] of Object.entries(A.config.llm.providers || {})) {
    providersBox.appendChild(providerCard(pid, prov, pdefs));
  }
  pcard.appendChild(providersBox);
  const pa = document.createElement("div");
  pa.className = "row-actions";
  const addP = document.createElement("button");
  addP.textContent = "＋ 加接入点";
  addP.onclick = () => {
    const id = prompt("接入点 id（英文，如 cloud2）");
    if (!id) return;
    if (A.config.llm.providers[id]) { toast("这个 id 已经有了", true); return; }
    A.config.llm.providers[id] = { label: id, kind: "cloud", base_url: "", api_key: "", model: "", multimodal: true, thinking: "low", thinking_preset: "auto", extra_body: {}, timeout_read: 600, note: "" };
    doSave({ llm: { providers: A.config.llm.providers } }, true).then(renderLLM);
  };
  const selfCheck = document.createElement("button");
  selfCheck.textContent = "🔌 自检两条路径";
  selfCheck.onclick = selfCheckLLM;
  pa.appendChild(addP); pa.appendChild(selfCheck);
  pcard.appendChild(pa);
  const checkOut = document.createElement("div");
  checkOut.id = "llmCheckOut";
  pcard.appendChild(checkOut);
  box.appendChild(pcard);

  const tcard = document.createElement("div");
  tcard.className = "card";
  tcard.innerHTML = `<h3>任务分工</h3><div class="desc">聊天、记忆提炼、心情判断、起标题、联网归纳——每个任务各自选走本地还是云端。</div>`;
  const tdefs = A.schema.defs["TaskRoute"] || [];
  const tbox = document.createElement("div");
  tbox.id = "tasksBox";
  for (const [task, route] of Object.entries(A.config.llm.tasks || {})) {
    tbox.appendChild(taskRow(task, route, tdefs));
  }
  tcard.appendChild(tbox);
  const ta = document.createElement("div");
  ta.className = "row-actions";
  const saveLLM = document.createElement("button");
  saveLLM.className = "primary";
  saveLLM.textContent = "保存模型接入";
  saveLLM.onclick = async () => {
    const patch = { llm: { providers: collectProviders(), tasks: collectTasks(tdefs) } };
    await doSave(patch);
  };
  ta.appendChild(saveLLM);
  tcard.appendChild(ta);
  box.appendChild(tcard);
  loadLlamaModels();
  loadLlamaStatus();
}

/* ---------------- 本地 llama.cpp（内置引擎 + Models 扫描） ---------------- */
async function loadLlamaModels(refresh = false) {
  const sel = $("#llmLocalModel");
  if (!sel) return;
  try {
    const scan = await api("/api/models/local" + (refresh ? "?refresh=true" : ""));
    sel.innerHTML = "";
    const list = scan.llm || [];
    if (!list.length) {
      const o = document.createElement("option");
      o.value = ""; o.textContent = "（Models/ 里没有可用的聊天模型，把 .gguf 放进去即可）";
      sel.appendChild(o);
    }
    for (const m of list) {
      const o = document.createElement("option");
      const flags = [`${m.size_mb}MB`];
      if (m.vision) flags.push(m.mmproj ? "视觉 ✓" : "视觉·缺 mmproj");
      o.value = m.path;
      o.textContent = `${m.name}（${flags.join("，")}）`;
      sel.appendChild(o);
    }
    const cur = (A.config.llm.providers && A.config.llm.providers.local && A.config.llm.providers.local.model) || "";
    const hit = list.find((x) => x.name === cur);
    if (hit) sel.value = hit.path;
    const btnUse = $("#llmUseModel");
    if (btnUse) {
      btnUse.onclick = async () => {
        const m = list.find((x) => x.path === sel.value);
        if (!m) return;
        await doSave({ llm: { providers: { local: { model: m.name } } } }, true);
        const inp = document.querySelector('#providersBox .provider-card[data-pid="local"] .field[data-name="model"] input');
        if (inp) inp.value = m.name;
        toast(`local 接入点的模型名 → ${m.name}`);
      };
    }
  } catch (e) {
    toast("扫描 Models/ 失败：" + e.message, true);
  }
}

async function genLlamaCommand() {
  try {
    const scan = await api("/api/models/local?refresh=true");
    if (!scan.engines || !scan.engines.llama_server_ok) {
      toast("没找到内置引擎 engines/llama.cpp/llama-server.exe", true);
      return;
    }
    const sel = $("#llmLocalModel");
    const m = (scan.llm || []).find((x) => x.path === (sel && sel.value));
    if (!m) { toast("先在列表里选一个模型", true); return; }
    const prov = (A.config.llm.providers && A.config.llm.providers.local) || {};
    const port = (String(prov.base_url || "").match(/:(\d+)/) || [, "8080"])[1];
    let cmd = `"${scan.engines.llama_server}" -m "${m.path}"`;
    if (m.mmproj && m.mmproj.path) cmd += ` --mmproj "${m.mmproj.path}"`;
    cmd += ` --host 127.0.0.1 --port ${port} -ngl 99 -c 8192`;
    await doSave({ llm: { local_command: cmd, local_auto_start: true } }, true);
    const inp = document.querySelector('#llmBody .field[data-name="local_command"] input');
    if (inp) inp.value = cmd;
    const sw = document.querySelector('#llmBody .field[data-name="local_auto_start"] .switch input');
    if (sw) sw.checked = true;
    toast("启动命令已生成（端口跟 local 接入点一致；已设开机自动拉起）");
    loadLlamaStatus();
  } catch (e) {
    toast("生成失败：" + e.message, true);
  }
}

async function loadLlamaStatus() {
  const box = $("#llmLocalStatus");
  if (!box) return;
  try {
    const r = await api("/api/llm/local/status");
    box.innerHTML = `
      <span class="tag ${r.running ? "ok" : "warn"}">${r.running ? "llama-server 在跑（pid " + r.pid + "）" : "llama-server 没在跑"}</span>
      ｜地址 ${esc(r.base_url || "（没填）")}｜模型名 ${esc(r.model || "（没填）")}
      ${r.auto_start ? '<span class="tag">开机自动拉起</span>' : ""}
      ${r.command ? `<div class="hint">启动命令：<code>${esc(r.command)}</code></div>` : `<div class="hint">还没生成启动命令——选模型后点「🧩 生成启动命令」。</div>`}
      ${r.log_tail ? `<details class="adv"><summary>服务日志（尾巴）</summary><pre style="max-height:180px;overflow:auto;white-space:pre-wrap">${esc(r.log_tail)}</pre></details>` : ""}`;
  } catch (e) {
    box.innerHTML = `<div class="hint">${esc(e.message)}</div>`;
  }
}

function providerCard(pid, prov, defs) {
  const card = document.createElement("div");
  card.className = "provider-card";
  card.dataset.pid = pid;
  const head = document.createElement("div");
  head.style.display = "flex"; head.style.alignItems = "center"; head.style.gap = "10px"; head.style.marginBottom = "8px";
  head.innerHTML = `<b>${esc(pid)}</b> <span class="hint">${esc(prov.label || "")}</span>`;
  const del = document.createElement("button");
  del.textContent = "删除";
  del.className = "danger";
  del.style.marginLeft = "auto";
  del.onclick = () => {
    if (!confirm(`删掉接入点「${pid}」？（任务分工里引用它的会失效）`)) return;
    delete A.config.llm.providers[pid];
    doSave({ llm: { providers: A.config.llm.providers } }, true).then(renderLLM);
  };
  head.appendChild(del);
  card.appendChild(head);
  const grid = document.createElement("div");
  grid.className = "grid";
  for (const f of defs) grid.appendChild(fieldEl("llm", f, prov[f.name]));
  card.appendChild(grid);
  return card;
}

function collectProviders() {
  const out = {};
  const pdefs = A.schema.defs["ProviderConfig"] || [];
  $$("#providersBox .provider-card").forEach((card) => {
    const pid = card.dataset.pid;
    out[pid] = collectFields(card, pdefs);
  });
  return out;
}

function taskRow(task, route, defs) {
  const row = document.createElement("div");
  row.className = "provider-card";
  row.dataset.task = task;
  const title = document.createElement("div");
  title.style.marginBottom = "8px";
  title.innerHTML = `<b>${esc(task)}</b> <span class="hint">${esc(taskDesc(task))}</span>`;
  row.appendChild(title);
  const grid = document.createElement("div");
  grid.className = "grid";
  const ids = Object.keys(A.config.llm.providers || {});
  for (const f of defs) {
    if (f.name === "provider") {
      const f2 = { ...f, ui: "select", choices: ids };
      grid.appendChild(fieldEl("llm", f2, route.provider));
    } else {
      grid.appendChild(fieldEl("llm", f, route[f.name]));
    }
  }
  row.appendChild(grid);
  return row;
}

function taskDesc(t) {
  return ({
    chat: "聊天主回复",
    memory_extract: "记忆提炼 / 扩词 / 洗键 / 补标",
    mood_judge: "心情判断（每轮一次小调用）",
    title: "会话自动起标题",
    search_summary: "B站扫描代理（bili_scan）",
  }[t] || "");
}

function collectTasks(defs) {
  const out = {};
  $$("#tasksBox .provider-card").forEach((row) => {
    out[row.dataset.task] = collectFields(row, defs);
  });
  return out;
}

async function selfCheckLLM() {
  const out = $("#llmCheckOut");
  out.innerHTML = `<div class="hint">自检中…（本地模型没启动时会等一会儿）</div>`;
  try {
    const r = await api("/api/llm/status");
    out.innerHTML = "";
    for (const p of r.providers) {
      const div = document.createElement("div");
      div.className = "field";
      div.innerHTML = `<span class="tag ${p.ok ? "ok" : "bad"}">${p.ok ? "通" : "不通"}</span>
        <b>${esc(p.id)}</b>（${esc(p.kind)}）${esc(p.detail || "")}
        ${p.models && p.models.length ? `<div class="hint">模型：${esc(p.models.slice(0, 8).join(" / "))}</div>` : ""}`;
      out.appendChild(div);
    }
  } catch (e) {
    out.innerHTML = `<div class="hint">自检失败：${esc(e.message)}</div>`;
  }
}

/* ---------------- 记忆 ---------------- */
async function renderMemory() {
  const box = $("#memBody");
  box.innerHTML = `<div class="card"><div class="hint">加载中…</div></div>`;
  let data;
  try { data = await api("/api/memory/list?include_deleted=true"); }
  catch (e) { box.innerHTML = `<div class="card"><div class="hint">读记忆失败：${esc(e.message)}</div></div>`; return; }
  const st = data.stats;
  box.innerHTML = "";
  const cards = [];

  const s1 = card("记忆总览", "「该记的记下来，想起来时自己会想起来，不重要的事慢慢淡掉」。参数在「记忆」多的那一节……本面板只做浏览与整理。");
  s1.innerHTML += `<div class="stat-row">
    <div class="st"><b>${st.active}</b><span>有效</span></div>
    <div class="st"><b>${st.facts}</b><span>长期事实</span></div>
    <div class="st"><b>${st.superseded}</b><span>已被取代</span></div>
    <div class="st"><b>${st.deleted}</b><span>回收站</span></div>
    <div class="st"><b id="memFiltered">–</b><span>筛出</span></div></div>
    <div class="hint">库文件：${esc(st.file)}（每次改动前自动备份 .bak + 每天一份）</div>`;
  cards.push(s1);

  const s2 = card("整理工具（一批一批点，失败可重跑）", "批量工具会调用「记忆提炼」那条模型路径，先少量试再全量。");
  const tools = document.createElement("div");
  tools.className = "row-actions";
  tools.appendChild(btn("提炼一批(6)", async () => {
    const r = await api("/api/memory/refine", { method: "POST", body: { limit: 6 } });
    toast(r.message || `改了 ${r.changed} 条（跳过 ${r.skipped}，没解析 ${r.unparsed}），还剩 ${r.remaining} 条`);
    renderMemory();
  }));
  tools.appendChild(btn("扩词一批(8)", async () => {
    const r = await api("/api/memory/expand", { method: "POST", body: { limit: 8 } });
    toast(r.message || `补了 ${r.got} 条检索词（本批 ${r.total}）`);
    renderMemory();
  }));
  tools.appendChild(btn("洗键·先试 8 条", () => washDry()));
  tools.appendChild(btn("洗键·真跑一批", async () => {
    const r = await api("/api/memory/wash-keys", { method: "POST", body: { limit: 8 } });
    toast(r.message || `洗了 ${r.answered} 条，补上 ${r.given} 个键`);
    renderMemory();
  }));
  tools.appendChild(btn("撤掉补的键", async () => {
    const r = await api("/api/memory/wash-keys/undo", { method: "POST" });
    toast(`摘掉 ${r.reverted} 个自动补的键`);
    renderMemory();
  }));
  tools.appendChild(btn("补标不淡一批", async () => {
    const r = await api("/api/memory/backfill-stable", { method: "POST", body: { limit: 20 } });
    toast(r.message || `判了 ${r.answered} 条（改类别 ${r.changed}），还剩 ${r.remaining} 条`);
    renderMemory();
  }));
  tools.appendChild(btn("撤掉补标", async () => {
    const r = await api("/api/memory/backfill-stable/undo", { method: "POST" });
    toast(`撤了 ${r.reverted} 条`);
    renderMemory();
  }));
  tools.appendChild(btn("查重合并", () => checkDuplicates()));
  s2.appendChild(tools);
  cards.push(s2);

  const s3 = card("手动加一条 / 人称", "");
  s3.querySelector(".desc").innerHTML = "开关关着也能加（最高权限）；人称改了可以顺手把旧记忆一起换。";
  s3.innerHTML += `<div class="grid">
    <div class="field full"><label>内容（一句话，一条只装一件事）</label><textarea id="memAddText"></textarea></div>
    <div class="field"><label>重要度（0.5 平常 / 1 长期事实）</label><input type="number" id="memAddImp" value="0.5" step="0.5"></div>
    <div class="field"><label>事实键（可留空，如 ta.身高）</label><input id="memAddKey" placeholder="留空就行"></div>
    <div class="field"><label>发生时间（可留空）</label><input id="memAddAt" placeholder="YYYY-MM-DD"></div></div>
    <div class="row-actions"><button id="memAddBtn" class="primary">加一条</button></div>
    <div class="sep"></div>
    <div class="grid">
      <div class="field"><label>记忆里的人称</label><select id="memPerson">
        <option value="ta">ta</option><option value="她">她</option><option value="他">他</option></select></div>
      <div class="field"><label>&nbsp;</label><button id="memPersonBtn">改人称</button></div>
    </div>`;
  cards.push(s3);
  for (const c of cards) box.appendChild(c);
  $("#memAddBtn").onclick = async () => {
    const r = await api("/api/memory/add", { method: "POST", body: {
      text: $("#memAddText").value, importance: Number($("#memAddImp").value || 0.5),
      fact_key: $("#memAddKey").value, at: $("#memAddAt").value || null } });
    toast(r.message || (r.ok ? "加好了" : "没加上"), !r.ok);
    renderMemory();
  };
  $("#memPerson").value = A.config.memory.person;
  $("#memPersonBtn").onclick = async () => {
    const person = $("#memPerson").value;
    const first = await api("/api/memory/change-person", { method: "POST", body: { person } });
    if (!first.changed) { await api("/api/memory/change-person", { method: "POST", body: { person, apply: true } }); toast("人称已改（没有需要动的旧记忆）"); return; }
    if (confirm(`已有 ${first.changed} 条旧记忆里还写着旧的字。要一起改吗？\n例：${first.examples?.[0] || ""}`)) {
      await api("/api/memory/change-person", { method: "POST", body: { person, apply: true } });
      toast("一起改了");
    } else {
      await api("/api/memory/change-person", { method: "POST", body: { person, apply: true } });
    }
    renderMemory();
  };

  const s4 = card("旧档案导入", "扫出候选 → 你勾选 → 落库 → 再「提炼」把它们改写成第一人称记忆。认得 .md/.txt/.log/.rst 等纯文本，子文件夹往下找两层。");
  s4.innerHTML += `<div class="grid">
    <div class="field full"><label>档案文件夹路径</label><input id="impPath" placeholder="如 G:\\AI生成\\旧档案"></div></div>
    <div class="row-actions"><button id="impScan">扫描档案</button></div>
    <div id="impOut"></div>`;
  cards.push(s4);
  $("#impScan").onclick = () => importScan();

  const s5 = card("最近一次召回（为什么没想起来）", "");
  const tr = await api("/api/memory/traces?limit=3");
  const omLabel = { hard_gate: "没命中线索", below_threshold: "分数不够", weak: "条数档位到顶", duplicate: "跟另一条重复", fact_cap: "长期事实占满名额", budget: "超过字数上限", limit: "超过条数上限" };
  s5.innerHTML += (tr.traces || []).map((t) =>
    `<div class="hint" style="margin:6px 0 2px">线索：「${esc(t.query || "")}」${t.gate != null ? `（信息量 ${t.gate}/${t.gateLimit}）` : ""}</div>` +
    `<table class="list"><tr><th>记忆</th><th>C</th><th>P</th><th>结果</th></tr>` +
    (t.rows || []).slice(0, 8).map((r) => {
      const m = (data.memories || []).find((x) => x.id === r.id);
      return `<tr><td>${esc((m?.text || r.id || "").slice(0, 40))}</td><td>${r.C}</td><td>${r.P}</td>
        <td>${r.omission ? `<span class="tag warn">${omLabel[r.omission] || r.omission}</span>` : `<span class="tag ok">进了提示词</span>`}${r.timeHit ? " ⭐时间词" : ""}</td></tr>`;
    }).join("") + `</table>`).join("") || `<div class="hint">还没有召回记录——先去聊两句。</div>`;
  cards.push(s5);

  const s6 = card("记忆列表", "");
  s6.innerHTML += `<div class="row-actions" style="margin-bottom:8px">
    <input id="memSearch" placeholder="搜内容…" style="max-width:260px">
    <label style="display:flex;gap:6px;align-items:center;color:var(--text)"><input type="checkbox" id="memShowGone" style="width:auto"> 显示已取代/回收站</label></div>
    <div class="scroll-y" id="memList"></div>`;
  cards.push(s6);
  const renderList = () => {
    const q = ($("#memSearch").value || "").trim();
    const showGone = $("#memShowGone").checked;
    const list = (data.memories || []).filter((m) => {
      if (m.deleted && !showGone) return false;
      if (m["已被取代"] && !showGone && !m.deleted) return false;
      if (q && !String(m.text).includes(q)) return false;
      return true;
    });
    $("#memFiltered").textContent = list.length;
    const box2 = $("#memList");
    box2.innerHTML = "";
    if (!list.length) { box2.innerHTML = `<div class="hint">空的。聊两句，它会自己记；或用手动加 / 导入。</div>`; return; }
    for (const m of list.slice(0, 400)) {
      const row = document.createElement("div");
      row.style.cssText = "padding:8px 4px;border-bottom:1px solid var(--line)";
      const tags = [];
      if (m.category === "stable_profile") tags.push(`<span class="tag ok">不淡</span>`);
      if (m["事实键"]) tags.push(`<span class="tag">键 ${esc(m["事实键"])}</span>`);
      if (m["已被取代"]) tags.push(`<span class="tag warn">已被取代</span>`);
      if (m.deleted) tags.push(`<span class="tag bad">回收站</span>`);
      if (m.importance >= 1) tags.push(`<span class="tag">非常重要</span>`);
      row.innerHTML = `<div>${esc(m.text)}</div>
        <div class="meta" style="color:var(--dim);font-size:12px;margin-top:4px">
          <span>${esc((m.at || m.createdAt || "").slice(0, 10) || "没记时间")}</span>
          鲜活 ${Math.round((m.activation ?? 0) * 100)}% ｜ ${tags.join(" ")}</div>`;
      const act = document.createElement("span");
      act.style.marginLeft = "10px";
      if (!m.deleted && !m["已被取代"]) {
        act.appendChild(btn("改", async () => {
          const nt = prompt("改成：", m.text);
          if (nt == null) return;
          await api("/api/memory/edit", { method: "POST", body: { id: m.id, text: nt } });
          renderMemory();
        }));
        act.appendChild(btn("删", async () => { await api("/api/memory/delete", { method: "POST", body: { id: m.id } }); renderMemory(); }));
      }
      if (m["已被取代"] && !m.deleted) {
        act.appendChild(btn("恢复", async () => { await api("/api/memory/unsupersede", { method: "POST", body: { id: m.id } }); renderMemory(); }));
      }
      if (m.deleted) {
        act.appendChild(btn("找回来", async () => { await api("/api/memory/restore", { method: "POST", body: { id: m.id } }); renderMemory(); }));
      }
      row.querySelector(".meta").appendChild(act);
      box2.appendChild(row);
    }
  };
  $("#memSearch").oninput = renderList;
  $("#memShowGone").onchange = renderList;
  renderList();

  async function washDry() {
    const r = await api("/api/memory/wash-keys", { method: "POST", body: { limit: 8, dry: true } });
    const lines = (r.results || []).map((x) => `${x.answered ? (x.key ? "✓ " + x.key : "· 留空") : "（没回）"}  ${x.text}`).join("\n");
    alert("干跑（一个字节都没写）：\n\n" + (lines || r.message || "没有可洗的条目"));
  }
  async function checkDuplicates() {
    const r = await api("/api/memory/duplicates");
    if (!r.pairs.length) { toast("没找到重复对"); return; }
    const p = r.pairs[0];
    const a = (data.memories || []).find((x) => x.id === p.a);
    const b = (data.memories || []).find((x) => x.id === p.b);
    if (confirm(`找到 ${r.pairs.length} 对相似（像度 ${p.sim}）：\n\nA: ${a?.text}\nB: ${b?.text}\n\n要把 B 合进 A 吗？（B 进回收站，可找回）`)) {
      const rr = await api("/api/memory/merge", { method: "POST", body: { keep: p.a, drop: p.b } });
      toast(rr.message || (rr.ok ? "合了" : "没合"), !rr.ok);
      renderMemory();
    }
  }
  async function importScan() {
    const path = $("#impPath").value.trim();
    const out = $("#impOut");
    out.innerHTML = `<div class="hint">扫描中…</div>`;
    const r = await api("/api/memory/import/scan", { method: "POST", body: { path } });
    if (!r.candidates || !r.candidates.length) {
      out.innerHTML = `<div class="hint">${esc(r.message || "一条候选都没拆出来")}</div>`;
      return;
    }
    out.innerHTML = `<div class="hint">扫出 ${r.candidates.length} 条候选（${r.files} 个文件），勾掉不要的再导入：</div>`;
    const listBox = document.createElement("div");
    listBox.className = "scroll-y";
    r.candidates.forEach((c, i) => {
      const lab = document.createElement("label");
      lab.style.cssText = "display:flex;gap:8px;margin-bottom:4px;color:var(--text)";
      lab.innerHTML = `<input type="checkbox" checked style="width:auto" data-i="${i}">
        <span class="hint">${esc(c.file)}:${c.line}</span><span>${esc(c.text.slice(0, 80))}</span>`;
      listBox.appendChild(lab);
    });
    out.appendChild(listBox);
    const impBtn = btn("导入选中的", async () => {
      const items = [...listBox.querySelectorAll("input:checked")].map((x) => r.candidates[Number(x.dataset.i)]);
      const rr = await api("/api/memory/import/apply", { method: "POST", body: { items } });
      toast(`导入：加 ${rr.added} / 重复 ${rr.dup} / 近似 ${rr.similar} / 补全 ${rr.completed} / 删过 ${rr.cameback}`);
      const undoBtn = btn("撤销这次导入", async () => {
        const ru = await api("/api/memory/import/undo", { method: "POST", body: { batch: rr.batch } });
        toast(`撤销了 ${ru.moved} 条`);
        renderMemory();
      });
      out.appendChild(undoBtn);
      renderMemory();
    });
    impBtn.className = "primary";
    out.appendChild(document.createElement("div")).className = "row-actions";
    out.lastChild.appendChild(impBtn);
  }
}

function fmtMsStr(ms) {
  if (!ms) return "";
  const d = new Date(Number(ms));
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function card(title, desc) {
  const c = document.createElement("div");
  c.className = "card";
  c.innerHTML = `<h3>${esc(title)}</h3><div class="desc">${esc(desc || "")}</div>`;
  return c;
}

function btn(text, onclick, cls = "") {
  const b = document.createElement("button");
  b.textContent = text;
  if (cls) b.className = cls;
  b.onclick = async () => { try { await onclick(); } catch (e) { toast(e.message, true); } };
  return b;
}

/* ---------------- 心情 ---------------- */
async function renderMood() {
  const box = $("#moodBody");
  try {
    const r = await api("/api/mood");
    box.innerHTML = "";
    const c1 = card("此刻的心情", "每轮聊完用模型判断一次；有衰减（心情半衰 12 小时、情绪 90 分钟），像活人。");
    c1.innerHTML += `<div style="display:flex;align-items:center;gap:16px;margin-bottom:12px">
      <div style="font-size:40px">${r.emoji}</div>
      <div><b style="font-size:22px">${Math.round(r.mood)}</b> <span class="hint">${esc(r.label)}（基线 ${Math.round(r.baseline)}）</span>
      <div class="hint">上次：${esc(r.last_reason || "——")} ${r.last_confidence ? `（可信度 ${r.last_confidence}）` : ""}</div></div></div>`;
    c1.innerHTML += `<div class="mood-bars">` + (r.dims || []).map((d) =>
      `<div class="mb"><div class="bar"><i style="height:${Math.max(2, d.value)}%"></i></div><span>${esc(d.label)} ${d.value}</span></div>`
    ).join("") + `</div>`;
    const actions = document.createElement("div");
    actions.className = "row-actions";
    actions.appendChild(btn("重置到基线", async () => { await api("/api/mood/reset", { method: "POST" }); renderMood(); toast("回到基线了"); }));
    c1.appendChild(actions);
    box.appendChild(c1);

    const c2 = card("它听到的那句话（注入）", "「一次变化只说一次」：同一次心情变化只带进对话一次，不重复，免得把它的口气定死。");
    c2.innerHTML += r.last_injected_text
      ? `<pre style="white-space:pre-wrap">${esc(r.last_injected_text)}</pre><div class="hint">${fmtTime(fmtMsStr(r.last_injected_at))}</div>`
      : `<div class="hint">还没说过（没有够格的变化、或者关着「告诉它」）</div>`;
    box.appendChild(c2);

    const c3 = card("完整记录", "");
    c3.innerHTML += (r.timeline || []).length
      ? `<table class="list"><tr><th>时间</th><th>心情</th><th>变化</th><th>理由</th></tr>` +
        r.timeline.map((t) => `<tr><td>${esc(t.ts)}</td><td>${Math.round(t.mood)}</td>
          <td>${t.delta > 0 ? "+" : ""}${t.delta}</td><td>${esc(t.reason)}${t.source === "decay" ? " <span class='tag'>自然平复</span>" : ""}</td></tr>`).join("") + `</table>`
      : `<div class="hint">还没有记录。</div>`;
    box.appendChild(c3);
  } catch (e) {
    box.innerHTML = `<div class="card"><div class="hint">读心情失败：${esc(e.message)}</div></div>`;
  }
}

/* ---------------- 主动搭话 ---------------- */
async function renderSpeakupExtra() {
  const box = $("#speakupExtra");
  box.innerHTML = "";
  const c1 = card("手动试一下（安全默认：试运行开着，掷中也只记账）", "先「演练一次」看措辞，再考虑真发。真发会往目标会话真的送一句并开一轮。");
  const actions = document.createElement("div");
  actions.className = "row-actions";
  actions.appendChild(btn("🎲 演练一次（绝不真发）", async () => { const r = await api("/api/speakup/roll", { method: "POST", body: { mode: "drill" } }); toast(r.record?.text || "记了一笔"); loadSpeakupLogs(); }));
  actions.appendChild(btn("📨 真发一次（跳过等待）", async () => {
    if (!confirm("真发会往目标会话送一句并花模型钱，确定？")) return;
    const r = await api("/api/speakup/roll", { method: "POST", body: { mode: "send" } });
    toast(r.record?.text || "发了"); loadSpeakupLogs();
  }));
  c1.appendChild(actions);
  box.appendChild(c1);
  const c2 = card("最近记录", "命中线等式都在这里：掷出多少、线多少、怎么来的。");
  c2.innerHTML += `<div class="scroll-y" id="spkLogs"><div class="hint">加载中…</div></div>`;
  box.appendChild(c2);
  loadSpeakupLogs();
}
async function loadSpeakupLogs() {
  try {
    const r = await api("/api/speakup/logs?limit=60");
    const box = $("#spkLogs");
    if (!box) return;
    box.innerHTML = (r.logs || []).map((l) => `<div class="log-line">${esc(l.ts)} ${esc(l.text)}</div>`).join("") || `<div class="hint">还没有记录</div>`;
  } catch {}
}

/* ---------------- QQ ---------------- */
async function renderQQExtra() {
  const box = $("#qqExtra");
  box.innerHTML = "";
  const c1 = card("连接状态", "需要本机跑一个 OneBot 11 后端（NapCat / SnowLuma 等）。改连接方式后重启一次进程。");
  box.appendChild(c1);
  const status = document.createElement("div");
  status.id = "qqStatusBox";
  status.innerHTML = `<div class="hint">查询中…</div>`;
  c1.appendChild(status);
  const actions = document.createElement("div");
  actions.className = "row-actions";
  actions.appendChild(btn("刷新状态", loadQQStatus));
  actions.appendChild(btn("📨 真发测试句（绕过试运行）", async () => {
    const r = await api("/api/qq/test-send", { method: "POST", body: {} });
    toast(r.msg || "发了", !r.ok);
  }));
  c1.appendChild(actions);
  loadQQStatus();
}
async function loadQQStatus() {
  const box = $("#qqStatusBox");
  if (!box) return;
  try {
    const r = await api("/api/qq/status");
    box.innerHTML = `
      <div><span class="tag ${r.connected ? "ok" : "warn"}">${r.connected ? "已连上" : "未连接"}</span>
      ${r.self_id ? `连着的 QQ：<b>${esc(r.nickname)}</b>（${esc(r.self_id)}）` : ""}</div>
      <div class="hint">模式 ${esc(r.mode)}｜${r.dry_run ? "试运行中（只记账）" : "真发模式"}｜收图目录 ${esc(r.inbox || "")}</div>`;
  } catch (e) {
    box.innerHTML = `<div class="hint">${esc(e.message)}</div>`;
  }
}

/* ---------------- 语音 ---------------- */
async function renderTTSExtra() {
  const box = $("#ttsExtra");
  box.innerHTML = "";

  // ① 本地部署
  const c0 = card("本地部署（后端选「qwentts」或「local_http」时）",
    "① qwentts.cpp 的 tts-server（GGUF，CPU/Vulkan，推荐）：命令示例 tts-server.exe --model qwen-talker-*.gguf --codec qwen-tokenizer-12hz-*.gguf --port 7865，后端选 qwentts、地址填 http://127.0.0.1:7865；" +
    "② 其它本地 Qwen3-TTS 服务（POST /tts）；③ OpenAI 风格 /v1/audio/speech。" +
    "「本地启动命令」填在上面配置卡里，填了这里就能一键启停。");
  c0.innerHTML += `<div class="grid">
      <div class="field"><label>本地模型（当前加载的 GGUF）</label><select id="ttsLocalModel"></select></div>
      <div class="field"><label>&nbsp;</label><button id="ttsApplyModel">用这个模型</button></div>
      <div class="field"><label>本地音色（qwentts 预设）</label><select id="ttsLocalVoice"></select></div>
      <div class="field"><label>&nbsp;</label><button id="ttsApplyVoice">用这个音色</button></div>
      <div class="field"><label>推理设备</label><select id="ttsLocalDevice">
        <option value="gpu">GPU（Vulkan 加速，默认）</option>
        <option value="cpu">CPU（不占显存，慢一些）</option>
        <option value="auto">自动（引擎自选）</option>
      </select></div>
      <div class="field"><label>&nbsp;</label><button id="ttsApplyDevice">应用设备（需重启服务）</button></div>
    </div>
    <div class="hint">「用这个模型」写进配置的「模型名」（qwentts 服务端的模型由启动命令里的 --model 决定，这里只是记录）；「用这个音色」写进「音色」字段，合成时送给服务。</div>
    <div class="hint" style="color:var(--warn)">⚠ 本地部署大语言模型时可能会抢占显存 —— TTS 若变慢或报“显存不足”，可把推理设备切到 CPU，或降低 LLM 的显存占用（-ngl / 上下文）。</div>
    <div id="ttsLocalStatus" class="hint" style="margin-top:8px">查询中…</div>`;
  const localActs = document.createElement("div");
  localActs.className = "row-actions";
  localActs.appendChild(btn("▶ 启动本地服务", async () => {
    const r = await api("/api/tts/local/start", { method: "POST", body: {} });
    toast(r.message || (r.ok ? "已启动" : "启动失败"), !r.ok);
    loadTTSLocal();
  }));
  localActs.appendChild(btn("■ 停止本地服务", async () => {
    const r = await api("/api/tts/local/stop", { method: "POST" });
    toast(r.message || "已停止", !r.ok);
    loadTTSLocal();
  }));
  localActs.appendChild(btn("🔌 检测本地服务", async () => {
    const r = await api("/api/tts/local/probe", { method: "POST" });
    toast(r.message, !r.ok);
    loadTTSLocal();
  }));
  localActs.appendChild(btn("刷新状态", loadTTSLocal));
  c0.appendChild(localActs);
  box.appendChild(c0);

  const c1 = card("当前状态 / 试听", "Qwen3-TTS：本地服务（local_http）/ 阿里云百炼（dashscope）/ OpenAI 兼容（openai_compat）。");
  box.appendChild(c1);
  const st = document.createElement("div");
  st.id = "ttsStatusBox";
  c1.appendChild(st);
  const actions = document.createElement("div");
  actions.className = "row-actions";
  actions.appendChild(btn("🔊 试合成一句", async (ev) => {
    try {
      const r = await api("/api/tts/test", { method: "POST" });
      if (r.ok && r.url) { playAudio(r.url); toast("合成好了，正在放"); }
      else toast(r.message || "合成失败", true);
    } finally { loadTTSStatus(); }
  }));
  actions.appendChild(btn("⏸ 停止 / 清空队列", async () => {
    const r = await api("/api/tts/stop", { method: "POST" });
    if (_audio) { try { _audio.pause(); } catch {} }
    toast(`清掉 ${r.cleared} 条`);
  }));
  actions.appendChild(btn("导出收藏 md", () => window.open("/api/tts/favorites/export", "_blank")));
  c1.appendChild(actions);
  loadTTSStatus();

  const c2 = card("收藏", "消息右下角「♡ 收藏」会进这里。");
  c2.innerHTML += `<div class="scroll-y" id="favList"><div class="hint">加载中…</div></div>`;
  box.appendChild(c2);
  loadFavorites();
  loadTTSLocal();
}

const LOCAL_TTS_MODELS = [
  "qwen-talker-1.7b-customvoice-Q4_K_M",
  "qwen-talker-1.7b-customvoice-Q8_0",
  "qwen-talker-1.7b-base-Q8_0",
  "qwen-talker-1.7b-voicedesign-Q8_0",
  "qwen-talker-0.6b-customvoice-Q4_K_M",
];

const LOCAL_TTS_VOICES = [
  ["serena", "serena（女声·中文）"],
  ["vivian", "vivian（女声·中文）"],
  ["uncle_fu", "uncle_fu（男声·中文）"],
  ["dylan", "dylan（北京话）"],
  ["eric", "eric（四川话）"],
  ["ryan", "ryan（英文）"],
  ["aiden", "aiden（英文）"],
  ["ono_anna", "ono_anna（日语）"],
  ["sohee", "sohee（韩语）"],
];

async function fillLocalModels() {
  const sel = $("#ttsLocalModel");
  if (!sel) return;
  const cur = (A.config && A.config.tts && A.config.tts.model) || "";
  let detected = [];
  try {
    const scan = await api("/api/models/local");
    detected = (scan.tts_talkers || []).map((t) => t.name);
  } catch {}
  const opts = detected.length ? detected : [...LOCAL_TTS_MODELS];
  if (cur && !opts.includes(cur)) opts.unshift(cur);
  sel.innerHTML = "";
  for (const m of opts) {
    const o = document.createElement("option");
    o.value = m; o.textContent = m;
    sel.appendChild(o);
  }
  const custom = document.createElement("option");
  custom.value = "__custom__"; custom.textContent = "自定义…";
  sel.appendChild(custom);
  if (cur && opts.includes(cur)) sel.value = cur;
  sel.onchange = () => {};
}

function fillLocalVoices() {
  const sel = $("#ttsLocalVoice");
  if (!sel) return;
  const cur = ((A.config && A.config.tts && A.config.tts.voice) || "").toLowerCase();
  sel.innerHTML = "";
  const known = LOCAL_TTS_VOICES.map(([v]) => v);
  if (cur && !known.includes(cur)) {
    const o = document.createElement("option");
    o.value = cur; o.textContent = `当前：${cur}`;
    sel.appendChild(o);
  }
  for (const [v, label] of LOCAL_TTS_VOICES) {
    const o = document.createElement("option");
    o.value = v; o.textContent = label;
    sel.appendChild(o);
  }
  const custom = document.createElement("option");
  custom.value = "__custom__"; custom.textContent = "自定义…";
  sel.appendChild(custom);
  if (cur && known.includes(cur)) sel.value = cur;
}

async function loadTTSLocal() {
  await fillLocalModels();
  fillLocalVoices();
  const devSel = $("#ttsLocalDevice");
  if (devSel) {
    devSel.value = (A.config.tts && A.config.tts.local_device) || "gpu";
    const btnDev = $("#ttsApplyDevice");
    if (btnDev) {
      btnDev.onclick = async () => {
        await doSave({ tts: { local_device: devSel.value } }, true);
        const inp = document.querySelector('#cfg-tts .field[data-name="local_device"] select');
        if (inp) inp.value = devSel.value;
        toast("设备已保存：点「■ 停止本地服务」再「▶ 启动」生效");
        loadTTSLocal();
      };
    }
  }
  const btnApply = $("#ttsApplyModel");
  if (btnApply) {
    btnApply.onclick = async () => {
      let v = $("#ttsLocalModel").value;
      if (v === "__custom__") {
        v = prompt("填本地服务认识的模型名：", (A.config.tts && A.config.tts.model) || "") || "";
        if (!v.trim()) return;
      }
      await doSave({ tts: { model: v } }, true);
      const inp = document.querySelector('#cfg-tts .field[data-name="model"] input');
      if (inp) inp.value = v;
      toast(`模型换成 ${v}`);
      loadTTSLocal();
    };
  }
  const btnVoice = $("#ttsApplyVoice");
  if (btnVoice) {
    btnVoice.onclick = async () => {
      let v = $("#ttsLocalVoice").value;
      if (v === "__custom__") {
        v = prompt("填服务支持的音色名（qwentts 预设见列表；克隆音色名也行）：", (A.config.tts && A.config.tts.voice) || "") || "";
        if (!v.trim()) return;
      }
      await doSave({ tts: { voice: v } }, true);
      const inp = document.querySelector('#cfg-tts .field[data-name="voice"] input');
      if (inp) inp.value = v;
      toast(`音色换成 ${v}`);
      loadTTSLocal();
    };
  }
  const box = $("#ttsLocalStatus");
  if (!box) return;
  try {
    const r = await api("/api/tts/local/status");
    const devLabel = { gpu: "GPU（Vulkan0）", cpu: "CPU", auto: "自动" }[r.device] || r.device;
    const actual = r.backend ? ` <span class="tag ${/vulkan/i.test(r.backend) ? "ok" : ""}">实际后端 ${esc(r.backend)}</span>` : "";
    box.innerHTML = `
      <span class="tag ${r.running ? "ok" : "warn"}">${r.running ? "本地服务在跑（pid " + r.pid + "）" : "本地服务没在跑"}</span>${actual}
      ｜设备 ${esc(devLabel)}｜地址 ${esc(r.base_url || "（没填）")}｜模型 ${esc(r.model || "（没填）")}
      ${r.command
        ? `<div class="hint">启动命令（实际执行）：<code>${esc(r.effective_command || r.command)}</code>${r.auto_start ? "（随程序自动拉起）" : ""}</div>`
        : `<div class="hint">还没填「本地启动命令」——填了才能一键启停；不填也行，自己把服务起好再「检测」。</div>`}
      ${r.log_tail ? `<details class="adv"><summary>服务日志（尾巴）</summary><pre style="max-height:180px;overflow:auto;white-space:pre-wrap">${esc(r.log_tail)}</pre></details>` : ""}`;
  } catch (e) {
    box.innerHTML = `<div class="hint">${esc(e.message)}</div>`;
  }
}

async function loadTTSStatus() {
  const box = $("#ttsStatusBox");
  if (!box) return;
  try {
    const r = await api("/api/tts/status");
    box.innerHTML = `<div><span class="tag ${r.enabled ? "ok" : "warn"}">${r.enabled ? "开着" : "关着"}</span>
      后端 ${esc(r.backend)}｜队列 ${r.queued}｜已合成 ${r.synth_count}
      ${r.last_error ? `<div class="hint" style="color:var(--warn)">上次错误：${esc(r.last_error)}</div>` : ""}</div>`;
  } catch (e) { box.innerHTML = `<div class="hint">${esc(e.message)}</div>`; }
}
async function loadFavorites() {
  const box = $("#favList");
  if (!box) return;
  try {
    const r = await api("/api/tts/favorites");
    if (!r.items.length) { box.innerHTML = `<div class="hint">还没有收藏</div>`; return; }
    box.innerHTML = "";
    for (const it of r.items) {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--line);padding:6px 2px";
      row.innerHTML = `<span class="tag">${esc(it.category)}</span><span style="flex:1">${esc(String(it.text).slice(0, 80))}</span>`;
      row.appendChild(btn("▶", () => api("/api/tts/speak", { method: "POST", body: { text: it.text } }).then((x) => x.ok ? playAudio(x.url) : toast(x.message, true))));
      row.appendChild(btn("✕", async () => { await api(`/api/tts/favorites/${it.id}`, { method: "DELETE" }); loadFavorites(); }));
      box.appendChild(row);
    }
  } catch (e) { box.innerHTML = `<div class="hint">${esc(e.message)}</div>`; }
}

/* ---------------- 联网搜集 ---------------- */
async function renderSearchExtra() {
  const box = $("#searchExtra");
  box.innerHTML = "";
  const c1 = card("B站采集", "登录后部分接口更稳（搜索/字幕匿名也能用）。扫码确认时选「记住我 / 长期」。");
  c1.innerHTML += `<div id="biliStatus"><div class="hint">查询中…</div></div>`;
  const actions = document.createElement("div");
  actions.className = "row-actions";
  actions.appendChild(btn("刷新登录状态", loadBiliStatus));
  actions.appendChild(btn("扫码登录（出二维码）", startBiliLogin));
  actions.appendChild(btn("🧪 测一次网页搜索", async () => {
    const r = await api("/api/search/test", { method: "POST", body: { query: "测试 ai-chat-core" } });
    toast(r.ok ? `搜到 ${r.count} 条：${(r.results?.[0]?.title || "").slice(0, 40)}` : (r.message || "没搜到"), !r.ok);
  }));
  c1.appendChild(actions);
  const qr = document.createElement("div");
  qr.id = "biliQr";
  c1.appendChild(qr);
  box.appendChild(c1);
  loadBiliStatus();
}
async function loadBiliStatus() {
  const box = $("#biliStatus");
  if (!box) return;
  try {
    const r = await api("/api/bili/status");
    box.innerHTML = r.logged_in
      ? `<span class="tag ok">已登录</span> <b>${esc(r.username || "")}</b>（${esc(r.uid || "")}）｜报告 ${r.reports} 天`
      : `<span class="tag warn">未登录</span> ${esc(r.message || "")}`;
  } catch (e) { box.innerHTML = `<div class="hint">${esc(e.message)}</div>`; }
}
async function startBiliLogin() {
  const out = $("#biliQr");
  out.innerHTML = `<div class="hint">取二维码…</div>`;
  const r = await api("/api/bili/login/start", { method: "POST" });
  if (!r.ok) { out.innerHTML = `<div class="hint">${esc(r.message || "失败")}</div>`; return; }
  out.innerHTML = `<div class="qrbox"><img src="${r.qr}" alt="二维码"></div>
    <div class="hint">用手机 B站 扫码（选「记住我」）。<a href="${esc(r.url)}" target="_blank">打不开就点这个链接</a></div>
    <div class="hint" id="biliPoll">等待扫码…</div>`;
  let n = 0;
  const timer = setInterval(async () => {
    n++;
    if (n > 90) { clearInterval(timer); return; }
    try {
      const p = await api("/api/bili/login/poll", { method: "POST", body: { key: r.key } });
      const el = $("#biliPoll");
      if (el) el.textContent = p.message || p.state;
      if (p.state === "ok") { clearInterval(timer); toast("登录好了"); loadBiliStatus(); out.querySelector("#biliPoll").textContent = "✓ 已登录"; }
      if (p.state === "expired" || (p.state === "unknown" && !p.ok)) { clearInterval(timer); }
    } catch {}
  }, 2000);
}

/* ---------------- 系统 ---------------- */
async function renderSysExtra() {
  const box = $("#sysExtra");
  box.innerHTML = "";
  const c1 = card("运行信息", "");
  try {
    const r = await api("/api/system/info");
    c1.innerHTML += `<div class="hint">
      版本 v${esc(r.version)}｜已运行 ${Math.floor(r.uptime / 60)} 分钟｜会话 ${r.conversations} 个<br>
      数据目录：${esc(r.data_dir)}<br>配置文件：${esc(r.config_file)}<br>
      日志文件：logs/app.log（本页「日志」标签是实时的）</div>`;
  } catch (e) { c1.innerHTML += `<div class="hint">${esc(e.message)}</div>`; }
  box.appendChild(c1);
}

/* ---------------- 日志 ---------------- */
let logSource = null;
function renderLogs() {
  const box = $("#logsBody");
  box.innerHTML = "";
  const c = card("实时日志", "所有模块共用这一条日志线；级别在「系统」里可改。");
  c.innerHTML += `<div class="row-actions" style="margin-bottom:10px">
    <select id="logLevel" style="max-width:150px">
      <option>DEBUG</option><option selected>INFO</option><option>WARNING</option><option>ERROR</option>
    </select>
    <button id="logPause">暂停</button>
    <button id="logClear">清屏</button>
    <span class="hint" id="logStat"></span></div>
    <div class="log-console" id="logConsole"></div>`;
  box.appendChild(c);
  let paused = false;
  const cons = () => $("#logConsole");
  const push = (item) => {
    if (paused) return;
    const el = document.createElement("div");
    el.className = "log-line " + item.level;
    el.innerHTML = `<span class="t">${esc(item.ts)}</span> <span class="lv">[${esc(item.level)}]</span> <span class="lg">${esc(item.logger)}</span> ${esc(item.message)}`;
    cons().appendChild(el);
    while (cons().children.length > 800) cons().firstChild.remove();
    cons().scrollTop = cons().scrollHeight;
  };
  const connect = () => {
    if (logSource) logSource.close();
    const lv = $("#logLevel").value;
    logSource = new EventSource("/api/logs/stream?level=" + lv);
    logSource.onmessage = (e) => { try { push(JSON.parse(e.data)); } catch {} };
    logSource.onerror = () => { $("#logStat").textContent = "连接断开，自动重连中…"; };
    logSource.onopen = () => { $("#logStat").textContent = "已连上"; };
  };
  $("#logLevel").onchange = connect;
  $("#logPause").onclick = (e) => { paused = !paused; e.target.textContent = paused ? "继续" : "暂停"; };
  $("#logClear").onclick = () => { cons().innerHTML = ""; };
  connect();
}