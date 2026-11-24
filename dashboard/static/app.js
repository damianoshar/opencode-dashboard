/* OpenCode Dashboard - vanilla JS front end. */
"use strict";

const state = {
  view: "overview",
  filters: {
    search: "", project_id: "", directory: "", agent: "", model: "",
    state: "", kind: "", created_from: "", created_to: "",
  },
  sort: localStorage.getItem("ocd.sort") || "time_updated",
  order: localStorage.getItem("ocd.order") || "desc",
  page: 1,
  limit: 50,
  mode: localStorage.getItem("ocd.mode") || "table",
  facets: null,
  session: null,
  msgOffset: 0,
  msgTotal: 0,
  selection: new Set(),
  selectAllMatching: false,
  matchingTotal: 0,
  projectSelection: new Set(),
  projectSelectAllMatching: false,
  projectMatchingTotal: 0,
  lastError: "",
};

/* ------------------------------------------------------------------ utils */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  if (value === null || value === undefined) return "";
  const amp = String.fromCharCode(38);
  const lt = String.fromCharCode(60);
  const gt = String.fromCharCode(62);
  const quot = String.fromCharCode(34);
  const apos = String.fromCharCode(39);
  return String(value)
    .split(amp).join(amp + "amp;")
    .split(lt).join(amp + "lt;")
    .split(gt).join(amp + "gt;")
    .split(quot).join(amp + "quot;")
    .split(apos).join(amp + "#39;");
}

function fmtBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes);
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(value < 10 && i > 0 ? 2 : 0)} ${units[i]}`;
}

function fmtNum(value) {
  if (value === null || value === undefined) return "";
  const n = Number(value);
  if (!isFinite(n)) return String(value);
  return n.toLocaleString("en-US");
}

function fmtTime(ms) {
  if (!ms) return "";
  const d = new Date(Number(ms));
  if (isNaN(d.getTime())) return "";
  return d.toLocaleString();
}

function fmtDate(ms) {
  if (!ms) return "";
  const d = new Date(Number(ms));
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString();
}

function debounce(fn, delay = 250) {
  let handle;
  return (...args) => {
    clearTimeout(handle);
    handle = setTimeout(() => fn(...args), delay);
  };
}

function toast(message, kind = "") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), 5200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { raw: text }; }
  if (!response.ok) {
    const message = (data && (data.detail || data.error || data.message)) || response.statusText;
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return data;
}

function queryString(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== "" && value !== null && value !== undefined) search.set(key, value);
  });
  const out = search.toString();
  return out ? `?${out}` : "";
}

/* ----------------------------------------------------------------- routing */
const ROUTES = {
  overview: "/",
  sessions: "/sessions",
  projects: "/projects",
  directories: "/directories",
  backups: "/backups",
  maintenance: "/maintenance",
  schema: "/schema",
  storage: "/storage",
};
const VIEW_BY_PATH = Object.fromEntries(
  Object.entries(ROUTES).map(([view, path]) => [path, view]));

function routePath() {
  return window.location.pathname.replace(/\/+$/, "") || "/";
}

function parseRoute() {
  const parts = routePath().split("/").filter(Boolean);
  const view = parts.length ? VIEW_BY_PATH[`/${parts[0]}`] : "overview";
  if (!view) return { view: "overview", params: {} };
  const params = {};
  if (view === "sessions" && parts[1]) params.session_id = decodeURIComponent(parts[1]);
  return { view, params };
}

function buildUrl(view, params = {}, query = "") {
  const base = ROUTES[view] || "/";
  const path = params.session_id
    ? `${base}/${encodeURIComponent(params.session_id)}` : base;
  return path + (query ? `?${query}` : "");
}

function navigate(view, options = {}) {
  const { params = {}, query = "", replace = false } = options;
  const url = buildUrl(view, params, query);
  const entry = { view, ...params };
  if (replace) window.history.replaceState(entry, "", url);
  else window.history.pushState(entry, "", url);
}

function dateToInput(ms) {
  if (!ms) return "";
  const date = new Date(Number(ms));
  if (isNaN(date.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function inputToDate(value) {
  if (!value) return "";
  const date = new Date(`${value}T00:00:00`);
  return isNaN(date.getTime()) ? "" : date.getTime();
}

function sessionQueryString() {
  const params = new URLSearchParams();
  Object.entries(state.filters).forEach(([key, value]) => {
    if (value === "" || value === null || value === undefined) return;
    if (key === "created_from" || key === "created_to") {
      params.set(key, dateToInput(value));
    } else {
      params.set(key, value);
    }
  });
  return params.toString();
}

function projectQueryString() {
  const search = ($("#p-search") && $("#p-search").value.trim()) || "";
  return search ? `search=${encodeURIComponent(search)}` : "";
}

/** Put the current filters of the active view into the URL. */
function syncUrl() {
  const route = parseRoute();
  const query = route.view === "projects" ? projectQueryString() : sessionQueryString();
  navigate(route.view, { params: route.params, query, replace: true });
}

/** Read the filters of the URL into the form and into the state. */
function applyQueryToForm() {
  const params = new URLSearchParams(window.location.search);
  const view = parseRoute().view;

  if (view === "projects") {
    const search = $("#p-search");
    if (search) search.value = params.get("search") || "";
    return;
  }
  if (view !== "sessions") return;

  Object.keys(state.filters).forEach((key) => { state.filters[key] = ""; });
  ["#f-search", "#f-from", "#f-to"].forEach((sel) => { $(sel).value = ""; });
  ["#f-project", "#f-directory", "#f-agent", "#f-model", "#f-state", "#f-kind"]
    .forEach((sel) => { $(sel).value = ""; });

  ["search", "project_id", "directory", "agent", "model", "state", "kind"]
    .forEach((key) => {
      const value = params.get(key);
      if (!value) return;
      state.filters[key] = value;
      const input = $(`#f-${key}`);
      if (input) input.value = value;
    });
  ["created_from", "created_to"].forEach((key) => {
    const value = params.get(key);
    if (!value) return;
    const ms = inputToDate(value);
    state.filters[key] = ms;
    const input = $(key === "created_from" ? "#f-from" : "#f-to");
    if (input) input.value = value;
  });
}

