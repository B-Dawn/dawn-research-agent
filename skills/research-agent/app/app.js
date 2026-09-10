"use strict";

// ---------------- 极简 Markdown 渲染（覆盖脚本输出：标题/表格/引用/代码/列表/加粗） ----------------
function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
const esc = (s) => escapeHtml(s === undefined || s === null ? "" : String(s));
function inline(s) {
  // 行内：先转义，再做 **粗体** 与 `代码`
  s = escapeHtml(s);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  return s;
}
function renderMarkdown(md) {
  if (!md) return "";
  const lines = md.split("\n");
  let html = "", i = 0;
  while (i < lines.length) {
    let line = lines[i];
    // 代码块
    if (line.trim().startsWith("```")) {
      let buf = [], j = i + 1;
      while (j < lines.length && !lines[j].trim().startsWith("```")) { buf.push(lines[j]); j++; }
      i = j + 1;
      html += "<pre><code>" + escapeHtml(buf.join("\n")) + "</code></pre>";
      continue;
    }
    // 标题
    let h = line.match(/^(#{1,3})\s+(.*)$/);
    if (h) { const lvl = h[1].length; html += "<h" + lvl + ">" + inline(h[2]) + "</h" + lvl + ">"; i++; continue; }
    // 引用
    if (line.startsWith(">")) {
      let buf = [], j = i;
      while (j < lines.length && lines[j].startsWith(">")) { buf.push(lines[j].replace(/^>\s?/, "")); j++; }
      i = j;
      html += "<blockquote>" + inline(buf.join("<br>")) + "</blockquote>";
      continue;
    }
    // 表格：当前行含 | 且下一行是分隔行
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      const parseRow = (r) => r.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(c => c.trim());
      const head = parseRow(line);
      let j = i + 2, body = [];
      while (j < lines.length && lines[j].includes("|")) { body.push(parseRow(lines[j])); j++; }
      i = j;
      let t = "<table><thead><tr>" + head.map(c => "<th>" + inline(c) + "</th>").join("") + "</tr></thead><tbody>";
      for (const row of body) t += "<tr>" + row.map(c => "<td>" + inline(c) + "</td>").join("") + "</tr>";
      t += "</tbody></table>";
      html += t;
      continue;
    }
    // 列表
    if (/^\s*[-*]\s+/.test(line)) {
      let buf = [], j = i;
      while (j < lines.length && /^\s*[-*]\s+/.test(lines[j])) { buf.push(lines[j].replace(/^\s*[-*]\s+/, "")); j++; }
      i = j;
      html += "<ul>" + buf.map(c => "<li>" + inline(c) + "</li>").join("") + "</ul>";
      continue;
    }
    // 空行
    if (!line.trim()) { i++; continue; }
    // 普通段
    html += "<p>" + inline(line) + "</p>";
    i++;
  }
  return html;
}

// ---------------- 工具 ----------------
const $ = (id) => document.getElementById(id);
function setStatus(id, msg, kind) {
  const el = $(id);
  el.className = "status" + (kind ? " " + kind : "");
  el.textContent = msg;
}
async function post(api, payload) {
  const headers = { "Content-Type": "application/json" };
  const tok = localStorage.getItem("ra_token");
  if (tok) headers["Authorization"] = "Bearer " + tok;
  const resp = await fetch("/api/" + api, {
    method: "POST",
    headers,
    body: JSON.stringify(payload || {}),
  });
  const data = await resp.json();
  // 会话失效/未登录时，回到登录门禁
  if (data && data.need_login) onSessionLost();
  return data;
}

// ---------------- 登录 / 注册 / 账户 ----------------
let ME = null;
function getToken() { return localStorage.getItem("ra_token") || ""; }
function flash(msg) { try { addAgent("> " + msg); } catch (e) {} }
async function refreshMe() {
  try {
    const r = await post("auth", { action: "me" });
    ME = (r && r.ok && r.user) ? r.user : null;
  } catch (e) { ME = null; }
  if (!ME) localStorage.removeItem("ra_token");
  renderUserbar();
  return ME;
}
function renderUserbar() {
  const box = $("userbar-top");
  if (!box) return;
  if (ME) {
    const ROLE_ZH = { admin: "管理员", teacher: "导师", member: "学生" };
const roleTag = '<span class="tagrole ' + ME.role + '">' + (ROLE_ZH[ME.role] || ME.role) + "</span>";
    box.innerHTML = '<div class="who">你好，<b>' + escapeHtml(ME.display || ME.name) + "</b>" + roleTag + "</div>" +
      '<button class="run ghost" id="ub-logout">退出登录</button>';
    $("ub-logout").addEventListener("click", doLogout);
    const ac = $("acct-card");
    if (ME.role === "admin") { if (ac) ac.style.display = ""; loadAccounts(); }
    else { if (ac) ac.style.display = "none"; }
  } else {
    box.innerHTML = '<button class="run" id="ub-login">登录</button>' +
      '<button class="run ghost" id="ub-reg">注册</button>';
    $("ub-login").addEventListener("click", showGate);
    $("ub-reg").addEventListener("click", showGate);
  }
}
function showGate() { document.body.classList.add("logged-out"); }
function onSessionLost() {
  localStorage.removeItem("ra_token");
  ME = null;
  showGate();
}
let _appEntered = false;
function enterApp() {
  document.body.classList.remove("logged-out");
  if (_appEntered) return;
  _appEntered = true;
  chatWelcome();
  switchTab("chat");
  // 模型未配置时给一次提示
  post("model_status", {}).then(r => {
    if (r && r.ok === false && !r.need_login) {
      addAgent("> **提示**：模型尚未配置（默认腾讯混元，需填 API Key）。去「模型与API」选「腾讯混元」预设并粘贴 Key，"
        + "或安装本地 Ollama 后，我才能**诊断画像、AI 深度推荐方向、自由回答科研问题**（不再只做命令检索）。");
    }
  });
}
function boot() { refreshMe().then(() => { if (ME) enterApp(); else showGate(); }); }
let gateMode = "login";
function setGateMode(m) {
  gateMode = m;
  const isReg = m === "register";
  $("g-display-row").classList.toggle("hidden", !isReg);
  const rr = $("g-role-row"); if (rr) rr.classList.toggle("hidden", !isReg);
  $("g-login").textContent = isReg ? "注册" : "登录";
  $("g-reg").textContent = isReg ? "返回登录" : "注册新账户";
}
function gateSubmit(isLogin) {
  const name = $("g-name").value.trim();
  const pwd = $("g-pwd").value;
  if (!name || !pwd) { setStatus("g-status", "用户名和密码都要填", "err"); return; }
  setStatus("g-status", isLogin ? "登录中…" : "注册中…");
  const payload = { action: isLogin ? "login" : "register", name, password: pwd };
  if (!isLogin) {
    const d = $("g-display").value.trim(); if (d) payload.display = d;
    const rsel = $("g-role"); if (rsel) payload.role = rsel.value;
  }
  post("auth", payload)
    .then(r => {
      if (!r.ok) { setStatus("g-status", r.error || "操作失败", "err"); return; }
      if (r.pending) {  // 导师注册：待审核，不自动进入
        setGateMode("login");
        setStatus("g-status", "✅ " + (r.message || "注册成功，待管理员审核") + "。审核通过后用此账户登录。", "ok");
        return;
      }
      if (r.token) localStorage.setItem("ra_token", r.token);
      refreshMe().then(() => { if (ME) { setGateMode("login"); enterApp(); } else setStatus("g-status", "登录成功但未能获取账户信息", "err"); });
    })
    .catch(e => { setStatus("g-status", "连接失败，请确认服务已启动（python web_app.py）。" + e, "err"); });
}
setGateMode("login");
$("g-login").addEventListener("click", () => gateSubmit(gateMode === "login"));
$("g-reg").addEventListener("click", () => setGateMode(gateMode === "register" ? "login" : "register"));
["g-name", "g-pwd", "g-display"].forEach(id => $(id) && $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") gateSubmit(gateMode === "login"); }));
async function doLogout() {
  const tok = getToken();
  if (tok) { try { await post("auth", { action: "logout", token: tok }); } catch (e) {} }
  localStorage.removeItem("ra_token");
  ME = null;
  renderUserbar();
  showGate();
  flash("已退出登录。");
}

// ---------------- 标签页 ----------------
const TITLES = {
  chat: ["智能对话", "用自然语言指挥科研智能体：检索、对比矩阵、选刊、方向推荐、团队协作与实验复现。"],
  guide: ["论文十步走", "第一次写论文？按十步流程图一步步完成：每步告诉你为什么做、怎么做、用哪个模块、交付什么。"],
  team: ["协作中心", "成员档案与任务板分工；合作邀请在「合作对接」，共同写论文去「论文协作」。"],
  collab: ["合作对接", "向系统内其他注册用户发起合作邀请；接受后互为合作者，再到「论文协作」授权共同写作。"],
  paper: ["论文协作", "论文项目：章节草稿 / 审阅意见 / 协作者权限（可编辑·仅查看）；项目自动收录进协作者「我的论文」。"],
  reflib: ["文献库", "参考文献文件夹：只收「文献检索」的真实结果；库内检索、批量精读分析表、综合创新点与可行性。"],
  reviewhub: ["评审工作台", "AI 专家盲审（三位匿名专家、材料去标识化）与协作者审核：四维评分+结论，全程留痕。"],
  accounts: ["账户管理", "（仅管理员）账户角色、启用/停用、改密、删除与新建。"],
  lab: ["实验台账", "登记实验、填结果、发起复现与 CSV 校验——实验证据链的主入口。"],
  stat: ["统计分析", "描述统计 + Welch t 检验（纯本地）+ 出图前的数据把关。"],
  algo: ["算法与训练", "训练集检索下载 · 算法生成/校验 · 自动训练设计（可调用我的论文）。"],
  search: ["文献检索", "多源检索（arXiv / Semantic Scholar / OpenAlex / Crossref），跨源去重与 Markdown 清单。"],
  matrix: ["对比矩阵", "把检索结果转成可填写的文献对比矩阵，自动填充年份/标题/被引/链接。"],
  plot: ["出图", "读 CSV 生成论文级 SVG 图（折线/柱状/散点），零依赖，可直接贴进论文。"],
  journal: ["选刊", "按摘要与期刊 scope 契合度打分排序，输出初筛报告与人工决策表。"],
  format: ["论文格式检查", "结构完整性 / 参考文献编号 / 图表标号 / 标点 / 段落规则检查；支持本科毕业论文与期刊论文，可按你的模板自定义必需章节。"],
  digest: ["论文精读", "粘贴论文文本：技术点分析 / 创新点总结 / 可行性分析 / 空白研究分析 / 中英翻译；只依据文本不编造。"],
  recommend: ["方向推荐", "技能×方向交叉检索按复用度/热度/数据/时间打分（规则版），或用 AI 深度推荐（需配置模型）。"],
  profile: ["科研画像", "自主输入研究方向/创新点/约束，AI 才能据此诊断画像、驱动方向推荐（模型配置已移到「模型与API」）。"],
  write: ["写作综述", "AI 写作助手（大纲/初稿/润色/投稿信/审稿回复）+ 文献综述工作台 + 草稿库；不编造文献与数据。"],
  mypaper: ["我的论文", "个人论文台账（仅本人可见）+ 合作对接：向其他注册用户发起合作邀请，接受后互为合作者。"],
  memory: ["长期记忆", "记下你的知识/经验/约定，只属于当前账户；智能对话的 AI 问答会自动带上这些记忆。"],
  modelapi: ["模型与API", "配置 AI 模型（默认腾讯混元，免费额度）与 API 信息识别；AI 问答 / AI 诊断 / 方向推荐 均依赖此配置。"],
  settings: ["设置", "个人信息、技能中心与账户管理；登录/注册入口在左侧栏底部。"],
};
function switchTab(tab) {
  if (!tab || !TITLES[tab]) return;  // 分组标题按钮等无 data-tab 的直接忽略
  document.querySelectorAll(".nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll("[data-pane]").forEach(p => p.classList.toggle("hidden", p.dataset.pane !== tab));
  $("tabTitle").textContent = TITLES[tab][0];
  $("tabSub").textContent = TITLES[tab][1];
  if (tab === "team") { loadMembers(); refreshTasks(); refreshPapers(); fillPartnerSelects(); }
  if (tab === "collab") { loadDir(); loadInvites(); }
  if (tab === "lab") { refreshExps(); refreshReps(); fillPartnerSelects(); }
  if (tab === "algo") { loadTpPapers(); listMine(); }
  if (tab === "profile") { fillProfileExtra(); loadDirection(); }
  if (tab === "modelapi") { refreshModelStatus(); }
  if (tab === "write") { loadDocs(); loadRevs(); }
  if (tab === "format") { loadFtPapers(); loadFtTemplate(); }
  if (tab === "memory") { loadMems(); }
  if (tab === "mypaper") { loadMyPapers(); loadDpPapers(); }
  if (tab === "guide") { bootGuide(); }
  if (tab === "chat") { refreshChatSelectors(); refreshChatModelBadge(); }
  if (tab === "reflib") { loadFolders(); }
  if (tab === "reviewhub") { loadReviews(); loadMine(); loadPartnersForReview(); }
  if (tab === "accounts") { loadAccounts(); }
  if (tab === "settings") { loadSelfInfo(); loadSkillCenter(); }
  syncAccountsVisibility();
  // 未选中的组自动折叠（手风琴）
  document.querySelectorAll(".nav-group").forEach(g => {
    const hasActive = !!g.querySelector(".nav button.active");
    g.classList.toggle("collapsed", !hasActive);
  });
  persistNav();
  // 顶栏已打开页面标签
  if (!_openedTabs.includes(tab)) { _openedTabs.push(tab); if (_openedTabs.length > 8) _openedTabs.shift(); }
  _curTab = tab;
  renderTopTabs();
}
document.querySelectorAll(".nav button[data-tab]").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));
// 侧栏折叠分组：点击大标题 = 手风琴（展开该组并收起其他组；再点一次可全部收起），状态记住
document.querySelectorAll(".nav-head").forEach(h => h.addEventListener("click", () => {
  const g = h.closest(".nav-group");
  const willOpen = g.classList.contains("collapsed");
  document.querySelectorAll(".nav-group").forEach(x => x.classList.add("collapsed"));
  if (willOpen) g.classList.remove("collapsed");
  persistNav();
}));
function persistNav() {
  const st = {};
  document.querySelectorAll(".nav-group").forEach(g => {
    st[g.querySelector(".nav-head").textContent.trim()] = g.classList.contains("collapsed") ? "1" : "0";
  });
  localStorage.setItem("ra_nav", JSON.stringify(st));
}
try {
  const _navSt = JSON.parse(localStorage.getItem("ra_nav") || "{}");
  document.querySelectorAll(".nav-group").forEach(g => {
    if (_navSt[g.querySelector(".nav-head").textContent.trim()] === "1") g.classList.add("collapsed");
  });
} catch (e) {}

// ---------------- 智能对话 ----------------
const chatLog = $("chat-log");
const chatIn = $("chat-in");
const chatBtn = $("chat-btn");
let chatting = false;

function addBubble(who, html) {
  const b = document.createElement("div");
  b.className = "bubble " + who;
  b.innerHTML = html;
  chatLog.appendChild(b);
  chatLog.scrollTop = chatLog.scrollHeight;
  return b;
}
function addUser(text) {
  return addBubble("user", escapeHtml(text).replace(/\n/g, "<br>"));
}
function addAgent(md) {
  return addBubble("agent", '<div class="md">' + renderMarkdown(md) + "</div>");
}

// ---------------- 对话联动：module → 平台功能映射 ----------------
const MODULE_INFO = {
  memory:   { tab: "memory",   label: "长期记忆",     icon: "🧠", changed: true },
  workflow: { tab: "guide",    label: "论文十步走",   icon: "🧭", changed: true },
  mypaper:  { tab: "mypaper",  label: "我的论文",     icon: "📄" },
  journal:  { tab: "journal",  label: "选刊",         icon: "📮" },
  plot:     { tab: "plot",     label: "出图",         icon: "📊" },
  ai:       { tab: "profile",  label: "AI 诊断/推荐", icon: "🤖" },
  recommend:{ tab: "recommend",label: "方向推荐",     icon: "🎯" },
  profile:  { tab: "profile",  label: "科研画像",     icon: "👤" },
  task:     { tab: "team",     label: "协作中心",     icon: "✅", changed: true },
  paper:    { tab: "collab",   label: "合作对接",     icon: "🤝" },
  experiment:{tab: "lab",      label: "实验台账",     icon: "🔬", changed: true },
  validate: { tab: "lab",      label: "实验台账",     icon: "🔬" },
  matrix:   { tab: "matrix",   label: "对比矩阵",     icon: "📋" },
  search:   { tab: "search",   label: "文献检索",     icon: "🔍" },
  ask:      { tab: null,       label: "AI 自由问答",  icon: "💬" },
  help:     { tab: null,       label: "帮助",         icon: "❓" },
};

/** 在对话气泡下方渲染操作栏（跳转标签页 / 数据变更提示 / AI 建议操作） */
function renderChatActions(module) {
  const info = MODULE_INFO[module];
  if (!info) return "";
  const parts = [];
  // 功能标签
  parts.push('<span class="chat-mod-tag">' + (info.icon || "📎") + ' ' + esc(info.label) + '</span>');
  // 跳转按钮
  if (info.tab) {
    parts.push('<button class="chat-act-btn" onclick="switchTab(\'' + info.tab + '\')">前往「' + esc(info.label) + '」</button>');
  }
  // 数据变更提示
  if (info.changed) {
    parts.push('<span class="chat-changed-hint">✏️ 数据已更新</span>');
  }
  // AI 自由问答：追加平台功能快捷入口
  if (module === "ask") {
    parts.push('<button class="chat-act-btn ghost" onclick="switchTab(\'search\')">🔍 检索文献</button>');
    parts.push('<button class="chat-act-btn ghost" onclick="switchTab(\'journal\')">📮 选刊</button>');
    parts.push('<button class="chat-act-btn ghost" onclick="switchTab(\'recommend\')">🎯 方向推荐</button>');
    parts.push('<button class="chat-act-btn ghost" onclick="switchTab(\'memory\')">🧠 记住结论</button>');
  }
  return '<div class="chat-actions">' + parts.join("") + '</div>';
}

function chatWelcome() {
  const md =
    "你好，我是**破晓**，你的论文全流程助手（🧭 论文十步走）。可以直接说：\n\n" +
    "**从零开始（工作流）：**\n" +
    "- 粘贴你的**个人简历/擅长技术**，我按十步走帮你把方向定下来\n" +
    "- 确认方向：<最终方向>（定稿后自动进入文献调研）\n\n" +
    "**文献与写作：**\n" +
    "- 检索 UAV 入侵检测 近三年论文 / 把刚才的结果生成对比矩阵\n" +
    "- 帮我选刊：<粘贴摘要>（可选国际/国内/全部）\n" +
    "- 推荐下个研究方向 / 我的论文\n\n" +
    "**实验与团队：**\n" +
    "- 登记实验 <名称> / 查一下实验台账 / 校验：<粘贴CSV>\n" +
    "- 看看任务进度 / 新建论文 <标题>\n\n" +
    "**记忆：** 记住：<内容> / 查看记忆\n\n" +
    "> 技能在各模块内调用（检索页有「是否调用」开关）；自定义技能在「设置 → 技能中心」上传。当前为**离线环境**时检索类返回空并提示；统计分析/团队协作无需联网。";
  addAgent(md);
}
async function sendChat() {
  if (chatting) return;
  const text = chatIn.value.trim();
  if (!text) return;
  addUser(text);
  chatIn.value = "";
  chatting = true;
  chatBtn.disabled = true;
  const thinking = addBubble("agent", '<span class="tag">思考中…</span>');
  try {
    const r = await post("chat", { text, model_preset: ($("chat-model") || {}).value || "" });
    thinking.remove();
    if (r.ok) {
      if (r.module === "plot" && r.svg) {
        addAgent(r.reply || "图已生成");
        addBubble("agent", r.svg);
      } else {
        addAgent((r.reply || "…") + renderChatActions(r.module));
      }
    } else {
      addAgent("出错了：" + r.error);
    }
  } catch (e) {
    thinking.remove();
    addAgent("请求失败，请确认服务仍在运行：" + e);
  } finally {
    chatting = false;
    chatBtn.disabled = false;
    chatIn.focus();
  }
}
chatBtn.addEventListener("click", sendChat);
chatIn.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); sendChat(); }
});

