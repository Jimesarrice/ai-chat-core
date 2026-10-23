# 03 · QQ 桥 + 朗读桥 —— 机制提炼（可重实现规格书）

> 阅读标的（行号 = 对应源文件行号；引文均为逐字引用）：
>
> | 代号 | 文件 | 行数 | 作用 |
> |---|---|---|---|
> | A | `references/dsh-qq-bridge/lib/host.js` | 841 | 通到 QQ：DSH↔QQ 双向（宿主半边） |
> | C | `references/dsh-qq-bridge/tools/语音/`（`录语音.mjs` 294 行、`录音回环.ps1` 398 行、`README.md`、`启动-录语音.bat`） | — | 录音回环 → QQ 语音条（附加件） |
> | B | `references/dsh-tts-bridge/lib/index.js` | 857 | 朗读桥（宿主半边） |
> | B′ | `references/dsh-tts-bridge/lib/panel.js`（另 `extension/panel.js` 同款） | 612 | DSH 右下角卡片 UI（导出格式、状态文案） |
>
> 目标：为独立项目（FastAPI + Web 聊天页 + OneBot QQ + Qwen3-TTS）提炼「接口形状 / 去重逻辑 / 常量 / 文案」，标注哪些是 DSH 专有、如何替代。

---

## 一、通到 QQ（host.js）

### A0. 全局常量与文件位置（逐字）

| 常量 | 值 / 表达式 | 行号 |
|---|---|---|
| 数据目录 | `const 目录 = process.env.DSH_HOME \|\| join(homedir(), '.dsh')` | L25 |
| 配置文件 | `join(目录, 'qq-bridge-config.json')` | L26 |
| 路由前缀 | `const 路由前缀 = '/dsh-qq-bridge'` | L27 |
| 日志上限 | `const 日志上限 = 60` | L28 |
| 收图备份目录 | `const 收图目录 = join(目录, 'qq-bridge-inbox')` | L98 |
| 收图反查表 | `const 收图表 = join(目录, 'qq-bridge-received.json')` | L105 |
| NapCat 默认地址 | `NapCat: 'http://127.0.0.1:3000'` | L38 |
| 收消息轮询间隔 | `ctx.interval(..., 4000)`（毫秒） | L831 |
| 发段间隔 | `const 发段延迟 = 450 // 每段之间隔一下，像真人一条条发` | L373 |
| 收图最多（归一化夹取） | 1–10，默认 4 | L86 |
| 图上限MB（归一化夹取） | 1–30，默认 5（两个方向共用） | L87 |

### A1. OneBot HTTP 接口用法

**统一请求形状**（`发一个`，L301-313；`拉`，L733-743）：`POST {base}/{action}`，headers `{'content-type': 'application/json'}`，body `JSON.stringify(体)`。发送判定：``ok: !!(j && j.status === 'ok' && j.retcode === 0)``（L309）。网络异常返回 `{ action, ok: false, 数据: { 错: String(...) } }`（L311）。

| action | 调用点 | 请求体（逐字） | 响应消费 |
|---|---|---|---|
| `send_private_msg` | L419、L423 | `{ user_id: Number(配置.私聊QQ), message: [{ type: 'text', data: { text } }] }`；图片：`{ user_id, message: [一段图] }` | `j.status==='ok' && j.retcode===0` |
| `send_group_msg` | L431、L435 | `{ group_id: Number(配置.群号), message: [...] }` 同上 | 同上 |
| `get_login_info` | L595（`/state` 概况里） | body `'{}'` | `j.data.user_id` / `j.data.nickname`（L597） |
| `get_friend_msg_history` | L753 | `{ user_id: 她的号, count: 10 }` | `j.data.messages`（必须数组，L739） |
| `get_group_msg_history` | L768 | `{ group_id: Number(配置.群号), count: 30 }` | 同上 |
| `record`（在 C 节 `录语音.mjs` 里） | L217-221 | `{ user_id: Number(私聊QQ), message: [{ type: 'record', data: { file: 'file:///…wav' } }] }` | `j.status === 'ok'` |

- 消息段构造：私聊 `私人消息体(text)` = `{ user_id: 0, message: [{ type: 'text', data: { text } }] }`（L230-232）；群 = `{ group_id: 0, message: [...] }`（L235-237，导出给冒烟测试喂，0 为占位）。
- 图片段两种形态（L245-248、L413）：
  - base64：`图片段(base64)` = `{ type: 'image', data: { file: 'base64://' + String(base64) } }`；
  - 网络图：`{ type: 'image', data: { file: x.url, url: x.url } }`（直接把 url 交给 QQ，不依赖本机文件）。
- 消费侧消息条目字段（拉历史后逐条读）：`time`（秒级时间戳）、`user_id`、`message_id`、`message`（段数组）。
- ⚠ 移植注意：`get_friend_msg_history` / `get_group_msg_history` 是 NapCat/SnowLuma 系的扩展接口，不是 OneBot v11 基础动作；README 明确「只要你的后端把这 6 个接口开在 HTTP 上，插件就能用」（README L152-162）。我们改用 WS 事件后不再依赖它们。QQ 号全程 `Number()` 转换，JS 安全整数范围内的号码没问题。

### A2. 接收轮询（QQ → DSH）

- **间隔 4 秒**：`ctx.interval(() => { void 轮询收消息()... }, 4000)`（L827-831）；启动日志自称「收消息 4 秒轮询一次」（L839）。拿不到 timer 时只记一条日志、放弃轮询（L823-825）。
- **冷启动跳过历史**：两条游标在插件加载时初始化为「此刻」——
  - `let 上次私聊时间 = Math.floor(Date.now() / 1000) // 从这一刻起只认新消息，不补旧的`（L287）
  - `let 上次群时间 = Math.floor(Date.now() / 1000)`（L288）
- **关闭状态不补账**：总开关关 / 没选目标会话时，只把两条游标推到此刻再返回（L725-730）。
- **私聊拉取与过滤**（L752-765，逐条）：
  1. 拉 `get_friend_msg_history { user_id: 她的号, count: 10 }`，`sort((a, b) => (a.time || 0) - (b.time || 0))` 升序；
  2. `if (!条 || Number(条.user_id) !== 她的号) continue`（**只认「私聊QQ」配置的那个人**）；
  3. `if ((条.time || 0) < 上次私聊时间) continue`（旧消息跳过）；
  4. `if (已转消息.has(条.message_id)) continue`（去过重）；
  5. `上次私聊时间 = 条.time || 上次私聊时间`（**先推游标**）；
  6. `已转消息.add(条.message_id)`（**先记 id**，哪怕下面判定为空不转）；
  7. 无文字且无图 → `continue`（纯表情不转，但 id 已记，不再反复拉）；
  8. `await 转入一条(文字, 图们, 条.message_id)`。
