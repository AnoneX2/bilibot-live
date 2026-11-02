/* bilibot 设置面板：原生 JS，无构建步骤 */

const state = {
  schema: null,
  config: null,
  rooms: [],
  roomMeta: {},
  prompts: { dir: "", system: "", user: "" },
  activePrompt: "system",
  promptDir: "",
  promptKind: "default",
  profiles: [],
  dirty: false,
  saving: false,
  apiKeyMasked: "",
};

/* ------------------------------------------------------------------ 工具 */

function $(sel, root = document) {
  return root.querySelector(sel);
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function getPath(obj, path) {
  return path.split(".").reduce((cur, key) => (cur == null ? undefined : cur[key]), obj);
}

function setPath(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (typeof cur[parts[i]] !== "object" || cur[parts[i]] === null) cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}

function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

/* 机密字段（API Key）：浏览器不保留明文，输入框留空 = 保持原值。
   后端只回掩码（sk-abc…wxyz），这里统一清空，避免把人家的掩码再提交回去。 */
const SECRET_KEYS = ["llm.api_key"];

function scrubSecrets(cfg) {
  if (!cfg) return cfg;
  SECRET_KEYS.forEach((key) => setPath(cfg, key, ""));
  return cfg;
}

function applyKeyMeta(data) {
  state.apiKeyMasked = data.api_key_masked || "";
}

/* 保存/加载后清空输入框（明文不留在 DOM 里），只留掩码占位提示 */
function refreshSecretFields() {
  document.querySelectorAll('input[data-key="llm.api_key"]').forEach((input) => {
    input.value = "";
    input.placeholder = state.apiKeyMasked
      ? `${state.apiKeyMasked}（已保存到配置文件，留空表示不修改）`
      : "未设置：粘贴 API Key 后点「保存配置」";
  });
}

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = "toast" + (isError ? " error" : "");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.add("hidden"), 3800);
}

