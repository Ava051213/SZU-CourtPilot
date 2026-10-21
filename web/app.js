const $ = (id) => document.getElementById(id);

const FALLBACK = {
  campuses: ["粤海校区", "丽湖校区"],
  sports: {
    粤海校区: ["羽毛球", "足球", "排球", "网球", "篮球", "壁球", "乒乓球", "游泳", "一楼重量型健身", "二楼有氧健身"],
    丽湖校区: ["羽毛球", "足球", "排球", "网球", "篮球", "乒乓球", "游泳", "匹克球", "桌球", "瑜伽"],
  },
  venues: {},
};

let currentConfig = null;
let options = FALLBACK;
let lastLogSeq = 0;
let serverSeq = 0;
let logCleared = false;
let busy = false;
let runnerActive = false;

let selectedSlots = [];
let selectedCourts = [];
let allSlots = [];
let allCourts = [];
let slotStatus = new Map();
let slotDate = "";
let slotsRequest = 0;
let courtsRequest = 0;
let slotsKey = "";

const FALLBACK_SLOTS = (() => {
  const out = [];
  for (let h = 8; h < 22; h += 1) {
    const a = String(h).padStart(2, "0");
    const b = String(h + 1).padStart(2, "0");
    out.push({ code: `${a}:00-${b}:00`, disabled: true, text: "状态未知", state: "" });
  }
  return out;
})();

// 场地编号是固定的（羽毛球：A1-D8 共 32 片），所以在目标日期还没开放、
// 接口拿不到场地列表时，仍然可以提前按这份清单选好偏好。
const COURT_PREFIX = { 羽毛球: "羽毛球场" };
const COURT_GROUPS = ["A", "B", "C", "D"];
const COURT_PER_GROUP = 8;

function fixedCourts() {
  const prefix = COURT_PREFIX[$("sport").value];
  if (!prefix) return [];
  const out = [];
  for (const group of COURT_GROUPS) {
    for (let i = 1; i <= COURT_PER_GROUP; i += 1) {
      out.push({ name: `${prefix}${group}${i}`, available: null, total: 0, venues: [] });
    }
  }
  return out;
}

function mergeCourts(list) {
  const base = fixedCourts();
  if (!base.length) return Array.isArray(list) ? list : [];
  const byName = new Map(base.map((x) => [x.name, x]));
  for (const item of Array.isArray(list) ? list : []) {
    const hit = byName.get(item.name);
    if (hit) {
      hit.available = item.available;
      hit.total = item.total || 0;
      hit.venues = item.venues || [];
    } else {
      byName.set(item.name, { ...item, available: item.available ?? null });
    }
  }
  return Array.from(byName.values());
}

function slotStateText(item) {
  if (!item) return "状态未知";
  if (item.text) return item.text;
  return item.disabled ? "暂不可约" : "可预约";
}

function normalizeSlotList(list) {
  return (Array.isArray(list) ? list : [])
    .map((x) => String(x || "").trim())
    .filter(Boolean);
}

function slotsQuery() {
  const params = new URLSearchParams({
    campus: $("campus").value,
    sport: $("sport").value,
    date_mode: $("dateMode").value,
    target_date: $("targetDate").value || "",
  });
  return params.toString();
}