/* ------------------------------------------------------------------- boot */
async function boot() {
  bindTabs();
  bindGlobalButtons();
  bindDrawer();
  bindBulkActions();
  await refreshHealth();
  await loadFacets();
  applyQueryToForm();
  const route = parseRoute();
  switchView(route.view, { params: route.params, push: false, replace: true });
  window.addEventListener("popstate", () => {
    const next = parseRoute();
    applyQueryToForm();
    switchView(next.view, { params: next.params, push: false });
  });
}

function bindTabs() {
  $$("#tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => switchView(tab.dataset.view));
  });
}

function switchView(name, options = {}) {
  const { params = {}, push = true, replace = false } = options;
  if (!ROUTES[name]) name = "overview";
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  state.view = name;
  $$(".view").forEach((v) => v.classList.add("hidden"));
  const view = $(`#view-${name}`);
  if (view) view.classList.remove("hidden");
  document.title = `OpenCode Dashboard · ${name}`;
  const query = name === "projects" ? projectQueryString() : sessionQueryString();
  if (push) navigate(name, { params, query, replace });
  else if (replace) navigate(name, { params, query, replace: true });
  renderView();
  if (params.session_id) openSession(params.session_id, { updateUrl: false });
}

function bindGlobalButtons() {
  $("#refresh-btn").addEventListener("click", () => renderView());
  const closeDrawer = () => {
    $("#drawer").classList.add("hidden");
    // the deep link of the open session is replaced by the list itself
    if (parseRoute().params.session_id) {
      navigate("sessions", { query: sessionQueryString(), replace: true });
    }
  };
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer").addEventListener("click", (e) => {
    if (e.target === $("#drawer")) closeDrawer();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
  });
}

async function refreshHealth() {
  const pill = $("#health-pill");
  try {
    const health = await api("/api/health");
    $("#db-path").textContent = health.database;
    pill.textContent = health.ok ? "database online" : "database missing";
    pill.className = `pill ${health.ok ? "pill-ok" : "pill-bad"}`;
  } catch (err) {
    pill.textContent = "offline";
    pill.className = "pill pill-bad";
  }
}

async function loadFacets() {
  try {
    state.facets = await api("/api/facets");
  } catch (err) {
    state.facets = null;
  }
  fillSelect("#f-project", state.facets?.projects?.map((p) => [p.id, `${p.worktree} (${p.sessions})`]) || []);
  fillSelect("#f-directory", state.facets?.directories?.map((d) => [d.directory, `${d.directory} (${d.sessions})`]) || []);
  fillSelect("#f-agent", state.facets?.agents?.map((a) => [a.agent, `${a.agent} (${a.sessions})`]) || []);
  fillSelect("#f-model", state.facets?.models?.map((m) => [m.model_label, `${m.model_label} (${m.sessions})`]) || []);
  fillSelect("#f-state", state.facets?.states?.map((s) => [s.state, `${s.state} (${s.sessions})`]) || []);
  fillSelect("#f-kind", [["root", "root"], ["child", "child"], ["fork", "fork"]]);
}

function fillSelect(selector, pairs) {
  const select = $(selector);
  if (!select) return;
  const first = select.options[0];
  select.innerHTML = "";
  if (first) select.appendChild(first);
  pairs.forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  });
}

async function renderView() {
  switch (state.view) {
    case "overview": return renderOverview();
    case "sessions": return renderSessions();
    case "projects": return renderProjects();
    case "directories": return renderDirectories();
    case "backups": return renderBackups();
    case "maintenance": return renderMaintenance();
    case "schema": return renderSchema();
    case "storage": return renderStorage();
    default: return undefined;
  }
}

/* --------------------------------------------------------------- overview */
async function renderOverview() {
  const data = await api("/api/overview");
  const db = data.db;
  const s = data.sessions;

  const tiles = [
    ["Sessions", fmtNum(s.total), `${fmtNum(data.counts.session_message || 0)} messages`],
    ["Projects", fmtNum(data.counts.project || 0), `${fmtNum(data.counts.session_v2 || 0)} v2 sessions`],
    ["Messages", fmtNum(data.counts.session_message || 0), `${fmtNum(data.counts.session_part || 0)} parts`],
    ["Events", fmtNum(data.counts.event || 0), `${fmtNum(data.counts.event_sequence || 0)} aggregates`],
    ["Database", fmtBytes(db.size_bytes), `WAL ${fmtBytes(db.wal_bytes)}`],
    ["Free pages", fmtNum(db.free_pages), fmtBytes(db.free_bytes)],
    ["Tokens in", fmtNum(s.tokens_input), `out ${fmtNum(s.tokens_output)}`],
    ["Cost", `$${(s.cost || 0).toFixed(2)}`, `${fmtNum(s.tokens_cache_read)} cached`],
    ["First session", fmtDate(s.first_created), ""],
    ["Latest session", fmtDate(s.last_created), ""],
  ];
  $("#overview-tiles").innerHTML = tiles.map(([label, value, sub]) => `
    <div class="tile"><div class="label">${esc(label)}</div>
      <div class="value">${esc(value)}</div>
      <div class="sub">${esc(sub)}</div></div>`).join("");

  $("#overview-db").innerHTML = kvRows([
    ["path", db.path],
    ["size", fmtBytes(db.size_bytes)],
    ["wal", fmtBytes(db.wal_bytes)],
    ["pages", `${fmtNum(db.page_count)} × ${db.page_size} B`],
    ["free pages", `${fmtNum(db.free_pages)} (${fmtBytes(db.free_bytes)})`],
    ["journal", db.journal_mode],
    ["modified", fmtTime(db.mtime * 1000)],
  ]);

  const health = data.health;
  $("#overview-health").innerHTML = kvRows([
    ["orphan messages", health.orphan_messages],
    ["orphan events", health.orphan_events],
    ["seq-lag sessions", health.seq_lag_sessions.length],
    ["states", (data.states || []).map((x) => `${x.state}:${x.count}`).join("  ")],
    ["kinds", (data.kinds || []).map((x) => `${x.kind}:${x.count}`).join("  ")],
  ]);
  if (health.seq_lag_sessions.length) {
    $("#overview-health").innerHTML += `<div class="muted small" style="margin-top:8px">
      Sessions that cannot send prompts until repaired: ${health.seq_lag_sessions
        .map((x) => `<code>${esc(x.id)}</code>`).join(", ")}</div>`;
  }

  drawTimeline($("#overview-chart"), data.timeline);
  $("#overview-chart-note").textContent = data.timeline.length
    ? `${data.timeline[0].day} → ${data.timeline[data.timeline.length - 1].day}`
    : "no data";

  $("#overview-msgs").innerHTML = barList(data.message_types.map((m) => [m.type, m.count]));
  $("#overview-models").innerHTML = barList(data.top_models.map((m) => [m.model, m.sessions]));
  $("#overview-agents").innerHTML = barList(data.top_agents.map((m) => [m.agent, m.sessions]));
  $("#overview-dirs").innerHTML = barList(data.top_directories.map((m) => [m.directory, m.sessions]));

  const counts = Object.entries(data.counts).sort((a, b) => b[1] - a[1]);
  $("#overview-tables").innerHTML = tableHtml(
    ["Table", "Rows"],
    counts.map(([name, rows]) => [esc(name), fmtNum(rows)]));
}