// ---------------- 各模块动作 ----------------
const ACT = {
  search: async () => {
    if ($("s-use-strategy") && $("s-use-strategy").checked) {
      setStatus("s-status", "调用「调研策略」技能定边界…");
      const sr = await post("skills", { action: "run", skill: "lit_strategy", input: $("s-query").value });
      if (sr.ok) { $("s-strat-out").classList.remove("hidden"); $("s-strat-md").innerHTML = renderMarkdown(sr.reply); }
    }
    setStatus("s-status", "检索中…");
    const r = await post("search", {
      query: $("s-query").value, sources: $("s-sources").value,
      limit: +$("s-limit").value, from: $("s-from").value || null, to: $("s-to").value || null,
    });
    if (!r.ok) { setStatus("s-status", "失败：" + r.error, "err"); return; }
    $("s-md").innerHTML = renderMarkdown(r.markdown);
    $("s-out").classList.remove("hidden");
    setStatus("s-status", "命中 " + r.count + " 条" + (r.note ? "（" + r.note + "）" : ""), r.count ? "ok" : "warn");
    window._lastRecords = r.records || [];
    renderCollect();
  },
  matrix: async () => {
    setStatus("m-status", "生成中…");
    const r = await post("matrix", {
      sort: $("m-sort").value, max: +$("m-max").value || 0,
      json: $("m-json").value.trim(),
    });
    if (!r.ok) { setStatus("m-status", "失败：" + r.error, "err"); return; }
    $("m-md").innerHTML = renderMarkdown(r.markdown);
    $("m-out").classList.remove("hidden");
    setStatus("m-status", "已生成 " + r.rows + " 行对比矩阵", "ok");
  },
  plot: async () => {
    setStatus("p-status", "绘制中…");
    const r = await post("plot", {
      csv: $("p-csv").value, x: $("p-x").value, y: $("p-y").value, type: $("p-type").value,
      title: $("p-title").value, xlabel: $("p-xlabel").value, ylabel: $("p-ylabel").value,
    });
    if (!r.ok) { setStatus("p-status", "失败：" + r.error, "err"); return; }
    $("p-svg").innerHTML = r.svg;
    $("p-out").classList.remove("hidden");
    setStatus("p-status", "SVG 已生成，可右键另存为 .svg 贴入论文", "ok");
  },
  journal: async () => {
    setStatus("j-status", "匹配中…");
    const r = await post("journal", {
      text: $("j-text").value, target_if: +$("j-if").value, top: +$("j-top").value, min_if: +$("j-min").value,
      scope: $("j-scope") ? $("j-scope").value : "all",
    });
    if (!r.ok) { setStatus("j-status", "失败：" + r.error, "err"); return; }
    $("j-md").innerHTML = renderMarkdown(r.markdown);
    $("j-out").classList.remove("hidden");
    setStatus("j-status", (r.warning || "完成"), "warn");
  },
  recommend: async () => {
    setStatus("r-status", "推荐中（联网检索可能需数秒）…");
    const r = await post("recommend", {
      offline: $("r-offline").checked, max_queries: +$("r-mq").value,
      years: +$("r-years").value, limit: +$("r-limit").value,
    });
    if (!r.ok) { setStatus("r-status", "失败：" + r.error, "err"); return; }
    $("r-md").innerHTML = renderMarkdown(r.markdown);
    $("r-out").classList.remove("hidden");
    setStatus("r-status", "基于画像 " + r.profile + "，每周约 " + r.budget + " 小时", "ok");
  },
  profile: async () => {
    setStatus("pf-status", "解析中…");
    const r = await post("profile", {});
    if (!r.ok) { setStatus("pf-status", "失败：" + r.error, "err"); return; }
    $("pf-txt").textContent = r.text;
    $("pf-out").classList.remove("hidden");
    setStatus("pf-status", "画像来源：" + r.profile, "ok");
  },
};
document.querySelectorAll("button.run").forEach(b => {
  const act = b.dataset.act;
  if (act && ACT[act]) b.addEventListener("click", () => ACT[act]());
});

// ---------------- 写作助手 + 综述工作台 + 长期记忆 + 统计 ----------------
let _wrLast = "", _rvLast = "", _docsCache = [], _revsCache = [];

$("wr-gen").addEventListener("click", async () => {
  setStatus("wr-status", "生成中（最长 90 秒）…");
  const r = await post("write", { action: "gen", kind: $("wr-kind").value,
    topic: $("wr-topic").value, points: $("wr-points").value, extra: $("wr-extra").value });
  if (!r.ok) { setStatus("wr-status", r.error || "生成失败", "err"); return; }
  _wrLast = r.reply || "";
  $("wr-out").classList.remove("hidden");
  $("wr-md").innerHTML = renderMarkdown(_wrLast);
  setStatus("wr-status", r.offline ? ("已生成（离线骨架）" + (r.hint ? "：" + r.hint : "")) : "已生成（AI）",
            r.offline ? "warn" : "ok");
});
$("wr-save").addEventListener("click", async () => {
  if (!_wrLast) { setStatus("wr-status", "先点「生成」再保存", "err"); return; }
  const r = await post("write", { action: "save", kind: $("wr-kind").value,
    title: $("wr-topic").value, content: _wrLast });
  setStatus("wr-status", r.ok ? "已存入草稿库" : (r.error || "保存失败"), r.ok ? "ok" : "err");
  if (r.ok) { _docsCache = r.docs; renderDocs(); }
});
$("wr-refresh").addEventListener("click", loadDocs);
async function loadDocs() {
  const r = await post("write", { action: "list" });
  if (r.ok) { _docsCache = r.docs || []; renderDocs(); }
}
function renderDocs() {
  const box = $("wr-docs");
  if (!_docsCache.length) { box.innerHTML = '<p class="chat-hint">暂无草稿。生成后点「保存到草稿库」。</p>'; return; }
  box.innerHTML = '<table class="tbl"><tr><th>标题</th><th>类型</th><th>时间</th><th>操作</th></tr>' +
    _docsCache.map(d =>
      '<tr><td>' + esc(d.title) + '</td><td>' + esc(d.kind_zh || d.kind) + '</td><td>' + esc(d.ts) + '</td>' +
      '<td><button class="btn-mini" data-ac="view" data-id="' + d.id + '">查看</button>' +
      '<button class="btn-mini" data-ac="copy" data-id="' + d.id + '">复制</button>' +
      '<button class="btn-mini danger" data-ac="del" data-id="' + d.id + '">删除</button></td></tr>').join("") +
    '</table>';
}
$("wr-docs").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button.btn-mini");
  if (!b) return;
  const id = b.dataset.id, d = _docsCache.find(x => x.id === id);
  if (!d) return;
  if (b.dataset.ac === "view") {
    _wrLast = d.content;
    $("wr-out").classList.remove("hidden");
    $("wr-md").innerHTML = renderMarkdown(d.content);
    setStatus("wr-status", "已载入草稿：" + d.title, "ok");
  } else if (b.dataset.ac === "copy") {
    try { await navigator.clipboard.writeText(d.content); flash("已复制到剪贴板"); }
    catch (e) { flash("复制失败，请用「查看」后手动复制"); }
  } else if (b.dataset.ac === "del") {
    if (!confirm("确认删除草稿「" + d.title + "」？")) return;
    const r = await post("write", { action: "remove", id });
    if (r.ok) { _docsCache = r.docs; renderDocs(); flash("已删除"); }
  }
});

$("rv-gen").addEventListener("click", async () => {
  setStatus("rv-status", "生成综述中（最长 90 秒）…");
  const r = await post("review", { action: "gen", topic: $("rv-topic").value,
    angle: $("rv-angle").value, json: $("rv-json").value });
  if (!r.ok) { setStatus("rv-status", r.error || "生成失败", "err"); return; }
  _rvLast = r.reply || "";
  $("rv-out").classList.remove("hidden");
  $("rv-md").innerHTML = renderMarkdown(_rvLast);
  setStatus("rv-status", (r.offline ? "已生成（离线骨架）" : "已生成（AI，基于 %d 篇）").replace("%d", r.paper_count || 0) +
            (r.hint ? "：" + r.hint : ""), r.offline ? "warn" : "ok");
});
$("rv-save").addEventListener("click", async () => {
  if (!_rvLast) { setStatus("rv-status", "先点「生成综述初稿」再存档", "err"); return; }
  const r = await post("review", { action: "save", title: $("rv-topic").value,
    angle: $("rv-angle").value, content: _rvLast });
  setStatus("rv-status", r.ok ? "综述已存档" : (r.error || "存档失败"), r.ok ? "ok" : "err");
  if (r.ok) { _revsCache = r.reviews; renderRevs(); }
});
async function loadRevs() {
  const r = await post("review", { action: "list" });
  if (r.ok) { _revsCache = r.reviews || []; renderRevs(); }
}
function renderRevs() {
  const box = $("rv-list");
  if (!_revsCache.length) { box.innerHTML = '<p class="chat-hint">暂无综述存档。</p>'; return; }
  box.innerHTML = '<table class="tbl"><tr><th>标题</th><th>角度</th><th>时间</th><th>操作</th></tr>' +
    _revsCache.map(d =>
      '<tr><td>' + esc(d.title) + '</td><td>' + esc(d.angle) + '</td><td>' + esc(d.ts) + '</td>' +
      '<td><button class="btn-mini" data-ac="view" data-id="' + d.id + '">查看</button>' +
      '<button class="btn-mini" data-ac="copy" data-id="' + d.id + '">复制</button>' +
      '<button class="btn-mini danger" data-ac="del" data-id="' + d.id + '">删除</button></td></tr>').join("") +
    '</table>';
}
$("rv-list").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button.btn-mini");
  if (!b) return;
  const id = b.dataset.id, d = _revsCache.find(x => x.id === id);
  if (!d) return;
  if (b.dataset.ac === "view") {
    _rvLast = d.content;
    $("rv-out").classList.remove("hidden");
    $("rv-md").innerHTML = renderMarkdown(d.content);
    setStatus("rv-status", "已载入综述：" + d.title, "ok");
  } else if (b.dataset.ac === "copy") {
    try { await navigator.clipboard.writeText(d.content); flash("已复制到剪贴板"); }
    catch (e) { flash("复制失败，请用「查看」后手动复制"); }
  } else if (b.dataset.ac === "del") {
    if (!confirm("确认删除综述「" + d.title + "」？")) return;
    const r = await post("review", { action: "remove", id });
    if (r.ok) { _revsCache = r.reviews; renderRevs(); flash("已删除"); }
  }
});

// ---------------- 长期记忆 ----------------
$("mm-add").addEventListener("click", async () => {
  const text = $("mm-text").value.trim();
  if (!text) { setStatus("mm-status", "内容不能为空", "err"); return; }
  const r = await post("memory", { action: "add", kind: $("mm-kind").value, text });
  setStatus("mm-status", r.ok ? "已记住" : (r.error || "添加失败"), r.ok ? "ok" : "err");
  if (r.ok) { $("mm-text").value = ""; renderMems(r.entries); }
});
$("mm-refresh").addEventListener("click", loadMems);
async function loadMems() {
  const r = await post("memory", { action: "list" });
  if (r.ok) renderMems(r.entries);
}
function renderMems(entries) {
  const box = $("mm-list");
  if (!entries || !entries.length) { box.innerHTML = '<p class="chat-hint">暂无记忆。添加后，AI 问答会自动参考。</p>'; return; }
  box.innerHTML = '<table class="tbl"><tr><th>类型</th><th>内容</th><th>时间</th><th></th></tr>' +
    entries.map(e =>
      '<tr><td>' + esc(e.kind_zh) + '</td><td>' + esc(e.text) + '</td><td>' + esc(e.ts) + '</td>' +
      '<td><button class="btn-mini danger" data-id="' + e.id + '">删除</button></td></tr>').join("") +
    '</table>';
}
$("mm-list").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button.btn-mini");
  if (!b) return;
  const r = await post("memory", { action: "remove", id: b.dataset.id });
  if (r.ok) { renderMems(r.entries); flash("已删除该条记忆"); }
});