- **群聊**（L767-779）：同上，`count: 30`，**同样只认「她的号」**（`Number(条.user_id) !== 她的号`，L770）发的群消息；游标 `上次群时间` 独立。
- **去重机制**：内存 `Set`「已转消息」记 `message_id`；`if (已转消息.size > 300) 已转消息.clear()`（L781）。清空后若历史接口仍返回旧消息，靠游标（时间戳）二次兜底。
- **白名单语义**：不是「所有好友」——私聊只收 `私聊QQ` 那个号的，群聊只收该号在该群发的；机器人自己发的（user_id 不是她的号）天然过滤，不会自我繁殖（L720-721 注释）。
- **额外入口**：`POST /dsh-qq-bridge/receive`（L784-789），body `{text}`，无鉴权，是「用别的方式把 QQ 消息灌进来」的备用口；内部仍走 `转入一条` 的全部 gate（总开关 + 目标会话 + agent 活着）。
- **入队 gate**（`转入一条` 前三条，L639-644）：
  - `{ ok: false, 说: '还没选「目标会话」' }`
  - `{ ok: false, 说: '总开关关着，先打开' }`
  - ``{ ok: false, 说: `目标会话现在没活着（${短id(配置.目标会话)}），先把它打开` } ``（`短id` = 末 8 位，L525-528）

### A3. 消息转换

**3.1 QQ 段 → 文本/图**（`条转片段`，L744-750）：
- 文字 = `段们.map((s) => (s && s.type === 'text' && s.data ? String(s.data.text) : '')).join('').trim()`（多段直接拼接，无分隔符）；
- 图 = `段们.filter((s) => s && s.data && (s.type === 'image' || s.type === 'mface'))`——**表情包（mface）也算图**，因为带 url 能拿到字节（L747 注释）。
- ⚠ 只吃「段数组」形态；字符串 CQ 码不做解析（部分实现返回 CQ 字符串时这条消息会被当空处理）。移植时按真实后端格式决定。