function banner(html, isError = false) {
  const node = $("#banner");
  node.innerHTML = html;
  node.className = "banner" + (isError ? " error" : "");
  node.classList.remove("hidden");
  node.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function clearBanner() {
  $("#banner").classList.add("hidden");
}

function markDirty() {
  state.dirty = true;
  $("#dirtyFlag").classList.remove("hidden");
  refreshDependencies();
}

function dependencyMet(dep) {
  const current = getPath(state.config, dep.key);
  if (typeof dep.value === "boolean") return Boolean(current) === dep.value;
  return String(current) === String(dep.value);
}

function refreshDependencies() {
  document.querySelectorAll(".field[data-depends-on]").forEach((node) => {
    try {
      const met = dependencyMet(JSON.parse(node.dataset.dependsOn));
      node.hidden = !met;
      // 同时写内联样式：即使浏览器用了旧 CSS（.field 可能是 flex），也能真正隐藏
      node.style.display = met ? "" : "none";
    } catch (err) {
      /* 元数据坏了就保持显示，不影响其它字段 */
    }
  });
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = null;
  try {
    data = await res.json();
  } catch (err) {
    throw new Error(`HTTP ${res.status}：响应不是 JSON`);
  }
  if (!res.ok || data.ok === false) {
    const message = data.message || `请求失败（HTTP ${res.status}）`;
    const error = new Error(message);
    error.payload = data;
    throw error;
  }
  return data;
}

/* ------------------------------------------------------------------ 字段渲染 */

function suffix(field) {
  return field.unit ? `（${field.unit}）` : "";
}

function renderLabel(field) {
  const label = el("div", "label");
  const name = el("span", "name", field.label);
  const key = el("span", "key", field.key);
  const meta = el("span", "meta-line");
  if (field.advanced) meta.appendChild(el("span", "unit", "高级"));
  label.append(name, key, meta);
  return label;
}

function resetButton(field) {
  const btn = el("button", "reset", "恢复默认");
  btn.type = "button";
  btn.title = `默认值：${JSON.stringify(field.default)}`;
  btn.addEventListener("click", () => {
    applyDefault(field);
  });
  return btn;
}

function applyDefault(field) {
  const value = deepClone(field.default);
  setPath(state.config, field.key, value);
  markDirty();
  const wrap = document.querySelector(`.field[data-key="${field.key}"]`);
  const control = wrap ? wrap.querySelector("input, select, textarea") : null;
  if (control) {
    renderControlValue(control, field, value);
  }
}

function renderControlValue(control, field, value) {
  if (field.type === "bool") {
    control.checked = Boolean(value);
    const label = control.closest(".switch");
    if (label) label.querySelector(".state").textContent = control.checked ? "已开启" : "已关闭";
  } else if (field.type === "list_str") {
    control.value = (value || []).join("\n");
  } else if (field.type === "dict") {
    control.value = JSON.stringify(value || {}, null, 2);
  } else if (field.type === "secret") {
    control.value = "";
  } else {
    control.value = value === null || value === undefined ? "" : String(value);
  }
}

function bindFieldControl(control, field) {
  const commit = (value) => {
    setPath(state.config, field.key, value);
    markDirty();
  };

  if (field.type === "bool") {
    control.addEventListener("change", () => {
      commit(control.checked);
      const label = control.closest(".switch");
      if (label) label.querySelector(".state").textContent = control.checked ? "已开启" : "已关闭";
      // 演练模式是「安全性」开关，不适合忘了保存：直接自动存盘并让 BOT 重新加载
      if (field.key === "app.dry_run") scheduleAutoSave();
    });
    return;
  }

  if (field.type === "list_str") {
    control.addEventListener("change", () => {
      commit(control.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean));
    });
    return;
  }

  if (field.type === "dict") {
    control.addEventListener("change", () => {
      const text = control.value.trim();
      if (!text) {
        commit({});
        control.classList.remove("invalid");
        return;
      }
      try {
        const parsed = JSON.parse(text);
        if (typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("必须是对象");
        commit(parsed);
        control.classList.remove("invalid");
        control.title = "";
      } catch (err) {
        control.classList.add("invalid");
        control.title = `JSON 格式错误：${err.message}`;
        toast("额外请求参数不是合法 JSON，保存前请修正", true);
      }
    });
    return;
  }

  if (field.type === "int" || field.type === "float") {
    const commitNumber = () => {
      let value = field.type === "int" ? parseInt(control.value, 10) : parseFloat(control.value);
      if (Number.isNaN(value)) return;
      if (field.min !== null && field.min !== undefined) value = Math.max(field.min, value);
      if (field.max !== null && field.max !== undefined) value = Math.min(field.max, value);
      commit(value);
    };
    control.addEventListener("input", commitNumber);
    control.addEventListener("change", () => {
      commitNumber();
      const value = field.type === "int" ? parseInt(control.value, 10) : parseFloat(control.value);
      if (!Number.isNaN(value)) control.value = String(value);
    });
    return;
  }

  if (field.type === "enum") {
    control.addEventListener("change", () => {
      const raw = control.value;
      const numeric = field.options.length && typeof field.options[0].value === "number";
      commit(numeric ? Number(raw) : raw);
    });
    return;
  }

  const commitText = () => commit(control.value);
  control.addEventListener("input", commitText);
  control.addEventListener("change", commitText);
}

function renderField(field, value) {
  const wrap = el("div", "field");
  wrap.dataset.key = field.key;
  if (field.depends_on) {
    wrap.dataset.dependsOn = JSON.stringify(field.depends_on);
    const met = dependencyMet(field.depends_on);
    wrap.hidden = !met;
    wrap.style.display = met ? "" : "none";
  }

  const label = renderLabel(field);
  const control = el("div", "control");
  let input;

  if (field.type === "bool") {
    const labelEl = el("label", "switch");
    input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.key = field.key;
    input.checked = Boolean(value);
    const track = el("span", "track");
    const stateText = el("span", "state", input.checked ? "已开启" : "已关闭");
    labelEl.append(input, track, stateText);
    control.append(labelEl);
    bindFieldControl(input, field);
  } else if (field.type === "enum") {
    input = document.createElement("select");
    input.dataset.key = field.key;
    field.options.forEach((opt) => {
      const option = document.createElement("option");
      option.value = String(opt.value);
      option.textContent = opt.label;
      input.appendChild(option);
    });
    input.value = value === null || value === undefined ? "" : String(value);
    control.append(input);
    bindFieldControl(input, field);
  } else if (field.type === "list_str") {
    input = document.createElement("textarea");
    input.dataset.key = field.key;
    input.rows = 3;
    input.placeholder = "一行一个，留空表示不限制";
    input.value = (value || []).join("\n");
    control.append(input);
    bindFieldControl(input, field);
  } else if (field.type === "dict") {
    input = document.createElement("textarea");
    input.dataset.key = field.key;
    input.rows = 3;
    input.placeholder = '例如 {"thinking":{"type":"disabled"}}';
    input.value = JSON.stringify(value || {}, null, 2);
    control.append(input);
    bindFieldControl(input, field);
  } else if (field.type === "int" || field.type === "float") {
    const row = el("div", "input-row");
    input = document.createElement("input");
    input.type = "number";
    input.dataset.key = field.key;
    if (field.min !== null && field.min !== undefined) input.min = field.min;
    if (field.max !== null && field.max !== undefined) input.max = field.max;
    if (field.step !== null && field.step !== undefined) input.step = field.step;
    else input.step = field.type === "float" ? "0.01" : "1";
    input.value = value === null || value === undefined ? "" : String(value);
    row.append(input);
    if (field.unit) row.append(el("span", "unit", field.unit));
    control.append(row);
    bindFieldControl(input, field);
  } else if (field.type === "secret") {
    input = document.createElement("input");
    input.type = "password";
    input.dataset.key = field.key;
    input.autocomplete = "new-password";
    input.spellcheck = false;
    input.value = "";
    const saved = field.key === "llm.api_key" ? state.apiKeyMasked : "";
    input.placeholder = saved
      ? `${saved}（已保存，留空表示不修改）`
      : "未设置：粘贴 API Key";

    if (field.key === "llm.api_key") {
      const row = el("div", "input-row");
      const btnSave = el("button", "btn primary small", "保存 Key");
      const btnTest = el("button", "btn ghost small", "测试连通");
      btnSave.type = "button";
      btnTest.type = "button";
      const status = el("span", "unit", "");
      row.append(input, btnSave, btnTest);
      control.append(row, status);
      bindFieldControl(input, field);

      btnSave.addEventListener("click", async () => {
        const value = input.value.trim();
        if (!value) {
          status.textContent = "先粘贴 Key 再点保存";
          status.style.color = "var(--warn)";
          return;
        }
        btnSave.disabled = true;
        status.textContent = "保存中…";
        status.style.color = "var(--muted)";
        try {
          const data = await api("/api/llm/key", {
            method: "POST",
            body: JSON.stringify({ api_key: value }),
          });
          state.apiKeyMasked = data.api_key_masked || "";
          input.value = "";
          input.placeholder = `${state.apiKeyMasked}（已保存，留空表示不修改）`;
          setPath(state.config, field.key, "");
          status.textContent = "已保存到配置文件";
          status.style.color = "var(--accent)";
          toast("API Key 已保存");
        } catch (err) {
          status.textContent = err.message;
          status.style.color = "var(--danger)";
          toast(err.message, true);
        } finally {
          btnSave.disabled = false;
        }
      });

      btnTest.addEventListener("click", async () => {
        btnTest.disabled = true;
        status.textContent = "测试中…（最长 30 秒）";
        status.style.color = "var(--muted)";
        try {
          const data = await api("/api/llm/test", {
            method: "POST",
            body: JSON.stringify({ api_key: input.value.trim() }),
          });
          status.textContent = `连接正常 · ${data.model} · ${data.elapsed}s · 回复「${data.reply || ""}」`;
          status.style.color = "var(--accent)";
        } catch (err) {
          status.textContent = `连接失败：${err.message}`;
          status.style.color = "var(--danger)";
        } finally {
          btnTest.disabled = false;
        }
      });
    } else {
      control.append(input);
      bindFieldControl(input, field);
    }
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.dataset.key = field.key;
    input.value = value === null || value === undefined ? "" : String(value);
    control.append(input);
    bindFieldControl(input, field);
  }

  const help = el("div", "help", field.help);
  const reset = resetButton(field);
  help.appendChild(document.createTextNode(" "));
  help.appendChild(reset);

  wrap.append(label, control, help);
  return wrap;
}

/* ------------------------------------------------------------------ 分组 */

function showPromptModal(title, content) {
  let overlay = document.getElementById("promptModal");
  if (!overlay) {
    overlay = el("div", "modal hidden");
    overlay.id = "promptModal";
    const box = el("div", "modal-box");
    const head = el("div", "modal-head");
    const titleEl = el("h3", null, "");
    titleEl.id = "promptModalTitle";
    const actions = el("div", "modal-actions");
    const copyBtn = el("button", "btn ghost small", "复制");
    copyBtn.type = "button";
    const closeBtn = el("button", "btn ghost small", "关闭");
    closeBtn.type = "button";
    actions.append(copyBtn, closeBtn);
    head.append(titleEl, actions);
    const body = document.createElement("pre");
    body.id = "promptModalBody";
    box.append(head, body);
    overlay.appendChild(box);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) overlay.classList.add("hidden");
    });
    closeBtn.addEventListener("click", () => overlay.classList.add("hidden"));
    copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(body.textContent || "");
        toast("已复制到剪贴板");
      } catch (err) {
        toast("复制失败，请手动选择文本", true);
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") overlay.classList.add("hidden");
    });
    document.body.appendChild(overlay);
  }
  document.getElementById("promptModalTitle").textContent = title;
  document.getElementById("promptModalBody").textContent = content;
  overlay.classList.remove("hidden");
}

function renderRoomCell(td, room) {
  td.innerHTML = "";
  td.appendChild(el("span", "mono", String(room.room_id)));
  const meta = state.roomMeta[room.room_id];
  const name = meta && meta.uname ? meta.uname : meta && meta.uid ? `UID ${meta.uid}` : "—";
  const live = meta ? (meta.live_status === 1 ? " · 直播中" : " · 未开播") : "";
  td.appendChild(el("span", "room-title", `${name}${live}`));
}

/* 页面打开时补齐已有房间的主播 ID（resolve 接口顺带把房间号归一化） */
async function prefillRoomMeta() {
  const missing = state.rooms.filter((r) => !state.roomMeta[r.room_id]).slice(0, 30);
  if (!missing.length) return;
  await Promise.allSettled(
    missing.map(async (room) => {
      try {
        const data = await api("/api/rooms/resolve", {
          method: "POST",
          body: JSON.stringify({ room_id: String(room.room_id) }),
        });
        state.roomMeta[room.room_id] = data.room;
        document
          .querySelectorAll(`td[data-room-cell="${room.room_id}"]`)
          .forEach((td) => renderRoomCell(td, room));
      } catch (err) {
        /* 未开播/被风控时保留「—」，不打扰用户 */
      }
    })
  );
}