// ---------------- 实验统计（lab 页） ----------------
$("st-run").addEventListener("click", async () => {
  setStatus("st-status", "计算中…");
  const r = await post("stats", { text: $("st-text").value, ai: $("st-ai").checked });
  if (!r.ok) { setStatus("st-status", r.error || "计算失败", "err"); return; }
  $("st-out").classList.remove("hidden");
  $("st-md").innerHTML = renderMarkdown(r.markdown);
  setStatus("st-status", r.ai_interpret ? "完成（含 AI 解读）" : "完成（纯本地计算）", "ok");
});

// ---------------- 我的论文 + 合作对接 ----------------
const MP_ST_ZH = { planning: "规划中", writing: "撰写中", submitted: "已投稿",
                   review: "评审中", revision: "修回中", accepted: "已录用", rejected: "已拒" };
let _mpCache = [], _dirCache = [], _cbData = {};

$("mp-add").addEventListener("click", async () => {
  const title = $("mp-title").value.trim();
  if (!title) { setStatus("mp-status-box", "论文标题不能为空", "err"); return; }
  const r = await post("mypaper", { action: "add", title, ptype: $("mp-type").value,
    target_journal: $("mp-journal").value, deadline: $("mp-deadline").value,
    status: $("mp-status").value, notes: $("mp-notes").value });
  setStatus("mp-status-box", r.ok ? "已添加" : (r.error || "添加失败"), r.ok ? "ok" : "err");
  if (r.ok) { _mpCache = r.papers; renderMyPapers();
    $("mp-title").value = ""; $("mp-journal").value = ""; $("mp-notes").value = ""; }
});
$("mp-refresh").addEventListener("click", loadMyPapers);
async function loadMyPapers() {
  const r = await post("mypaper", { action: "list" });
  if (r.ok) {
    _mpCache = r.papers || [];
    renderMyPapers();
    const PP_ST = { draft: "草稿", writing: "撰写中", doing: "进行中", review: "评审中", done: "完成" };
    const sh = r.shared || [];
    $("mp-shared").innerHTML = !sh.length
      ? '<p class="chat-hint">暂无参与的合作论文。被创建者加为协作者后会自动出现在这里。</p>'
      : '<table class="tbl"><tr><th>标题</th><th>创建者</th><th>状态</th><th>我的权限</th><th></th></tr>' +
        sh.map(s => '<tr><td>' + esc(s.title) + '</td><td>@' + esc(s.creator) + '</td><td>' +
          esc(PP_ST[s.status] || s.status || "—") + '</td><td>' +
          (s.perm === "edit" ? "可编辑" : "仅查看") + '</td>' +
          '<td><button class="btn-mini" data-pid="' + esc(s.id) + '">去协作</button></td></tr>').join("") +
        '</table>';
  }
}
$("mp-shared").addEventListener("click", (ev) => {
  const b = ev.target.closest("button.btn-mini");
  if (!b) return;
  switchTab("team");
  setTimeout(() => { $("pp-sel").value = b.dataset.pid; loadPaper(); }, 300);
});
function renderMyPapers() {
  const box = $("mp-list");
  if (!_mpCache.length) { box.innerHTML = '<p class="chat-hint">还没有论文记录。填好上方表单点「添加论文」，或用「导入 Word 自动建档」。</p>'; return; }
  box.innerHTML = '<table class="tbl"><tr><th>标题</th><th>类型</th><th>目标期刊</th><th>状态</th><th>截止</th><th>附件</th><th></th></tr>' +
    _mpCache.map(p =>
      '<tr><td title="' + esc(p.notes || "") + '">' + esc(p.title) + '</td><td>' + esc(p.ptype || "—") + '</td>' +
      '<td>' + esc(p.target_journal || "—") + '</td>' +
      '<td><select class="btn-mini" data-ac="st" data-id="' + p.id + '">' +
      Object.keys(MP_ST_ZH).map(k => '<option value="' + k + '"' + (k === p.status ? " selected" : "") + '>' + MP_ST_ZH[k] + '</option>').join("") +
      '</select></td><td>' + esc(p.deadline || "—") + '</td>' +
      '<td>' + (p.file_name ? '<a class="btn-mini" href="/download/mp/' + p.id + '?token=' + esc(getToken()) + '" download>下载 Word</a>' : "—") + '</td>' +
      '<td><button class="btn-mini danger" data-ac="del" data-id="' + p.id + '">删除</button></td></tr>').join("") +
    '</table>';
}
$("mp-list").addEventListener("change", async (ev) => {
  const sel = ev.target.closest("select[data-ac='st']");
  if (!sel) return;
  const r = await post("mypaper", { action: "update", id: sel.dataset.id, status: sel.value });
  if (r.ok) { _mpCache = r.papers; setStatus("mp-status-box", "状态已更新", "ok"); }
  else setStatus("mp-status-box", r.error, "err");
});
$("mp-list").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button.btn-mini");
  if (!b || b.dataset.ac !== "del") return;
  const p = _mpCache.find(x => x.id === b.dataset.id);
  if (!p || !confirm("确认删除论文「" + p.title + "」？")) return;
  const r = await post("mypaper", { action: "remove", id: b.dataset.id });
  if (r.ok) { _mpCache = r.papers; renderMyPapers(); flash("已删除"); }
});

$("cb-refresh").addEventListener("click", () => { loadDir(); loadInvites(); });
async function loadDir() {
  const r = await post("auth", { action: "directory" });
  if (!r.ok) return;
  _dirCache = (r.users || []).filter(u => u.name !== (ME && ME.name));
  const box = $("cb-dir");
  const isAdmin = ME && ME.role === "admin";
  const ROLE_ZH = { admin: "管理员", teacher: "导师", member: "学生" };
  if (!_dirCache.length) { box.innerHTML = '<p class="chat-hint">' + (isAdmin ? "系统里还没有其他用户。" : "系统里还没有其他注册用户。把对方拉进来（注册账户）即可发邀请。") + '</p>'; return; }
  box.innerHTML = '<table class="tbl"><tr><th>用户名</th><th>显示名</th><th>角色</th><th>状态</th><th></th></tr>' +
    _dirCache.map(u => {
      const isSelf = ME && u.name === ME.name;
      const isTeacher = u.role === "teacher";
      const pending = isTeacher && u.active === false;
      let ops = '<button class="btn-mini" data-ac="invite" data-name="' + esc(u.name) + '">邀请合作</button>';
      if (isAdmin && !isSelf) {
        const nextRole = isTeacher ? "member" : "teacher";
        ops += ' <button class="btn-mini" data-cb="role" data-name="' + esc(u.name) + '" data-role="' + nextRole + '">' +
          (isTeacher ? "设为学生" : "设为导师") + '</button>';
        if (pending) ops += ' <button class="btn-mini" data-cb="approve" data-name="' + esc(u.name) + '">✅ 审核通过</button>';
      }
      const roleTxt = (ROLE_ZH[u.role] || u.role) + (pending ? ' <span class="tag">待审核</span>' : "");
      return '<tr><td>' + esc(u.name) + '</td><td>' + esc(u.display || u.name) + '</td><td>' + roleTxt + '</td>' +
        '<td>' + (pending ? "待审核" : (u.active === false ? "停用" : "启用")) + '</td><td>' + ops + '</td></tr>';
    }).join("") +
    '</table>' + (isAdmin ? '<p class="chat-hint">你是管理员：可直接为用户切换导师/学生角色、审核导师注册。</p>' : "");
}
$("cb-dir").addEventListener("click", async (ev) => {
  const rb = ev.target.closest("button[data-cb]");
  if (rb) {
    const name = rb.dataset.name;
    if (rb.dataset.cb === "role") {
      const to = rb.dataset.role;
      const r = await post("auth", { action: "update", name, role: to });
      setStatus("cb-status", r.ok ? ("已将 " + name + " 设为 " + (to === "teacher" ? "导师" : "学生")) : r.error, r.ok ? "ok" : "err");
      loadDir();
    } else if (rb.dataset.cb === "approve") {
      const r = await post("auth", { action: "update", name, active: true });
      setStatus("cb-status", r.ok ? (name + " 已审核通过") : r.error, r.ok ? "ok" : "err");
      loadDir();
    }
    return;
  }
  if (!b || b.dataset.ac !== "invite") return;
  const msg = prompt("给 " + b.dataset.name + " 的邀请留言（可留空，如想合作的论文/方向）：");
  if (msg === null) return;
  const r = await post("collab", { action: "invite", to: b.dataset.name, message: msg });
  setStatus("cb-status", r.ok ? "邀请已发出" : (r.error || "发送失败"), r.ok ? "ok" : "err");
  if (r.ok) loadInvites();
});
$("cb-invite-btn").addEventListener("click", async () => {
  const to = $("cb-user").value.trim();
  if (!to) { setStatus("cb-status", "请输入对方注册的用户名", "err"); return; }
  const msg = prompt("给 " + to + " 的邀请留言（可留空）：");
  if (msg === null) return;
  const r = await post("collab", { action: "invite", to, message: msg });
  setStatus("cb-status", r.ok ? "已向 " + to + " 发出邀请" : (r.error || "邀请失败"), r.ok ? "ok" : "err");
  if (r.ok) { $("cb-user").value = ""; loadInvites(); }
});
async function loadInvites() {
  const r = await post("collab", { action: "list" });
  if (r.ok) { _cbData = r; renderInvites(); }
}
function renderInvites() {
  const inbox = _cbData.inbox || [], sent = _cbData.sent || [], partners = _cbData.partners || [];
  const ST = { pending: "待处理", accepted: "已接受", declined: "已婉拒", cancelled: "已取消" };
  $("cb-inbox").innerHTML = !inbox.length ? '<p class="chat-hint">暂无收到的邀请。</p>' :
    '<table class="tbl"><tr><th>来自</th><th>关于</th><th>留言</th><th>时间</th><th></th></tr>' +
    inbox.map(v => '<tr><td>' + esc(v.from) + '</td><td>' + esc(v.paper || "—") + '</td><td>' + esc(v.message || "—") + '</td><td>' + esc(v.ts) + '</td>' +
      '<td>' + (v.status === "pending"
        ? '<button class="btn-mini" data-ac="accept" data-id="' + v.id + '">接受</button>' +
          '<button class="btn-mini danger" data-ac="decline" data-id="' + v.id + '">婉拒</button>'
        : '<span class="chat-hint">' + ST[v.status] + '</span>') + '</td></tr>').join("") + '</table>';
  $("cb-sent").innerHTML = !sent.length ? '<p class="chat-hint">暂未发出邀请。</p>' :
    '<table class="tbl"><tr><th>发给</th><th>关于</th><th>状态</th><th>时间</th><th></th></tr>' +
    sent.map(v => '<tr><td>' + esc(v.to) + '</td><td>' + esc(v.paper || "—") + '</td><td>' + ST[v.status] + '</td><td>' + esc(v.ts) + '</td>' +
      '<td>' + (v.status === "pending" ? '<button class="btn-mini danger" data-ac="cancel" data-id="' + v.id + '">取消</button>' : "") + '</td></tr>').join("") + '</table>';
  $("cb-partners").innerHTML = !partners.length ? '<p class="chat-hint">还没有合作者。邀请被接受后会显示在这里。</p>' :
    '<table class="tbl"><tr><th>合作者</th><th>显示名</th><th>成为合作者时间</th></tr>' +
    partners.map(p => '<tr><td>' + esc(p.name) + '</td><td>' + esc(p.display) + '</td><td>' + esc(p.since) + '</td></tr>').join("") +
    '</table><p class="chat-hint">去「协作中心」即可共用任务板、论文协作与实验复现。</p>';
}
$("cb-inbox").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button.btn-mini");
  if (!b) return;
  const r = await post("collab", { action: b.dataset.ac, id: b.dataset.id });
  if (r.ok) { _cbData = r; renderInvites(); flash(b.dataset.ac === "accept" ? "已接受，你们互为合作者" : "已婉拒"); }
  else setStatus("cb-status", r.error, "err");
});
$("cb-sent").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button.btn-mini");
  if (!b || b.dataset.ac !== "cancel") return;
  const r = await post("collab", { action: "cancel", id: b.dataset.id });
  if (r.ok) { _cbData = r; renderInvites(); flash("已取消邀请"); }
  else setStatus("cb-status", r.error, "err");
});

// ---------------- 论文项目删除（创建者/管理员） ----------------
$("pp-del").addEventListener("click", async () => {
  const pid = $("pp-sel").value;
  if (!pid) { setStatus("pp-status", "请先在「切换论文」中选择要删除的项目", "err"); return; }
  const sel = $("pp-sel");
  const name = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : pid;
  if (!confirm("确认删除论文项目「" + name + "」？\n\n该项目下的章节草稿与审阅意见将一并删除，此操作不可恢复！")) return;
  const r = await post("paper", { action: "remove", id: pid });
  setStatus("pp-status", r.ok ? "项目已删除" : (r.error || "删除失败"), r.ok ? "ok" : "err");
  if (r.ok) refreshPapers();
});