function kvRows(pairs) {
  return pairs.filter(([, v]) => v !== undefined && v !== null)
    .map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>`).join("");
}

function barList(items) {
  if (!items || !items.length) return `<p class="muted small">none</p>`;
  const max = Math.max(...items.map((i) => i[1])) || 1;
  return `<div class="bars">${items.map(([label, count]) => `
    <div class="bar-row">
      <span class="truncate" title="${esc(label)}">${esc(label)}</span>
      <span class="bar"><i style="width:${(count / max) * 100}%"></i></span>
      <span class="num">${fmtNum(count)}</span>
    </div>`).join("")}</div>`;
}

function drawTimeline(canvas, points) {
  const ctx = canvas.getContext("2d");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 600;
  const height = 170;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);
  if (!points || !points.length) return;
  const max = Math.max(...points.map((p) => p.sessions)) || 1;
  const pad = 6;
  const barWidth = Math.max(2, (width - pad * 2) / points.length - 3);
  ctx.fillStyle = "#4c9aff";
  points.forEach((point, index) => {
    const barHeight = ((height - 24) * point.sessions) / max;
    const x = pad + index * ((width - pad * 2) / points.length);
    const y = height - 18 - barHeight;
    ctx.fillRect(x, y, barWidth, barHeight);
  });
  ctx.fillStyle = "#8b98a9";
  ctx.font = "10px sans-serif";
  ctx.fillText(points[0].day, pad, height - 4);
  const last = points[points.length - 1].day;
  ctx.fillText(last, width - ctx.measureText(last).width - pad, height - 4);
}

/* --------------------------------------------------------------- sessions */
function bindSessions() {
  if (state.sessionBound) return;
  state.sessionBound = true;

  const apply = () => {
    state.page = 1;
    syncUrl();
    renderSessions();
  };
  $("#f-search").addEventListener("input", debounce(() => {
    state.filters.search = $("#f-search").value;
    apply();
  }));
  ["#f-project", "#f-directory", "#f-agent", "#f-model", "#f-state", "#f-kind"].forEach((sel) => {
    $(sel).addEventListener("change", () => {
      const key = sel.replace("#f-", "");
      state.filters[key === "state" ? "state" : key] = $(sel).value;
      apply();
    });
  });
  ["#f-from", "#f-to"].forEach((sel) => {
    $(sel).addEventListener("change", () => {
      const key = sel.replace("#f-", "");
      const value = $(sel).value;
      state.filters[key === "from" ? "created_from" : "created_to"] = value
        ? new Date(value + "T00:00:00").getTime() : "";
      apply();
    });
  });
  $("#filter-reset").addEventListener("click", () => {
    Object.keys(state.filters).forEach((key) => { state.filters[key] = ""; });
    ["#f-search", "#f-from", "#f-to"].forEach((sel) => { $(sel).value = ""; });
    ["#f-project", "#f-directory", "#f-agent", "#f-model", "#f-state", "#f-kind"]
      .forEach((sel) => { $(sel).value = ""; });
    state.page = 1;
    syncUrl();
    renderSessions();
  });

  $$("#sort-chips .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const sort = chip.dataset.sort;
      if (state.sort === sort) {
        state.order = state.order === "desc" ? "asc" : "desc";
      } else {
        state.sort = sort;
        state.order = "desc";
      }
      localStorage.setItem("ocd.sort", state.sort);
      localStorage.setItem("ocd.order", state.order);
      state.page = 1;
      renderSessions();
    });
  });

  $$("#view-toggle button").forEach((button) => {
    button.addEventListener("click", () => {
      $$("#view-toggle button").forEach((b) => b.classList.remove("active"));
      button.classList.add("active");
      state.mode = button.dataset.mode;
      localStorage.setItem("ocd.mode", state.mode);
      renderSessions();
    });
  });

  $("#export-csv").addEventListener("click", () => {
    window.location.href = `/api/export/sessions.csv${sessionQuery(1000)}`;
  });
  $("#export-json").addEventListener("click", () => {
    window.location.href = `/api/export/sessions.json${sessionQuery(1000)}`;
  });
}

function sessionQuery(limit) {
  const params = { ...state.filters, sort: state.sort, order: state.order, limit };
  return queryString(params);
}

function activeFilterCount() {
  return Object.values(state.filters).filter((v) => v !== "" && v !== null).length;
}

async function renderSessions() {
  bindSessions();
  const badge = $("#filter-badge");
  const count = activeFilterCount();
  badge.textContent = count;
  badge.classList.toggle("hidden", count === 0);

  $$("#sort-chips .chip").forEach((chip) => {
    chip.classList.toggle("active", chip.dataset.sort === state.sort);
    chip.textContent = chip.textContent.replace(/ [▲▼]$/, "") +
      (chip.dataset.sort === state.sort ? (state.order === "asc" ? " ▲" : " ▼") : "");
  });
  $$("#view-toggle button").forEach((b) => b.classList.toggle("active", b.dataset.mode === state.mode));

  const offset = (state.page - 1) * state.limit;
  const listParams = { ...state.filters, sort: state.sort, order: state.order,
    limit: state.limit, offset };
  const [list, stats] = await Promise.all([
    api(`/api/sessions${queryString(listParams)}`),
    api(`/api/stats${queryString(state.filters)}`),
  ]);

  $("#session-stats").innerHTML = `
    <span><b>${fmtNum(list.total)}</b> sessions</span>
    <span>in-tokens <b>${fmtNum(stats.tokens_input)}</b></span>
    <span>cost <b>$${(stats.cost || 0).toFixed(2)}</b></span>
    <span>${stats.first_created ? fmtDate(new Date(stats.first_created).getTime()) : ""} → ${stats.last_created ? fmtDate(new Date(stats.last_created).getTime()) : ""}</span>
    <span>top model <b>${esc(stats.by_model?.[0]?.key || "-")}</b></span>`;

  const container = $("#sessions-table");
  state.matchingTotal = list.total;
  const checkCell = (s) => `<input type="checkbox" class="sel-box" data-id="${esc(s.id)}"
      ${(state.selectAllMatching || state.selection.has(s.id)) ? "checked" : ""}>`;
  if (state.mode === "grid") {
    container.className = "grid-cards";
    container.innerHTML = list.items.map((s) => `
      <div class="scard" data-id="${esc(s.id)}">
        <div class="scard-head">
          <input type="checkbox" class="sel-box" data-id="${esc(s.id)}"
            ${(state.selectAllMatching || state.selection.has(s.id)) ? "checked" : ""}>
          <h3>${esc(s.title || "(untitled)")}</h3>
        </div>
        <div class="meta">${esc(s.directory || "")}</div>
        <div class="meta">${fmtNum(s.msg_count)} msgs · ${esc(s.agent || "-")} · ${esc(s.model_label || "-")}</div>
        <div class="meta">${esc(s.created_iso || "")}</div>
      </div>`).join("");
  } else if (state.mode === "compact") {
    container.className = "compact-list";
    container.innerHTML = list.items.map((s) => `
      <div class="compact-row" data-id="${esc(s.id)}">
        <input type="checkbox" class="sel-box" data-id="${esc(s.id)}"
          ${(state.selectAllMatching || state.selection.has(s.id)) ? "checked" : ""}>
        <span>${esc(s.created_iso || "")}</span>
        <span class="state state-${esc(s.state)}">${esc(s.state)}</span>
        <span>${esc(s.title || "(untitled)")}</span>
        <span>${fmtNum(s.msg_count)} msg</span>
      </div>`).join("");
  } else {
    container.className = "table-wrap";
    container.innerHTML = tableHtml([
      `<input type="checkbox" id="sel-all-page" title="Select every row on this page">`,
      "Title", "State", "Kind", "Directory", "Agent", "Model", "Msgs", "Seq", "Event", "Cost",
      "Created", "Updated",
    ], list.items.map((s) => [
      checkCell(s),
      `<span class="truncate" title="${esc(s.title || "")}">${esc(s.title || "(untitled)")}</span>`,
      `<span class="state state-${esc(s.state)}">${esc(s.state)}</span>`,
      `<span class="kind">${esc(s.kind)}</span>`,
      `<span class="truncate mono" title="${esc(s.directory || "")}">${esc(s.directory || "")}</span>`,
      esc(s.agent || ""),
      `<span class="truncate" title="${esc(s.model_label || "")}">${esc(s.model_label || "")}</span>`,
      fmtNum(s.msg_count),
      fmtNum(s.max_seq),
      s.event_seq === null ? "—" : fmtNum(s.event_seq),
      `$${(s.cost || 0).toFixed(2)}`,
      esc(fmtDate(s.time_created)),
      esc(fmtDate(s.time_updated)),
    ]), true, list.items.map((s) => s.id));
  }

  $$("[data-id]", container).forEach((row) => {
    if (row.classList.contains("sel-box")) return;
    row.addEventListener("click", (event) => {
      if (event.target.classList.contains("sel-box")) return;
      openSession(row.dataset.id);
    });
  });
  $$(".sel-box", container).forEach((box) => {
    box.addEventListener("click", (event) => event.stopPropagation());
    box.addEventListener("change", () => {
      state.selectAllMatching = false;
      if (box.checked) state.selection.add(box.dataset.id);
      else state.selection.delete(box.dataset.id);
      updateSessionBulkBar();
    });
  });
  const selectAll = $("#sel-all-page");
  if (selectAll) {
    selectAll.checked = list.items.length > 0 &&
      list.items.every((s) => state.selectAllMatching || state.selection.has(s.id));
    selectAll.addEventListener("change", () => {
      state.selectAllMatching = false;
      list.items.forEach((s) => {
        if (selectAll.checked) state.selection.add(s.id);
        else state.selection.delete(s.id);
      });
      renderSessions();
    });
  }
  updateSessionBulkBar();

  const pages = Math.max(1, Math.ceil(list.total / state.limit));
  $("#sessions-pager").innerHTML = `
    <button class="btn" id="prev-page" ${state.page <= 1 ? "disabled" : ""}>← Prev</button>
    <span>page ${state.page} of ${pages} · ${fmtNum(list.total)} rows</span>
    <button class="btn" id="next-page" ${state.page >= pages ? "disabled" : ""}>Next →</button>`;
  const prev = $("#prev-page");
  const next = $("#next-page");
  if (prev) prev.addEventListener("click", () => { state.page -= 1; renderSessions(); });
  if (next) next.addEventListener("click", () => { state.page += 1; renderSessions(); });
}

/* --------------------------------------------------------- bulk selection */
function updateSessionBulkBar() {
  const bar = $("#session-bulkbar");
  if (!bar) return;
  const count = state.selectAllMatching ? state.matchingTotal : state.selection.size;
  bar.classList.toggle("hidden", count === 0);
  $("#session-selection-count").textContent = state.selectAllMatching
    ? `${fmtNum(count)} sessions (all matching)`
    : `${fmtNum(count)} selected`;
  const matching = bar.querySelector('[data-bulk="select-all"]');
  if (matching) matching.textContent = `Select all ${fmtNum(state.matchingTotal)} matching`;
  $$('[data-bulk]', bar).forEach((button) => {
    if (button.dataset.bulk === "select-all") {
      button.classList.toggle("active", state.selectAllMatching);
      return;
    }
    button.disabled = count === 0;
  });
}

function updateProjectBulkBar() {
  const bar = $("#project-bulkbar");
  if (!bar) return;
  const count = state.projectSelectAllMatching ? state.projectMatchingTotal
    : state.projectSelection.size;
  bar.classList.toggle("hidden", count === 0);
  $("#project-selection-count").textContent = state.projectSelectAllMatching
    ? `${fmtNum(count)} projects (all matching)`
    : `${fmtNum(count)} selected`;
  const matching = bar.querySelector('[data-bulk="select-all"]');
  if (matching) matching.textContent = `Select all ${fmtNum(state.projectMatchingTotal)} matching`;
  $$('[data-bulk]', bar).forEach((button) => {
    if (button.dataset.bulk === "select-all") {
      button.classList.toggle("active", state.projectSelectAllMatching);
      return;
    }
    button.disabled = count === 0;
  });
}

function sessionSelectionPayload() {
  if (state.selectAllMatching) return { filters: { ...state.filters } };
  return { ids: Array.from(state.selection) };
}

function projectSelectionPayload() {
  if (state.projectSelectAllMatching) {
    return { filters: { search: $("#p-search").value.trim() } };
  }
  return { ids: Array.from(state.projectSelection) };
}

function bindBulkActions() {
  if (state.bulkBound) return;
  state.bulkBound = true;

  $("#session-bulkbar").addEventListener("click", (event) => {
    const button = event.target.closest("[data-bulk]");
    if (!button) return;
    handleSessionBulk(button.dataset.bulk);
  });
  $("#project-bulkbar").addEventListener("click", (event) => {
    const button = event.target.closest("[data-bulk]");
    if (!button) return;
    handleProjectBulk(button.dataset.bulk);
  });
}

async function handleSessionBulk(action) {
  const payload = sessionSelectionPayload();
  const count = state.selectAllMatching ? state.matchingTotal : state.selection.size;
  if (action === "clear") {
    state.selection.clear();
    state.selectAllMatching = false;
    renderSessions();
    return;
  }
  if (action === "select-all") {
    state.selectAllMatching = !state.selectAllMatching;
    state.selection.clear();
    renderSessions();
    return;
  }
  if (action === "export-markdown") {
    if (!window.confirm(`Export ${count} session(s) to Markdown?`)) return;
    await runMaintenance("export-markdown", payload, "#sessions-result");
    return;
  }
  if (action === "export-bundle") {
    const ids = state.selectAllMatching
      ? (await api(`/api/sessions${queryString({ ...state.filters, limit: 1000 })}`)).items.map((s) => s.id)
      : Array.from(state.selection);
    if (!ids.length) return;
    toast(`exporting ${ids.length} bundle(s)…`);
    let done = 0;
    for (const id of ids) {
      const result = await runMaintenance("export-bundle", { session_id: id }, "#sessions-result");
      if (result && result.ok) done += 1;
    }
    toast(`${done}/${ids.length} bundles exported`, "ok");
    return;
  }
  if (action === "move") {
    const target = window.prompt("Move the selected sessions to which directory?");
    if (!target) return;
    await runMaintenance("move-sessions", { ...payload, target }, "#sessions-result");
    return;
  }
  if (action === "delete") {
    const warning = state.selectAllMatching
      ? `Delete ALL ${count} sessions matching the current filter?\n\nThis is irreversible.`
      : `Delete ${count} selected session(s) permanently?\n\nThis is irreversible.`;
    if (!window.confirm(warning)) return;
    const result = await runMaintenance("delete-sessions", payload, "#sessions-result");
    if (result && result.ok) {
      state.selection.clear();
      state.selectAllMatching = false;
      renderSessions();
    }
  }
}

async function handleProjectBulk(action) {
  const payload = projectSelectionPayload();
  const count = state.projectSelectAllMatching ? state.projectMatchingTotal
    : state.projectSelection.size;
  if (action === "clear") {
    state.projectSelection.clear();
    state.projectSelectAllMatching = false;
    renderProjects();
    return;
  }
  if (action === "select-all") {
    state.projectSelectAllMatching = !state.projectSelectAllMatching;
    state.projectSelection.clear();
    renderProjects();
    return;
  }
  if (action === "move") {
    const target = window.prompt("Move the sessions of the selected projects to which directory?");
    if (!target) return;
    const ids = state.projectSelectAllMatching
      ? (await api(`/api/projects${queryString({ search: $("#p-search").value.trim(), limit: 1000 })}`))
        .items.map((p) => p.id)
      : Array.from(state.projectSelection);
    const moved = [];
    for (const projectId of ids) {
      const result = await runMaintenance("move-sessions",
        { target, filters: { project_id: projectId } }, "#projects-result");
      if (result && result.ok) moved.push(projectId);
    }
    toast(`moved sessions of ${moved.length}/${ids.length} project(s)`, "ok");
    return;
  }
  if (action === "delete") {
    const confirmed = window.confirm(
      `Delete ${count} selected project(s)?\n\n` +
      "Projects that still own sessions will be refused unless you confirm the second step.");
    if (!confirmed) return;
    const result = await runMaintenance("delete-projects", payload, "#projects-result");
    if (result && result.ok) {
      state.projectSelection.clear();
      state.projectSelectAllMatching = false;
      renderProjects();
      return;
    }
    // The server refused because the selection still owns sessions. Destroying
    // them is irreversible, so the user has to type the word.
    const answer = window.prompt(
      "Deleting a project CASCADES: every session that belongs to it is destroyed.\n\n" +
      (state.lastError ? state.lastError + "\n\n" : "") +
      "Type DELETE to destroy those sessions as well, anything else cancels.");
    if (!answer || answer.trim().toUpperCase() !== "DELETE") return;
    const forced = await runMaintenance("delete-projects",
      { ...payload, delete_sessions: true }, "#projects-result");
    if (forced && forced.ok) {
      state.projectSelection.clear();
      state.projectSelectAllMatching = false;
      renderProjects();
    }
  }
}

/* ---------------------------------------------------------------- drawer */
async function openSession(sessionId, options = {}) {
  const { updateUrl = true } = options;
  $("#drawer").classList.remove("hidden");
  state.msgOffset = 0;
  if (updateUrl) {
    navigate("sessions", { params: { session_id: sessionId }, query: sessionQueryString() });
  }
  try {
    const detail = await api(`/api/sessions/${sessionId}`);
    state.session = detail;
    $("#drawer-title").textContent = detail.title || "(untitled)";
    $("#drawer-sub").textContent = `${detail.id} · ${detail.directory || ""}`;
    $("#drawer-meta").innerHTML = kvRows([
      ["state", detail.state],
      ["kind", detail.kind],
      ["agent", detail.agent],
      ["model", detail.model_label],
      ["project", detail.project_id],
      ["messages", fmtNum(detail.msg_count)],
      ["seq max", fmtNum(detail.max_seq)],
      ["event seq", detail.event_seq === null ? "—" : fmtNum(detail.event_seq)],
      ["cost", `$${(detail.cost || 0).toFixed(4)}`],
      ["tokens", `${fmtNum(detail.tokens_input)} in / ${fmtNum(detail.tokens_output)} out / ${fmtNum(detail.tokens_reasoning)} reasoning`],
      ["cached", fmtNum(detail.tokens_cache_read)],
      ["created", fmtTime(detail.time_created)],
      ["updated", fmtTime(detail.time_updated)],
      ["duration", detail.duration_ms ? `${Math.round(detail.duration_ms / 60000)} min` : ""],
      ["parent", detail.parent_id || ""],
      ["children", (detail.children || []).length],
      ["events", detail.events],
      ["parts", detail.parts],
      ["legacy row", detail.legacy_row],
      ["todos", (detail.todos || []).length],
    ]);
    $("#bundle-export").onclick = () => runMaintenance("export-bundle", { session_id: detail.id },
      "#m-export-result");
    loadMessages();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function loadMessages() {
  if (!state.session) return;
  const params = queryString({
    limit: 50,
    offset: state.msgOffset,
    type: $("#msg-type").value,
    search: $("#msg-search").value,
  });
  const data = await api(`/api/sessions/${state.session.id}/messages${params}`);
  state.msgTotal = data.total;
  const types = new Set((state.session.messages_by_type || []).map((m) => m.type));
  const select = $("#msg-type");
  const current = select.value;
  select.innerHTML = '<option value="">All types</option>';
  Array.from(types).sort().forEach((type) => {
    const option = document.createElement("option");
    option.value = type;
    option.textContent = type;
    select.appendChild(option);
  });
  select.value = current;

  $("#drawer-messages").innerHTML = data.items.map((m) => {
    const body = m.text ? esc(m.text) : `<pre>${esc(JSON.stringify(m.data).slice(0, 4000))}</pre>`;
    return `<div class="msg ${esc(m.type)}">
      <div class="head"><b>${esc(m.type)}</b><span>seq ${m.seq}</span>
        <span>${esc(m.time_created_iso || "")}</span><span>${fmtBytes(m.data_bytes)}</span></div>
      <div class="body">${body}</div></div>`;
  }).join("") || `<p class="muted small">no messages</p>`;

  const pages = Math.max(1, Math.ceil(data.total / 50));
  $("#drawer-pager").innerHTML = `
    <button class="btn" id="msg-prev" ${state.msgOffset === 0 ? "disabled" : ""}>← Prev</button>
    <span>${fmtNum(data.total)} messages · page ${Math.floor(state.msgOffset / 50) + 1} of ${pages}</span>
    <button class="btn" id="msg-next" ${state.msgOffset + 50 >= data.total ? "disabled" : ""}>Next →</button>`;
  const prev = $("#msg-prev");
  const next = $("#msg-next");
  if (prev) prev.addEventListener("click", () => { state.msgOffset = Math.max(0, state.msgOffset - 50); loadMessages(); });
  if (next) next.addEventListener("click", () => { state.msgOffset += 50; loadMessages(); });
}

function bindDrawer() {
  if (state.drawerBound) return;
  state.drawerBound = true;
  $("#msg-search").addEventListener("input", debounce(() => { state.msgOffset = 0; loadMessages(); }));
  $("#msg-type").addEventListener("change", () => { state.msgOffset = 0; loadMessages(); });
}

/* -------------------------------------------------------------- projects */
async function renderProjects() {
  bindBulkActions();
  const search = $("#p-search").value.trim();
  const data = await api(`/api/projects${queryString({ search, limit: 500 })}`);
  state.projectMatchingTotal = data.total;
  $("#projects-table").innerHTML = tableHtml([
    `<input type="checkbox" id="sel-all-projects" title="Select every row on this page">`,
    "Project", "Directory", "Sessions", "Messages", "Cost", "On disk", "Created", "Active",
  ], data.items.map((p) => [
    `<input type="checkbox" class="sel-box project-sel" data-id="${esc(p.id)}"
      ${(state.projectSelectAllMatching || state.projectSelection.has(p.id)) ? "checked" : ""}>`,
    `<span class="truncate mono" title="${esc(p.id)}">${esc(p.id)}</span>`,
    `<span class="truncate mono clickable" data-project="${esc(p.id)}" title="${esc(p.worktree)}">${esc(p.worktree)}</span>`,
    fmtNum(p.sessions),
    fmtNum(p.messages),
    `$${(p.cost || 0).toFixed(2)}`,
    p.exists_on_disk ? "yes" : "no",
    esc(fmtDate(p.time_created)),
    esc(fmtDate(p.time_active)),
  ]), true);
  $$(".project-sel").forEach((box) => {
    box.addEventListener("change", () => {
      state.projectSelectAllMatching = false;
      if (box.checked) state.projectSelection.add(box.dataset.id);
      else state.projectSelection.delete(box.dataset.id);
      updateProjectBulkBar();
    });
  });
  const selectAll = $("#sel-all-projects");
  if (selectAll) {
    selectAll.checked = data.items.length > 0 &&
      data.items.every((p) => state.projectSelectAllMatching || state.projectSelection.has(p.id));
    selectAll.addEventListener("change", () => {
      state.projectSelectAllMatching = false;
      data.items.forEach((p) => {
        if (selectAll.checked) state.projectSelection.add(p.id);
        else state.projectSelection.delete(p.id);
      });
      renderProjects();
    });
  }
  updateProjectBulkBar();
  $$("[data-project]").forEach((link) => {
    link.addEventListener("click", () => {
      state.filters.project_id = link.dataset.project;
      $("#f-project").value = link.dataset.project;
      switchView("sessions");
    });
  });
  if (!$("#p-search").dataset.bound) {
    $("#p-search").dataset.bound = "1";
    $("#p-search").addEventListener("input", debounce(() => {
      syncUrl();
      renderProjects();
    }, 300));
    $("#export-projects-csv").addEventListener("click", () => {
      window.location.href = `/api/export/projects.csv${queryString({ search })}`;
    });
  }
}

/* ----------------------------------------------------------- directories */
async function renderDirectories() {
  const data = await api("/api/directories");
  $("#directories-summary").innerHTML = `
    <span><b>${fmtNum(data.total)}</b> directories</span>
    <span><b>${fmtNum(data.session_directories)}</b> with sessions</span>
    <span><b>${fmtNum(data.project_directories)}</b> registered projects</span>
    <span><b>${fmtNum(data.orphan_directories)}</b> without sessions</span>`;
  $("#directories-table").innerHTML = tableHtml(
    ["Directory", "Sessions", "Messages", "Sources", "Projects", "On disk"],
    data.items.map((d) => [
      `<span class="truncate mono" title="${esc(d.directory)}">${esc(d.directory)}</span>`,
      fmtNum(d.sessions),
      fmtNum(d.messages),
      esc((d.sources || []).join(", ")),
      `<span class="truncate mono">${esc((d.project_ids || []).join(", "))}</span>`,
      d.exists_on_disk ? "yes" : "no",
    ]));
}

/* --------------------------------------------------------------- backups */
async function renderBackups() {
  const data = await api("/api/backups");
  $("#backup-dir").textContent = data.dashboard_backup_dir;
  $("#backups-table").innerHTML = tableHtml(
    ["File", "Kind", "Source", "Size", "Modified", ""],
    data.items.map((b) => [
      `<span class="truncate mono" title="${esc(b.path)}">${esc(b.name)}</span>`,
      esc(b.kind),
      esc(b.source),
      fmtBytes(b.bytes),
      esc(b.mtime_iso),
      b.kind === "database"
        ? `<button class="btn" data-verify="${esc(b.path)}">Verify</button>`
        : "",
    ]));
  $$("[data-verify]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      $("#backup-status").textContent = "checking…";
      try {
        const status = await api(`/api/backups/status${queryString({ path: button.dataset.verify })}`);
        $("#backup-status").innerHTML = `<div class="result ${status.ok ? "ok" : "err"}">${esc(
          JSON.stringify(status, null, 2))}</div>`;
      } catch (err) {
        $("#backup-status").innerHTML = `<div class="result err">${esc(err.message)}</div>`;
      } finally {
        button.disabled = false;
      }
    });
  });
  if (!$("#backup-create").dataset.bound) {
    $("#backup-create").dataset.bound = "1";
    $("#backup-create").addEventListener("click", async () => {
      const button = $("#backup-create");
      button.disabled = true;
      button.textContent = "backing up…";
      try {
        const result = await api("/api/maintenance/backup", {
          method: "POST",
          body: JSON.stringify({
            confirm: true,
            label: $("#backup-label").value,
            to_opencode_dir: $("#backup-opencode-dir").checked,
          }),
        });
        toast(`backup written: ${result.details.path}`, "ok");
        await renderBackups();
      } catch (err) {
        toast(err.message, "err");
      } finally {
        button.disabled = false;
        button.textContent = "＋ Create backup";
      }
    });
  }
}

/* ----------------------------------------------------------- maintenance */
async function renderMaintenance() {
  bindDrawer();
  // the maintenance buttons live in the static markup: they have to be bound
  // when this view is shown, not only when the schema view happens to render
  bindMaintenance();
  const audit = await api("/api/audit?limit=100");
  $("#audit-table").innerHTML = tableHtml(
    ["When", "Action", "OK", "Message", "Payload"],
    audit.items.map((entry) => [
      esc(entry.at),
      esc(entry.action),
      entry.ok ? "yes" : "no",
      esc(entry.message || ""),
      `<span class="truncate mono">${esc(JSON.stringify(entry.payload))}</span>`,
    ]));
}

function maintenancePayload(operation) {
  switch (operation) {
    case "backup": return { label: $("#m-backup-label").value || "" };
    case "vacuum": return { secure: $("#m-vacuum-secure").checked };
    case "prune-projects": return { keep: $("#m-prune-keep").value };
    case "move-sessions": return { target: $("#m-move-target").value, all: true };
    case "export-markdown": return { all: true };
    case "export-bundle": return { session_id: $("#m-bundle-id").value };
    case "restore-bundle": return { path: $("#m-restore-path").value };
    case "delete-sessions": return {
      ids: $("#m-delete-ids").value.split(",").map((s) => s.trim()).filter(Boolean),
    };
    default: return {};
  }
}

async function runMaintenance(operation, payload, targetSelector) {
  const body = { ...payload, confirm: true };
  state.lastError = "";
  try {
    const result = await api(`/api/maintenance/${operation}`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (targetSelector) {
      $(targetSelector).className = `result ${result.ok ? "ok" : "err"}`;
      $(targetSelector).textContent = JSON.stringify(result, null, 2);
    }
    toast(`${operation}: ${result.message || "done"}`, result.ok ? "ok" : "err");
    return result;
  } catch (err) {
    state.lastError = err.message;
    if (targetSelector) {
      $(targetSelector).className = "result err";
      $(targetSelector).textContent = err.message;
    }
    toast(err.message, "err");
    return null;
  }
}

function bindMaintenance() {
  if (state.maintBound) return;
  state.maintBound = true;
  $$('[data-op]').forEach((button) => {
    button.addEventListener("click", async () => {
      const operation = button.dataset.op;
      if (operation === "verify") {
        button.disabled = true;
        try {
          const result = await api("/api/backups/status?path=" + encodeURIComponent(
            (await api("/api/health")).database));
          $("#m-verify-result").className = `result ${result.ok ? "ok" : "err"}`;
          $("#m-verify-result").textContent = JSON.stringify(result, null, 2);
        } catch (err) {
          $("#m-verify-result").className = "result err";
          $("#m-verify-result").textContent = err.message;
        } finally {
          button.disabled = false;
        }
        return;
      }
      if (operation === "checkpoint" || operation === "vacuum") {
        await runMaintenance(operation, maintenancePayload(operation), "#m-shrink-result");
        return;
      }
      if (operation === "repair-sequences") {
        await runMaintenance(operation, {}, "#m-repair-result");
        return;
      }
      if (operation === "prune-event-sequences") {
        if (!window.confirm("Delete the counters of sessions that no longer exist?")) return;
        await runMaintenance(operation, {}, "#m-repair-result");
        return;
      }
      if (operation === "prune-projects") {
        await runMaintenance(operation, maintenancePayload(operation), "#m-prune-result");
        return;
      }
      if (operation === "move-sessions") {
        await runMaintenance(operation, maintenancePayload(operation), "#m-move-result");
        return;
      }
      if (operation === "export-markdown" || operation === "export-bundle") {
        await runMaintenance(operation, maintenancePayload(operation), "#m-export-result");
        return;
      }
      if (operation === "restore-bundle") {
        await runMaintenance(operation, maintenancePayload(operation), "#m-restore-result");
        return;
      }
      if (operation === "delete-sessions") {
        const ids = maintenancePayload(operation).ids;
        if (!ids.length) { toast("no session ids given", "err"); return; }
        if (!window.confirm(`Delete ${ids.length} session(s) permanently?\n${ids.join("\n")}`)) return;
        await runMaintenance(operation, { ids }, "#m-delete-result");
      }
    });
  });
}

/* ---------------------------------------------------------------- schema */
async function renderSchema() {
  bindMaintenance();
  const data = await api("/api/tables");
  $("#schema-tables").innerHTML = tableHtml(
    ["Table", "Rows", "Columns", "Indexes"],
    data.items.map((t) => [
      `<span class="clickable" data-table="${esc(t.name)}">${esc(t.name)}</span>`,
      fmtNum(t.rows),
      t.columns.length,
      t.indexes.map((i) => `${i.name}${i.unique ? " *" : ""}`).join(", "),
    ]));
  $$("[data-table]").forEach((link) => {
    link.addEventListener("click", () => {
      $("#schema-table-select").value = link.dataset.table;
      loadSchemaRows();
    });
  });
  const select = $("#schema-table-select");
  if (!select.options.length) {
    data.items.forEach((t) => {
      const option = document.createElement("option");
      option.value = t.name;
      option.textContent = `${t.name} (${fmtNum(t.rows)})`;
      select.appendChild(option);
    });
  }
  if (!$("#schema-load").dataset.bound) {
    $("#schema-load").dataset.bound = "1";
    $("#schema-load").addEventListener("click", loadSchemaRows);
    $("#sql-run").addEventListener("click", runSql);
    $("#sql-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) runSql();
    });
  }
  if (!$("#schema-rows").dataset.loaded) {
    $("#schema-rows").dataset.loaded = "1";
    loadSchemaRows();
  }
}

async function loadSchemaRows() {
  const table = $("#schema-table-select").value;
  if (!table) return;
  try {
    const data = await api(`/api/tables/${encodeURIComponent(table)}/rows${queryString({
      where: $("#schema-where").value, limit: 100,
    })}`);
    const columns = data.columns;
    $("#schema-rows").innerHTML = tableHtml(
      columns.map((c) => c),
      data.items.map((row) => columns.map((c) => {
        const value = row[c];
        const text = value === null || value === undefined ? "—"
          : typeof value === "object" ? JSON.stringify(value) : String(value);
        return `<span class="truncate" title="${esc(text)}">${esc(text)}</span>`;
      })));
  } catch (err) {
    $("#schema-rows").innerHTML = `<div class="result err">${esc(err.message)}</div>`;
  }
}

async function runSql() {
  const sql = $("#sql-input").value.trim();
  if (!sql) return;
  try {
    const data = await api("/api/query", { method: "POST", body: JSON.stringify({ sql, limit: 200 }) });
    const items = data.items || [];
    if (!items.length) {
      $("#sql-result").innerHTML = `<p class="muted small">no rows</p>`;
      return;
    }
    const columns = Object.keys(items[0]);
    $("#sql-result").innerHTML = tableHtml(columns, items.map((row) => columns.map((c) => {
      const value = row[c];
      const text = value === null || value === undefined ? "—"
        : typeof value === "object" ? JSON.stringify(value) : String(value);
      return `<span class="truncate" title="${esc(text)}">${esc(text)}</span>`;
    })));
  } catch (err) {
    $("#sql-result").innerHTML = `<div class="result err">${esc(err.message)}</div>`;
  }
}

/* --------------------------------------------------------------- storage */
async function renderStorage() {
  const [storage, logs] = await Promise.all([api("/api/storage"), api("/api/logs")]);
  $("#storage-table").innerHTML = tableHtml(
    ["Area", "Path", "Files", "Size", "Exists"],
    storage.items.map((s) => [
      esc(s.label),
      `<span class="truncate mono" title="${esc(s.path)}">${esc(s.path)}</span>`,
      fmtNum(s.files),
      fmtBytes(s.bytes),
      s.exists ? "yes" : "no",
    ]));
  $("#log-table").innerHTML = tableHtml(
    ["Log", "Size", "Modified", ""],
    logs.items.map((l) => [
      `<span class="truncate mono" title="${esc(l.path)}">${esc(l.name)}</span>`,
      fmtBytes(l.bytes),
      esc(l.mtime_iso),
      `<button class="btn" data-log="${esc(l.path)}">Tail</button>`,
    ]));
  $$("[data-log]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        const data = await api(`/api/logs/tail${queryString({ path: button.dataset.log, lines: 400 })}`);
        $("#log-tail").textContent = data.lines.join("\n");
      } catch (err) {
        $("#log-tail").textContent = err.message;
      }
    });
  });
}

/* ----------------------------------------------------------------- table */
function tableHtml(headers, rows, rawHeaders = false, rowIds = null) {
  if (!rows.length) return `<p class="muted small" style="padding:12px">no rows</p>`;
  const head = headers.map((h) => `<th>${rawHeaders ? h : esc(h)}</th>`).join("");
  const body = rows.map((row, index) => {
    const id = rowIds && rowIds[index] ? ` data-id="${esc(rowIds[index])}"` : "";
    return `<tr${id}>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`;
  }).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

boot().catch((err) => toast(err.message, "err"));