function groupRooms(group) {
  const card = el("section", "card");
  card.id = `group-${group.id}`;

  const title = el("h2", null, group.title);
  const desc = el("p", "desc", group.desc);
  card.append(title, desc);

  const table = el("table", "rooms-table");
  const thead = el("thead");
  const headRow = el("tr");
  ["启用", "房间号 / 主播", "别名", "独立提示词目录", "Prompt", ""].forEach((text) => {
    headRow.appendChild(el("th", null, text));
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = el("tbody");
  if (!state.rooms.length) {
    const row = el("tr");
    const cell = el("td", "hint", "还没有直播间，在下面添加一个吧。");
    cell.colSpan = 6;
    row.appendChild(cell);
    tbody.appendChild(row);
  }

  state.rooms.forEach((room, index) => {
    const row = el("tr");

    const tdEnabled = el("td");
    const sw = el("label", "switch");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = Boolean(room.enabled);
    cb.addEventListener("change", () => {
      room.enabled = cb.checked;
      const cell = row.querySelector("td[data-room-cell]");
      if (cell) cell.classList.toggle("disabled", !cb.checked);
      markDirty();
      scheduleAutoSave();
    });
    const track = el("span", "track");
    sw.append(cb, track);
    tdEnabled.appendChild(sw);
    row.appendChild(tdEnabled);

    const tdId = el("td");
    tdId.dataset.roomCell = String(room.room_id);
    if (!room.enabled) tdId.classList.add("disabled");
    renderRoomCell(tdId, room);
    row.appendChild(tdId);

    const tdAlias = el("td");
    const aliasInput = document.createElement("input");
    aliasInput.type = "text";
    aliasInput.placeholder = "可选";
    aliasInput.value = room.alias || "";
    aliasInput.addEventListener("input", () => {
      room.alias = aliasInput.value;
      markDirty();
    });
    tdAlias.appendChild(aliasInput);
    row.appendChild(tdAlias);

    const tdPrompt = el("td");
    const promptSelect = document.createElement("select");
    promptSelect.style.maxWidth = "220px";
    const defaultOpt = document.createElement("option");
    defaultOpt.value = "";
    defaultOpt.textContent = "默认人设";
    promptSelect.appendChild(defaultOpt);
    (state.profiles || []).forEach((profile) => {
      if (!profile.id) return;
      const opt = document.createElement("option");
      opt.value = profile.id;
      opt.textContent = profile.label;
      promptSelect.appendChild(opt);
    });
    if (room.prompt_dir && !(state.profiles || []).some((p) => p.id === room.prompt_dir)) {
      const opt = document.createElement("option");
      opt.value = room.prompt_dir;
      opt.textContent = `${room.prompt_dir}（未找到）`;
      promptSelect.appendChild(opt);
    }
    promptSelect.value = room.prompt_dir || "";
    promptSelect.addEventListener("change", () => {
      room.prompt_dir = promptSelect.value;
      markDirty();
    });
    tdPrompt.appendChild(promptSelect);
    row.appendChild(tdPrompt);

    const tdPreview = el("td");
    const previewBtn = el("button", "btn ghost small", "预览 Prompt");
    previewBtn.type = "button";
    previewBtn.title = "用该房间的人设 + 别名渲染一份示例 Prompt";
    previewBtn.addEventListener("click", async () => {
      previewBtn.disabled = true;
      previewBtn.textContent = "加载中…";
      try {
        const params = new URLSearchParams({
          room_id: String(room.room_id),
          dir: room.prompt_dir || "",
          alias: room.alias || "",
        });
        const data = await api(`/api/prompt/preview?${params.toString()}`);
        const title = `房间 ${room.room_id}${room.alias ? `（${room.alias}）` : ""} · ${data.dir || "默认人设"}`;
        showPromptModal(
          title,
          `=== system.md ===\n${data.system}\n\n=== user.md（示例渲染） ===\n${data.user}`
        );
      } catch (err) {
        toast(err.message, true);
      } finally {
        previewBtn.disabled = false;
        previewBtn.textContent = "预览 Prompt";
      }
    });
    tdPreview.appendChild(previewBtn);
    row.appendChild(tdPreview);

    const tdOps = el("td");
    const del = el("button", "btn danger small", "删除");
    del.type = "button";
    del.addEventListener("click", () => {
      if (!confirm(`确定移除直播间 ${room.room_id}？`)) return;
      state.rooms.splice(index, 1);
      markDirty();
      renderAll();
      scheduleAutoSave();
    });
    tdOps.appendChild(del);
    row.appendChild(tdOps);

    tbody.appendChild(row);
  });

  table.appendChild(tbody);
  card.appendChild(table);

  const adder = el("div", "add-room");
  const idInput = document.createElement("input");
  idInput.type = "text";
  idInput.placeholder = "直播间号，如 123456";
  const aliasInput = document.createElement("input");
  aliasInput.type = "text";
  aliasInput.placeholder = "别名（可选）";
  const addBtn = el("button", "btn primary small", "查询并添加");
  addBtn.type = "button";
  const refreshBtn = el("button", "btn ghost small", "刷新房间信息");
  refreshBtn.type = "button";
  const hint = el("span", "hint", "添加时会调用 B 站接口校验房间是否存在");
  adder.append(idInput, aliasInput, addBtn, refreshBtn, hint);
  card.appendChild(adder);

  refreshBtn.addEventListener("click", async () => {
    refreshBtn.disabled = true;
    refreshBtn.textContent = "查询中…";
    for (const room of state.rooms) {
      try {
        const data = await api("/api/rooms/resolve", {
          method: "POST",
          body: JSON.stringify({ room_id: String(room.room_id) }),
        });
        state.roomMeta[data.room.room_id] = data.room;
      } catch (err) {
        /* 单个房间失败不影响其他房间 */
      }
    }
    refreshBtn.disabled = false;
    refreshBtn.textContent = "刷新房间信息";
    renderAll();
  });

  addBtn.addEventListener("click", async () => {
    const raw = idInput.value.trim();
    if (!/^\d+$/.test(raw)) {
      toast("房间号必须是数字", true);
      return;
    }
    if (state.rooms.some((r) => String(r.room_id) === raw)) {
      toast("该房间已在列表里", true);
      return;
    }
    addBtn.disabled = true;
    addBtn.textContent = "查询中…";
    try {
      const data = await api("/api/rooms/resolve", {
        method: "POST",
        body: JSON.stringify({ room_id: raw }),
      });
      const room = data.room;
      state.roomMeta[room.room_id] = room;
      state.rooms.push({
        room_id: room.room_id,
        alias: aliasInput.value.trim() || room.uname || "",
        enabled: true,
        prompt_dir: "",
      });
      markDirty();
      renderAll();
      scheduleAutoSave();
      toast(`已添加：${room.uname || room.room_id}（${room.live_status === 1 ? "直播中" : "未开播"}）`);
    } catch (err) {
      toast(err.message, true);
    } finally {
      addBtn.disabled = false;
      addBtn.textContent = "查询并添加";
    }
  });

  return card;
}

function groupFields(group) {
  const card = el("section", "card");
  card.id = `group-${group.id}`;
  card.append(el("h2", null, group.title), el("p", "desc", group.desc));

  // app.dry_run 挪到「运行控制」卡片里（启动按钮旁边），这里不再重复渲染
  const normal = group.fields.filter((f) => f.type !== "rooms" && !f.advanced && f.key !== "app.dry_run");
  const advanced = group.fields.filter((f) => f.type !== "rooms" && f.advanced && f.key !== "app.dry_run");

  normal.forEach((field) => {
    card.appendChild(renderField(field, getPath(state.config, field.key)));
  });

  if (advanced.length) {
    const details = document.createElement("details");
    details.className = "advanced";
    const summary = document.createElement("summary");
    summary.textContent = `高级参数（${advanced.length} 项，一般不用改）`;
    details.appendChild(summary);
    advanced.forEach((field) => {
      details.appendChild(renderField(field, getPath(state.config, field.key)));
    });
    card.appendChild(details);
  }
  return card;
}

/* ------------------------------------------------------------------ 提示词 */

function promptSection() {
  const card = el("section", "card");
  card.id = "group-prompts";
  card.append(
    el("h2", null, "提示词（人设 / 语气）"),
    el("p", "desc", "可以有多套人设：prompts/*.md 单文件，或 prompts/<名字>/system.md 目录。保存后运行中的 BOT 会自动重新加载。")
  );

  const bar = el("div", "input-row");
  const dirSelect = document.createElement("select");
  dirSelect.style.maxWidth = "320px";
  (state.profiles || []).forEach((profile) => {
    const opt = document.createElement("option");
    opt.value = profile.id;
    opt.textContent = profile.label;
    dirSelect.appendChild(opt);
  });
  if (state.promptDir && !(state.profiles || []).some((p) => p.id === state.promptDir)) {
    const opt = document.createElement("option");
    opt.value = state.promptDir;
    opt.textContent = `${state.promptDir}（未找到）`;
    dirSelect.appendChild(opt);
  }
  dirSelect.value = state.promptDir || "";
  dirSelect.addEventListener("change", () => {
    state.promptDir = dirSelect.value;
    api("/api/ui-state", {
      method: "POST",
      body: JSON.stringify({ prompt_dir: dirSelect.value }),
    }).catch(() => {});
    loadPrompts(state.promptDir);
  });
  const reload = el("button", "btn ghost small", "重新读取");
  reload.type = "button";
  reload.addEventListener("click", () => loadPrompts(dirSelect.value));
  const preview = el("button", "btn ghost small", "预览实际 Prompt");
  preview.type = "button";
  preview.addEventListener("click", () => previewPrompt(dirSelect.value));
  const create = el("button", "btn ghost small", "新建人设");
  create.type = "button";
  bar.append(el("span", "detail", "人设："), dirSelect, reload, preview, create);
  card.appendChild(bar);

  const createForm = el("div", "create-profile hidden");
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.placeholder = "人设名，如 persona / 午夜电台";
  nameInput.maxLength = 32;
  const kindSelect = document.createElement("select");
  [["file", "单文件（*.md，共用 user.md）"], ["dir", "目录（system.md + user.md 分开）"]].forEach(
    (item) => {
      const opt = document.createElement("option");
      opt.value = item[0];
      opt.textContent = item[1];
      kindSelect.appendChild(opt);
    }
  );
  const copyLabel = el("label", "inline-check");
  const copyCheck = document.createElement("input");
  copyCheck.type = "checkbox";
  copyCheck.checked = true;
  copyLabel.append(copyCheck, document.createTextNode(" 复制当前人设内容"));
  const submitCreate = el("button", "btn primary small", "创建");
  submitCreate.type = "button";
  const cancelCreate = el("button", "btn ghost small", "取消");
  cancelCreate.type = "button";
  createForm.append(nameInput, kindSelect, copyLabel, submitCreate, cancelCreate);

  create.addEventListener("click", () => {
    createForm.classList.toggle("hidden");
    if (!createForm.classList.contains("hidden")) nameInput.focus();
  });
  cancelCreate.addEventListener("click", () => createForm.classList.add("hidden"));
  submitCreate.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    if (!name) {
      toast("先填个人设名", true);
      return;
    }
    submitCreate.disabled = true;
    submitCreate.textContent = "创建中…";
    try {
      const data = await api("/api/prompts/create", {
        method: "POST",
        body: JSON.stringify({
          name,
          kind: kindSelect.value,
          copy: copyCheck.checked,
          copy_from: state.promptDir || "",
        }),
      });
      toast(data.message || "已创建");
      createForm.classList.add("hidden");
      nameInput.value = "";
      await loadProfiles();
      await loadPrompts(data.id);
    } catch (err) {
      toast(err.message, true);
    } finally {
      submitCreate.disabled = false;
      submitCreate.textContent = "创建";
    }
  });
  card.appendChild(createForm);

  const note = el("p", "detail", "");
  if (state.promptKind === "file") {
    note.textContent =
      "当前是单文件人设：只定义 system（人设/规则），动态内容共用 prompts/user.md，不能在右侧修改 user 模板。";
  } else if (state.promptKind === "dir") {
    note.textContent = "当前是目录人设：system.md 与 user.md 都保存在对应子目录。";
  } else {
    note.textContent = "当前是默认人设：prompts/system.md + prompts/user.md。";
  }
  card.appendChild(note);

  const tabs = el("div", "prompt-tabs");
  const systemTab = el("button", null, "system.md（人设/风格/规则）");
  const userTab = el("button", null, "user.md（动态内容模板）");
  tabs.append(systemTab, userTab);
  card.appendChild(tabs);

  const editor = el("div", "prompt-editor");
  const textarea = document.createElement("textarea");
  textarea.spellcheck = false;
  editor.appendChild(textarea);

  const meta = el("div", "prompt-meta");
  const count = el("span", null, "");
  const vars = el("span", null, "可用占位符见 prompts/README.md");
  meta.append(count, vars);
  editor.appendChild(meta);
  card.appendChild(editor);

  const actions = el("div", "input-row");
  actions.style.marginTop = "10px";
  const saveBtn = el("button", "btn primary small", "保存提示词");
  saveBtn.type = "button";
  const status = el("span", "unit", "");
  actions.append(saveBtn, status);
  card.appendChild(actions);

  const output = el("div", "preview hidden");
  const pre = document.createElement("pre");
  output.appendChild(pre);
  card.appendChild(output);

  function switchTab(which) {
    if (which === "user" && state.promptKind === "file") {
      toast("单文件人设共用 prompts/user.md，请切到「默认人设」修改 user 模板", true);
      return;
    }
    state.activePrompt = which;
    systemTab.classList.toggle("active", which === "system");
    userTab.classList.toggle("active", which === "user");
    textarea.value = state.prompts[which] || "";
    count.textContent = `${textarea.value.length} 字`;
    status.textContent = "";
  }

  systemTab.addEventListener("click", () => switchTab("system"));
  userTab.addEventListener("click", () => switchTab("user"));
  textarea.addEventListener("input", () => {
    state.prompts[state.activePrompt] = textarea.value;
    count.textContent = `${textarea.value.length} 字`;
    status.textContent = "有未保存修改";
  });

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      const data = await api("/api/prompts", {
        method: "POST",
        body: JSON.stringify({
          dir: dirSelect.value,
          system: state.prompts.system,
          user: state.prompts.user,
        }),
      });
      status.textContent = data.message || "已保存";
      toast("提示词已保存");
      output.classList.add("hidden");
      await loadProfiles();
      await loadPrompts(dirSelect.value);
    } catch (err) {
      status.textContent = "保存失败";
      const detail = (err.payload && err.payload.errors || []).join("；") || err.message;
      toast(detail, true);
    } finally {
      saveBtn.disabled = false;
    }
  });

  async function previewPrompt(dir) {
    try {
      const data = await api(`/api/prompt/preview?dir=${encodeURIComponent(dir)}`);
      pre.textContent = `=== system.md ===\n${data.system}\n\n=== user.md（示例渲染） ===\n${data.user}`;
      output.classList.remove("hidden");
      output.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) {
      toast(err.message, true);
    }
  }

  switchTab("system");
  return card;
}