// ---------------- 论文引导（小白十步流程） ----------------
const GD_STEPS = [
  { id: "s1", t: "① 定方向", tab: "profile",
    why: "方向不定，后面全是空转。先想清楚：课题要解决谁的什么问题？时间/数据/设备约束是什么？",
    how: "去「科研画像」填写研究方向、创新点、约束，再点「AI 诊断画像」，重点看 AI 指出的聚焦点与论证缺口。",
    out: "一段话研究方向 + 3 条约束" },
  { id: "s2", t: "② 文献调研", tab: "search",
    why: "不查文献就动手 = 闭门造车。要知道这个方向谁做过、做到哪、用什么方法。",
    how: "用「文献检索」跑 2–3 组检索式；结果顺手生成「对比矩阵」，看方法×年份×被引的分布。",
    out: "15–30 篇文献清单 / 一张对比矩阵" },
  { id: "s3", t: "③ 找创新点 · 确认可行性", tab: "digest",
    why: "创新点不是拍脑袋，是从『别人没做』里抠出来的；但光有新意不够——还要你真的做得出来。创新与可行必须同轮确认。",
    how: "挑 3–5 篇高被引论文放进「论文精读」分别做【空白研究分析】和【可行性分析】（数据/算力/时间够不够）；再把两份结论合到一起筛选：既有空白、你又够得着的才是真创新点，写回「科研画像」的创新点与约束栏。",
    out: "1–3 条有文献依据的创新点 + 每条的可行性结论（资源/风险）" },
  { id: "s4", t: "④ 定选题与假设", tab: "profile",
    why: "把创新点收拢成一个可验证的选题：一句话能说清『用什么方法解决什么问题、比谁好在哪』。",
    how: "回「科研画像」看 AI 诊断是否聚焦；用「智能对话」向 AI 描述选题，让它扮演审稿人挑毛病。",
    out: "选题一句话 + 预期贡献" },
  { id: "s5", t: "⑤ 设计实验", tab: "lab",
    why: "实验要先设计后动手：数据集、对比方法、指标、消融，缺一项论文就少一块证据。",
    how: "在「实验复现」登记实验（假设/数据集/超参/指标），每个对比对象一条记录。",
    out: "实验台账（≥1 组主实验 + 消融）" },
  { id: "s6", t: "⑥ 跑实验与统计", tab: "lab",
    why: "结果要能经得起问：多跑几个随机种子，报均值和显著性，而不是挑最好的一组。",
    how: "结果登记进台账；把两组结果粘到「实验复现 → 统计分析」算均值差与 Welch t 检验；论文里的曲线/柱状图用「出图」读 CSV 直接生成 SVG。",
    out: "结果表 + 显著性结论 + 论文级图表" },
  { id: "s7", t: "⑦ 撰写初稿", tab: "write",
    why: "先搭骨架再填肉：大纲定逻辑，分节初稿定内容，不要从摘要开始写。人多力量大，但要有分工和权限。",
    how: "「写作综述」先生成大纲，再按节生成初稿；真实数据自己填，AI 留的【待补】逐个补齐。需要帮手？去「合作对接」邀请对方，再到「论文协作」把他加为该论文的协作者（可编辑），对方「我的论文」会自动收录，即可分工写章节。",
    out: "论文初稿（各章节，多人分工完成）" },
  { id: "s8", t: "⑧ 润色与自查", tab: "write",
    why: "初稿一定有口语化和逻辑断点；审稿人对表达的第一印象很影响打分。",
    how: "逐段放进「写作综述 → 学术润色」；关键段落用「论文精读」反向检查创新点是否站得住。",
    out: "润色后的定稿" },
  { id: "s9", t: "⑨ 选刊与投稿", tab: "journal",
    why: "投错刊 = 白白多等几个月。 scope 契合比影响因子更重要。",
    how: "摘要贴进「选刊」按契合度排序；选定后用「写作综述 → Cover Letter」生成投稿信；更新「我的论文」状态为已投稿。",
    out: "目标期刊 + 投稿信 + 投出" },
  { id: "s10", t: "⑩ 审稿与修回", tab: "write",
    why: "修回是第二次机会：逐条回应、态度谦和、改动可核查，录用率大增。",
    how: "审稿意见逐条贴进「写作综述 → 审稿回复」生成回复框架；完成后在「我的论文」更新状态。",
    out: "修回稿 + 逐条回复" },
];
let _gdSel = null, _gdDone = {};
async function bootGuide() {
  const r = await post("guide", { action: "load" });
  if (r.ok) _gdDone = r.steps || {};
  renderGuide();
  if (!_gdSel) selectStep(GD_STEPS.find(s => !_gdDone[s.id]) || GD_STEPS[0]);
  else selectStep(GD_STEPS.find(s => s.id === _gdSel) || GD_STEPS[0]);
}
function renderGuide() {
  const box = $("gd-flow");
  box.innerHTML = GD_STEPS.map((s, i) =>
    '<span class="g-node' + (_gdDone[s.id] ? " done" : "") + (_gdSel === s.id ? " active" : "") +
    '" data-id="' + s.id + '"><span class="tick">' + (_gdDone[s.id] ? "✓" : "") + '</span>' + s.t + '</span>' +
    (i < GD_STEPS.length - 1 ? '<span class="g-arrow">→</span>' : "")).join("");
  const n = Object.keys(_gdDone).length;
  $("gd-progress").textContent = "进度：" + n + " / " + GD_STEPS.length + (n === GD_STEPS.length ? " 🎉 全部完成，论文已经在路上了！" : "");
  box.querySelectorAll(".g-node").forEach(nd => nd.addEventListener("click", () => selectStep(GD_STEPS.find(s => s.id === nd.dataset.id))));
}
function selectStep(step) {
  if (!step) return;
  _gdSel = step.id;
  $("gd-detail-card").classList.remove("hidden");
  $("gd-detail-title").textContent = step.t + "：" + (step.out ? "交付物——" + step.out : "");
  $("gd-detail").innerHTML = renderMarkdown(
    "**为什么要有这一步**\n\n" + step.why + "\n\n**怎么做（跟着走就行）**\n\n" + step.how +
    "\n\n**这一步的交付物**\n\n- " + step.out + (_gdDone[step.id] ? "\n\n> ✅ 你已完成这一步（" + _gdDone[step.id] + "）。" : ""));
  $("gd-done").textContent = _gdDone[step.id] ? "取消完成标记" : "标记这一步完成";
  renderGuide();
}
$("gd-goto").addEventListener("click", () => {
  const step = GD_STEPS.find(s => s.id === _gdSel);
  if (step && step.tab) switchTab(step.tab);
});
$("gd-done").addEventListener("click", async () => {
  const step = GD_STEPS.find(s => s.id === _gdSel);
  if (!step) return;
  if (_gdDone[step.id]) delete _gdDone[step.id];
  else _gdDone[step.id] = new Date().toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-");
  const r = await post("guide", { action: "save", steps: _gdDone });
  if (!r.ok) { setStatus("gd-status", "进度保存失败：" + (r.error || ""), "err"); return; }
  const next = GD_STEPS.find(s => !_gdDone[s.id]);
  selectStep(next || step);
  setStatus("gd-status", _gdDone[step.id] ? "已标记完成，继续下一步！" : "已取消", "ok");
});
$("gd-reset").addEventListener("click", async () => {
  if (!confirm("确认清空全部引导进度？")) return;
  _gdDone = {};
  await post("guide", { action: "save", steps: {} });
  selectStep(GD_STEPS[0]);
});

// ---------------- 论文精读 ----------------
document.querySelectorAll("button[data-dg]").forEach(b => b.addEventListener("click", async () => {
  const text = $("dg-text").value.trim();
  if (text.length < 100) { setStatus("dg-status", "请先粘贴论文全文/章节/摘要（至少 100 字）", "err"); return; }
  setStatus("dg-status", "AI 精读中（最长 90 秒）…");
  const r = await post("digest", { kind: b.dataset.dg, text });
  if (!r.ok) { setStatus("dg-status", r.error || "精读失败", "err"); return; }
  $("dg-out").classList.remove("hidden");
  $("dg-md").innerHTML = renderMarkdown(r.reply);
  setStatus("dg-status", "完成：" + r.kind_zh, "ok");
}));
$("dg-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("dg-md").innerText); flash("已复制到剪贴板"); }
  catch (e) { flash("复制失败，请手动选择文本复制"); }
});

// ---------------- 算法生成 / 校验 / 自动训练设计（实验页） ----------------
let _agLast = "", _tpLast = "";
$("ag-gen").addEventListener("click", async () => {
  setStatus("ag-status", "AI 生成中（最长 90 秒）…");
  const r = await post("algo", { action: "gen", kind: $("ag-kind").value, desc: $("ag-desc").value });
  if (!r.ok) { setStatus("ag-status", r.error || "生成失败", "err"); return; }
  _agLast = r.reply || "";
  $("ag-out").classList.remove("hidden");
  $("ag-md").innerHTML = renderMarkdown(_agLast);
  setStatus("ag-status", "已生成：" + r.kind_zh, "ok");
});
$("ag-save").addEventListener("click", async () => {
  if (!_agLast) { setStatus("ag-status", "先点「生成」再保存", "err"); return; }
  const r = await post("write", { action: "save", kind: "algo", title: "算法·" + ($("ag-desc").value.trim().slice(0, 24) || "未命名"), content: _agLast });
  setStatus("ag-status", r.ok ? "已存入草稿库" : (r.error || "保存失败"), r.ok ? "ok" : "err");
});
$("alc-run").addEventListener("click", async () => {
  setStatus("alc-status", "校验中…");
  const r = await post("algo", { action: "check", code: $("alc-code").value, ai: $("alc-ai").checked });
  if (!r.ok) { setStatus("alc-status", r.error || "校验失败", "err"); return; }
  $("alc-rout").classList.remove("hidden");
  $("alc-rmd").innerHTML = renderMarkdown(r.markdown);
  setStatus("alc-status", r.level === "fail" ? "发现语法错误" : "静态检查完成", r.level === "fail" ? "err" : "ok");
});
$("tp-run").addEventListener("click", async () => {
  setStatus("tp-status", "AI 设计训练方案中（最长 90 秒）…");
  const r = await post("algo", { action: "gen", kind: "train_plan", desc: $("tp-text").value });
  if (!r.ok) { setStatus("tp-status", r.error || "生成失败", "err"); return; }
  _tpLast = r.reply || "";
  $("tp-out").classList.remove("hidden");
  $("tp-md").innerHTML = renderMarkdown(_tpLast);
  setStatus("tp-status", "训练方案已生成", "ok");
});
$("tp-save").addEventListener("click", async () => {
  if (!_tpLast) { setStatus("tp-status", "先点「生成训练方案」再保存", "err"); return; }
  const r = await post("write", { action: "save", kind: "trainplan", title: "训练方案·" + ($("tp-text").value.trim().slice(0, 24) || "未命名"), content: _tpLast });
  setStatus("tp-status", r.ok ? "已存入草稿库" : (r.error || "保存失败"), r.ok ? "ok" : "err");
});

// ---------------- 开题报告 → 论文初稿流水线 ----------------
let _apLast = "";
$("ap-run").addEventListener("click", async () => {
  const btn = $("ap-run");
  const proposal = $("ap-text").value.trim();
  if (proposal.length < 200) { setStatus("ap-status", "请先粘贴开题报告全文（至少 200 字符）", "err"); return; }
  btn.disabled = true; $("ap-save").disabled = true;
  const lang = $("ap-lang").value, journal = $("ap-journal").value.trim();
  try {
    setStatus("ap-status", "第 1/4 步：设计论文大纲…");
    const o = await post("autopaper", { action: "outline", proposal, lang, journal });
    if (!o.ok) throw new Error(o.error || "大纲生成失败");
    const title = $("ap-title").value.trim() || o.title || "Paper Draft";
    let md = "# " + title + "\n\n## Abstract\n【生成中…】\n\n";
    const secs = o.sections || [];
    for (let i = 0; i < secs.length; i++) {
      setStatus("ap-status", "第 2/4 步：撰写第 " + (i + 1) + "/" + secs.length + " 节（" + secs[i].title + "）…");
      const s = await post("autopaper", { action: "section", proposal, lang, journal,
        outline: JSON.stringify(o.sections), sec_title: secs[i].title, sec_req: secs[i].req || "" });
      if (!s.ok) throw new Error("「" + secs[i].title + "」生成失败：" + (s.error || ""));
      md += "\n\n## " + secs[i].title + "\n\n" + s.text;
    }
    setStatus("ap-status", "第 3/4 步：生成摘要与关键词…");
    const ab = await post("autopaper", { action: "abstract", title, body: md, lang });
    md = md.replace("【生成中…】", ab.ok ? ab.text : "（摘要生成失败，请稍后用「润色」补写）");
    setStatus("ap-status", "第 4/4 步：完成（" + secs.length + " 节）");
    _apLast = md;
    $("ap-out").classList.remove("hidden");
    $("ap-md").innerHTML = renderMarkdown(md);
    $("ap-save").disabled = false;
  } catch (e) {
    setStatus("ap-status", "流水线中断：" + e.message, "err");
  } finally {
    btn.disabled = false;
  }
});
$("ap-save").addEventListener("click", async () => {
  if (!_apLast) return;
  const t = ($("ap-title").value.trim() || "论文初稿") + " · " + new Date().toLocaleDateString("zh-CN");
  const r = await post("write", { action: "save", kind: "paper", title: t, content: _apLast });
  setStatus("ap-status", r.ok ? "论文初稿已存入草稿库" : (r.error || "保存失败"), r.ok ? "ok" : "err");
});

// ---------------- 全流程联动：技能 / 方向链 / 文献库 / 评审台 / 数据集 / Word 导入 ----------------
function syncAccountsVisibility() {
  const isAdmin = !!(ME && ME.role === "admin");
  const grp = $("nav-admin");
  if (grp) grp.style.display = isAdmin ? "" : "none";
  document.querySelectorAll('[data-tab="accounts"]').forEach(b => {
    b.style.display = isAdmin ? "" : "none";
  });
}
async function refreshChatSelectors() {
  try {
    const m = await post("model_list", {});
    if (m.ok) {
      const sel = $("chat-model"), cur = sel.value;
      sel.innerHTML = '<option value="">全局配置（默认）</option>' +
        m.models.map(x => '<option value="' + x.id + '">' + esc(x.label) +
          (x.need_key && !x.has_key ? "（未配 Key）" : "") + (x.is_current ? " ★" : "") + '</option>').join("");
      if (cur) sel.value = cur;
    }
  } catch (e) {}
}


// ---- 方向链：定方向 → 文献调研 ----
async function loadDirection() {
  const r = await post("workflow", { action: "get" });
  if (r.ok && r.direction_final) $("wf-direction").value = r.direction_final;
}
$("wf-save").addEventListener("click", async () => {
  const r = await post("workflow", { action: "set_direction", direction: $("wf-direction").value });
  setStatus("wf-status", r.ok ? "方向已定稿——文献调研/雷达将以此为基准" : (r.error || "保存失败"), r.ok ? "ok" : "err");
});
$("wf-goto-search").addEventListener("click", async () => {
  let d = $("wf-direction").value.trim();
  if (!d) { const r = await post("workflow", { action: "get" }); d = r.direction_final || ""; }
  if (!d) { flash("请先填写并保存最终研究方向"); return; }
  $("s-query").value = d;
  switchTab("search");
  ACT.search();
});
$("wf-socratic").addEventListener("click", async () => {
  setStatus("wf-status", "苏格拉底追问中…");
  const input = "我的方向：" + $("wf-direction").value + "\n画像补充：" +
    PE_KEYS.map(k => ( $("pe-" + k) ? $("pe-" + k).value : "")).filter(Boolean).join("；");
  const r = await post("skills", { action: "run", skill: "socratic_idea", input });
  if (r.ok) { $("wf-out").classList.remove("hidden"); $("wf-md").innerHTML = renderMarkdown(r.reply); setStatus("wf-status", "追问已生成——回答这些问题后再回来改方向", "ok"); }
  else setStatus("wf-status", r.error || "失败", "err");
});

// ---- 检索 → 收录到文献库 ----
async function renderCollect() {
  const recs = window._lastRecords || [];
  if (!recs.length) { $("s-collect").classList.add("hidden"); return; }
  $("s-collect").classList.remove("hidden");
  const r = await post("reffolder", { action: "list" });
  const fs = r.ok ? (r.folders || []) : [];
  $("rf-sel-quick").innerHTML = '<option value="">— 选择文件夹 —</option>' +
    fs.map(f => '<option value="' + f.id + '">' + esc(f.name) + "（" + (f.papers || []).length + " 篇）</option>").join("");
  $("rf-collect-list").innerHTML = recs.slice(0, 30).map((p, i) =>
    '<label style="display:block;font-size:12.5px;margin:3px 0"><input type="checkbox" class="rc-ck" data-i="' + i + '" checked> ' +
    esc((p.title || "").slice(0, 90)) + ' <span class="chat-hint">[' + esc(p.year || "—") + "·" + esc(p.source || "") + ']</span></label>').join("");
}
$("rf-collect").addEventListener("click", async () => {
  const recs = window._lastRecords || [];
  const picked = [];
  document.querySelectorAll(".rc-ck:checked").forEach(ck => {
    const p = recs[+ck.dataset.i];
    if (p) picked.push({ title: p.title, year: p.year, venue: p.venue || p.source,
      abstract: p.abstract || p.summary || "", link: p.link || p.url || "", source: p.source || "" });
  });
  if (!picked.length) { setStatus("rf-collect-status", "请先勾选文献", "err"); return; }
  let rid = $("rf-sel-quick").value;
  const newName = $("rf-new-quick").value.trim();
  if (!rid && newName) { const c = await post("reffolder", { action: "create", name: newName }); if (c.ok) { rid = (c.folders[0] || {}).id; loadFolders(); } }
  if (!rid) { setStatus("rf-collect-status", "请选择或新建一个文件夹", "err"); return; }
  const r = await post("reffolder", { action: "add_papers", id: rid, papers: picked });
  setStatus("rf-collect-status", r.ok ? "已收录 " + r.added + " 篇 →「文献库」" : (r.error || "收录失败"), r.ok ? "ok" : "err");
});
$("s-use-direction").addEventListener("click", async () => {
  const r = await post("workflow", { action: "get" });
  if (!r.direction_final) { setStatus("s-status", "还没有定稿方向——先去「科研画像」确认", "warn"); return; }
  $("s-query").value = r.direction_final; ACT.search();
});
$("s-strategy").addEventListener("click", async () => {
  setStatus("s-status", "生成调研策略…");
  const r = await post("skills", { action: "run", skill: "lit_strategy", input: $("s-query").value });
  if (r.ok) { $("s-strat-out").classList.remove("hidden"); $("s-strat-md").innerHTML = renderMarkdown(r.reply); setStatus("s-status", "策略已生成", "ok"); }
  else setStatus("s-status", r.error || "失败", "err");
});