async function api(path, opts = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data = null;
  try { data = await response.json(); } catch (_) { data = null; }
  if (!response.ok) {
    let detail = data && (data.detail || data.message);
    if (Array.isArray(detail)) detail = detail.map((x) => x.msg || x).join("；");
    const error = new Error(detail || `请求失败（HTTP ${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function splitList(value) {
  return String(value || "")
    .split(/[,，、]/)
    .map((x) => x.trim())
    .filter(Boolean);
}

function normalizeTime(value) {
  const v = String(value || "").trim();
  if (/^\d{2}:\d{2}$/.test(v)) return `${v}:00`;
  return v;
}

function setMessage(text, kind = "") {
  const el = $("formMessage");
  el.textContent = text || "";
  el.className = `form-message ${kind}`.trim();
}

function fillForm(config) {
  currentConfig = config;
  $("campus").value = config.campus || "粤海校区";
  syncSports(config.sport);
  $("sport").value = config.sport || "羽毛球";
  $("dateMode").value = config.date_mode || "tomorrow";
  $("targetDate").value = config.target_date || "";
  $("customDateWrap").hidden = $("dateMode").value !== "custom";
  selectedSlots = normalizeSlotList(config.time_slots);
  selectedCourts = normalizeSlotList(config.courts);
  $("strictCourts").checked = Boolean(config.strict_courts);
  $("venues").value = (config.venues || []).join(", ");
  $("scheduleTime").value = normalizeTime((config.schedule || {}).time || "12:30:00");
  $("advance").value = (config.schedule || {}).advance_seconds ?? 60;
  $("maxSeconds").value = (config.retry || {}).max_seconds ?? 90;
  $("webhook").value = (config.notify || {}).webhook_url || "";
  renderSelectedSlots();
  renderSelectedCourts();
  syncVenues();
  loadSlots();
}

function readForm() {
  const base = currentConfig || {};
  return {
    ...base,
    campus: $("campus").value,
    sport: $("sport").value,
    booking_type: "包场",
    date_mode: $("dateMode").value,
    target_date: $("targetDate").value,
    time_slots: [...selectedSlots],
    venues: splitList($("venues").value),
    courts: [...selectedCourts],
    strict_courts: $("strictCourts").checked,
    schedule: {
      ...(base.schedule || {}),
      enabled: true,
      time: normalizeTime($("scheduleTime").value),
      advance_seconds: Number($("advance").value) || 0,
      weekdays: (base.schedule && base.schedule.weekdays) || [1, 2, 3, 4, 5, 6, 7],
    },
    retry: {
      ...(base.retry || {}),
      max_seconds: Number($("maxSeconds").value) || 90,
      interval_ms: (base.retry && base.retry.interval_ms) || 250,
      request_timeout: (base.retry && base.retry.request_timeout) || 15,
    },
    notify: {
      ...(base.notify || {}),
      webhook_url: $("webhook").value.trim(),
    },
  };
}

async function loadConfig() {
  const data = await api("/api/config");
  fillForm(data.config);
  if (data.errors && data.errors.length) setMessage(data.errors.join("；"), "error");
}

async function loadOptions() {
  try {
    const data = await api("/api/options");
    options = data;
  } catch (_) {
    options = FALLBACK;
  }
  if (!options || !options.campuses || !options.campuses.length) options = FALLBACK;
  const campusSelect = $("campus");
  const previous = campusSelect.value;
  campusSelect.innerHTML = "";
  for (const name of options.campuses) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    campusSelect.appendChild(opt);
  }
  if (previous && options.campuses.includes(previous)) campusSelect.value = previous;
  syncSports($("sport").value);
  syncVenues();
}

function syncSports(preferred) {
  const campus = $("campus").value;
  const list = (options.sports && options.sports[campus]) || FALLBACK.sports[campus] || FALLBACK.sports["粤海校区"];
  const select = $("sport");
  const current = preferred || select.value;
  select.innerHTML = "";
  for (const name of list) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    select.appendChild(opt);
  }
  if (list.includes(current)) select.value = current;
  else if (list.length) select.value = list[0];
}

function syncVenues() {
  const key = `${$("campus").value}|${$("sport").value}`;
  const list = (options.venues && options.venues[key]) || [];
  const datalist = $("venueList");
  datalist.innerHTML = "";
  for (const name of list) {
    const opt = document.createElement("option");
    opt.value = name;
    datalist.appendChild(opt);
  }
  if (!list.length && $("sport").value === "羽毛球" && $("campus").value === "粤海校区") {
    const opt = document.createElement("option");
    opt.value = "运动广场东馆羽毛球场";
    datalist.appendChild(opt);
  }
  $("venues").placeholder = list[0] || "填写场馆名称，多个用逗号分隔";
}

function iconButton(label, title, onClick, disabled = false) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "icon-btn";
  btn.textContent = label;
  btn.title = title;
  btn.disabled = disabled;
  btn.addEventListener("click", onClick);
  return btn;
}

function courtMatches(selected, fullName) {
  const a = String(selected || "").trim();
  const b = String(fullName || "").trim();
  if (!a || !b) return false;
  return a === b || b.endsWith(a) || b.includes(`场${a}`);
}

function renderSelectedSlots() {
  const box = $("selectedSlots");
  box.innerHTML = "";
  if (!selectedSlots.length) {
    box.className = "priority-list empty";
    box.textContent = "尚未选择时间段";
    return;
  }
  box.className = "priority-list";
  selectedSlots.forEach((code, index) => {
    const row = document.createElement("div");
    row.className = "priority-item";

    const num = document.createElement("span");
    num.className = "priority-index";
    num.textContent = String(index + 1);

    const name = document.createElement("span");
    name.className = "priority-name";
    name.textContent = code;

    const status = document.createElement("span");
    status.className = "priority-status";
    status.textContent = slotStateText(slotStatus.get(code));

    const up = iconButton("↑", "提高优先级", () => moveSlot(index, -1), index === 0);
    const down = iconButton("↓", "降低优先级", () => moveSlot(index, 1), index === selectedSlots.length - 1);
    const remove = iconButton("×", "移除", () => toggleSlot(code));

    row.append(num, name, status, up, down, remove);
    box.appendChild(row);
  });
}

function renderSlotGrid() {
  const grid = $("slotGrid");
  grid.innerHTML = "";
  const source = allSlots.length ? allSlots : FALLBACK_SLOTS;
  for (const item of source) {
    const code = String(item.code || "");
    if (!code) continue;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "slot-chip";
    if (selectedSlots.includes(code)) btn.classList.add("selected");
    if (item.disabled) btn.classList.add("disabled");
    btn.addEventListener("click", () => toggleSlot(code));

    const name = document.createElement("span");
    name.textContent = code;
    const state = document.createElement("span");
    state.className = "state";
    state.textContent = slotStateText(item);
    btn.append(name, state);
    grid.appendChild(btn);
  }
}

function renderSelectedCourts() {
  const box = $("selectedCourts");
  const strict = $("strictCourts");
  strict.disabled = selectedCourts.length === 0;
  if (!selectedCourts.length) strict.checked = false;
  box.innerHTML = "";
  if (!selectedCourts.length) {
    box.className = "priority-list empty";
    box.textContent = "不限场地（任意空场）";
    return;
  }
  box.className = "priority-list";
  selectedCourts.forEach((name, index) => {
    const row = document.createElement("div");
    row.className = "priority-item";

    const num = document.createElement("span");
    num.className = "priority-index";
    num.textContent = String(index + 1);

    const label = document.createElement("span");
    label.className = "priority-name";
    label.textContent = name;

    const info = allCourts.find((x) => courtMatches(name, x.name));
    const status = document.createElement("span");
    status.className = "priority-status";
    const unknown = !info || info.available === null || info.available === undefined;
    status.textContent = unknown
      ? (info ? "未开放" : "状态未知")
      : (info.available > 0 ? `可约 ${info.available}` : "暂无空位");

    const up = iconButton("↑", "提高优先级", () => moveCourt(index, -1), index === 0);
    const down = iconButton("↓", "降低优先级", () => moveCourt(index, 1), index === selectedCourts.length - 1);
    const remove = iconButton("×", "移除", () => toggleCourt(name));

    row.append(num, label, status, up, down, remove);
    box.appendChild(row);
  });
}

function renderCourtGrid() {
  const grid = $("courtGrid");
  grid.innerHTML = "";
  if (!selectedSlots.length) {
    const note = document.createElement("div");
    note.className = "chip-loading";
    note.textContent = "先选择时间段，再选择偏好场地";
    grid.appendChild(note);
    return;
  }
  if (!allCourts.length) {
    const note = document.createElement("div");
    note.className = "chip-loading";
    note.textContent = "暂无场地数据；不选则默认任意空场";
    grid.appendChild(note);
    return;
  }
  for (const item of allCourts) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "slot-chip";
    if (selectedCourts.some((x) => courtMatches(x, item.name))) btn.classList.add("selected");
    const unknown = item.available === null || item.available === undefined;
    if (!unknown && item.available === 0) btn.classList.add("disabled");
    btn.addEventListener("click", () => toggleCourt(item.name));

    const name = document.createElement("span");
    name.textContent = item.name;
    const state = document.createElement("span");
    state.className = "state";
    state.textContent = unknown
      ? "未开放"
      : (item.available > 0 ? `可约 ${item.available}/${item.total}` : `暂无空位（${item.total}）`);
    btn.append(name, state);
    grid.appendChild(btn);
  }
}

function moveSlot(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= selectedSlots.length) return;
  [selectedSlots[index], selectedSlots[target]] = [selectedSlots[target], selectedSlots[index]];
  renderSelectedSlots();
  if (index === 0 || target === 0) loadCourts();
}

function moveCourt(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= selectedCourts.length) return;
  [selectedCourts[index], selectedCourts[target]] = [selectedCourts[target], selectedCourts[index]];
  renderSelectedCourts();
}

function toggleSlot(code) {
  const index = selectedSlots.indexOf(code);
  if (index >= 0) selectedSlots.splice(index, 1);
  else selectedSlots.push(code);
  renderSelectedSlots();
  renderSlotGrid();
  loadCourts();
}

function toggleCourt(name) {
  const index = selectedCourts.findIndex((x) => courtMatches(x, name));
  if (index >= 0) selectedCourts.splice(index, 1);
  else selectedCourts.push(name);
  renderSelectedCourts();
  renderCourtGrid();
}

async function loadSlots(force = false, silent = false) {
  const key = slotsQuery();
  if (!force && key === slotsKey && allSlots.length) {
    loadCourts();
    return;
  }
  slotsKey = key;
  const request = ++slotsRequest;
  if (!silent) {
    $("slotGrid").innerHTML = '<div class="chip-loading">正在读取时间段……</div>';
    $("slotHint").className = "picker-hint";
    $("slotHint").textContent = "正在同步实时状态……";
  }
  try {
    const data = await api(`/api/slots?${key}`);
    if (request !== slotsRequest) return;
    slotDate = data.date || "";
    allSlots = Array.isArray(data.slots) ? data.slots : [];
    if (!allSlots.length) allSlots = FALLBACK_SLOTS.slice();
    for (const code of selectedSlots) {
      if (!allSlots.some((x) => x.code === code)) {
        allSlots.push({ code, disabled: true, text: "已选（状态未知）", state: "" });
      }
    }
    slotStatus = new Map(allSlots.map((x) => [x.code, x]));
    $("slotHint").className = "picker-hint";
    $("slotHint").textContent = data.open === false
      ? `${data.date} 尚未开放，可先选好时间段等待开放`
      : `${data.date} 实时状态已更新`;
  } catch (err) {
    if (request !== slotsRequest) return;
    slotDate = "";
    allSlots = FALLBACK_SLOTS.slice();
    for (const code of selectedSlots) {
      if (!allSlots.some((x) => x.code === code)) {
        allSlots.push({ code, disabled: true, text: "已选（状态未知）", state: "" });
      }
    }
    slotStatus = new Map();
    if (!silent) {
      $("slotHint").className = "picker-hint error";
      $("slotHint").textContent =
        err.status === 401 ? "登录已失效，请重新登录" : `实时状态读取失败：${err.message}`;
    }
  }
  renderSelectedSlots();
  renderSlotGrid();
  loadCourts(silent);
}

async function loadCourts(silent = false) {
  const request = ++courtsRequest;
  if (!selectedSlots.length) {
    allCourts = [];
    $("courtGrid").innerHTML = '<div class="chip-loading">先选择时间段，再选择偏好场地</div>';
    $("courtHint").textContent = "";
    renderSelectedCourts();
    return;
  }
  const slot = selectedSlots.find((code) => {
    const info = slotStatus.get(code);
    return info && !info.disabled;
  }) || selectedSlots[0];
  if (!slotDate) {
    allCourts = fixedCourts();
    $("courtHint").textContent = allCourts.length
      ? "目标日期未开放，以下为固定场地清单，可按顺序先选好"
      : "暂时拿不到场地列表；不选则默认任意空场";
    renderSelectedCourts();
    renderCourtGrid();
    return;
  }
  if (!silent) {
    $("courtGrid").innerHTML = '<div class="chip-loading">正在读取场地……</div>';
    $("courtHint").textContent = "";
  }
  try {
    const params = new URLSearchParams({
      campus: $("campus").value,
      sport: $("sport").value,
      date: slotDate,
      slot,
    });
    const data = await api(`/api/courts?${params.toString()}`);
    if (request !== courtsRequest) return;
    allCourts = mergeCourts(data.courts);
    if (data.open === false) {
      $("courtHint").textContent = `${data.date} 尚未开放，以下为固定场地清单，可按顺序先选好`;
    } else {
      $("courtHint").textContent = allCourts.length
        ? `${slotDate} ${slot}：共 ${allCourts.length} 个场地，可按顺序选择偏好；暂无空位的场地也可先选`
        : "该时间段暂无场地数据";
    }
  } catch (err) {
    if (request !== courtsRequest) return;
    allCourts = fixedCourts();
    if (!silent) {
      $("courtHint").textContent =
        err.status === 409
          ? `${slotDate} 暂未开放预约，以下为固定场地清单，可按顺序先选好`
          : `场地列表读取失败：${err.message}`;
    }
  }
  renderSelectedCourts();
  renderCourtGrid();
}

async function saveConfig(showOk = true) {
  const config = readForm();
  const data = await api("/api/config", {
    method: "POST",
    body: JSON.stringify({ data: config }),
  });
  currentConfig = data.config;
  if (showOk) setMessage("配置已保存", "ok");
  return data.config;
}

async function start(mode) {
  if (busy) return;
  busy = true;
  setMessage("正在启动……");
  try {
    await saveConfig(false);
    const data = await api("/api/run", {
      method: "POST",
      body: JSON.stringify({
        mode,
        dry_run: mode === "once" && $("dryRun").checked,
      }),
    });
    setMessage(
      mode === "schedule"
        ? "已开始定时任务"
        : ($("dryRun").checked ? "已开始演练（不会提交）" : "已开始立即尝试"),
      "ok"
    );
    await pollStatus();
  } catch (err) {
    setMessage(err.message, "error");
  } finally {
    busy = false;
    await pollStatus();
  }
}

async function login() {
  try {
    await api("/api/login", { method: "POST" });
    setMessage("登录窗口已打开，请在 Chrome 中完成登录", "ok");
    await pollStatus();
  } catch (err) {
    setMessage(err.message, "error");
  }
}

async function checkSession() {
  setMessage("正在检测登录……");
  try {
    const data = await api("/api/session/check", { method: "POST" });
    renderSession(data);
    setMessage(data.logged_in ? "登录有效" : (data.message || "未登录"), data.logged_in ? "ok" : "error");
  } catch (err) {
    setMessage(err.message, "error");
  }
}

async function stop() {
  try {
    await api("/api/stop", { method: "POST" });
    setMessage("已发送停止指令", "ok");
  } catch (err) {
    setMessage(err.message, "error");
  }
  await pollStatus();
}

function renderSession(session) {
  const chip = $("sessionChip");
  const text = $("sessionText");
  if (session && session.logged_in) {
    chip.className = "chip ok";
    chip.innerHTML = "<i></i>已登录";
    text.textContent = `${session.name || "同学"}（${session.user_id || ""}）· 会话有效`;
  } else {
    chip.className = "chip bad";
    chip.innerHTML = "<i></i>未登录";
    const msg = session && session.message ? `（${session.message}）` : "";
    text.textContent = `未检测到有效会话${msg}`;
  }
}

function renderStatus(data) {
  runnerActive = Boolean(data.running);
  const runner = $("runnerChip");
  if (data.running) {
    runner.className = "chip busy";
    runner.innerHTML = `<i></i>${data.phase || "运行中"}`;
  } else {
    runner.className = "chip muted";
    runner.innerHTML = "<i></i>空闲";
  }
  $("stopBtn").disabled = !data.running;
  $("onceBtn").disabled = data.running;
  $("scheduleBtn").disabled = data.running;
  $("loginBtn").disabled = data.running || (data.login && data.login.running);

  if (data.session) renderSession(data.session);
  $("nextRun").textContent = `下次执行：${data.next_run || "--"}`;
  $("clock").textContent = data.now || "--:--:--";

  const result = data.last_result;
  const banner = $("resultBanner");
  if (result && result.success && !result.dry_run) {
    banner.hidden = false;
    $("resultText").textContent = result.message || "请在 45 分钟内完成支付。";
  } else {
    banner.hidden = true;
  }

  if (data.login && data.login.running) {
    $("sessionChip").className = "chip busy";
    $("sessionChip").innerHTML = "<i></i>等待登录";
    $("sessionText").textContent = data.login.message || "请在 Chrome 窗口中登录……";
  }
}

async function pollStatus() {
  try {
    renderStatus(await api("/api/status"));
  } catch (_) { /* transient */ }
}

function appendLogs(items) {
  if (!items || !items.length) return;
  const box = $("log");
  if (logCleared && lastLogSeq === 0) {
    box.textContent = "";
    logCleared = false;
  }
  if (box.textContent === "等待操作……") box.textContent = "";
  const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  for (const item of items) {
    const line = document.createElement("div");
    const cls = /失败|错误|异常|超时|无效/.test(item.text) ? "err"
      : /成功|已保存|登录有效|时间到/.test(item.text) ? "ok"
      : /等待|尚未|暂不/.test(item.text) ? "warn" : "";
    line.className = cls;
    const time = document.createElement("span");
    time.className = "t";
    time.textContent = `[${item.time}] `;
    line.appendChild(time);
    line.appendChild(document.createTextNode(item.text));
    box.appendChild(line);
  }
  if (stick) box.scrollTop = box.scrollHeight;
}

async function pollLogs() {
  try {
    const data = await api(`/api/logs?after=${lastLogSeq}`);
    serverSeq = data.seq || serverSeq;
    if (data.logs && data.logs.length) {
      lastLogSeq = data.logs[data.logs.length - 1].seq;
      appendLogs(data.logs);
    }
  } catch (_) { /* transient */ }
}

function clearLogs() {
  $("log").textContent = "";
  lastLogSeq = serverSeq;
  logCleared = true;
}

function bind() {
  $("campus").addEventListener("change", () => {
    syncSports();
    syncVenues();
    loadSlots(true);
  });
  $("sport").addEventListener("change", () => {
    syncVenues();
    loadSlots(true);
  });
  $("dateMode").addEventListener("change", () => {
    $("customDateWrap").hidden = $("dateMode").value !== "custom";
    if ($("dateMode").value !== "custom" || $("targetDate").value) loadSlots(true);
  });
  $("targetDate").addEventListener("change", () => loadSlots(true));
  $("refreshSlots").addEventListener("click", () => loadSlots(true));
  $("refreshCourts").addEventListener("click", () => loadCourts());
  $("clearSlots").addEventListener("click", () => {
    selectedSlots = [];
    selectedCourts = [];
    renderSelectedSlots();
    renderSlotGrid();
    renderSelectedCourts();
    renderCourtGrid();
    loadCourts();
  });
  $("clearCourts").addEventListener("click", () => {
    selectedCourts = [];
    renderSelectedCourts();
    renderCourtGrid();
  });
  $("loginBtn").addEventListener("click", login);
  $("checkBtn").addEventListener("click", checkSession);
  $("saveBtn").addEventListener("click", async () => {
    try { await saveConfig(); } catch (err) { setMessage(err.message, "error"); }
  });
  $("onceBtn").addEventListener("click", () => start("once"));
  $("scheduleBtn").addEventListener("click", () => start("schedule"));
  $("stopBtn").addEventListener("click", stop);
  $("clearLog").addEventListener("click", clearLogs);
}

async function init() {
  bind();
  try {
    await loadOptions();
    await loadConfig();
  } catch (err) {
    setMessage(`初始化失败：${err.message}`, "error");
  }
  await pollStatus();
  await pollLogs();
  setInterval(pollStatus, 1000);
  setInterval(pollLogs, 1000);
}

init();