async function loadProfiles() {
  try {
    const data = await api("/api/prompts/list");
    state.profiles = data.profiles || [];
  } catch (err) {
    state.profiles = [];
  }
}

async function loadPrompts(dir, useSaved = false) {
  try {
    const query = useSaved ? "" : `?dir=${encodeURIComponent(dir || "")}`;
    const data = await api(`/api/prompts${query}`);
    state.prompts = { dir: data.dir, system: data.system, user: data.user };
    state.promptKind = data.kind || "default";
    state.promptDir = data.dir;
    renderAll();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ------------------------------------------------------------------ 体检 / 状态 */

function checkSection() {
  const card = el("section", "card");
  card.id = "group-check";
  card.append(
    el("h2", null, "体检与诊断"),
    el("p", "desc", "检查登录态、接口可用性、提示词文件与模型 Key。不会发送任何弹幕。")
  );

  const row = el("div", "input-row");
  const runBtn = el("button", "btn primary small", "开始体检");
  runBtn.type = "button";
  row.appendChild(runBtn);
  const result = el("div", "check-list");
  card.append(row, result);

  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true;
    runBtn.textContent = "检查中…";
    result.innerHTML = "";
    try {
      const data = await api("/api/check", { method: "POST", body: "{}" });
      data.results.forEach((item) => {
        const line = el("div", `check-item ${item.ok ? "ok" : "bad"}`);
        line.append(
          el("span", "badge", item.ok ? "OK" : "FAIL"),
          el("span", "name", item.name),
          el("span", "detail", item.detail || "")
        );
        result.appendChild(line);
      });
      if (!data.results.length) result.appendChild(el("div", "detail", "没有结果"));
    } catch (err) {
      toast(err.message, true);
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = "开始体检";
    }
  });

  return card;
}