// ---- 文献库 ----
let _rfCache = [], _rfCur = null, _rfAnaOpen = null;
async function loadFolders() {
  const r = await post("reffolder", { action: "list" });
  if (!r.ok) return;
  _rfCache = r.folders || [];
  $("rf-sel").innerHTML = !_rfCache.length ? '<option value="">（还没有文件夹）</option>' :
    _rfCache.map(f => '<option value="' + f.id + '">' + esc(f.name) + "（" + (f.papers || []).length + " 篇）</option>").join("");
  if (_rfCur && _rfCache.some(f => f.id === _rfCur)) $("rf-sel").value = _rfCur;
  renderFolder();
}
function curFolder() { return _rfCache.find(f => f.id === $("rf-sel").value) || null; }
function renderFolder() {
  _rfCur = $("rf-sel").value || null;
  const f = curFolder();
  const box = $("rf-papers");
  if (!f) { box.innerHTML = '<p class="chat-hint">还没有文件夹。先创建，然后去「文献检索」勾选收录。</p>';
    $("rf-ana-card").classList.add("hidden"); return; }
  const kw = ($("rf-filter").value || "").toLowerCase();
  const ps = (f.papers || []).map((p, i) => Object.assign({ _i: i }, p))
    .filter(p => !kw || (p.title + " " + p.year + " " + p.venue).toLowerCase().includes(kw));
  box.innerHTML = !ps.length ? '<p class="chat-hint">该文件夹暂无（匹配的）文献。</p>' :
    '<table class="tbl"><tr><th>#</th><th>标题（点击查看）</th><th>年份</th><th>来源</th><th>精读</th><th></th></tr>' +
    ps.map(p => '<tr><td>' + (p._i + 1) + '</td>' +
      '<td><a href="#" class="rf-view" data-i="' + p._i + '" style="color:var(--accent)">' + esc(p.title.slice(0, 70)) + '</a></td>' +
      '<td>' + esc(p.year || "—") + '</td><td>' + esc(p.venue || p.source || "—") + '</td>' +
      '<td>' + ((f.analyses || {})[p.title] ? "✓" : "—") + '</td>' +
      '<td><button class="btn-mini danger" data-del="' + p._i + '">移除</button></td></tr>').join("") +
    '</table>';
}
$("rf-sel").addEventListener("change", renderFolder);
$("rf-filter").addEventListener("input", renderFolder);
$("rf-papers").addEventListener("click", (ev) => {
  const del = ev.target.closest("button[data-del]");
  if (del) {
    const f = curFolder();
    if (f && confirm("从文件夹移除这篇文献？")) {
      post("reffolder", { action: "remove_paper", id: f.id, idx: +del.dataset.del }).then(() => loadFolders());
    }
    return;
  }
  const a = ev.target.closest("a.rf-view");
  if (a) {
    const f = curFolder(); const p = f.papers[+a.dataset.i];
    _rfAnaOpen = (_rfAnaOpen === p.title) ? null : p.title;
    renderAnaTable();
  }
});
$("rf-create").addEventListener("click", async () => {
  const r = await post("reffolder", { action: "create", name: $("rf-name").value });
  setStatus("rf-status", r.ok ? "已创建" : (r.error || "失败"), r.ok ? "ok" : "err");
  if (r.ok) { $("rf-name").value = ""; loadFolders(); }
});
$("rf-refresh").addEventListener("click", loadFolders);
$("rf-del").addEventListener("click", async () => {
  const f = curFolder();
  if (!f) return;
  if (!confirm("确认删除文件夹「" + f.name + "」及其全部收录？")) return;
  await post("reffolder", { action: "remove", id: f.id });
  _rfCur = null; loadFolders();
});
function renderAnaTable() {
  const f = curFolder();
  if (!f) return;
  const an = f.analyses || {};
  const titles = Object.keys(an);
  $("rf-ana-card").classList.remove("hidden");
  $("rf-ana").innerHTML = !titles.length
    ? '<p class="chat-hint">还没有精读分析。点上方「批量生成精读分析」。</p>'
    : '<table class="tbl"><tr><th>文献</th><th>分析</th><th></th></tr>' +
      titles.map(t => '<tr><td>' + esc(t.slice(0, 60)) + '</td><td>' + esc((an[t] || "").replace(/[#*`]/g, "").slice(0, 60)) + '…</td>' +
        '<td><button class="btn-mini" data-t="' + esc(t) + '">' + (_rfAnaOpen === t ? "收起" : "查看全文") + '</button></td></tr>').join("") +
      '</table><div class="md" id="rf-ana-detail">' +
      (_rfAnaOpen && an[_rfAnaOpen] ? renderMarkdown(an[_rfAnaOpen]) : "") + '</div>';
  $("rf-ana").querySelectorAll("button[data-t]").forEach(b => b.addEventListener("click", () => {
    _rfAnaOpen = (_rfAnaOpen === b.dataset.t) ? null : b.dataset.t; renderAnaTable();
  }));
}
$("rf-digest").addEventListener("click", async () => {
  const f = curFolder();
  if (!f) { setStatus("rf-status", "先选择文件夹", "err"); return; }
  const dims = Array.from($("rf-kind").querySelectorAll("input:checked"))
    .map(o => ({ id: o.value, name: o.parentElement.textContent.trim() }));
  if (!dims.length) { setStatus("rf-status", "请至少选择一个精读维度", "err"); return; }
  const ps = f.papers || [];
  if (!ps.length) { setStatus("rf-status", "文件夹为空", "err"); return; }
  const DIM_ZH = { tech: "技术点分析", innovation: "创新点总结", gap: "空白研究分析" };
  const total = ps.length * dims.length;
  let done = 0;
  for (let i = 0; i < ps.length; i++) {
    const p = ps[i];
    const text = (p.title + ". " + (p.venue || "") + " " + (p.year || "") + ". " + (p.abstract || "")).trim();
    if (text.length < 100) {
      await post("reffolder", { action: "set_analysis", id: f.id, title: p.title,
        analysis: "（元数据不足：缺少摘要，无法可靠精读。请先人工补全该文献的摘要后再生成。）" });
      done += dims.length;
      continue;
    }
    let combined = "";
    for (const d of dims) {
      setStatus("rf-status", "精读第 " + (i + 1) + "/" + ps.length + " 篇 · " + DIM_ZH[d.id] + "（总进度 " + (++done) + "/" + total + "）…");
      const r = await post("digest", { kind: d.id, text });
      combined += "\n\n## " + DIM_ZH[d.id] + "\n\n" + (r.ok ? r.reply : ("生成失败：" + (r.error || "")));
    }
    await post("reffolder", { action: "set_analysis", id: f.id, title: p.title, analysis: combined.trim() });
  }
  setStatus("rf-status", "批量精读完成：" + ps.length + " 篇 × " + dims.length + " 个维度", "ok");
  loadFolders().then(renderAnaTable);
});
let _rfSynLast = "";
$("rf-syn").addEventListener("click", async () => {
  const f = curFolder();
  if (!f) return;
  setStatus("rf-syn-status", "AI 综合分析中…");
  const r = await post("reffolder", { action: "synthesize", id: f.id });
  if (!r.ok) { setStatus("rf-syn-status", r.error || "失败", "err"); return; }
  _rfSynLast = r.reply;
  $("rf-syn-out").classList.remove("hidden");
  $("rf-syn-md").innerHTML = renderMarkdown(r.reply);
  setStatus("rf-syn-status", "综合完成——可送入盲审", "ok");
});
$("rf-review").addEventListener("click", async () => {
  if (!_rfSynLast) { setStatus("rf-syn-status", "请先「综合分析」再送审", "err"); return; }
  const r = await post("reviewflow", { action: "create", kind: "ai_blind",
    title: (curFolder() || {}).name || "文献综合结论", content: _rfSynLast });
  if (r.ok) { switchTab("reviewhub"); flash("已创建盲审任务，点「运行 AI 盲审」"); }
  else setStatus("rf-syn-status", r.error, "err");
});

// ---- 评审工作台 ----
let _rvCache = [], _rvMineId = null, _partnersCache = [];
async function fillPartnerSelects() {
  const r = await post("collab", { action: "list" });
  _partnersCache = (r.ok && r.partners) || [];
  const partnerOpts = _partnersCache.map(p => '<option value="' + esc(p.name) + '">' +
    esc(p.display || p.name) + '</option>').join("");
  const setSel = (id, withSelf, hint) => {
    const el = $(id);
    if (!el) return;
    const keep = el.value;
    if (!_partnersCache.length && !withSelf) { el.innerHTML = '<option value="">' + hint + '</option>'; return; }
    const selfOpt = (withSelf && ME)
      ? '<option value="' + esc(ME.name) + '">' + esc(ME.display || ME.name) + '（我）</option>' : "";
    el.innerHTML = selfOpt + partnerOpts ||
      '<option value="">' + hint + '</option>';
    if (keep) el.value = keep;
  };
  setSel("tk-assignee", true, "（还没有合作者——先去「合作对接」）");
  setSel("ex-owner", true, "（还没有合作者——先去「合作对接」）");
  setSel("pp-owner", true, "（还没有合作者——先去「合作对接」）");
  setSel("vh-reviewers", false, "（还没有合作者——先去「合作对接」）");
  setSel("pp-collab-user", false, "（暂无合作者——先去「合作对接」建立合作关系）");
}
async function loadPartnersForReview() { await fillPartnerSelects(); }
$("vh-kind").addEventListener("change", () => {
  $("vh-peer-row").style.display = $("vh-kind").value === "peer" ? "" : "none";
});
$("vh-create").addEventListener("click", async () => {
  const kind = $("vh-kind").value;
  const reviewers = kind === "peer"
    ? Array.from($("vh-reviewers").selectedOptions).map(o => o.value) : [];
  const r = await post("reviewflow", { action: "create", kind, title: $("vh-title").value,
    content: $("vh-content").value, reviewers });
  if (!r.ok) { setStatus("vh-status", r.error || "失败", "err"); return; }
  setStatus("vh-status", "已发起", "ok");
  if (kind === "ai_blind") {
    const rid = r.reviews[0].id;
    setStatus("vh-status", "三位匿名专家评审中（约 1-2 分钟）…");
    const r2 = await post("reviewflow", { action: "run_blind", id: rid });
    setStatus("vh-status", r2.ok ? "AI 盲审完成" : (r2.error || "盲审失败"), r2.ok ? "ok" : "err");
  }
  $("vh-content").value = ""; $("vh-title").value = "";
  loadReviews();
});
function renderReviewResults(r) {
  const one = (x) =>
    '<div class="md" style="border:1px solid var(--border);border-radius:8px;padding:10px;margin:8px 0">' +
    '<b>' + esc(x.reviewer) + '</b>' + (x.blind ? ' <span class="tag">盲审</span>' : '') +
    (x.verdict ? ' · <b>' + esc(x.verdict) + '</b>' : '') +
    (x.scores && Object.keys(x.scores).length ? '<br>' + Object.entries(x.scores).map(([k, v]) => esc(k) + " " + esc(v)).join(" / ") : "") +
    '<div style="margin-top:6px">' + renderMarkdown(x.comments || "") + '</div></div>';
  if (r.rounds && r.rounds.length) {
    return r.rounds.map(rd =>
      '<div style="margin:10px 0 4px"><b>第 ' + rd.round + ' 轮</b>' +
      (rd.note ? ' <span class="tag">修改说明：' + esc(rd.note.slice(0, 60)) + (rd.note.length > 60 ? "…" : "") + '</span>' : '') +
      ' <span class="chat-hint">' + esc(rd.ts) + '</span></div>' +
      (rd.results || []).map(one).join("")).join("");
  }
  return (r.results || []).map(one).join("") || '<p class="chat-hint">尚无评审结果</p>';
}
async function loadReviews() {
  const r = await post("reviewflow", { action: "list" });
  if (!r.ok) return;
  _rvCache = r.reviews || [];
  $("vh-list").innerHTML = !_rvCache.length ? '<p class="chat-hint">暂无评审。</p>' :
    _rvCache.map(v => '<div style="border:1px solid var(--border);border-radius:10px;padding:12px;margin:10px 0">' +
      '<b>' + esc(v.title) + '</b> <span class="tag">' + (v.kind === "ai_blind" ? "AI 盲审" : "协作者审核") + '</span> ' +
      '<span class="tag">' + esc(v.status) + '</span> <span class="chat-hint">' + esc(v.ts) + '</span>' +
      '<div style="margin:6px 0">' + esc(v.content.slice(0, 120)) + '…</div>' +
      (v.kind === "ai_blind" && v.status === "pending"
        ? '<button class="btn-mini" data-blind="' + v.id + '">运行 AI 盲审</button> '
        : "") +
      (v.kind === "ai_blind" && v.status === "done"
        ? '<button class="btn-mini" data-blind2="' + v.id + '">下一轮盲审（附修改说明）</button> ' +
          (v.rounds && v.rounds.length > 1 ? '<span class="tag">已迭代 ' + v.rounds.length + ' 轮</span>' : "")
        : "") +
      '<button class="btn-mini" data-show="' + v.id + '">查看评审意见</button>' +
      '<div id="vhres-' + v.id + '" class="hidden">' + renderReviewResults(v) + '</div></div>').join("");
  $("vh-list").querySelectorAll("button[data-blind]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "盲审中…";
    const r2 = await post("reviewflow", { action: "run_blind", id: b.dataset.blind });
    if (r2.ok) { flash("盲审完成"); loadReviews(); } else flash(r2.error || "失败");
  }));
  $("vh-list").querySelectorAll("button[data-blind2]").forEach(b => b.addEventListener("click", async () => {
    const note = prompt("填写本轮修改说明（匿名送审给三位专家；可留空直接重审）：");
    if (note === null) return;
    b.disabled = true; b.textContent = "盲审中…";
    const r2 = await post("reviewflow", { action: "run_blind", id: b.dataset.blind2, note: note || "" });
    if (r2.ok) { flash("第 " + (r2.round || "?") + " 轮盲审完成"); loadReviews(); } else flash(r2.error || "失败");
  }));
  $("vh-list").querySelectorAll("button[data-show]").forEach(b => b.addEventListener("click", () => {
    const d = $("vhres-" + b.dataset.show);
    d.classList.toggle("hidden");
  }));
}
async function loadMine() {
  const r = await post("reviewflow", { action: "mine" });
  const mine = r.ok ? (r.mine || []) : [];
  $("vh-mine").innerHTML = !mine.length ? '<p class="chat-hint">暂无待你审核的任务。</p>' :
    mine.map(m => '<div style="border:1px solid var(--border);border-radius:10px;padding:10px;margin:8px 0">' +
      '<b>' + esc(m.title) + '</b> <span class="chat-hint">来自 @' + esc(m.owner) + ' · ' + esc(m.ts) + '</span>' +
      '<div style="margin:6px 0">' + esc(m.content.slice(0, 160)) + '…</div>' +
      '<button class="btn-mini" data-rid="' + m.id + '">开始评审</button></div>').join("");
  $("vh-mine").querySelectorAll("button[data-rid]").forEach(b => b.addEventListener("click", () => {
    _rvMineId = b.dataset.rid;
    $("vh-submit-card").classList.remove("hidden");
    $("vh-submit-card").scrollIntoView({ behavior: "smooth" });
  }));
}
$("vh-mine-refresh").addEventListener("click", loadMine);
$("vh-refresh").addEventListener("click", loadReviews);
$("vh-submit").addEventListener("click", async () => {
  if (!_rvMineId) { setStatus("vh-submit-status", "先选择任务", "err"); return; }
  const r = await post("reviewflow", { action: "submit", id: _rvMineId,
    scores: { 创新性: $("sc-a").value, 严谨性: $("sc-b").value, 可行性: $("sc-c").value, 清晰度: $("sc-d").value },
    comments: $("vh-comments").value, verdict: $("vh-verdict").value });
  setStatus("vh-submit-status", r.ok ? "已提交，谢谢评审" : (r.error || "提交失败"), r.ok ? "ok" : "err");
  if (r.ok) { $("vh-submit-card").classList.add("hidden"); _rvMineId = null; loadMine(); }
});

// ---- 训练集检索与下载 ----
$("ds-extract").addEventListener("click", () => {
  const r = { names: [] };
  const text = $("ds-text").value;
  if (text.length < 50) { setStatus("ds-status", "先粘贴论文/开题文本", "err"); return; }
  const found = new Set();
  (text.match(/\b[A-Z][A-Za-z0-9]*-\d{4}\b/g) || []).forEach(x => found.add(x));
  ["UAVIDS", "CICIDS", "NSL-KDD", "CIC-IDS", "AWID", "TON_IoT"].forEach(k => { if (text.toUpperCase().includes(k)) found.add(k); });
  r.names = Array.from(found).slice(0, 8);
  $("ds-query").value = r.names[0] || $("ds-query").value;
  setStatus("ds-status", "提取到：" + (r.names.join(", ") || "（未发现明显数据集名，可手动填检索词）"), r.names.length ? "ok" : "warn");
});
$("ds-search").addEventListener("click", async () => {
  const q = $("ds-query").value.trim();
  if (!q) { setStatus("ds-status", "检索词不能为空", "err"); return; }
  setStatus("ds-status", "Zenodo 检索中…");
  const r = await post("dataset", { action: "search", query: q });
  if (!r.ok) { setStatus("ds-status", r.error || "检索失败", "err"); }
  const hits = r.hits || [];
  const fb = r.fallback_links || [];
  $("ds-hits").innerHTML = (!hits.length && !fb.length) ? '<p class="chat-hint">无结果。</p>' :
    (hits.map((h, i) => '<div style="border:1px solid var(--border);border-radius:8px;padding:8px;margin:6px 0">' +
      '<b>' + esc(h.title) + '</b> <span class="tag">' + esc(h.source) + '</span>' +
      (h.doi ? ' <span class="tag">DOI:' + esc(h.doi) + '</span>' : "") +
      (h.link ? ' <a href="' + esc(h.link) + '" target="_blank" class="chat-hint">打开页面</a>' : "") +
      (h.files || []).map(f => '<div class="chat-hint">' + esc(f.name) + '（' + Math.max(1, Math.round(f.size / 1024 / 1024)) + ' MB） ' +
        '<button class="btn-mini" data-url="' + esc(f.url) + '" data-fn="' + esc(f.name || "file") + '">下载</button></div>').join("") +
      '</div>').join("") +
     fb.map(x => '<div class="chat-hint">备选入口：<a href="' + esc(x.link) + '" target="_blank">' + esc(x.title) + '</a></div>').join(""));
  $("ds-hits").querySelectorAll("button[data-url]").forEach(b => b.addEventListener("click", async () => {
    setStatus("ds-status", "下载中（大文件请耐心等待）…");
    const d = await post("dataset", { action: "download", url: b.dataset.url,
      dest: $("ds-query").value.trim() || "misc", filename: b.dataset.fn });
    setStatus("ds-status", d.ok ? "已下载到：" + d.path : (d.error || "失败"), d.ok ? "ok" : "err");
    if (d.ok) listMine();
  }));
});
async function listMine() {
  const r = await post("dataset", { action: "downloaded" });
  const ds = r.ok ? (r.downloaded || []) : [];
  $("ds-mine").innerHTML = !ds.length ? '<p class="chat-hint">本地还没有已下载数据集。</p>' :
    '<table class="tbl"><tr><th>数据集</th><th>文件</th><th>大小</th></tr>' +
    ds.map(d => '<tr><td>' + esc(d.name) + '</td><td>' + d.files.map(f => esc(f[0])).join("<br>") +
      '</td><td>' + d.files.map(f => (f[1] / 1024 / 1024).toFixed(1) + " MB").join("<br>") + '</td></tr>').join("") + '</table>';
}
$("ds-mine-btn").addEventListener("click", listMine);
async function loadTpPapers() {
  const r = await post("mypaper", { action: "list" });
  if (!r.ok) return;
  $("tp-paper").innerHTML = '<option value="">— 选择论文条目 —</option>' +
    (r.papers || []).map(p => '<option value="' + p.id + '">' + esc(p.title) + (p.file_name ? " 📄" : "") + '</option>').join("");
}
$("tp-loadpaper").addEventListener("click", async () => {
  const id = $("tp-paper").value;
  if (!id) { setStatus("tp-status", "先选择论文条目", "err"); return; }
  setStatus("tp-status", "载入论文文本…");
  const r = await post("mypaper", { action: "get_text", id });
  if (!r.ok) { setStatus("tp-status", r.error || "载入失败", "err"); return; }
  $("tp-text").value = ("【来自我的论文：" + ($("tp-paper").selectedOptions[0].text) + "】\n\n" + (r.text || "")).trim();
  setStatus("tp-status", "已载入 " + (r.text || "").length + " 字，点「生成训练方案」", "ok");
});
$("tp-upload").addEventListener("click", () => {
  const f = $("tp-file").files[0];
  if (!f) { setStatus("tp-status", "先选择 .docx 文件", "err"); return; }
  const reader = new FileReader();
  reader.onload = async () => {
    setStatus("tp-status", "上传解析中…");
    const r = await post("mypaper", { action: "upload_word", data_b64: reader.result.split(",")[1],
      title: f.name.replace(/\.docx$/i, ""), ptype: "期刊论文" });
    if (!r.ok) { setStatus("tp-status", r.error || "导入失败", "err"); return; }
    setStatus("tp-status", "已导入并载入文本", "ok");
    await loadTpPapers();
    const nid = (r.papers || [])[0] && r.papers[0].id;
    if (nid) { $("tp-paper").value = nid; $("tp-loadpaper").click(); }
  };
  reader.readAsDataURL(f);
});
$("ds-to-tp").addEventListener("click", () => {
  const info = "【数据集信息】检索词：" + $("ds-query").value + "；已下载数据见「查看已下载数据集」。请据此设计训练方案。";
  $("tp-text").value = ($("tp-text").value + "\n\n" + info).trim();
  $("tp-text").scrollIntoView({ behavior: "smooth" });
  flash("已带入 AI 训练设计");
});

// ---- 我的论文 Word 导入 ----
$("mp-import").addEventListener("click", () => {
  const f = $("mp-file").files[0];
  if (!f) { setStatus("mp-status-box", "先选择 .docx 文件", "err"); return; }
  const reader = new FileReader();
  reader.onload = async () => {
    const b64 = reader.result.split(",")[1];
    setStatus("mp-status-box", "导入中…");
    const r = await post("mypaper", { action: "upload_word", data_b64: b64,
      title: f.name.replace(/\.docx$/i, ""), ptype: "期刊论文" });
    setStatus("mp-status-box", r.ok ? "已建档：" + (r.note || "") : (r.error || "导入失败"), r.ok ? "ok" : "err");
    if (r.ok) { _mpCache = r.papers; renderMyPapers(); }
  };
  reader.readAsDataURL(f);
});

// ---- 写作/出图页技能（勾选式多选运行） ----
$("wr-skill-run").addEventListener("click", async () => {
  const jobs = [];
  if ($("wr-sk-cs") && $("wr-sk-cs").checked)
    jobs.push({ skill: "cs_writing", name: "CS/AI 分学科写作检查",
      input: ($("wr-extra").value || $("wr-points").value || "").trim() });
  if ($("wr-sk-slides") && $("wr-sk-slides").checked)
    jobs.push({ skill: "html_slides", name: "生成 HTML 汇报幻灯",
      input: (_apLast || $("wr-extra").value || "").trim() });
  if (!jobs.length) { setStatus("wr-status", "请先勾选至少一个技能", "err"); return; }
  let md = "", skip = 0;
  for (let i = 0; i < jobs.length; i++) {
    if (!jobs[i].input) {
      md += "\n\n## " + jobs[i].name + "\n\n> 跳过：缺少输入内容（素材/要点，或先跑开题成文）";
      skip++; continue;
    }
    setStatus("wr-status", "运行技能 " + (i + 1) + "/" + jobs.length + "：" + jobs[i].name + "…");
    const r = await post("skills", { action: "run", skill: jobs[i].skill, input: jobs[i].input });
    if (r.ok) md += "\n\n## " + jobs[i].name + "\n\n" + r.reply;
    else { md += "\n\n## " + jobs[i].name + "\n\n> 失败：" + (r.error || ""); skip++; }
  }
  $("wr-out").classList.remove("hidden");
  $("wr-md").innerHTML = renderMarkdown(md.trim());
  setStatus("wr-status", skip ? ("完成（" + skip + " 个跳过/失败）") : "全部完成", skip ? "warn" : "ok");
});
$("pl-redline").addEventListener("click", async () => {
  const d = prompt("描述你论文图的做法（例如：用 matplotlib 画的 F1 曲线 / AI 生成了 workflow 图再手动描）：");
  if (!d) return;
  setStatus("pl-redline-status", "红线检查中…");
  const r = await post("skills", { action: "run", skill: "figure_redline", input: d });
  if (r.ok) { $("pl-redline-out").classList.remove("hidden"); $("pl-redline-md").innerHTML = renderMarkdown(r.reply); setStatus("pl-redline-status", "完成", "ok"); }
  else setStatus("pl-redline-status", r.error, "err");
});

// ---------------- 顶栏标签 / 手风琴状态 ----------------
let _openedTabs = [], _curTab = null;
function renderTopTabs() {
  const box = $("top-tabs");
  if (!box) return;
  box.innerHTML = _openedTabs.map(t =>
    '<span class="top-tab' + (t === _curTab ? " active" : "") + '" data-t="' + t + '">' +
    esc((TITLES[t] || ["?"])[0]) +
    '<span class="tab-x" data-x="' + t + '" title="关闭">✕</span></span>').join("");
  box.querySelectorAll(".top-tab").forEach(el => {
    el.addEventListener("click", (ev) => {
      if (ev.target.classList.contains("tab-x")) return;
      switchTab(el.dataset.t);
    });
    const x = el.querySelector(".tab-x");
    if (x) x.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const t = x.dataset.x;
      _openedTabs = _openedTabs.filter(x2 => x2 !== t);
      if (_curTab === t) { const nxt = _openedTabs[_openedTabs.length - 1] || "chat"; switchTab(nxt); }
      renderTopTabs();
    });
  });
}