**3.2 抠字节**（`取图字节`，L164-185）：依次尝试
1. `data.url` 以 `file://` 开头 → `readFileSync(new URL(url))`；
2. `data.url` 匹配 `/^https?:/i` → `fetch(url)`，`r.ok` 则 `Buffer.from(await r.arrayBuffer())`；
3. `data.file` 以 `base64://` 开头 → base64 解码；
4. `data.file` 含 `\` 或 `/`（本地路径）→ `readFileSync`；
5. 全失败返回 `null`。

**3.3 类型判别与扩展名**（L152-161）：魔数嗅探——`[0]==0x89 && [1]==0x50` → png；`0x47,0x49` → gif；`0x52,0x49 && [8]==0x57` → webp；**其余一律 jpeg**（判不了就当 jpeg）。

**3.4 本地备份目录规则**（L187-197 + L658）：
- 落盘路径 = `join(收图目录, `${名字}.${扩展(类)}`)`，目标目录 `~/.dsh/qq-bridge-inbox/`；
- 名字 = `` `${消息id || 'qq'}-${序}` ``（序从 1 起）；存不下返回空串，不影响转图（L188 注释「存不下就返回空串」）。

**3.5 写给模型的文案**（L662-670）：
- `说明` = `'\n（' + 谁 + '发了 ' + 收下.length + ' 张图：' + 每张「第N张 XXKB」(+「，要发回去就用 <本地路径>」) + '）'`；
- 文案 = `原话 + 说明`；原话为空且有图 → `'（' + 谁 + '发来了图片，没有配字）'`；
- 谁 = `String(配置.称呼 || '对方')`；文案仍为空 → `{ ok: false, 说: '这条既没文字也没图，不转' }`（L670）。
- 目的（L637-638 注释）：纯文本模型看不见图，至少能从文字里找到本地路径；要发回去就用这个路径。

**3.6 交给 DSH 附件系统**（L672-695）：`content = [{ type: 'text', text: 文案 }]`，然后 `content.concat(收下.map(→ { type: 'image', data: b64, mediaType, name: `qq-${消息id}-${i+1}.${ext}` }))`，交给 `attachments.admitPromptContent(待)`；失败则退回纯文字（`记账('图交给 DSH 没成功（这轮只转文字）：' + …)`），无 attachments 服务则 `记账('宿主没有 attachments 服务，图只能存本地（文字里已经给了路径）')`。

**3.7 原图反查表**（`收图表`，L107-150）——关键设计：
- 动机（L101-104 注释）：DSH 存图会留「归一化副本」（官方文档原话：may be resized or re-encoded），GIF 会变静态 webp，**回发必须用我们自己留的原图**；
- 记录两个键：`'id:' + 附件id` 和 `'p:' + 副本路径.toLowerCase()`（`记收图`，L124-139），值 = `{ p: 原图路径, t: Date.now() }`；
- 只留最近 **200** 条（按 t 排序取前 200，重写整个 JSON）；
- 查询 `原图路径(键)`（L142-150）：先查原键 → 小写键 → 去掉 `id:` 前缀的键；命中后还要 `existsSync(p)` 验证，不存在返回空串。
- 路径统一清洗：`清路径 = (p) => String(p || '').trim().replace(/^["']+|["']+$/g, '').trim()`（L117）。

**3.8 模型回复里的 markdown 图片 → 本地文件发送**（`抠正文图`，L250-273）：
- 正则：`/!\[[^\]]*\]\(\s*([^)\s]+)\s*\)/g` 匹配 `![名字](路径)`；
- `http(s)://` 开头 → `{kind:'url', value}`；否则 → `{kind:'path', value}`；同值去重（`图.some(x => x.value === p)`）；
- 抠完后清理：`.replace(/[ \t]+$/gm, '')`（行尾空白）、`.replace(/\n{3,}/g, '\n\n')`（3+ 连续换行压成 2）、trim；
- 返回 `{ 净文字, 图: [{kind, value}] }`；发送时正文用净文字（图那串 markdown 不会露出到 QQ，L19 README）。
- 触发条件：`配置.发图 ? 抠正文图(原文字) : { 净文字: String(原文字 || ''), 图: [] }`（L370 与 L453 两处）。

**3.9 相对路径解析根目录**（`找本地图`，L332-364，按序）：
1. 绝对路径（`/^[a-zA-Z]:[\\/]/`、`\\\\`、`/` 开头）→ 直接试；
2. 设置里的「图片根目录」（填了排最前）；
3. **目标会话所在工作区**——`workspaceRegistry.list()` 按 `sessionIds.indexOf(目标会话)` 排序，把目标会话的工作区排前面（L344-350）；工作区路径取 `w.path || w.directory || w.dir || w.root`；
4. 其它登记过的工作区；
5. `process.cwd()`；
6. `join(目录, 'workspace')`；
7. `homedir()`。
- 对每个根做 `join(根, 原路径)`，返回第一个 `existsSync` 命中的绝对路径；
- 全失败记账（逐字）：
  ``记账('找不到这张图：' + 原 + '（试过：' + 试.slice(0, 4).join(' ｜ ') + ' 等 ' + 试.length + ' 个位置；可在设置里填「图片根目录」）')``（L362）

**3.10 大小/数量限制**：
- 收图：`收图最多`（归一化 1–10，默认 4，L86）；`图上限MB`（1–30，默认 5，L87）；实际收图循环再夹一遍 `Math.max(1, Math.min(Number(配置.收图最多) || 4, 10))` 与 `… 图上限MB || 5, 30) * 1024 * 1024`（L649-650）；超限逐张跳过并记账。
- 发图：单张 `图上限MB`（夹 1–30，默认 5，L374）；**张数硬上限 4**——`加图` 里 `if (!源 || 图数据.length >= 4) return`（L378）；`提取图块` 最多取 10（`slice(0, 10)`，L242）；同一轮只发图的消息累计也 `slice(0, 10)`（L488）。

### A4. 发送路径（DSH → QQ）

**4.1 「整轮的最后一句」判定**（`收到事件`，L473-502）：
- 监听 `session/event`，只处理 `String(session.id) === String(配置.目标会话)` 的会话（L476）；每次事件重读磁盘配置（L474）。
- `assistant/message`（L478-490）：
  - `d.interrupted` 为真 → `上一轮 = { 轮号: -1, 文本: '', 图们: [] }` 直接重置（L480-483）；
  - `文 = String(提取正文(d.message) || '').trim()`（`提取正文` = 只拼 `type==='text'` 块、join('\n')、trim，丢掉 reasoning/image，L220-227）；
  - 有文字 → 覆盖 `上一轮 = { 轮号: d.turn, 文本: 文, 图们 }`；
  - 只有图没有字 → 保留/累计：`上一轮 = { 轮号: d.turn, 文本: 同轮?旧文本:'', 图们: (同轮?旧图们:[]).concat(图们).slice(0, 10) }`（L488，「先出图、后出字」不丢图）。
- `turn/end`（L492-501）落地条件（逐字）：
  ``const 完成 = !!(d.reason && d.reason.kind === 'completed')``；
  ``if (完成 && 上一轮.轮号 === d.turn && 有东西 && !已发轮.has(d.turn))`` →
  `已发轮.add(d.turn); void 落地一发(配置, 上一轮.文本, 上一轮.图们)`；
  然后无论如何重置 `上一轮`（L500）。
- 去重：`已发轮` Set 记 turn 号，`if (已发轮.size > 300) 已发轮.clear() // 防 Set 无限长`（L444）；同 turn 只发一次。
- 「最后一句」的准确含义：一个 turn 里最后一条**带文字的 assistant 消息的全文**（多轮工具步骤时取最后一条有字的；不是只取末段文字）。

**4.2 落地 gate 顺序**（`落地一发`，L443-470）：
1. `已发轮` 超 300 清空；
2. 没填目标会话 → `` '还没填「目标会话」，不知道该盯哪个，这轮不转' ``；
3. 目标会话已归档（`workspaceRegistry.archivedSessionIds`）→ `` '「目标会话」已经归档了，这轮不转（去设置里重新挑一个）' ``；
4. `!配置.总开关` → `'总开关关着，这轮该转但没转'`；
5. `配置.试运行` → 只记账：`` `试运行：本来要发到 QQ →「${净文.slice(0, 40)}${净文.length > 40 ? '…' : ''}」${图标}` ``（图标 = `` `（还有 ${几张} 张图）` ``，L455）；
6. 真发 → `发到QQ(配置, 文字, 图们)`，汇总记账：
   - 空结果：`'转私聊 / 转群都没开，或没填 QQ 号 / 群号，或者这轮既没有文字也没有图（而且图都没转成）：没地方发'`（L463）；
   - `` `${全过 ? '✅ 已发到 QQ：' : '⚠ 发 QQ 有问题：'}「${净文.slice(0, 50)}…」${图标}` ``（L466，截 50 字）；
   - 每条失败 `` `   ${r.action} 失败：${JSON.stringify(r.数据).slice(0, 140)}` ``（L468）。
- 安全默认（L9 注释）：`⚠ 安全默认：总开关 = false、试运行 = true —— 不手动打开，一句都不会真的发到 QQ。`

**4.3 拆段与节奏**（`发到QQ`，L366-440）：
- `按段拆`：`String(文字).split(/\n\s*\n/)` → trim → 过滤空（L316-322）；「一段一条连发」；
- 空文字兜底：`` 段们 = 按段拆(文字).length ? 按段拆(文字) : [String(文字||'').trim()].filter(s => s.length > 0) ``（L372，空文字最终 0 条文字段）；
- 每段之间 `await new Promise((r) => setTimeout(r, 发段延迟))`（450ms，L373/L420/L424/L432/L436）；**图片排在文字之后，一张一条**；
- 私聊段（`配置.转私聊 && 配置.私聊QQ`）→ 逐段 `send_private_msg`；群段（`配置.转群 && 配置.群号`）→ 逐段 `send_group_msg`；两个方向开关互相独立。

**4.4 测试发送**（`/test-send`，L792-802）：
- 固定文案（逐字）：`const 文字 = '（这是一条测试消息：DSH → QQ 通了）'`（L796）；
- **真发**：绕过 `总开关` 与 `试运行`，直接 `发到QQ(配置, 文字)`（注释 L791「手动试一发：**真发**（绕过试运行）」）；
- 前置：私聊 QQ 与群号都空 → `{ ok: false, 说: '私聊 QQ 和群号都是空的，先保存一个再试' }`；
- 记账：`` `手动测试：${全过 ? '✅ 已发' : '⚠ 有问题'}「${文字}」` `` + 逐条失败明细；
- 返回 `{ ok: 全过, 说: 全过 ? '测试句已经发到 QQ 了' : '有一步没发出去（看下面的记录）', 记录: 日志 }`（L801）。

**4.5 收图方向门禁**（见 A2 的 gate 与 3.5–3.7）。

### A5. 配置项清单与默认值（`默认配置`，L30-46；`归一化`，L66-91）

| 键（中文，逐字） | 默认 | 说明（注释逐字摘） | 行号 |
|---|---|---|---|
| `总开关` | `false` | 「关着 = 一句都不发（安全默认）」 | L31 |
| `试运行` | `true` | 「开着 = 只记账、不真发到 QQ」 | L32 |
| `目标会话` | `''` | 「盯哪个 DSH 会话（在设置页下拉里选，一般是你平时聊天的那一间）」 | L33 |
| `私聊QQ` | `''` | 「发到哪个私聊（QQ 号，字符串）」 | L34 |
| `群号` | `''` | 「发到哪个群（可空）」 | L35 |
| `转私聊` | `true` | | L36 |
| `转群` | `false` | | L37 |
| `NapCat` | `'http://127.0.0.1:3000'` | 「OneBot HTTP 接口（无 token）」 | L38 |
| `收图` | `true` | 「QQ → DSH：她发的图片也转进来」 | L40 |
| `收图最多` | `4` | 「一条消息最多转几张」（夹 1–10） | L41/L86 |
| `发图` | `true` | 「DSH → QQ：伙伴发的图片也发过去」 | L42 |
| `图上限MB` | `5` | 「单张最大多少 MB（两个方向共用）」（夹 1–30） | L43/L87 |
| `图片根目录` | `''` | 「正文里 `![名字](相对路径)` 按哪个目录去找（留空 = 自动猜）」 | L44 |
| `称呼` | `'对方'` | 「跟它说话的那个人怎么称呼（写进转给它的文案里，比如「小明发来了图片」）」 | L45 |

归一化规则（L60-90）：
- `布(值, 兜)`：接受 `true/'true'/1/'1'` 与 `false/'false'/0/'0'`，其余走兜底（L60-64）；
- `字(v)`：非字符串 → `''`，字符串 trim（L69）；
- `数(v, 兜, 低, 高)`：`Math.round(Number(v))`，非有限 → 兜底，否则 `Math.max(低, Math.min(n, 高))`（L70-74）；
- `NapCat` 空串回默认；`称呼` 空串回默认（L83/L89）；其余字符串键不清洗分支、直接取字。
- 读不到/坏 JSON → 全默认（`读配置`，L199-205）；写配置 `JSON.stringify(配置, null, 2)`（L210）。

### A6. 日志结构（内存环 + 宿主 logger）

- 结构（`记账`，L290-299）：`{ 时间, 文本 }` 推入数组，时间格式 `` `${补(月)}-${补(日)} ${补(时)}:${补(分)}:${补(秒)}` ``（`MM-DD HH:mm:ss`）；`if (日志.length > 日志上限) 日志.shift()`（上限 60）；同时 `ctx.logger?.info(`qq-bridge: ${文本}`)`（L297）。
- 日志数组通过 `/state` 概况返回（L601），设置页显示。
- 全部事件文案（逐条，行号 = 记账调用处）：
  | 类别 | 文案（模板） | 行号 |
  |---|---|---|
  | 启动 | `` `已装好 v${版本}：DSH↔QQ 两半都在｜总开关 ${开/关}｜试运行 ${'开（只记账）'/'关（真发）'}｜收消息 4 秒轮询一次` `` | L839 |
  | 无 timer | `'拿不到 timer（ctx.interval 不在）—— QQ → DSH 的轮询收消息跑不起来，但 DSH → QQ 照常能用'` | L824 |
  | 保存设置 | `` `设置已保存：目标会话 ${…'（没填）'}｜私聊 ${…'（没填）'}｜群 ${…'（没填）'}｜${'（只记账）'/'（真发）'/'（总开关关着）'}` `` | L630-631 |
  | 转发异常 | `` `转发时出错：${String(...)}` ``（session/event 回调 catch） | L811 |
  | 轮询异常 | `qq-bridge: 轮询出错：…`（走 ctx.logger，L829） | L829 |
  | 落地拦截 | `'还没填「目标会话」，…'` / `'「目标会话」已经归档了，…'` / `'总开关关着，这轮该转但没转'` | L445/450/452 |
  | 试运行 | `` `试运行：本来要发到 QQ →「…」（还有 N 张图）` `` | L458 |
  | 真发汇总 | `` `✅ 已发到 QQ：…` / `⚠ 发 QQ 有问题：…` `` + `` `   ${r.action} 失败：…` `` | L466-468 |
  | 无去处 | `'转私聊 / 转群都没开，或没填 QQ 号 / 群号，或者这轮既没有文字也没有图（而且图都没转成）：没地方发'` | L463 |
  | 收图异常 | `` `第 ${序} 张图下载失败，跳过` `` / `` `第 ${序} 张图太大（${N}MB），跳过` `` | L655-656 |
  | 发图异常 | `` `这张图太大（${N}MB），跳过` `` / `'这张图拿不到本地文件，跳过'` / `'读图失败，跳过：…'` | L388/404/391(407) |
  | 找不到图 | `'找不到这张图：${原}（试过：${前4个} 等 ${N} 个位置；可在设置里填「图片根目录」）'` | L362 |
  | 转进 DSH | `` `收到 QQ 里的${'图片'/'话'}（含 N 张图） → 转进 DSH：「…」` `` | L705-706 |
  | 转图降级 | `'图交给 DSH 没成功（这轮只转文字）：…'` / `'宿主没有 attachments 服务，图只能存本地（文字里已经给了路径）'` | L689/693 |
  | 手动测试 | `` `手动测试：✅ 已发/⚠ 有问题「…」` `` + 失败明细 | L799-800 |

### A7. DSH 耦合点 → 独立项目替代

| DSH 专有（行号） | 用途 | 独立项目替代 |
|---|---|---|
| `inject = ['agents','sessions','sessionTitle','webServer','timer','workspaceRegistry','attachments']`（L21） | 宿主能力声明 | 无此概念；能力直接 import 自己模块 |
| `ctx.on('session/event')` + `assistant/message` / `turn/end` 事件（L473-502, L807） | 「整轮说完」判定 | 聊天管道每轮完成时直接调 `on_turn_end(text, images)`；LLM 输出结束后就是「整轮」 |
| `agent.followup({id, role:'user', content, source:{kind:'qq'}})`（L701）+ `ctx.sessions.flush`（L703） | 把 QQ 消息喂进会话开一轮 | 直接投递进聊天队列（asyncio.Queue / 后台任务）；`source.kind='qq'` 标记来源的字段值得保留 |
| ⚠ `await agent.followup(...)` 必须 await（L698-700 注释）：不接住 rejection 会让宿主整体退出 | — | Python 侧 `await pipeline.push(...)`，别用 fire-and-forget |
| `workspaceRegistry.list()` / `archivedSessionIds`（L342-355, L449, L568） | 图片根目录自动猜 + 会话归档过滤 | 配置里显式「图片根目录」+「会话/聊天清单」来自自己的 DB |
| `attachments.admitPromptContent` / `imageHostPath`（L676, L685, L403） | 把图交给 DSH 附件系统 + 拿 DSH 副本路径 | 不需要；图落 `inbox/` 后直接在消息文本带路径（多模态时附 base64/文件） |
| `ctx.webServer.register({kind:'exact', path, handler})`（L604-617） | 设置页/测试路由 | FastAPI 路由（`/api/qq/state`、`/save`、`/test-send` 等） |
| `ctx.interval`（L827） | 4s 轮询 | **不用轮询**：OneBot v11 正向/反向 WebSocket 收 `message` 事件（`message_type: private/group`，`user_id/group_id/message[]/message_id/time`）；HTTP 只用于发送。若坚持 HTTP 双轨，保留「时间戳游标 + message_id 去重」 |
| `createSelfUpdate`（L280） | 插件自更新 | 丢弃 |
| 会话清单/标题（`sessions.list()`、`sessionTitle.get`，L563-588） | 设置页选目标会话 | 用自己聊天页的「会话/频道」列表；「目标会话」概念可推广为「转发目标：私聊/群/多个频道」 |

- 轮询去掉后仍需保留的：**message_id 去重**（WS 断线重连可能重放；若同时接 HTTP 拉取）——用有界 LRU/Set（原实现上限 300 清空，可改 LRU 更稳）；**白名单过滤**（只接受配置的 QQ 号，或扩展为 `allowed_users` 列表）；**图片落盘 + 路径注入文本**；**发送侧 450ms 节流**。

---

## 二、朗读桥（dsh-tts-bridge/lib/index.js）

### B0. 常量与文件位置

| 常量 | 值 | 行号 |
|---|---|---|
| 路由前缀 | `const P = '/dsh-tts-bridge'` | L14 |
| 配置目录 | `process.env.DSH_HOME \|\| join(homedir(), '.dsh')` | L36 |
| 配置文件 | `join(CFG_DIR, 'tts-bridge.json')` | L37 |
| 旧文件名（迁移读取） | `join(CFG_DIR, 'xiao-beike-tts.json')` | L39 |
| 默认分区 | `const DEFAULT_FOLDER = 'default'` | L41 |
| CORS 头 | `access-control-allow-origin: *`、`allow-headers: *`、`allow-methods: GET,POST,OPTIONS`、`allow-private-network: true`、`cache-control: no-store` | L19-26 |
| 请求体上限 | `if (raw.length > 100000) req.destroy()` | L99 |
| 问路端口 | `const 问路端口 = [19387, 39871, 39872]` | L802 |
| 扫描间隔 | `ctx.interval(scan, 1500)` | L358 |

### B1. 队列设计

**入队时机（「说完一整轮」判定）**——轮询式事件吸收（`scan` + `absorb`，L309-357）：
- `scan` 每 **1500ms**：对 `targets()`（`cfg.watch` 里还活着、未归档的会话）按 `session.seq` 增量拉取——首次见到的会话只记 `seen.set(id, seq)` 并 `say('开始盯着 ' + id + '（seq=' + seq + '）')`，**从当前 seq 起以后只认新事件**（不补历史）；之后 `if (seq <= prev) continue`，`events = s.snapshotEvents(prev)`，更新 seq → `absorb(events)`（L336-357）。这就是它的去重根基：**同一事件只处理一次**。
- `absorb`（L309-334）：用 `turnText: Map<turn, string[]>` 累计该 turn 内所有 `assistant/message` 的 `type==='text'` 块（trim 后 push）；收到 `turn/end` 时取 `kind = d.reason && d.reason.kind`，**只有 `kind === 'completed'` 且文字非空**才：`remember(text)`（最近列表）→ `if (state.auto) enqueue(text, false, null)`。
- 与 QQ 桥的区别：朗读桥把该 turn **所有** text 块 `join('\n\n')` 一起念（L328）；QQ 桥只取最后一条带字消息的全文。

**入队函数 `enqueue(text, force, messageId)`**（L290-306）：
1. `const clean = 去颜文字(text)`（L292，见 B6 清洗）；空 → `return false`；
2. **队列上限 6**：`if (state.queue.length >= 6) state.queue.shift()`（丢最旧，L294）；
3. push 条目（逐字）：
   ```js
   { id: state.nextId++, text: clean, force: !!force,
     messageId: messageId ? String(messageId) : null,
     at: Date.now(), claimedBy: null, claimedAt: 0 }
   ```
4. 日志：`` say('排队 ' + state.queue.length + ' 条：' + clean.slice(0, 40).replace(/\s+/g, ' ')) ``（L304）。

**条目状态机**（隐式四态）：
```
queued（在 queue，claimedBy=null）
  → claimed（扩展 /next 领走：claimedBy=client, claimedAt=now；30s 内同 client 可重领，他人可抢）
  → playing（扩展 /playing on:true 上报 {itemId, messageId, at} 心跳）
  → done（扩展 /ack：出队 + 写 history；或 sweep 超时清除）
中断态：calendar stop（stopWanted）→ /next 回 {id:0, stop:true} 一次
```
- 领走条件（/next，L494-504）：`` if (head && (state.auto || head.force)) `` 且 `` if (!head.claimedBy || head.claimedBy === client || Date.now() - head.claimedAt > 30000) `` → 领走并回 `item = { id, text }`；无 item 时 `item = null`。
- `force` 来源：面板「▶ 朗读」与 `/say` 用 `force: true`，**关闭自动朗读后仍可手动念**（L496）。

**「避免重复念同一段」实际手段**（没有文本级去重，靠三层）：
1. seq 增量快照——同一 `turn/end` 事件只被 absorb 一次（根本保证）；
2. `remember(text)` 只进「最近」展示列表，不去重入队（L283-288；`recent` 上限 30，`unshift` 新在前）；
3. 队列上限 6 + claimedBy 协议（同一 client 30s 内重申同一 head 不重复领取）。

### B2. 配置持久化：tts-bridge.json（`loadCfg`/`saveCfg`，L41-72）

结构（归一化后逐字）：
```js
{
  watch: [],                                  // Array<string>，勾选要听的会话 id
  folders: [{ id: 'default', name: '收藏' }], // 至少含默认分区（不存在则 unshift）
  favorites: [],                              // 见 B3；旧数据没有 folder 字段的补 'default'
  everSeen: false                             // 扩展是否来打过招呼（/hello 或 /next 的副作用）
}
```
- 读取：先读 `tts-bridge.json`，失败（不存在/坏）再读旧名 `xiao-beike-tts.json`（改名迁移，别丢收藏，L38-39、L44-49）；
- 读不到时默认：`{ watch: [], folders: [{ id: 'default', name: '收藏' }], favorites: [], everSeen: false }`（L62）；
- 保存：`mkdirSync(CFG_DIR, { recursive: true })` + `JSON.stringify(cfg, null, 2)`（L65-72）。
- 「听谁」语义：默认 `watch: []` = **一个都不听**（新用户自己勾，README L99）；`targets()` 跳过归档会话与非 live 会话（L136-146）；`/watch` 加/删后 `saveCfg` 并 `say('听这些会话：' + (cfg.watch.length ? cfg.watch.join(' , ') : '（一个都没选）'))`（L629-643）；`/sessions` 只列有标题的活会话（没起名的不列，L598-626）。
- `state.auto`（自动朗读开关）**不持久化**，进程内默认 `auto: true`（L77）；`/auto` body `auto !== false` 即开，关闭时清空队列（L540-546）。

### B3. 收藏（favorites）与导出

**条目结构**（`/favorite` 新建时，L665-672）：
```js
{ messageId, sessionId, text, who, folder, at }
```
- `text`：由 `locate(sessionId, messageId)` 找回的原文（L660；找回逻辑见 B6 的 deriveMessages 说明）；
- `who`：`titleOf(sid)`——会话标题（「记下『这句话是谁说的』——用会话自己的标题，通用又准确」，L662）；
- `folder`：`String(body.folder || DEFAULT_FOLDER)`；`unshift` 新在前；**上限 200**（`slice(0, 200)`，L673）；
- 去重键：`messageId`（`findIndex` 命中则 `splice` 取消收藏，返回 `on:false`，L653-658）；
- 分区（folders）：默认 `{id:'default', name:'收藏'}`；`/folder` 建/改名/删（L699-731）：删分区不能删 default（`'默认分区不能删'`），删时把里面收藏**挪回默认**（`folder → DEFAULT_FOLDER`）；新分区 id 生成：`'f' + Date.now().toString(36)`（L727）；改名空名 → `'分区名不能空'`；
- `/move`（L734-743）：按 messageId 改 folder；`/forget`（L745-751）：按 messageId 删除；
- 列表接口 `/favorites`（L680-696）：folders 带 `count`（该分区条目数），favorites 每条 `text.slice(0, 400)`。

**导出 markdown**（panel.js，L417-449，逐字格式）：
```js
const out = ['# 朗读桥 · 收藏', '']
for (const fo of folders) {
  const its = all.filter((f) => String(f.folder) === fo.id)
  if (!its.length) continue
  out.push('## ' + fo.name, '')
  for (const f of its) {
    out.push('- ' + (f.who ? '**' + f.who + '**' : '') + (f.at ? '（' + stamp(f.at) + '）' : '') + '：' + String(f.text || '').replace(/\n+/g, ' '))
  }
  out.push('')
}
```
- 即：`# 朗读桥 · 收藏` 一级标题，每分区一个 `## 分区名`，每条 `- **谁**（MM-DD HH:mm）：正文（换行压成空格）`；
- `stamp` = `MM-DD HH:mm`（panel.js L113-119）；下载文件名 `'朗读桥-收藏.md'`，Blob 类型 `'text/markdown;charset=utf-8'`；空分区跳过。

### B4. 停止（一键停）

- `POST /stop`（L441-446）：`state.stopWanted = true; state.playing = null; state.queue = []`——**停当前 + 清队列**（「免得后面还排着」注释，L440）；
- 停止信号传递：下次 `/next` 时 `if (state.stopWanted) { state.stopWanted = false; base.item = { id: 0, stop: true }; return }`（L488-492）——用 `id:0` 的特殊条目通知浏览器扩展停声；
- `/reset`（L554-559）同义（playings 置空、清队列、置 stopWanted），另用于面板手动复位；
- 两道超时清扫（`sweep`，L111-123）：
  - 播放心跳断：`state.playing && now - state.playing.at > 120000` → `say('播放心跳断了，当作已停')`、`playing = null`；
  - 领走没回执：`claimedBy && now - claimedAt > 150000` → 丢掉，`say('领走太久没回执，丢掉 N 条')`。
- `/status` 与 `/state` 调 sweep 后再回（L449-457, L565-593）。
- 状态文案（panel.js L598-601，逐字）：`'自动朗读：关'` / `'正在出声…'` / `'已发给 DeepSeek，等它复述…'` / `'待命（有新消息就念）'` / `'没在听任何会话'`。

### B5. 协议一览（同源 HTTP，前缀 `/dsh-tts-bridge`）

| 路由 | 方法 | 入参 | 出参 / 副作用 | 行号 |
|---|---|---|---|---|
| `/next` | GET/POST | `{client}` | `{ok, auto, watching[], waiting, lastError, item:{id,text} \| {id:0,stop:true} \| null}`；顺带 `lastNext=Date.now()`、`everSeen` 置真 | L471-505 |
| `/ack` | POST | `{id, ok, error, detail}` | 出队；`ok===false` 时清 playing；history 前插 `{id, ok, error(≤200), detail(≤700), text(≤80), at}`，**上限 20** | L507-523 |
| `/playing` | POST | `{id, on}` | on=true：`playing={itemId:id, messageId, at}`；on=false：`playing=null` | L526-538 |
| `/stop` `/reset` | POST | — | 见 B4 | L441-446 / L554-559 |
| `/auto` | POST | `{auto}` | 开关自动朗读；关时清队列 | L540-546 |
| `/say` | POST | `{text}` | `enqueue(text, true, null)`（人手念，force） | L548-552 |
| `/speak` | POST | `{sessionId, messageId}` | `locate` 找回文字 → `enqueue(找.text, true, body.messageId)`；找不到回 `{ok:false, error:'没找到这条回复的文字'}`；另写诊断文件 `tts-bridge-diag.json` | L398-438 |
| `/recent` | GET | — | `{recent:[{id,text,at}]}`（最近 30） | L561-563 |
| `/state` | GET | — | 全量体检：auto/watching/waiting/playing/next/remembered/watch/lastNext/lastHello/everSeen/extDir/live/favorites 数/injectRows/history(前 8) | L565-593 |
| `/sessions` `/watch` | GET/POST | — / `{id, remove}` | 会话清单 / 增减听谁 | L598-643 |
| `/favorite` `/favorites` `/folder` `/move` `/forget` | POST/GET | 见 B3 | 收藏与分区 | L646-751 |
| `/hello` | GET | — | 扩展报到（区分「没装扩展」与「装了但页面没开」） | L462-469 |
| `/panel.js` | GET | — | 现读磁盘返回卡片脚本（改卡片不用重启） | L755-765 |
| `/update` | GET | `?refresh=1` | 自更新检查 | L392-396 |
| `/where`（问路端口） | GET | — | `{ok:true, base: ctx.webServer.getOrigin()+P, pid}` | L802-851 |

### B6. 哪些逻辑值得复用，哪些要丢弃

**值得复用（移植到 Python）：**
- **队列 + claiming/ack/心跳/超时清扫**：`queue`（上限 6，丢最旧）+ `claimedBy/claimedAt`（30s 可重领）+ `playing` 心跳（120s 判死）+ claimed 超时（150s 清）+ history（20 条，带头部错误回显 `lastError`）——Qwen3-TTS 合成 worker 也适用（worker 认领→合成中→完成回执，超时清扫卡死任务）；
- **「整轮说完」判定**：turn/end + `reason.kind === 'completed'` + 文字非空；移植为「LLM 本轮输出结束且非空」钩子（我们的聊天管道天然就是一轮一文本，几乎不需要 turn 累计）；
- **`去颜文字` 清洗**（L254-281）——进 TTS 前必过；规则逐字：
  1. 括号内无正常字（`[\u4e00-\u9fa5A-Za-z0-9]` 测不到）的整块 → 空格：`/[（(][^（()）]{0,30}[)）]/g`；
  2. 白名单外全清：`/[^\u4e00-\u9fa5\u3000-\u303F\uFF00-\uFFEFA-Za-z0-9\s.,!?;:'"()\[\]…—－-]+/g → ' '`（emoji、★♡♪、ωΩ 等一律去掉，防「欧米伽」怪音）；
  3. ASCII 颜文字：`/[（(]?\s*[>＞<＜^vV][-_^.,;:·]{1,3}[>＜<^vV]\s*[)）]?/g`、`/[（(]?\s*[TToO0xX][-_^.,;:·]{1,3}[TToO0xX]\s*[)）]?/g`、`/\borz\b/gi`、`/[:;=8][-o*']?[)(\[\]dDpP/\\|{}]/g`、`/[（(]\s*[)）]/g`；
  4. 收尾空格式：`/ ([，。！？、；：）】》])/g → '$1'`、`/([（【《]) /g → '$1'`、trim。
  （注释警告：321 行处的 `return s` 曾被误删导致「一条都排不进队列」，移植时写单测盯住。）
- **「最近念过」`remember`**（30 条 unshift）——UI 展示用，可留；
- **收藏 + 分区 + 导出 markdown**——结构（messageId/sessionId/text/who/folder/at、上限 200、默认分区不可删、删分区回默认）与导出格式照抄即可，`who` 用我们自己的会话标题、`at` 时间戳。

**DSH / DeepSeek 网页端专有，丢弃或替代：**
- 浏览器扩展取件协议（/next、/ack、/playing、/hello、问路端口 `[19387, 39871, 39872]`、`panel.js` 注入、`tapIndex`/`webserver/index-inject` 双路注册 L776-792）——**全丢**；我们用 Qwen3-TTS 直接合成，不需要浏览器；
- `locate`/`textOfMessage` 里 DSH 官方推导 `session.deriveMessages()` 与 `snapshotEvents` 兜底、`data.stream` 的 `text-chunks` 恢复逻辑（L160-224）——**丢**（我们自己的聊天记录库就是唯一事实源）；
- `sessionTitle`、`workspaceRegistry.archivedSessionIds`、`ctx.sessions.list/get`、`ctx.interval` 扫描（1500ms）、`createSelfUpdate`、`tts-bridge-diag.json` 调试眼——丢/换成自己的会话表、异步事件、直接函数调用；
- 状态行 `'已发给 DeepSeek，等它复述…'` 之类文案改写成 TTS 语境（如「合成中…」）。

### B7. 朗读桥日志文案（`say = console.log('[朗读桥] ' + m)`，L88）

| 文案 | 行号 |
|---|---|
| `'播放心跳断了，当作已停'` | L115 |
| `'领走太久没回执，丢掉 ' + N + ' 条'` | L121 |
| `'排队 N 条：<前40字>'` | L304 |
| `'开始盯着 <id>（seq=<seq>）'` | L342 |
| `'读 <id> 出错：…'`（scan catch） | L351 |
| `'路由 <path> 出错：…'`（route catch） | L380 |
| `'自动朗读 = 开' / '自动朗读 = 关'` | L544 |
| `'听这些会话：a , b' / '（一个都没选）'` | L641 |
| `'问路端口已就位：127.0.0.1:<端口>'` | L837 |
| `'问路端口一个都没占上（19387/39871/39872 都被占了）'` | L841 |
| `'宿主端就绪：/dsh-tts-bridge/next'` | L853 |
| `console.error('[朗读桥] 读 panel.js 失败：…' / '存配置失败：…' / '启动失败（不影响 DSH）：…'` | L32/L70/L855 |
| 更新诊断 `'查更新：…'`（diag 回调） | L363 |

---

## 三、tools/语音/（录音回环 → QQ 语音）设计取舍

> 来源：`tools/语音/README.md`、`录语音.mjs`（看门狗）、`录音回环.ps1`（WASAPI 回环录音器）、`启动-录语音.bat`、`朗读信号哨兵/`（Chrome 扩展）。

**数据流**：AI 在朗读桥开始念 → 面板出现「🔊 点朗读」→ 哨兵扩展 `POST http://127.0.0.1:8765/start` → 看门狗 spawn PowerShell 录音器 → 边录边切段 → 发 QQ `record` 消息。

**两种模式（核心取舍）**：

| 模式 | 行为 | 取舍 | 实现要点 |
|---|---|---|---|
| **1 = 切断发**（默认） | 边录边发，念一段发一段 | 响应快；QQ 上连着几条 | 分段一落盘就进「发队列」，**串行发送**保证顺序（`消费()`，L152-164）；每 500ms 扫新段 |
| **2 = 完整发** | 整段念完，拼接成一条再发 | 一条干净语音；要等念完 | 分段先攒 `待拼[]`，录音进程 exit 后 `合成一条发()`：取第一段前 **44 字节**做 WAV 头 + 各段从 44 字节起的 PCM 体直接 `Buffer.concat`，回写 RIFF 大小（偏移 4）= `36 + 体长`、data 大小（偏移 40）= 体长；存 `-整段.wav`；**合成失败降级为分段发**（L166-200） |

- **切段策略（智能模式，`录音回环.ps1` `RecordSmart`，L272-368）**：目标 ~N 秒一段，**尽量切在停顿处**——`quietThresh = 400`（16bit 幅度 < 400，约 −38 dBFS 算静音）、`pauseMs = 350`（连续静音 350ms 才算「一句念完」）；录满 `minSec`（= 目标−2）后遇停顿切，`maxSegSec`（= 目标+2）硬切兜底；末端残段补写。
- **原子可见性**：分段先写 `path + '.part'` 再改名 `.wav`（`WriteWav`，L169-192），保证看门狗读到的都是完整文件；文件命名 `<base>-000.wav`、`-001.wav`…。
- **静音/废段处理**：≤1000 字节的分段视为没录到声，跳过并删除（L205-212、L171-176「这一轮没录到声，什么都不发」）；发送成功后删除文件（L226-228）。
- **停止链**：看门狗主循环 400ms 轮询朗读桥 `/status`，`playing` 由 true→false（「静了 → 停」）→ 写停止文件 `%TMP%\qqrec-stop.txt`（`os.tmpdir()/qqrec-stop.txt`，L27）；PS 每循环查 `File.Exists(stopFile)` 就 break，退出时删除停止文件（L141/165/268/359/366）；另有 **5 分钟强制停**（`Date.now() - 开始时刻 > 300000`，L285-287）。
- **路径坑**：分段写在系统临时目录（纯 ASCII，`os.tmpdir()/qqrec-<ts>`，L92-93 注释「不踩中文路径的坑」），发 QQ 用 `file:///` + 反斜杠换正斜杠（L217）。
- **环境变量默认值**（README「五、可调的环境变量」）：`QQ_BACKEND=http://127.0.0.1:3000`、`QQ_VOICE_TARGET=空（只录不发）`、`VOICE_OUT_DIR=桌面\AI语音`（日志 `记录.log` 同目录）、`VOICE_SEG_SECONDS=10`、`VOICE_WATCHDOG_PORT=8765`、`DSH_PORTS=19387,39871,39872`、`RECORDER_PS1=同目录`。
- **入口提示文案**（bat）：`1 = 切断发 —— 边录边发，念一段发一段（响应快，但会连着好几条）` / `2 = 完整发 —— 整段念完，合成一条再发（干净，但要等念完）`，默认 1；未填 QQ 号=只录不发。
- **对我们的口径参考**：我们不走「录系统回环」路线（Qwen3-TTS 直接出音频），但「**分段/整段**」两种交付口径可平移：按标点/句子切分逐段合成逐条发（等同模式 1，首响快）vs 整轮合成一条发送（等同模式 2）；`VOICE_SEG_SECONDS=10` 这个「目标段长」经验值、以及「停止信号 = 清队列 + 停当前」的语义都可借鉴。

---

## 四、独立项目落地映射（一页总表）

| 能力 | 复刻/参考 | 弃用 |
|---|---|---|
| OneBot 发送 | `send_private_msg` / `send_group_msg` 段数组形状（text/image/record）、`status==='ok' && retcode===0` 判定、450ms 节流、按空行拆段 | — |
| OneBot 接收 | 段数组解析（text + image/mface）、只认白名单 QQ 号、message_id 去重（改有界 LRU） | 4s 轮询 + 时间戳游标（改 OneBot v11 正向/反向 WS 直推事件进聊天管道；HTTP 拉历史可留作兜底） |
| 图片 | 三形态抠字节（url file:// / http(s) fetch / base64:// / 本地路径）、魔数嗅探、`inbox/<msgid>-<n>.<ext>` 落盘、文案带路径、原图反查表（id:/p: 双键、200 条上限）、markdown `![alt](path)` 抠图与相对路径根目录列表、发送前先查原图 | DSH attachments（admitPromptContent/imageHostPath） |
| 安全 | 总开关默认关 + 试运行默认开、测试发送绕过试运行真发、`已发轮`防重复、落地前重读配置 | DSH agent.followup / 会话归档检查 |
| 一轮判定 | `turn/end + reason.kind==='completed'` + 最后一条带字消息（QQ 桥）或全轮文字 join('\n\n')（朗读桥） | session/event、snapshotEvents、deriveMessages |
| TTS 队列 | 队列上限 6 丢最旧、claimedBy/claimedAt(30s) 领取、playing 心跳(120s) 判死、claimed 超时(150s) 清、ack 出队 + history(20)、stopWanted + 清队列 + 「id:0 stop 条目」语义 | 浏览器扩展取件协议、问路端口、panel.js 注入、DeepSeek 网页朗读 |
| 去颜文字 | 四条正则规则整段照抄（进 TTS 前清洗；面板/历史保留原文） | — |
| 收藏 | 条目 {messageId, sessionId, text, who, folder, at}、上限 200、默认分区不可删、删分区回默认、`f+base36` 分区 id、导出 markdown 格式与文件名 | — |
| UI | 状态文案句式（待命/合成中/正在出声、听谁 ▾、收藏 ▾、按住 0.6s 切自动朗读） | DSH 右下角卡片、index-inject/tapIndex |

> 移植红线（两条来自源码注释的教训）：
> 1. QQ 桥 L698-700：`followup` 必须 await，未处理的 rejection 会拖垮整个宿主 —— Python 侧同样「不要 fire-and-forget」；
> 2. 朗读桥 L271-275：清洗函数丢了 `return s` 导致「一条都排不进队列」——清洗/整轮判定要配单测。