function statusSection() {
  const card = el("section", "card");
  card.id = "group-status";
  card.append(
    el("h2", null, "运行状态"),
    el("p", "desc", "BOT 每一次判断（说了什么、为什么没说）都在这里，按房间和场次浏览。")
  );

  const row = el("div", "input-row");
  const refresh = el("button", "btn ghost small", "刷新");
  refresh.type = "button";
  const running = el("span", "unit", "");
  row.append(refresh, running);
  card.append(row);

  const reloadLine = el("div", "detail", "");
  card.append(reloadLine);

  /* ---------------- 决策历史：房间 + 场次 ---------------- */
  const roomSel = document.createElement("select");
  const sessSel = document.createElement("select");
  const spanMeta = el("span", "unit", "");
  const bar = el("div", "decision-bar");
  bar.append(
    el("span", "unit", "房间"), roomSel,
    el("span", "unit", "场次"), sessSel,
    spanMeta
  );

  const logBox = el("div", "decision-log");
  const logEmpty = el("p", "detail", "还没有决策记录。启动 BOT 后这里会显示它刚才判断了什么。");
  const moreRow = el("div", "more-row");
  const moreBtn = el("button", "btn ghost small", "加载更早的");
  moreBtn.type = "button";
  moreRow.appendChild(moreBtn);
  logBox.append(logEmpty, moreRow);

  card.append(bar, logBox);
  card.append(el("p", "detail", "实时日志在页面顶部「运行控制」卡片里（自动刷新，与 BOT 终端输出一致）。"));

  const PAGE = 50;
  let curRoom = "";
  let curSession = "";
  let allItems = [];
  let shown = 0;

  const pad2 = (n) => String(n).padStart(2, "0");

  function fmtStamp(ts) {
    const d = new Date(ts * 1000);
    const today = new Date();
    const sameDay = d.toDateString() === today.toDateString();
    const clock = `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
    return sameDay ? clock : `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${clock.slice(0, 5)}`;
  }

  function fmtSession(s, newest) {
    const start = new Date(s.start * 1000);
    const end = new Date(s.end * 1000);
    const day = `${pad2(start.getMonth() + 1)}-${pad2(start.getDate())}`;
    const t1 = `${pad2(start.getHours())}:${pad2(start.getMinutes())}`;
    const t2 = `${pad2(end.getHours())}:${pad2(end.getMinutes())}`;
    const live = newest && Date.now() / 1000 - s.end < 1800 ? " · 进行中" : "";
    return `${day} ${t1}–${t2} · ${s.count} 条${live}`;
  }

  function fillRooms(rooms, selected) {
    roomSel.innerHTML = "";
    rooms.forEach((room) => {
      const option = document.createElement("option");
      option.value = room.room;
      option.textContent = room.alias ? `${room.alias}（${room.room}）` : `房间 ${room.room}`;
      roomSel.appendChild(option);
    });
    if (selected) roomSel.value = selected;
  }

  function fillSessions(sessions, selected) {
    sessSel.innerHTML = "";
    sessions.forEach((session, index) => {
      const option = document.createElement("option");
      option.value = session.key;
      option.textContent = fmtSession(session, index === 0);
      sessSel.appendChild(option);
    });
    if (selected) sessSel.value = selected;
  }

  function makeRow(item) {
    const tr = el("tr");
    const actionCell = el("td");
    const tag = el("span", `tag ${item.action === "say" ? "say" : "skip"}`, item.action || "-");
    actionCell.appendChild(tag);
    if (item.sent) {
      actionCell.appendChild(el("span", "sent-mark ok", "已发"));
    } else if (item.dry_run) {
      actionCell.appendChild(el("span", "sent-mark", "演练"));
    }
    tr.append(
      el("td", null, item.ts ? fmtStamp(item.ts) : "-"),
      el("td", null, item.trigger || "-"),
      actionCell,
      el("td", null, item.text || "-"),
      el("td", null, item.error || item.reason || "-")
    );
    return tr;
  }

  function renderLog(reset) {
    if (reset) {
      logBox.innerHTML = "";
      const table = el("table", "decision-table");
      const head = el("tr");
      ["时间", "触发", "动作", "内容", "原因"].forEach((t) => head.appendChild(el("th", null, t)));
      table.appendChild(head);
      logBox.appendChild(table);
      logBox.appendChild(moreRow);
    }
    const table = logBox.querySelector("table");
    if (!allItems.length) {
      logEmpty.textContent = "这场直播还没有决策记录。";
      logBox.insertBefore(logEmpty, logBox.firstChild);
      moreRow.style.display = "none";
      return;
    }
    if (logEmpty.parentNode) logEmpty.remove();
    const next = allItems.slice(shown, shown + PAGE);
    next.forEach((item) => table.appendChild(makeRow(item)));
    shown += next.length;
    moreRow.style.display = shown < allItems.length ? "" : "none";
    moreBtn.textContent = `加载更早的 ${Math.min(PAGE, allItems.length - shown)} 条`;
  }

  async function loadDecisions() {
    const query = new URLSearchParams();
    if (curRoom) query.set("room", curRoom);
    if (curSession) query.set("session", curSession);
    const data = await api(`/api/decisions?${query.toString()}`);
    curRoom = data.room || "";
    curSession = data.session || "";
    fillRooms(data.rooms || [], curRoom);
    fillSessions(data.sessions || [], curSession);
    allItems = data.items || [];
    shown = 0;
    const pick = (data.sessions || []).find((s) => s.key === curSession);
    spanMeta.textContent = pick ? `共 ${data.total} 条` : "";
    renderLog(true);
    if (!(data.rooms || []).length) {
      logEmpty.textContent = "还没有决策记录。启动 BOT 后这里会显示它刚才判断了什么。";
    }
  }

  async function load() {
    refresh.disabled = true;
    try {
      const data = await api("/api/status");
      const rt = data.runtime;
      if (rt) {
        const mode = rt.dry_run ? "演练模式" : "真实发送模式";
        const pending = rt.config_changed
          ? (rt.auto_reload ? " · 配置已修改，正在自动加载…" : " · 配置已修改，需重启")
          : "";
        running.textContent =
          `${rt.alive ? "运行中" : "已停止"} · ${mode} · 已运行 ${Math.round((rt.age_sec || 0) / 60)} 分钟` + pending;
        running.style.color = rt.config_changed ? "var(--warn)" : "";
      } else {
        running.textContent = data.bot_running ? "BOT 正在运行（无运行标记）" : "BOT 未运行";
      }
      const reload = data.reload;
      if (reload && reload.error) {
        reloadLine.textContent = `上次热加载失败（${reload.time || "-"}）：${reload.error}`;
        reloadLine.style.color = "var(--danger)";
      } else if (reload && reload.at) {
        const changed = (reload.changed || []).slice(0, 12).join("、");
        reloadLine.textContent =
          `上次热加载：${reload.time} · ${reload.count} 项变更：${changed || "（无）"}` +
          ((reload.changed || []).length > 12 ? "…" : "");
        reloadLine.style.color = "var(--muted)";
      } else {
        reloadLine.textContent = "还没有热加载记录（启动后修改配置，BOT 会自动重启加载）";
        reloadLine.style.color = "var(--muted)";
      }
      await loadDecisions();
    } catch (err) {
      toast(err.message, true);
    } finally {
      refresh.disabled = false;
    }
  }

  roomSel.addEventListener("change", () => {
    curRoom = roomSel.value;
    curSession = "";
    load().catch((err) => toast(err.message, true));
  });
  sessSel.addEventListener("change", () => {
    curSession = sessSel.value;
    loadDecisions().catch((err) => toast(err.message, true));
  });
  moreBtn.addEventListener("click", () => renderLog(false));
  refresh.addEventListener("click", load);
  load();
  return card;
}

/* ------------------------------------------------------------------ 运行控制 */

function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}

function controlSection() {
  // 重绘时先把上一轮的定时器清掉，避免重复轮询
  (state.controlTimers || []).forEach((t) => clearInterval(t));
  state.controlTimers = [];

  const card = el("section", "card");
  card.id = "group-control";
  card.append(
    el("h2", null, "运行控制"),
    el("p", "desc",
      "运行流程：扫码登录 B 站 → 在左侧「大模型」里填 API Key → 调整其余设置 →保存配置 → 启动 BOT。")
  );

  /* ---------------- 状态与按钮 ---------------- */
  const stateRow = el("div", "control-row");
  const dot = el("span", "status-dot");
  const stateText = el("span", "detail", "正在读取状态…");
  stateRow.append(dot, stateText);

  const btns = el("div", "control-row");
  const btnStart = el("button", "btn primary small", "启动 BOT");
  const btnStop = el("button", "btn ghost small", "停止");
  const btnRestart = el("button", "btn ghost small", "重启");
  [btnStart, btnStop, btnRestart].forEach((b) => (b.type = "button"));

  // 演练模式开关：和启动按钮放一起，启动前一眼能确认会不会真的发弹幕。
  // 开关语义：左 = 关 = 演练模式（默认），右 = 开 = 真实发送。
  const dryLabel = el("label", "switch");
  dryLabel.title = "左边「演练模式」只把将要发送的内容写进日志；拨到右边才会真的发弹幕";
  const dryCb = document.createElement("input");
  dryCb.type = "checkbox";
  dryCb.dataset.key = "app.dry_run";
  // 勾选（右）代表「关掉演练」= dry_run: false
  dryCb.checked = !Boolean(getPath(state.config, "app.dry_run"));
  const dryTrack = el("span", "track");
  const dryState = el("span", "state");
  const paintDry = () => {
    dryState.textContent = dryCb.checked ? "真实发送模式" : "演练模式（只记录）";
    dryState.style.color = dryCb.checked ? "var(--ok)" : "var(--warn)";
  };
  paintDry();
  dryLabel.append(dryCb, dryTrack, dryState);

  btns.append(btnStart, btnStop, btnRestart, el("span", "unit", "·"), dryLabel);

  /* ---------------- 扫码登录 ---------------- */
  const loginRow = el("div", "control-row");
  const btnLogin = el("button", "btn ghost small", "扫码登录 B 站");
  btnLogin.type = "button";
  const loginText = el("span", "detail", "读取登录态…");
  loginRow.append(btnLogin, loginText);

  const qrBox = el("div", "qr-box hidden");
  const qrImg = document.createElement("img");
  qrImg.className = "qr-img";
  qrImg.alt = "B 站登录二维码";
  const qrHint = el("div", "detail", "用 B 站手机客户端扫一扫");
  qrBox.append(qrImg, qrHint);

  /* ---------------- 实时日志 ---------------- */
  const logRow = el("div", "control-row");
  const lineSel = document.createElement("select");
  [100, 200, 500, 1000, 2000].forEach((n) => {
    const option = document.createElement("option");
    option.value = String(n);
    option.textContent = `最近 ${n} 行`;
    lineSel.appendChild(option);
  });
  lineSel.value = "200";

  const autoLabel = el("label", "check-inline");
  const autoChk = document.createElement("input");
  autoChk.type = "checkbox";
  autoChk.checked = true;
  autoLabel.append(autoChk, document.createTextNode(" 自动刷新"));

  const btnLogRefresh = el("button", "btn ghost small", "刷新日志");
  btnLogRefresh.type = "button";
  const logMeta = el("span", "unit", "");
  logRow.append(lineSel, autoLabel, btnLogRefresh, logMeta);

  const logWrap = el("div", "console-wrap");
  const logPre = el("pre", "console", "（正在加载日志…）");
  logWrap.appendChild(logPre);

  card.append(
    stateRow, btns,
    el("h3", null, "B 站登录"), loginRow, qrBox,
    el("h3", null, "实时日志"), logRow, logWrap
  );

  /* ---------------- 行为 ---------------- */
  let logSig = "";
  let loginTimer = null;
  let botRunning = false;

  function stopLoginPolling() {
    if (loginTimer) {
      clearInterval(loginTimer);
      loginTimer = null;
    }
  }

  async function refreshStatus() {
    try {
      const data = await api("/api/status");
      const bot = data.bot || {};
      const rt = data.runtime || {};
      const running = Boolean(bot.running);
      botRunning = running;
      dot.classList.toggle("on", running);
      const bits = [running ? "运行中" : "未运行"];
      if (running) bits.push(`pid ${bot.pid}`);
      if (running && bot.uptime_sec) bits.push(`已运行 ${fmtDuration(bot.uptime_sec)}`);
      if (running && bot.source === "external") bits.push("由外部启动");

      // 运行模式单独上色：演练模式橙色、真实发送绿色
      stateText.textContent = "";
      stateText.appendChild(document.createTextNode(bits.join(" · ")));
      if (running && rt.dry_run !== undefined) {
        stateText.appendChild(document.createTextNode(" · "));
        const mode = el("span", null, rt.dry_run ? "演练模式" : "真实发送模式");
        mode.style.color = rt.dry_run ? "var(--warn)" : "var(--ok)";
        mode.style.fontWeight = "600";
        stateText.appendChild(mode);
      }
      // runtime.json 是「上一次启动」的快照：BOT 已经停了就不能再说「正在重载」，
      // 否则会出现「一直是配置已改、正在重载」的假状态。
      if (rt && rt.config_changed) {
        let note = "配置已改，正在自动重载…";
        let color = "var(--warn)";
        if (data.reload && data.reload.error) {
          note = "配置有误，BOT 仍在用旧配置";
          color = "var(--danger)";
        } else if (!rt.alive) {
          note = "配置已改，启动后生效";
        }
        stateText.appendChild(document.createTextNode(" · "));
        const badge = el("span", null, note);
        badge.style.color = color;
        stateText.appendChild(badge);
      } else if (rt && rt.alive && data.reload && data.reload.at) {
        // 重载完成后给个明确的「已生效」，而不是继续挂着「正在重载」
        const age = Math.floor(Date.now() / 1000 - Number(data.reload.at));
        if (age >= 0 && age <= 300) {
          stateText.appendChild(document.createTextNode(" · "));
          const done = el("span", null, `配置已生效（${data.reload.time}）`);
          done.style.color = "var(--ok)";
          stateText.appendChild(done);
        }
      }
      btnStart.disabled = running;
      btnStop.disabled = !running;

    } catch (err) {
      stateText.textContent = `状态读取失败：${err.message}`;
    }
  }

  async function refreshLoginAccount() {
    try {
      const d = await api("/api/login/whoami");
      loginText.dataset.fresh = "1";
      if (d.logged_in) {
        loginText.textContent = `当前登录账号：${d.uname || "（未知昵称）"}（UID:${d.mid}）`;
        loginText.style.color = "var(--accent)";
      } else {
        loginText.textContent = "未登录：点右侧「扫码登录 B 站」";
        loginText.style.color = "var(--muted)";
      }
    } catch (err) {
      loginText.textContent = "登录态读取失败";
      loginText.style.color = "var(--warn)";
    }
  }

  async function refreshLog() {
    if (document.hidden) return;
    try {
      const data = await api(`/api/logs?lines=${encodeURIComponent(lineSel.value)}&source=bot`);
      const text = (data.lines || []).join("\n") ||
        "（还没有日志；启动 BOT 后这里会实时滚动）";
      if (text !== logSig) {
        const atBottom = logWrap.scrollHeight - logWrap.scrollTop - logWrap.clientHeight < 60;
        logSig = text;
        logPre.textContent = text;
        if (atBottom) logWrap.scrollTop = logWrap.scrollHeight;
      }
      logMeta.textContent = data.size ? `${Math.round(data.size / 1024)} KB` : "";
    } catch (err) {
      logMeta.textContent = `日志读取失败：${err.message}`;
      if (!logSig) logPre.textContent = "（日志读取失败，稍后自动重试）";
    }
  }

  async function botAction(name) {
    const labels = { start: "启动", stop: "停止", restart: "重启" };
    [btnStart, btnStop, btnRestart].forEach((b) => (b.disabled = true));
    try {
      const data = await api(`/api/bot/${name}`, { method: "POST" });
      toast(`${labels[name]}：${data.message || "完成"}`);
      if (data.log_tail && data.log_tail.length) {
        logSig = "";
        logPre.textContent = data.log_tail.join("\n");
        logWrap.scrollTop = logWrap.scrollHeight;
      }
    } catch (err) {
      toast(err.message, true);
    } finally {
      btnRestart.disabled = false;
      await refreshStatus();
      await refreshLog();
    }
  }

  function renderLogin(d) {
    const st = d.state;
    loginText.dataset.fresh = "1";
    if (st === "waiting" || st === "scanned") {
      qrBox.classList.remove("hidden");
      if (d.qr) qrImg.src = d.qr;
      qrHint.textContent = st === "scanned"
        ? "已扫码，请在手机上点「确认登录」"
        : `等待扫码 · 剩余约 ${d.expires_in || 0} 秒`;
      loginText.textContent = st === "scanned" ? "已扫码，等待确认" : "等待扫码…";
      loginText.style.color = "var(--muted)";
      btnLogin.textContent = "重新获取二维码";
    } else if (st === "ok") {
      qrBox.classList.add("hidden");
      loginText.textContent =
        `当前登录账号：${d.uname || "（未知昵称）"}${d.mid ? `（UID:${d.mid}）` : ""}` +
        (botRunning ? " · BOT 正在运行，点「重启」生效" : "");
      loginText.style.color = "var(--accent)";
      btnLogin.textContent = "重新扫码";
      toast("登录成功，已保存 cookie");
    } else if (st === "expired") {
      qrBox.classList.add("hidden");
      loginText.textContent = "二维码已过期，请重新获取";
      loginText.style.color = "var(--warn)";
      btnLogin.textContent = "重新获取二维码";
    } else if (st === "error") {
      qrBox.classList.add("hidden");
      loginText.textContent = d.message || "登录失败";
      loginText.style.color = "var(--danger)";
      btnLogin.textContent = "重新获取二维码";
    }
  }

  async function pollLogin() {
    stopLoginPolling();
    loginTimer = setInterval(async () => {
      try {
        const d = await api("/api/login/status");
        renderLogin(d);
        if (["ok", "expired", "error", "idle"].includes(d.state)) {
          stopLoginPolling();
          loginTimer = null;
          if (d.state === "ok") await refreshLoginAccount();
          await refreshStatus();
        }
      } catch (err) {
        stopLoginPolling();
        loginTimer = null;
        loginText.textContent = `登录状态读取失败：${err.message}`;
      }
    }, 1500);
    state.controlTimers.push(loginTimer);
  }

  dryCb.addEventListener("change", () => {
    // 开关拨到右（勾选）= 真实发送，所以 dry_run 取反
    setPath(state.config, "app.dry_run", !dryCb.checked);
    paintDry();
    markDirty();
    scheduleAutoSave();
  });

  btnStart.addEventListener("click", () => botAction("start"));
  btnStop.addEventListener("click", () => botAction("stop"));
  btnRestart.addEventListener("click", () => botAction("restart"));
  btnLogRefresh.addEventListener("click", refreshLog);
  lineSel.addEventListener("change", () => {
    logSig = "";
    refreshLog();
  });

  btnLogin.addEventListener("click", async () => {
    btnLogin.disabled = true;
    loginText.dataset.fresh = "1";
    loginText.textContent = "正在获取二维码…";
    try {
      const d = await api("/api/login/start", { method: "POST" });
      renderLogin(d);
      if (d.state === "waiting" || d.state === "scanned") pollLogin();
    } catch (err) {
      toast(err.message, true);
      loginText.textContent = `获取二维码失败：${err.message}`;
      loginText.style.color = "var(--danger)";
    } finally {
      btnLogin.disabled = false;
    }
  });

  refreshStatus();
  refreshLoginAccount();
  refreshLog();
  state.controlTimers.push(setInterval(refreshStatus, 3000));
  state.controlTimers.push(setInterval(() => {
    if (autoChk.checked) refreshLog();
  }, 2000));

  return card;
}

/* ------------------------------------------------------------------ 总装 */

function renderNav(groups) {
  const nav = $("#nav");
  nav.innerHTML = "";
  const items = [{ id: "group-control", label: "运行控制" }];
  items.push(...groups.map((g) => ({ id: `group-${g.id}`, label: g.title })));
  items.push({ id: "group-prompts", label: "提示词" });
  items.push({ id: "group-check", label: "体检与诊断" });
  items.push({ id: "group-status", label: "运行状态" });

  items.forEach((item, index) => {
    const link = document.createElement("a");
    link.href = `#${item.id}`;
    link.textContent = item.label;
    if (index === 0) link.classList.add("active");
    link.addEventListener("click", (event) => {
      event.preventDefault();
      document.getElementById(item.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    nav.appendChild(link);
  });
}

function updateActiveNav() {
  const sections = Array.from(document.querySelectorAll(".card"));
  const links = Array.from(document.querySelectorAll(".sidenav a"));
  let current = sections[0];
  sections.forEach((section) => {
    if (section.getBoundingClientRect().top <= 120) current = section;
  });
  links.forEach((link) => link.classList.toggle("active", link.getAttribute("href") === `#${current.id}`));
}

function renderAll() {
  const groups = state.schema.groups;
  const container = $("#groups");
  container.innerHTML = "";
  container.appendChild(controlSection());
  groups.forEach((group) => {
    if (group.id === "rooms") {
      container.appendChild(groupRooms(group));
    } else {
      container.appendChild(groupFields(group));
    }
  });
  container.appendChild(promptSection());
  container.appendChild(checkSection());
  container.appendChild(statusSection());
  renderNav(groups);
  refreshDependencies();
}

function collectConfig() {
  const cfg = deepClone(state.config);
  cfg.app.rooms = state.rooms.map((room) => ({
    room_id: Number(room.room_id),
    alias: room.alias || "",
    enabled: Boolean(room.enabled),
    prompt_dir: room.prompt_dir || "",
  }));
  return cfg;
}

let autoSaveQueued = false;

/* 启用/添加/删除这类「结构性」改动不需要再点保存：立即存盘，让 BOT 马上切房间 */
function scheduleAutoSave() {
  autoSaveQueued = true;
  clearTimeout(scheduleAutoSave._timer);
  scheduleAutoSave._timer = setTimeout(() => {
    if (!autoSaveQueued) return;
    autoSaveQueued = false;
    save(true);
  }, 250);
}

async function save(auto = false) {
  if (state.saving) {
    if (auto) autoSaveQueued = true;
    return;
  }
  if (document.querySelector(".invalid")) {
    banner("有字段格式不正确（例如额外请求参数不是合法 JSON），请先修正标红的输入框。", true);
    return;
  }
  state.saving = true;
  $("#btnSave").disabled = true;
  $("#btnSave").textContent = "保存中…";
  clearBanner();
  try {
    const data = await api("/api/config", {
      method: "POST",
      body: JSON.stringify({ config: collectConfig() }),
    });
    applyKeyMeta(data);
    state.config = scrubSecrets(data.config);
    // 保留原有 room 对象（行内事件闭包引用的是它们），只把服务端结果合并进去，
    // 否则保存后表格里再点开关，改的是旧对象，下一次保存就丢了。
    const prevRooms = state.rooms;
    state.rooms = (data.config.app.rooms || []).map((saved) => {
      const existing = prevRooms.find((r) => String(r.room_id) === String(saved.room_id));
      return existing ? Object.assign(existing, saved) : saved;
    });
    refreshSecretFields();
    state.dirty = false;
    $("#dirtyFlag").classList.add("hidden");
    await loadPrompts(state.promptDir);
    const message = data.message || "已保存";
    if (auto) {
      toast(message);
    } else {
      let html = `<strong>${message}</strong>`;
      if (data.backup) html += `<br>旧配置已备份为 <code>${data.backup}</code>`;
      if (data.warnings && data.warnings.length) {
        html += "<ul>" + data.warnings.map((w) => `<li>${w}</li>`).join("") + "</ul>";
      }
      banner(html);
      toast("配置已保存");
    }
  } catch (err) {
    const errors = (err.payload && err.payload.errors) || [];
    const html =
      `<strong>保存失败：</strong>${err.message}` +
      (errors.length ? "<ul>" + errors.map((e) => `<li>${e}</li>`).join("") + "</ul>" : "");
    banner(html, true);
    toast("保存失败，请检查表单", true);
  } finally {
    state.saving = false;
    $("#btnSave").disabled = false;
    $("#btnSave").textContent = "保存配置";
    if (autoSaveQueued) {
      autoSaveQueued = false;
      save(true);
    }
  }
}

async function init() {
  try {
    const [schemaData, configData, profileData] = await Promise.all([
      api("/api/schema"),
      api("/api/config"),
      api("/api/prompts/list").catch(() => ({ profiles: [] })),
    ]);
    state.schema = schemaData.schema;
    state.profiles = profileData.profiles || [];
    applyKeyMeta(configData);
    state.config = scrubSecrets(configData.config);
    state.rooms = deepClone(configData.config.app.rooms || []);
    state.promptDir = "";
    let pathText = configData.config_path;
    if (configData.env_overrides && configData.env_overrides.length) {
      pathText += ` · 环境变量会覆盖：${configData.env_overrides.join(", ")}`;
    }
    if (state.apiKeyMasked) {
      pathText += ` · API Key 已保存（${state.apiKeyMasked}）`;
    }
    $("#configPath").textContent = pathText;

    if (configData.errors && configData.errors.length) {
      banner(`配置有问题，请修改后保存：<ul>${configData.errors.map((e) => `<li>${e}</li>`).join("")}</ul>`, true);
    } else if (configData.reload && configData.reload.error) {
      banner(
        `上次热加载失败（${configData.reload.time || "-"}）：${configData.reload.error}` +
        `<br>BOT 仍在使用旧配置，修好配置后会再次自动加载。`,
        true
      );
    } else if (configData.runtime && configData.runtime.alive &&
               (configData.runtime.config_changed || configData.runtime.mode_changed)) {
      const rt = configData.runtime;
      if (rt.auto_reload) {
        banner(
          "配置已变更，BOT 正在<b>自动重启加载</b>新配置（约几秒），稍后刷新本页即可看到新的运行状态。",
          false
        );
      } else {
        banner(
          `运行中的 BOT 仍在用<b>旧配置</b>（${rt.dry_run ? "演练模式" : "真实发送模式"}）。` +
          `你刚改的配置要<b>重启 BOT</b> 才生效，否则它会按旧模式继续运行。` +
          `<br>停止旧进程后执行：<code>python -m src.app run</code>`,
          true
        );
      }
    } else if (configData.warnings && configData.warnings.length) {
      banner(
        `配置文件里有已废弃的字段，已自动忽略（保存后会清理）：<ul>${configData.warnings
          .map((w) => `<li>${w}</li>`)
          .join("")}</ul>`
      );
    }

    renderAll();
    loadPrompts("", true);
    prefillRoomMeta();
  } catch (err) {
    banner(`加载失败：${err.message}`, true);
  }
}

$("#btnSave").addEventListener("click", save);
$("#btnReload").addEventListener("click", () => {
  if (state.dirty && !confirm("有未保存的修改，确定重新加载？")) return;
  location.reload();
});
window.addEventListener("beforeunload", (event) => {
  if (!state.dirty) return;
  event.preventDefault();
  event.returnValue = "";
});
window.addEventListener("scroll", updateActiveNav, { passive: true });

init();