// ---------------- 个人信息（设置页） ----------------
function loadSelfInfo() {
  if (!ME) return;
  $("pi-name").value = ME.name || "";
  $("pi-display").value = ME.display || "";
  $("pi-email").value = ME.email || "";
}
$("pi-save").addEventListener("click", async () => {
  const r = await post("auth", { action: "self_update",
    display: $("pi-display").value, email: $("pi-email").value });
  setStatus("pi-status", r.ok ? "已保存" : (r.error || "保存失败"), r.ok ? "ok" : "err");
  if (r.ok) { ME = Object.assign(ME || {}, r.user); renderUserbar(); }
});
$("api-parse").addEventListener("click", () => {
  const t = $("api-paste").value || "";
  if (t.trim().length < 8) { setStatus("api-parse-status", "请先粘贴 API 信息", "err"); return; }
  let url = "", key = "", model = "";
  const urls = (t.match(/https?:\/\/[^\s"'\u4e00-\u9fff，。；、（）()]+/g) || []);
  url = (urls.filter(u => /\/v\d/i.test(u))[0] || urls[0] || "").replace(/[，。；】」]+$/, "");
  const km = t.match(/(?:api[\s_-]?key|密钥|令牌|key)\s*[:=：]\s*["']?([A-Za-z0-9_\-]{16,})/i)
          || t.match(/\b(sk-[A-Za-z0-9_\-]{8,})\b/);
  if (km) key = km[1];
  const mm = t.match(/(?:["']?model["']?|模型)\s*[:=：]\s*["']?([A-Za-z0-9._\-\/]+)/i);
  if (mm) model = mm[1];
  if (!model) {
    const known = t.match(/\b(hunyuan-turbos-latest|hunyuan-[\w\.\-]+|deepseek-[\w\.\-]+|qwen[\w\.\-]*|gpt-[\w\.\-]+|glm-[\w\.\-]+|claude-[\w\.\-]+|llama[\w\.\-]*)\b/i);
    if (known) model = known[1];
  }
  if (!url && !key && !model) { setStatus("api-parse-status", "没有识别到 API 信息（端点/Key/模型名）。请检查粘贴内容格式。", "err"); return; }
  if (url) $("ms-url").value = url;
  if (key) $("ms-key").value = key;
  if (model) $("ms-model").value = model;
  const got = [url && "端点 ✓", key && "Key ✓", model && "模型 ✓"].filter(Boolean).join("、");
  setStatus("api-parse-status", "已识别并填入：" + got + "。请核对「模型设置」无误后点「保存配置」。", "ok");
});
$("pi-pwd").addEventListener("click", async () => {
  const np = $("pi-new").value;
  if (!np || np.length < 6) { setStatus("pi-pwd-status", "新密码至少 6 位", "err"); return; }
  const r = await post("auth", { action: "self_update",
    old_password: $("pi-old").value, new_password: np });
  setStatus("pi-pwd-status", r.ok ? "密码已修改" : (r.error || "修改失败"), r.ok ? "ok" : "err");
  if (r.ok) { $("pi-old").value = ""; $("pi-new").value = ""; }
});

// ---------------- 技能中心（多选运行 + 上传自定义技能） ----------------
async function loadSkillCenter() {
  const r = await post("skills", {});
  if (!r.ok) return;
  const list = r.skills || [];
  $("sk-multi").innerHTML = list.map(x =>
    '<option value="' + esc(x.id) + '">' + esc(x.name) + (x.custom ? "（自定义·" + esc(x.by) + "）" : "") + '</option>').join("");
}
$("sk-refresh").addEventListener("click", loadSkillCenter);
$("sk-export").addEventListener("click", async () => {
  const picks = Array.from($("sk-multi").selectedOptions).map(o => o.value);
  if (!picks.length) { setStatus("sk-exp-status", "请先在「技能管理」列表中选中要导出的技能（Ctrl/Shift 多选）", "err"); return; }
  setStatus("sk-exp-status", "打包中…");
  const r = await post("skills", { action: "export", ids: picks });
  if (!r.ok) { setStatus("sk-exp-status", r.error || "导出失败", "err"); return; }
  const bin = atob(r.zip_b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const blob = new Blob([bytes], { type: "application/zip" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = r.filename || "dawn_skills.zip";
  a.click();
  URL.revokeObjectURL(a.href);
  setStatus("sk-exp-status", "已导出 " + r.count + " 个技能 → " + (r.filename || ""), "ok");
});
$("sk-del").addEventListener("click", async () => {
  const picks = Array.from($("sk-multi").selectedOptions).map(o => ({ id: o.value, text: o.text }));
  if (!picks.length) { setStatus("sk-status", "请先选中要删除的自定义技能", "err"); return; }
  let n = 0;
  for (const pk of picks) {
    const r = await post("skills", { action: "remove", id: pk.id });
    if (r.ok) n++;
  }
  setStatus("sk-status", "已删除 " + n + " 个自定义技能", "ok");
  loadSkillCenter();
});
$("sk-file").addEventListener("change", () => {
  const f = $("sk-file").files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = async () => {
    setStatus("sk-up-status", "上传解析中…");
    const r = await post("skills", { action: "upload", data_b64: reader.result.split(",")[1],
      filename: f.name, name: $("sk-name").value.trim() });
    setStatus("sk-up-status", r.ok ? "技能已上传：" + (r.id || "") : (r.error || "上传失败"), r.ok ? "ok" : "err");
    if (r.ok) { $("sk-name").value = ""; $("sk-file").value = ""; loadSkillCenter(); }
  };
  reader.readAsDataURL(f);
});

// ---------------- 论文格式检查 ----------------
async function loadFtPapers() {
  const r = await post("mypaper", { action: "list" });
  if (!r.ok) return;
  const opts = '<option value="">— 选择已导入 Word 的条目 —</option>' +
    (r.papers || []).map(p => '<option value="' + p.id + '">' + esc(p.title) + (p.file_name ? " 📄" : "") + '</option>').join("");
  const el = $("ft-paper"); if (el) el.innerHTML = opts;
}
async function loadFtTemplate() {
  const t = $("ft-type") ? $("ft-type").value : "thesis";
  const r = await post("format", { action: "template_get", ptype: t });
  if (r.ok) {
    $("ft-template").value = (r.items || []).join("\n");
    setStatus("ft-t-status", r.default ? "当前为内置标准结构（可自定义覆盖）" : "已加载你的自定义模板", r.default ? "" : "ok");
  }
}
$("ft-type").addEventListener("change", loadFtTemplate);
$("ft-t-load").addEventListener("click", loadFtTemplate);
$("ft-t-save").addEventListener("click", async () => {
  const r = await post("format", { action: "template_save",
    ptype: $("ft-type").value, text: $("ft-template").value });
  setStatus("ft-t-status", r.ok ? "模板已保存（" + r.count + " 项）" : (r.error || "保存失败"), r.ok ? "ok" : "err");
});
$("ft-loadpaper").addEventListener("click", async () => {
  const id = $("ft-paper").value;
  if (!id) { setStatus("ft-status", "先选择论文条目", "err"); return; }
  setStatus("ft-status", "载入论文文本…");
  const r = await post("mypaper", { action: "get_text", id });
  if (!r.ok) { setStatus("ft-status", r.error || "载入失败（该条目需先在「我的论文」导入 Word）", "err"); return; }
  $("ft-text").value = r.text || "";
  setStatus("ft-status", "已载入 " + (r.text || "").length + " 字", "ok");
});
$("ft-paper-file").addEventListener("change", () => {
  const f = $("ft-paper-file").files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = async () => {
    setStatus("ft-status", "解析论文中…");
    const r = await post("format", { action: "upload_doc", kind: "paper",
      data_b64: reader.result.split(",")[1], filename: f.name });
    if (!r.ok) { setStatus("ft-status", r.error || "解析失败", "err"); return; }
    $("ft-text").value = r.text || "";
    setStatus("ft-status", "已载入论文（" + r.chars + " 字），可点「运行格式检查」", "ok");
  };
  reader.readAsDataURL(f);
});
$("ft-req-file").addEventListener("change", () => {
  const f = $("ft-req-file").files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = async () => {
    setStatus("ft-status", "解析论文要求中…");
    const r = await post("format", { action: "upload_doc", kind: "req",
      data_b64: reader.result.split(",")[1], filename: f.name,
      ptype: $("ft-type").value });
    if (!r.ok) { setStatus("ft-status", r.error || "解析失败", "err"); return; }
    $("ft-template").value = (r.items || []).join("\n");
    setStatus("ft-status", "✅ " + (r.note || "要求已保存为模板") + "，现在可运行格式检查", "ok");
    loadFtTemplate();
  };
  reader.readAsDataURL(f);
});
$("ft-run").addEventListener("click", async () => {
  const text = $("ft-text").value.trim();
  if (text.length < 100) { setStatus("ft-status", "请粘贴论文全文或较长章节（≥100 字）", "err"); return; }
  setStatus("ft-status", "格式检查中…");
  const r = await post("format", { action: "check", text,
    ptype: $("ft-type").value, ai: $("ft-ai").checked });
  if (!r.ok) { setStatus("ft-status", r.error || "检查失败", "err"); return; }
  $("ft-out").classList.remove("hidden");
  $("ft-md").innerHTML = renderMarkdown(r.markdown || "");
  setStatus("ft-status", "完成：发现问题 " + r.problems + " 个，通过 " + r.pass + " 项" + (r.ai_md ? "（含 AI 补充）" : ""), r.problems ? "warn" : "ok");
});

// ---------------- 论文降重 ----------------
async function loadDpPapers() {
  const r = await post("mypaper", { action: "list" });
  if (!r.ok) return;
  const el = $("dp-paper"); if (!el) return;
  el.innerHTML = '<option value="">— 选择已导入 Word 的条目 —</option>' +
    (r.papers || []).map(p => '<option value="' + p.id + '">' + esc(p.title) + (p.file_name ? " 📄" : "") + '</option>').join("");
}
$("dp-load").addEventListener("click", async () => {
  const id = $("dp-paper").value;
  if (!id) { setStatus("dp-status", "先选择论文条目（需已在「我的论文」导入 Word）", "err"); return; }
  setStatus("dp-status", "载入全文…");
  const r = await post("mypaper", { action: "get_text", id });
  if (!r.ok) { setStatus("dp-status", r.error || "载入失败", "err"); return; }
  $("dp-text").value = r.text || "";
  setStatus("dp-status", "已载入 " + (r.text || "").length + " 字", "ok");
});
let _dpLast = "";
$("dp-run").addEventListener("click", async () => {
  const text = $("dp-text").value.trim();
  if (text.length < 50) { setStatus("dp-status", "待降重文本太短（≥50 字）；超长文本建议分段落处理", "err"); return; }
  if (text.length > 6000) { setStatus("dp-status", "单次建议 ≤6000 字（当前 " + text.length + "），请分段降重", "err"); return; }
  setStatus("dp-status", "降重中（AI 改写，约 1-2 分钟）…");
  const r = await post("skills", { action: "run", skill: $("dp-mode").value, input: text });
  if (!r.ok) { setStatus("dp-status", r.error || "降重失败", "err"); return; }
  _dpLast = r.reply || "";
  $("dp-out").classList.remove("hidden");
  $("dp-md").innerHTML = renderMarkdown(_dpLast);
  setStatus("dp-status", "降重完成（" + _dpLast.length + " 字）——请人工复核术语与引用后再用", "ok");
});
$("dp-save").addEventListener("click", async () => {
  if (!_dpLast) { setStatus("dp-status", "还没有降重结果", "err"); return; }
  const title = "降重改写 · " + (($("dp-paper").selectedOptions[0] || {}).text || $("dp-mode").selectedOptions[0].text) + " · " + new Date().toLocaleString();
  const r = await post("write", { action: "save", kind: "dup", title, content: _dpLast });
  setStatus("dp-status", r.ok ? "已存入草稿库（写作综述 → 草稿库可查看）" : (r.error || "保存失败"), r.ok ? "ok" : "err");
});

boot();

// ---------------- 换肤（白蓝 / 暗夜蓝 / 青绿） ----------------
const SKINS = ["blue", "pink", "dark", "green"];
function applySkin(name) {
  if (!SKINS.includes(name)) name = "blue";
  document.documentElement.setAttribute("data-skin", name);
  localStorage.setItem("ra_skin", name);
  document.querySelectorAll(".skin-btn").forEach(b => b.classList.toggle("active", b.dataset.skin === name));
}
function buildSkinSwitchers() {
  ["skins-gate", "skins-top"].forEach(id => {
    const box = $(id);
    if (!box) return;
    box.innerHTML = '<span class="lbl">主题</span>' +
      SKINS.map(s => '<button class="skin-btn" data-skin="' + s + '" title="' + s + '"></button>').join("");
    box.querySelectorAll(".skin-btn").forEach(b => b.addEventListener("click", () => applySkin(b.dataset.skin)));
  });
  applySkin(localStorage.getItem("ra_skin") || "blue");
}
buildSkinSwitchers();


// ---------------- 协作中心（成员 / 任务板 / 论文） ----------------
const mini = (label, danger, extra) =>
  '<button class="btn-mini' + (danger ? " danger" : "") + '" data-k="' + label + '"' + (extra || "") + ">" + esc(label) + "</button>";
function bindMini(root, fnMap) {
  root.addEventListener("click", (ev) => {
    const b = ev.target.closest("button.btn-mini");
    if (!b) return;
    const fn = fnMap[b.dataset.k];
    if (fn) fn(b.dataset);
  });
}

// --- 成员 ---
async function loadMembers() {
  const r = await post("member", { action: "list" });
  if (!r.ok) return;
  const ms = r.members || [];
  $("tm-list").innerHTML = ms.length
    ? '<table class="tbl"><tr><th>姓名</th><th>角色</th><th>专长</th><th>备注</th><th></th></tr>' +
      ms.map(m =>
        "<tr><td><b>" + esc(m.name) + "</b></td><td>" + esc(m.role || "成员") + "</td><td>" +
        esc(m.skills || "") + "</td><td>" + esc(m.notes || "") + "</td><td>" +
        mini("移除", true, ' data-id="' + m.id + '"') + "</td></tr>").join("") + "</table>"
    : '<p style="color:var(--muted)">暂无成员。把小组同学加进来，任务才能指派到人。</p>';
  bindMini($("tm-list"), {
    移除: async (d) => { await post("member", { action: "remove", id: d.id }); loadMembers(); refreshTasks(); },
  });
}
$("tm-add").addEventListener("click", async () => {
  const name = $("tm-name").value.trim();
  if (!name) { alert("成员姓名不能为空"); return; }
  await post("member", { action: "add", name, role: $("tm-role").value, skills: $("tm-skills").value.trim(), notes: $("tm-note").value.trim() });
  $("tm-name").value = ""; $("tm-skills").value = ""; $("tm-note").value = "";
  loadMembers();
});

// --- 任务板 ---
const ST_ZH = { todo: "待办", doing: "进行中", review: "评审", done: "完成" };
async function refreshTasks() {
  const r = await post("task", { action: "list" });
  if (!r.ok) { return; }
  const tasks = r.tasks || [];
  const order = ["todo", "doing", "review", "done"];
  const groups = order.map(st => [st, tasks.filter(t => t.status === st)]);
  let html = "";
  for (const [st, list] of groups) {
    if (!list.length) continue;
    html += "<p><b>" + ST_ZH[st] + "</b>（" + list.length + "）</p>";
    html += '<table class="tbl"><tr><th>任务</th><th>指派</th><th>优先级</th><th>模块</th><th>操作</th></tr>';
    html += list.map(t => {
      const idAttr = ' data-id="' + t.id + '"';
      let ops = "";
      if (t.status === "todo") ops += mini("开始", false, idAttr);
      if (t.status === "doing") { ops += mini("评审", false, idAttr); ops += mini("完成", false, idAttr); }
      if (t.status === "review") ops += mini("完成", false, idAttr);
      if (t.status !== "done") ops += mini("删除", true, idAttr);
      else ops += mini("重开", false, idAttr);
      return "<tr><td>" + esc(t.title) + "</td><td>" + esc(t.assignee || "—") + "</td><td>" +
        esc(t.priority || "中") + "</td><td>" + esc(t.module || "—") + "</td><td>" + ops + "</td></tr>";
    }).join("");
    html += "</table>";
  }
  $("tk-board").innerHTML = html || '<p style="color:var(--muted)">任务板为空——把论文写作拆成任务指派下去。</p>';
  bindMini($("tk-board"), {
    开始: (d) => tkStatus(d.id, "doing"),
    评审: (d) => tkStatus(d.id, "review"),
    完成: (d) => tkStatus(d.id, "done"),
    重开: (d) => tkStatus(d.id, "todo"),
    删除: (d) => tkRemove(d.id),
  });
}
async function tkStatus(id, status) {
  if (!id) return;
  await post("task", { action: "update", id, status });
  refreshTasks();
}
async function tkRemove(id) {
  if (!id) return;
  await post("task", { action: "remove", id });
  refreshTasks();
}
$("tk-add").addEventListener("click", async () => {
  const title = $("tk-title").value.trim();
  if (!title) { setStatus("tk-status", "任务标题不能为空", "err"); return; }
  const r = await post("task", { action: "add", title, assignee: $("tk-assignee").value.trim(), priority: $("tk-priority").value, module: $("tk-module").value });
  $("tk-title").value = "";
  if (r.ok) { setStatus("tk-status", "已添加任务", "ok"); refreshTasks(); }
  else setStatus("tk-status", r.error, "err");
});

// --- 论文协作 ---
let _papers = [];
async function refreshPapers() {
  const r = await post("paper", { action: "list" });
  if (!r.ok) return;
  _papers = r.papers || [];
  const sel = $("pp-sel");
  const prev = sel.value;
  sel.innerHTML = '<option value="">— 选择论文 —</option>' +
    _papers.map(p => '<option value="' + p.id + '">' + esc(p.title) + "</option>").join("");
  if (prev && _papers.some(p => p.id === prev)) sel.value = prev;
  loadPaper();
}
async function loadPaper() {
  const pid = $("pp-sel").value;
  if (!pid) {
    $("pp-detail").innerHTML = "";
    $("pp-sec-card").style.display = "none";
    $("pp-collab-card").classList.add("hidden");
    return;
  }
  const r = await post("paper", { action: "get", id: pid });
  if (!r.ok) {
    $("pp-detail").innerHTML = '<p class="chat-hint">' + esc(r.error || "无法加载") + "</p>";
    $("pp-sec-card").style.display = "none";
    $("pp-collab-card").classList.add("hidden");
    return;
  }
  $("pp-detail").innerHTML = r.markdown ? renderMarkdown(r.markdown) : "";
  $("pp-sec-card").style.display = "";
  // 只读协作者隐藏编辑区
  const canEdit = ["creator", "edit", "admin"].includes(r.my_perm);
  $("pp-sec-card").querySelectorAll("input,textarea,button").forEach(el => el.disabled = !canEdit);
  // 协作者管理（仅创建者/管理员）
  const p = r.paper || {};
  const isOwner = ["creator", "admin"].includes(r.my_perm);
  $("pp-collab-card").classList.toggle("hidden", !isOwner);
  if (isOwner) {
    const cl = p.collaborators || [];
    $("pp-collab-list").innerHTML = !cl.length
      ? '<p class="chat-hint">暂无协作者。从用户目录选择并授予「可编辑」或「仅查看」。</p>'
      : '<table class="tbl"><tr><th>用户</th><th>权限</th><th>添加时间</th><th></th></tr>' +
        cl.map(c => '<tr><td>' + esc(c.name) + '</td><td>' + (c.perm === "edit" ? "可编辑" : "仅查看") +
          '</td><td>' + esc(c.added || "") + '</td>' +
          '<td><button class="btn-mini danger" data-cname="' + esc(c.name) + '">移除</button></td></tr>').join("") +
        '</table>';
    $("pp-collab-user").innerHTML = !_partnersCache.length
      ? '<option value="">（暂无合作者——先去「合作对接」建立合作关系）</option>'
      : _partnersCache.map(pp => '<option value="' + esc(pp.name) + '">' + esc(pp.name) +
          (pp.display && pp.display !== pp.name ? "（" + esc(pp.display) + "）" : "") + '</option>').join("");
  }
}
$("pp-collab-add").addEventListener("click", async () => {
  const pid = $("pp-sel").value;
  const cname = $("pp-collab-user").value;
  if (!pid || !cname) { setStatus("pp-collab-status", "先选择论文与用户", "err"); return; }
  const r = await post("paper", { action: "collab_add", id: pid, cname, cperm: $("pp-collab-perm").value });
  setStatus("pp-collab-status", r.ok ? "已添加，对方的「我的论文」将自动收录该项目" : (r.error || "失败"), r.ok ? "ok" : "err");
  if (r.ok) loadPaper();
});
$("pp-collab-list").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button.btn-mini");
  if (!b) return;
  if (!confirm("确认移除协作者 " + b.dataset.cname + "？其「我的论文」将不再显示该项目。")) return;
  const r = await post("paper", { action: "collab_remove", id: $("pp-sel").value, cname: b.dataset.cname });
  setStatus("pp-collab-status", r.ok ? "已移除" : (r.error || "失败"), r.ok ? "ok" : "err");
  if (r.ok) loadPaper();
});
$("pp-create").addEventListener("click", async () => {
  const title = $("pp-title").value.trim();
  if (!title) { setStatus("pp-status", "论文标题不能为空", "err"); return; }
  const r = await post("paper", { action: "create", title, owner: $("pp-owner").value.trim(), target_journal: $("pp-journal").value.trim() });
  if (r.ok) { setStatus("pp-status", "已创建论文项目", "ok"); refreshPapers(); }
  else setStatus("pp-status", r.error, "err");
});
$("pp-sel").addEventListener("change", loadPaper);
$("pp-sec-save").addEventListener("click", async () => {
  const pid = $("pp-sel").value;
  if (!pid) { alert("先选择论文"); return; }
  await post("paper", { action: "section", id: pid, key: $("pp-sec-key").value.trim(), title: $("pp-sec-title").value.trim(), assignee: $("pp-sec-assignee").value.trim(), status: $("pp-sec-status").value, content: $("pp-sec-content").value });
  $("pp-sec-content").value = "";
  loadPaper();
});
$("pp-cmt-add").addEventListener("click", async () => {
  const pid = $("pp-sel").value;
  const text = $("pp-cmt-text").value.trim();
  if (!pid || !text) { alert("先选论文并填写意见"); return; }
  await post("paper", { action: "comment", id: pid, section: $("pp-cmt-section").value.trim() || "*", by: $("pp-cmt-by").value.trim() || "匿名", text });
  $("pp-cmt-text").value = "";
  loadPaper();
});

// ---------------- 实验复现（台账 / 复现 / CSV 校验） ----------------
let _exps = [];
function fillExpSel(selId) {
  const sel = $(selId);
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">— 选择实验 —</option>' +
    _exps.map(e => '<option value="' + e.id + '">' + e.id + " · " + esc(e.name) + "</option>").join("");
  if (prev && _exps.some(e => e.id === prev)) sel.value = prev;
}
async function refreshExps() {
  const r = await post("experiment", { action: "list" });
  if (!r.ok) return;
  _exps = r.experiments || [];
  fillExpSel("ex-sel");
  $("lab-tables").innerHTML = renderMarkdown(r.markdown || "**实验台账**：暂无。");
}
$("ex-add").addEventListener("click", async () => {
  const name = $("ex-name").value.trim();
  if (!name) { setStatus("ex-status-bar", "实验名称不能为空", "err"); return; }
  const r = await post("experiment", { action: "add", name, dataset: $("ex-dataset").value.trim(), owner: $("ex-owner").value.trim(), status: $("ex-status").value, seed: $("ex-seed").value.trim(), hypothesis: $("ex-hyp").value.trim() });
  if (r.ok) { setStatus("ex-status-bar", "已登记：可在下方填结果 / 发起复现", "ok"); $("ex-name").value = ""; refreshExps(); refreshReps(); }
  else setStatus("ex-status-bar", r.error, "err");
});
$("ex-save-result").addEventListener("click", async () => {
  const pid = $("ex-sel").value;
  const raw = $("ex-result").value.trim();
  if (!pid) { alert("先选实验"); return; }
  let result = {};
  if (raw) { try { result = JSON.parse(raw); } catch (e) { alert("指标结果需是 JSON，如 {\"F1\": 0.921}"); return; } }
  await post("experiment", { action: "update", id: pid, status: "done", result });
  $("ex-result").value = "";
  refreshExps();
});
async function refreshReps() {
  const r = await post("reproduce", { action: "list" });
  if (!r.ok) return;
  const reps = r.reproductions || [];
  let block = $("rep-block");
  if (!reps.length) { if (block) block.remove(); return; }
  if (!block) {
    block = document.createElement("div");
    block.id = "rep-block";
    $("lab-tables").insertAdjacentElement("afterend", block);
  }
  block.innerHTML = "<p><b>复现记录</b></p>" + renderMarkdown(r.markdown);
}
$("rp-add").addEventListener("click", async () => {
  const pid = $("ex-sel").value;
  if (!pid) { alert("先选要复现的实验"); return; }
  await post("reproduce", { action: "add", exp_id: pid, by: $("rp-by").value.trim() || "匿名", env: $("rp-env").value.trim(), result: $("rp-result").value, diff_note: $("rp-note").value.trim() });
  $("rp-by").value = ""; $("rp-note").value = "";
  refreshReps();
});
$("v-run").addEventListener("click", async () => {
  setStatus("v-status", "校验中…");
  const r = await post("validate", { csv: $("v-csv").value });
  if (!r.ok) { setStatus("v-status", r.error, "err"); return; }
  $("v-md").innerHTML = renderMarkdown(r.markdown);
  $("v-out").classList.remove("hidden");
  setStatus("v-status", r.pass ? "校验通过" : "发现 " + r.errors.length + " 个问题", r.pass ? "ok" : "err");
});

// ---------------- 模型设置 + 画像输入 + AI 判断 ----------------
const PRESET_MAP = {
  hunyuan: { backend: "openai", base_url: "https://api.hunyuan.cloud.tencent.com/v1", model: "hunyuan-turbos-latest" },
  deepseek: { backend: "openai", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  ollama: { backend: "ollama", base_url: "http://127.0.0.1:11434", model: "qwen2.5:7b" },
  siliconflow: { backend: "openai", base_url: "https://api.siliconflow.cn/v1", model: "Qwen/Qwen2.5-7B-Instruct" },
  zhipu: { backend: "openai", base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash" },
  modelscope: { backend: "openai", base_url: "https://api-inference.modelscope.cn/v1", model: "Qwen/Qwen2.5-7B-Instruct" },
  custom: { backend: "openai", base_url: "", model: "" },
};
const PE_KEYS = ["direction_cn", "direction_en", "skills", "works", "innovations", "target_journals", "constraints", "goals"];
function peVal() {
  const o = {};
  PE_KEYS.forEach(k => o[k] = ($("pe-" + k) ? $("pe-" + k).value : "").trim());
  return o;
}
async function fillProfileExtra() {
  const r = await post("profile_extra", {});
  if (!r.ok) return;
  const ex = r.profile_extra || {};
  PE_KEYS.forEach(k => { const el = $("pe-" + k); if (el && ex[k] != null) el.value = ex[k]; });
}
async function refreshModelStatus() {
  const r = await post("model_status", {});
  if (!r) return;
  // 预填表单（不含 api_key）
  if (r.preset) $("ms-preset").value = r.preset;
  if (r.backend_cfg) $("ms-backend").value = r.backend_cfg;
  if (r.base_url) $("ms-url").value = r.base_url;
  if (r.model) $("ms-model").value = r.model;
  const admin = !!(ME && ME.role === "admin");
  $("ms-save").disabled = !admin;
  $("ms-test").disabled = !admin;
  // 统一写入 ms-status（合并了原来的 ms-state + ms-status 双区）
  const st = $("ms-status");
  if (r.ok) {
    st.className = "status ok";
    st.textContent = "已连接：" + (r.backend === "ollama" ? "本地 Ollama" : "OpenAI 兼容 API") + " · " + (r.model || "auto") + (r.has_key ? "" : "（未填 Key）");
  } else {
    st.className = "status err";
    if (!admin && ME) st.textContent = "模型为全局设置，仅管理员可修改。";
    else st.textContent = r.reason || "未配置模型" + (r.has_key ? "" : "（缺 API Key）");
  }
  renderSavedModel(r, "ms-current", true);
}

// 统一的"当前已保存模型"指示（设置页详尽版 / 对话页精简版）
function renderSavedModel(r, id, detail) {
  const el = $(id);
  if (!el || !r) return;
  const model = r.model || "（未设置模型名）";
  const label = r.preset_label || r.preset || "自定义";
  const keyTxt = r.has_key ? ("Key 已保存 " + (r.key_tail || "")) : "未填 API Key";
  let verified = "";
  if (r.last_ok) {
    const sameModel = !r.last_ok_model || r.last_ok_model === model;
    verified = sameModel
      ? '<span style="color:var(--ok,#0a8)">✓ 已验证可用（' + r.last_ok + '）</span>'
      : '<span style="color:var(--ok,#0a8)">✓ 曾验证成功（' + r.last_ok + '，当时模型 ' + esc(r.last_ok_model) + '）</span>';
  } else {
    verified = '<span style="color:var(--warn,#c80)">○ 尚未验证（点「测试连接」实际调一次）</span>';
  }
  let html = '<b>当前已保存模型：</b>' + esc(model) +
    ' <span style="color:var(--muted)">· ' + esc(label) + ' · ' + esc(keyTxt) + '</span><br>' + verified;
  if (r.last_error) {
    html += '<br><span style="color:var(--err,#c33)">最近失败：' + esc(r.last_error) + '</span>';
  }
  if (detail) {
    html += '<br><span style="color:var(--muted)">端点：' + esc(r.base_url || "（空）") +
      ' · 后端：' + esc(r.backend_cfg || "auto") + '</span>';
  }
  el.innerHTML = html;
}

// 对话页：显示当前会走哪个模型
async function refreshChatModelBadge() {
  const el = $("chat-curmodel");
  if (!el) return;
  const r = await post("model_status", {});
  if (!r) return;
  const cur = ($("chat-model") || {}).value || "";
  if (cur) {
    el.innerHTML = '<span style="color:var(--muted)">本次对话使用：</span><b>' + esc(cur) + '</b>' +
      '<span style="color:var(--muted)">（临时切换，不影响已保存配置）</span>';
  } else {
    renderSavedModel(r, "chat-curmodel", false);
  }
}
function cfgFromForm() {
  return { preset: $("ms-preset").value, backend: $("ms-backend").value, base_url: $("ms-url").value.trim(), api_key: $("ms-key").value.trim(), model: $("ms-model").value.trim() };
}
$("ms-save").addEventListener("click", async () => {
  const r = await post("model_set", cfgFromForm());
  setStatus("ms-status", r.ok ? "配置已保存" : ("保存失败：" + (r.error || "未知")), r.ok ? "ok" : "err");
  refreshModelStatus();
});
$("ms-test").addEventListener("click", async () => {
  setStatus("ms-status", "测试连接中（最长 90 秒）…");
  const r = await post("model_set", Object.assign(cfgFromForm(), { test: true }));
  if (r.ok && r.test && r.test.ok) setStatus("ms-status", "连接成功，模型回复：" + (r.test.reply || ""), "ok");
  else setStatus("ms-status", "测试失败：" + ((r.test && r.test.error) || r.error || "未知"), "err");
  refreshModelStatus();
});
$("ms-preset").addEventListener("change", () => {
  const p = PRESET_MAP[$("ms-preset").value] || {};
  if (p.backend) $("ms-backend").value = p.backend;
  if (p.base_url !== undefined) $("ms-url").value = p.base_url;
  if (p.model !== undefined) $("ms-model").value = p.model;
});
$("pe-save").addEventListener("click", async () => {
  const r = await post("profile_extra", Object.assign({ action: "save" }, peVal()));
  setStatus("pe-status", r.ok ? "已保存——AI 诊断与 AI 方向推荐都会以这些输入为依据" : "保存失败", r.ok ? "ok" : "err");
});
$("pe-ai").addEventListener("click", async () => {
  setStatus("pe-status", "AI 诊断中（模型思考可能需要 30–90 秒）…");
  const r = await post("ai_profile", {});
  if (!r.ok) { setStatus("pe-status", "无法诊断：" + (r.reason || r.error || "模型未配置"), "err"); return; }
  setStatus("pe-status", "AI 诊断完成，已把结果发到智能对话。", "ok");
  addAgent(r.reply);
  switchTab("chat");
});
$("ra-ai").addEventListener("click", async () => {
  setStatus("r-status", "AI 深度推荐中（模型思考 30–90 秒）…");
  const r = await post("ai_recommend", { offline: $("r-offline").checked, max_queries: 8, years: 3, limit: 10 });
  if (!r.ok) { setStatus("r-status", "AI 深度推荐失败：" + (r.reason || r.error || "模型未配置"), "err"); return; }
  $("ra-ai-md").innerHTML = renderMarkdown(r.reply);
  $("ra-ai-out").classList.remove("hidden");
  setStatus("r-status", "AI 深度推荐完成（含规则候选依据）", "ok");
});
// ---------------- 账户管理（管理员） ----------------
async function loadAccounts() {
  const box = $("acct-box");
  if (!box) return;
  const r = await post("auth", { action: "list" });
  if (!r.ok) { box.innerHTML = '<p style="color:var(--muted)">' + escapeHtml(r.error || "无权限") + "</p>"; return; }
  const users = r.users || [];
  if (!users.length) { box.innerHTML = '<p style="color:var(--muted)">暂无账户。</p>'; return; }
  let html = '<table class="tbl"><tr><th>用户名</th><th>显示名</th><th>角色</th><th>状态</th><th>创建</th><th>操作</th></tr>';
  for (const u of users) {
    const isAdmin = u.role === "admin";
    const active = u.active !== false;
    const isTeacher = u.role === "teacher";
    const pending = isTeacher && !active;
    const ROLE_ZH2 = { admin: "管理员", teacher: "导师", member: "学生" };
    let ops = "";
    if (!isAdmin) {  // 管理员只是管理员：不改角色、不停用
      const nextRole = isTeacher ? "member" : "teacher";
      ops += '<button class="btn-mini" data-ac="role" data-name="' + esc(u.name) + '" data-role="' + nextRole + '">' +
        (isTeacher ? "设为学生" : "设为导师") + "</button>";
    }
    if (!isAdmin) {
      const targetActive = pending ? true : !active;
      ops += '<button class="btn-mini" data-ac="toggle" data-name="' + esc(u.name) + '" data-active="' + targetActive + '">' +
        (pending ? "✅ 审核通过" : (active ? "停用" : "启用")) + "</button>";
    }
    ops += '<button class="btn-mini" data-ac="pwd" data-name="' + esc(u.name) + '">改密</button>';
    if (!isAdmin) ops += '<button class="btn-mini danger" data-ac="del" data-name="' + esc(u.name) + '">删除</button>';
    html += "<tr><td><b>" + esc(u.name) + "</b></td><td>" + esc(u.display) + "</td><td>" + (ROLE_ZH2[u.role] || u.role) +
      "</td><td>" + (pending ? '<span class="tag">待审核</span>' : (active ? "启用" : "停用")) + "</td><td>" + esc(u.created) + "</td><td>" + ops + "</td></tr>";
  }
  html += "</table>";
  box.innerHTML = html;
}
function bindAccountOps() {
  const box = $("acct-box");
  if (!box) return;
  box.onclick = async (ev) => {
    const b = ev.target.closest("button[data-ac]");
    if (!b) return;
    const name = b.dataset.name, ac = b.dataset.ac;
    const ROLE_ZH3 = { admin: "管理员", teacher: "导师", member: "学生" };
    if (ac === "role") {
      const to = b.dataset.role || "member";
      const r = await post("auth", { action: "update", name, role: to });
      setStatus("ac-status", r.ok ? ("已将 " + name + " 设为 " + (ROLE_ZH3[to] || to)) : r.error, r.ok ? "ok" : "err");
      loadAccounts();
    } else if (ac === "toggle") {
      const active = b.dataset.active === "true";
      const r = await post("auth", { action: "update", name, active });
      setStatus("ac-status", r.ok ? (name + (active ? " 已启用（审核通过）" : " 已停用")) : r.error, r.ok ? "ok" : "err");
      loadAccounts();
    } else if (ac === "pwd") {
      const np = prompt("为 " + name + " 设置新密码（≥6 位）：");
      if (np == null) return;
      if (np.length < 6) { setStatus("ac-status", "密码至少 6 位", "err"); return; }
      const r = await post("auth", { action: "update", name, password: np });
      setStatus("ac-status", r.ok ? (name + " 密码已重置") : r.error, r.ok ? "ok" : "err");
      loadAccounts();
    } else if (ac === "del") {
      if (!confirm("确认删除账户 " + name + "？该操作不可撤销。")) return;
      const r = await post("auth", { action: "delete", name });
      setStatus("ac-status", r.ok ? (name + " 已删除") : r.error, r.ok ? "ok" : "err");
      loadAccounts();
    }
  };
}
$("ac-add").addEventListener("click", async () => {
  const name = $("ac-name").value.trim();
  const pwd = $("ac-pwd").value;
  if (!name || !pwd) { setStatus("ac-status", "用户名和密码都要填", "err"); return; }
  const r = await post("auth", { action: "register", name, password: pwd, display: $("ac-display").value.trim() });
  if (!r.ok) { setStatus("ac-status", r.error, "err"); return; }
  setStatus("ac-status", "已创建账户 " + name, "ok");
  $("ac-name").value = ""; $("ac-pwd").value = ""; $("ac-display").value = "";
  loadAccounts();
});
bindAccountOps();

