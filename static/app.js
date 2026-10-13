// Claude Usage Dashboard — single-page client.
// Plain script (no modules), talks to /api/* endpoints.
//
// Shared building blocks, reused by every section (and meant for new ones):
//   api(path, params, opts)          fetch with the active window/account/project filters
//   runCard(key, loader)             loading skeleton, held previous render, per-card retry
//   renderTable(table, cols, rows)   sortable table with aria-sort; rows=null draws a skeleton
//   upsertChart(id, build, a11y)     Chart.js instance that re-themes live; a11y label + summary
//   readTheme() / T                  colour tokens read from CSS custom properties
//   modelSlots(models)               sticky top-5 + "other" colour assignment for models

// ---------- reconciliation keys ----------
// The backend is still naming these figures. Every reconciliation value is read
// through this one map (via rk()), so a rename on the server is a one-line edit
// here. The figure is labelled "Gap vs Claude Code's own tally": only part of it
// is calls the transcript never logs, the rest is not explained yet.
const RECON = {
  // /api/reconciliation -> totals
  totalSessions:    "total_sessions",
  comparedSessions: "reconciled_sessions",
  sessionCoverage:  "coverage_pct",
  costCoverage:     "cost_coverage_pct",
  ours:             "our_cost_reconciled_usd",
  theirs:           "cc_cost_reconciled_usd",
  gap:              "cc_gap_usd",                   // signed, theirs - ours; computed when absent
  gapUnlogged:      "side_call_cost_usd",           // evidenced calls the transcript never logs
  gapPartial:       "subagent_partial_output_usd",  // subagent output logged before its final usage
  gapUnexplained:   "unexplained_gap_usd",          // the rest; the split shows only when this exists
  gapWebSearch:     "side_call_web_search_usd",
  webSearches:      "web_search_requests",
  // /api/reconciliation -> rows[]
  rowOurs:          "our_cost_usd",
  rowTheirs:        "cc_cost_usd",
  rowDelta:         "delta_usd",
  rowUnlogged:      "side_call_cost_usd",
  rowPartial:       "subagent_partial_output_usd",
  rowUnexplained:   "unexplained_gap_usd",
  rowWebSearches:   "web_search_requests",
};
const rk = (obj, name) => obj?.[RECON[name]];

// ---------- formatting ----------
const fmtInt    = n => (n == null) ? "—" : Math.round(n).toLocaleString();
const fmtCompact= n => (n == null) ? "—" : Intl.NumberFormat("en", {notation:"compact", maximumFractionDigits:1}).format(n);
const fmtMoney  = n => (n == null) ? "—" : "$" + (Math.round(n*100)/100).toLocaleString("en", {minimumFractionDigits:2, maximumFractionDigits:2});
const fmtMoneyShort = n => (n == null) ? "—" : (Math.abs(n) >= 1000 ? "$" + fmtCompact(n) : fmtMoney(n));
const fmtDate   = s => s ? new Date(s).toLocaleString() : "—";

// ---------- dates: browser locale via Intl, never a hard-coded order ----------
const DF = {
  md:   new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }),
  mdy:  new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }),
  wmdy: new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" }),
  mon:  new Intl.DateTimeFormat(undefined, { month: "short" }),
  mony: new Intl.DateTimeFormat(undefined, { month: "short", year: "numeric" }),
};
const isoLocal = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
// "2026-09-24" is a local calendar day; anything else (a timestamp, a Date) passes through Date.
const toDate = v => v instanceof Date ? new Date(v)
  : (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v) ? new Date(v + "T00:00:00") : new Date(v));
const addDays = (d, n) => { const x = toDate(d); x.setDate(x.getDate() + n); return x; };
// "Sep 24" this year, "Sep 24, 2025" otherwise.
const fmtDay = s => {
  if (!s) return "—";
  const d = toDate(s);
  return (d.getFullYear() === new Date().getFullYear() ? DF.md : DF.mdy).format(d);
};
// Range between two timestamps, as local days.
const tsRange = (a, b) => (a && b) ? fmtRange(new Date(a), new Date(b)) : a ? fmtRange(new Date(a)) : "—";
const fmtDayLong = s => s ? DF.wmdy.format(toDate(s)) : "—";
// "Feb 7 – Oct 7, 2026" in one year, "Dec 19, 2025 – Feb 3, 2026" across years, "Sep 24, 2026" for one day.
function fmtRange(a, b = a) {
  const x = toDate(a), y = toDate(b);
  if (isoLocal(x) === isoLocal(y)) return DF.mdy.format(x);
  try { return DF.mdy.formatRange(x, y); }
  catch { return `${(x.getFullYear() === y.getFullYear() ? DF.md : DF.mdy).format(x)} – ${DF.mdy.format(y)}`; }
}
// Day-axis ticks: a fixed stride (no autoSkip, so we know which ticks show), and a year
// on the first tick and at each year boundary when the labels span more than one year.
function dayTicks(labels, fmtOne) {
  const limit = window.innerWidth < 640 ? 4 : 9;
  const step = Math.max(1, Math.ceil(labels.length / limit));
  const multi = new Set(labels.map(l => String(l).slice(0, 4))).size > 1;
  return {
    autoSkip: false, maxRotation: 0,
    callback(_v, i) {
      if (i % step) return null;
      if (fmtOne) return fmtOne(labels[i], i);
      const d = toDate(labels[i]);
      if (!multi) return DF.md.format(d);
      const prev = i - step >= 0 ? String(labels[i - step]).slice(0, 4) : null;
      return (prev !== String(labels[i]).slice(0, 4) ? DF.mdy : DF.md).format(d);
    },
  };
}
const fmtDur    = m => { if (m == null) return "—"; if (m < 60) return m + "m"; const h = Math.floor(m/60), mm = m%60; return mm ? `${h}h ${mm}m` : `${h}h`; };
const fmtPct    = (n, d = 1) => (n == null || !isFinite(n)) ? "—" : n.toFixed(d) + "%";

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}
const icon = (name, cls = "") => `<svg class="ic ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

// Shorten a model id for display: claude-opus-5-5 -> "Opus 5.5",
// claude-3-5-sonnet-20241022 -> "Sonnet 3.5". Unknown ids pass through.
function shortModel(id) {
  if (!id) return "unknown";
  if (id === "<synthetic>") return "Synthetic";
  const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
  const m = id.replace(/^claude-/, "").replace(/-\d{8}$/, "");
  let mm = m.match(/^(\d+)-(\d+)-([a-z]+)$/);              // 3-5-sonnet
  if (mm) return `${cap(mm[3])} ${mm[1]}.${mm[2]}`;
  mm = m.match(/^(\d+)-([a-z]+)$/);                         // 3-opus
  if (mm) return `${cap(mm[2])} ${mm[1]}`;
  mm = m.match(/^([a-z]+)-(\d+)(?:-(\d+))?$/);              // opus-5-5, sonnet-5
  if (mm && id.startsWith("claude-")) return `${cap(mm[1])} ${mm[2]}${mm[3] ? "." + mm[3] : ""}`;
  return id;
}

// Display names for account and tool ids. Unknown ids show as-is, never title-cased
// ("codex-cli" must not become "Codex-Cli").
const ACCOUNT_NAMES = { personal: "Personal", work: "Work", claudex: "Claudex", "codex-cli": "Codex CLI", pi: "Pi", default: "Default" };
const TOOL_NAMES = { "claude-code": "Claude Code", "codex-cli": "Codex CLI", pi: "Pi", cursor: "Cursor", gemini: "Gemini CLI", "gemini-cli": "Gemini CLI" };
const acctName = id => ACCOUNT_NAMES[id] || id || "—";
const toolName = id => TOOL_NAMES[id] || id || "—";
// Who a subscription is paid to (billing.json provider / config paid_by).
const PROVIDER_NAMES = { anthropic: "Anthropic (Claude)", openai_chatgpt: "ChatGPT plan", openai_api: "OpenAI API",
                         google_play: "Google Play", other: "Other" };
const PLAN_NAMES = { openai_chatgpt: "ChatGPT", anthropic: "Claude", openai_api: "OpenAI API" };
const providerName = p => PROVIDER_NAMES[p] || p || "Other";

// Anything that is not a Claude model id: gateway accounts can route to other providers.
const isNonClaude = id => !!id && !id.startsWith("claude-") && id !== "<synthetic>";

// ---------- theme ----------
const THEME_KEY = "cud-theme";
const root = document.documentElement;
let T = {};   // current colour tokens, refreshed by readTheme()

function getCSS(name) { return getComputedStyle(root).getPropertyValue(name).trim(); }
function alpha(c, a) {
  if (!c || !c.startsWith("#")) return c;
  const r = parseInt(c.slice(1,3),16), g = parseInt(c.slice(3,5),16), b = parseInt(c.slice(5,7),16);
  return `rgba(${r},${g},${b},${a})`;
}
function readTheme() {
  T = {
    dark: getCSS("color-scheme") === "dark",
    bg: getCSS("--bg"), panel: getCSS("--panel"), panel2: getCSS("--panel-2"),
    text: getCSS("--text"), text2: getCSS("--text-2"), muted: getCSS("--muted"),
    border: getCSS("--border"), borderSoft: getCSS("--border-soft"), grid: getCSS("--grid"),
    accent: getCSS("--accent"), accentText: getCSS("--accent-text"), good: getCSS("--good"),
    series: [1,2,3,4,5].map(i => getCSS(`--chart-${i}`)),
    other: getCSS("--chart-other"),
    hatchInk: getCSS("--hatch-ink"),
    heat: [0,1,2,3,4,5].map(i => getCSS(`--heat-${i}`)),
  };
  Chart.defaults.color = T.muted;
  Chart.defaults.borderColor = T.grid;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = 11;
  Chart.defaults.plugins.legend.labels.color = T.text2;
  const tt = Chart.defaults.plugins.tooltip;
  tt.backgroundColor = T.panel; tt.borderColor = T.border; tt.borderWidth = 1;
  tt.titleColor = T.text; tt.bodyColor = T.text2; tt.footerColor = T.text;
  tt.padding = 10; tt.boxPadding = 4; tt.usePointStyle = false;
  Chart.defaults.animation.duration = matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 450;
  // Line datasets sit last in every legend, after the bar series they summarise.
  // (Doughnut legend items are per slice and carry no datasetIndex.)
  const isLine = (item, data) => data.datasets[item.datasetIndex]?.type === "line";
  Chart.defaults.plugins.legend.labels.sort = (a, b, data) => isLine(a, data) - isLine(b, data);
  return T;
}
function themeMode() {
  try { const t = localStorage.getItem(THEME_KEY); return (t === "light" || t === "dark") ? t : "system"; }
  catch { return "system"; }
}
function setThemeMode(mode, persist = true) {
  if (mode === "light" || mode === "dark") root.setAttribute("data-theme", mode);
  else root.removeAttribute("data-theme");
  if (persist) { try { localStorage.setItem(THEME_KEY, mode); } catch {} }
  document.querySelectorAll("#theme-toggle button").forEach(b =>
    b.setAttribute("aria-checked", String(b.dataset.themeMode === mode)));
  retheme();
}
function retheme() {
  readTheme();
  for (const id of Object.keys(chartBuilders)) buildChart(id, false);
}

// Diagonal hatch: the fast-mode segment is told apart by pattern, not colour alone.
function hatch(base) {
  const s = 8, c = document.createElement("canvas");
  c.width = s; c.height = s;
  const x = c.getContext("2d");
  x.fillStyle = base; x.fillRect(0, 0, s, s);
  x.strokeStyle = T.hatchInk; x.lineWidth = 1.6;
  x.beginPath();
  x.moveTo(-1, s + 1); x.lineTo(s + 1, -1);
  x.moveTo(-1, 1); x.lineTo(1, -1);
  x.moveTo(s - 1, s + 1); x.lineTo(s + 1, s - 1);
  x.stroke();
  return x.createPattern(c, "repeat");
}

// ---------- state ----------
const STATE = {
  window: "all",
  account: "",         // "" = all accounts; set from the header switcher
  project: "",         // "" = all projects; set by clicking a project (cross-filter)
  model: "",           // "" = all models; set by clicking a model (donut slice or table row)
  tool: "",            // "" = every coding agent; "codex-cli", "pi", "claude-code" (from ?tool=)
  dims: {},            // breakdown filters: { agent: "Explore", sidechain: "1", ... } (from Breakdown rows)
  day: "",             // "" = the whole window; "YYYY-MM-DD" = drilled into one local day
  from: "", to: "",    // window "custom": inclusive local days "YYYY-MM-DD"
  hour: "",            // "" or "0".."23": local hour of day (heatmap cross-filter)
  dow: "",             // "" or "0".."6": local weekday, 0 = Monday (heatmap cross-filter)
  otherOpen: false,    // models donut: "Other" expanded into its members
  tab: "overview",     // active tab: overview | sessions | activity
  metric: "tokens",    // daily activity chart: tokens | cost (by model) | cost_type
  heatMetric: "cost",  // heatmap colour: cost | tokens
  heatView: "auto",    // heatmap: auto (by window length) | hours | calendar
  projBy: "tokens",
  projView: "chart",
  sessBy: "cost",
  bdDim: "token_type", // breakdown dimension
  skillsBy: "least",   // 'least' | 'most'
  extrasReady: false,
  callsTool: "",
  callsStatus: "",
  callsShown: 0,
};

const WIN_DAYS = { today: 1, "1w": 7, "15d": 15, "1m": 30, "3m": 90, "6m": 180, "1y": 365 };
const PREV_LABEL = { today: "yesterday", "1w": "prev week", "15d": "prev 15d", "1m": "prev month",
                     "3m": "prev 3m", "6m": "prev 6m", "1y": "prev year", custom: "prev period" };
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const validDay = d => ISO_DAY.test(d || "") && !isNaN(toDate(d));

// Every request goes through here, so every card sees the same filters:
//   day drill   -> a `window` param becomes from=to=<day> (opts.ignoreDay keeps the window)
//   custom      -> a `window` param becomes from=<STATE.from>&to=<STATE.to>
//   account, project, model, hour, dow -> added as-is (opts.skipProject / opts.skipModel /
//   opts.skipWhen for a card that is itself the filter source and highlights the selection instead)
async function api(path, params = {}, opts = {}) {
  const u = new URL(path, location.origin);
  const p = { ...params };
  if (STATE.day && "window" in p && !opts.ignoreDay) { delete p.window; p.from = STATE.day; p.to = STATE.day; }
  else if (STATE.window === "custom" && "window" in p) { delete p.window; p.from = STATE.from; p.to = STATE.to; }
  for (const [k, v] of Object.entries(p)) if (v != null) u.searchParams.set(k, v);
  if (path.startsWith("/api/")) {
    if (STATE.account) u.searchParams.set("account", STATE.account);
    if (STATE.project && !opts.skipProject) u.searchParams.set("project", STATE.project);
    if (STATE.model && !opts.skipModel) u.searchParams.set("model", STATE.model);
    if (STATE.tool) u.searchParams.set("tool", STATE.tool);
    // /api/breakdown drops the filter on its own dimension, so the selected row stays listed.
    for (const [k, v] of Object.entries(STATE.dims)) if (v) u.searchParams.set(k, v);
    if (STATE.hour !== "" && !opts.skipWhen) u.searchParams.set("hour", STATE.hour);
    if (STATE.dow !== "" && !opts.skipWhen) u.searchParams.set("dow", STATE.dow);
  }
  const r = await fetch(u);
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json();
}

// The local calendar days the current view covers. Rolling windows reach back N×24h,
// so they touch N+1 dates; "all" starts at the first activity (firstDay).
function windowSpan(firstDay) {
  const today = toDate(isoLocal(new Date()));
  let from, to = today;
  if (STATE.day) { from = toDate(STATE.day); to = toDate(STATE.day); }
  else if (STATE.window === "custom") { from = toDate(STATE.from); to = toDate(STATE.to); }
  else if (STATE.window === "today") from = today;
  else if (WIN_DAYS[STATE.window]) from = addDays(today, -WIN_DAYS[STATE.window]);
  else from = firstDay ? toDate(firstDay instanceof Date ? isoLocal(firstDay) : String(firstDay).slice(0, 10)) : today;
  if (from > to) from = to;
  const days = Math.round((to - from) / 864e5) + 1;
  return { from, to, days };
}
const WINDOW_NAME = { today: "today", "1w": "last week", "15d": "last 15 days", "1m": "last month",
                      "3m": "last 3 months", "6m": "last 6 months", "1y": "last year", all: "all time" };
// What the page is showing, in words, for chart labels and the export header.
const windowName = () => STATE.window === "custom" ? fmtRange(STATE.from, STATE.to) : WINDOW_NAME[STATE.window] || STATE.window;
const viewLabel = () => STATE.day ? fmtRange(STATE.day) : windowName();
let lastRangeText = "";   // header range, kept for the exporter
// Short views (today, a drilled day, a custom range of two days or less) chart by the hour.
const isHourly = () => !!STATE.day || STATE.window === "today" || (STATE.window === "custom" && windowSpan().days <= 2);
// Local "YYYY-MM-DDTHH" buckets for every hour of the view (the timeseries' hour keys).
function hourBuckets() {
  const { from, to } = windowSpan(), out = [];
  for (let d = new Date(from); d <= to; d.setDate(d.getDate() + 1))
    for (let h = 0; h < 24; h++) out.push(`${isoLocal(d)}T${String(h).padStart(2, "0")}`);
  return out;
}

window.__dash = { api, STATE, shortModel, fmtRange, fmtDay, fmtDayLong, windowSpan, viewLabel, isHourly, hourBuckets,
                  rangeText: () => lastRangeText, activeFilters: () => activeFilters(), modelSlots };

// Same-length window that ends where the current one starts (dates, UTC, inclusive).
function prevWindowRange(w) {
  if (STATE.day) { const y = isoLocal(addDays(STATE.day, -1)); return { from: y, to: y }; }
  if (w === "custom") {
    const n = windowSpan().days;
    return { from: isoLocal(addDays(STATE.from, -n)), to: isoLocal(addDays(STATE.from, -1)) };
  }
  const d = WIN_DAYS[w];
  if (!d) return null;
  const iso = dt => dt.toISOString().slice(0, 10);
  const now = new Date();
  if (w === "today") {
    const y = new Date(now); y.setDate(y.getDate() - 1);
    const s = `${y.getFullYear()}-${String(y.getMonth()+1).padStart(2,"0")}-${String(y.getDate()).padStart(2,"0")}`;
    return { from: s, to: s };
  }
  const start = new Date(now.getTime() - d * 864e5);
  const to = new Date(start.getTime() - 864e5);
  const from = new Date(to.getTime() - (d - 1) * 864e5);
  return { from: iso(from), to: iso(to) };
}

// ---------- model colours: top five by spend, then "other" ----------
// Slots are sticky for the page session, so a model keeps its hue when the
// window or a filter changes, as long as it stays in the top five.
const stickySlots = new Map();
function modelSlots(modelsByCostDesc) {
  const top = modelsByCostDesc.slice(0, 5);
  const used = new Set(), out = new Map();
  for (const m of top) {
    const s = stickySlots.get(m);
    if (s != null && !used.has(s)) { out.set(m, s); used.add(s); }
  }
  for (const m of top) {
    if (out.has(m)) continue;
    const free = [0,1,2,3,4].find(i => !used.has(i));
    out.set(m, free); used.add(free); stickySlots.set(m, free);
  }
  return out;
}
const slotVar = s => s == null ? "var(--chart-other)" : `var(--chart-${s + 1})`;
const slotColor = s => s == null ? T.other : T.series[s];
// Token types in billing order; colours follow the Tokens view of Daily activity.
const TOKEN_TYPES = [
  { key: "input",          label: "Input",                slot: 0 },
  { key: "output",         label: "Output",               slot: 1 },
  { key: "cache_5m_write", label: "Cache write (5 min)",  slot: 2 },
  { key: "cache_1h_write", label: "Cache write (1 hour)", slot: 4 },
  { key: "cache_read",     label: "Cache read",           slot: 3 },
  { key: "web_search",     label: "Web search",           slot: null },
];
const TOKEN_TYPE = Object.fromEntries(TOKEN_TYPES.map(t => [t.key, t]));

// ---------- cards: loading, held render, per-card retry ----------
async function runCard(key, loader) {
  const cards = [...document.querySelectorAll(`[data-card="${key}"]`)];
  cards.forEach(c => {
    c.classList.add("is-loading");
    c.setAttribute("aria-busy", "true");
    c.querySelectorAll(":scope > .card-error").forEach(e => e.remove());
  });
  try {
    await loader();
    cards.forEach(c => { c.dataset.loaded = "1"; });
  } catch (err) {
    console.error(`[${key}]`, err);
    const host = cards[0];
    if (host) {
      const div = document.createElement("div");
      div.className = "card-error";
      div.setAttribute("role", "alert");
      div.innerHTML = `<span>Couldn't load this section. ${escapeHtml(err.message || "")}</span><button type="button">Retry</button>`;
      div.querySelector("button").addEventListener("click", () => runCard(key, loader));
      host.appendChild(div);
    }
    throw err;
  } finally {
    cards.forEach(c => { c.classList.remove("is-loading"); c.removeAttribute("aria-busy"); });
  }
}

// ---------- tables: sortable, with aria-sort ----------
// cols: [{ key, label, num?, fast?, render?(row) -> html, sort?(row) -> value, sortable? }]
// rows: array, or null to draw skeleton rows while the first load is in flight.
function renderTable(table, cols, rows, opts = {}) {
  if (typeof table === "string") table = document.getElementById(table);
  if (!table) return;
  const st = table._t || (table._t = { sortKey: null, sortDir: null });
  st.cols = cols; st.rows = rows; st.opts = opts;
  if (st.sortKey && !cols.some(c => c.key === st.sortKey)) { st.sortKey = null; st.sortDir = null; }
  const anyFast = (rows || []).some(r => (r.fast_cost_usd || 0) > 0 || (r.fast_msgs || 0) > 0);
  table.classList.toggle("hide-fast", !anyFast && !opts.showFast);
  drawTableHead(table);
  drawTableBody(table);
}
function drawTableHead(table) {
  const { cols, sortKey, sortDir } = table._t;
  const thead = table.tHead || table.createTHead();
  thead.innerHTML = "<tr>" + cols.map(c => {
    const cls = [c.num ? "num" : "", c.fast ? "col-fast" : "", c.sortable === false ? "" : "sortable"].filter(Boolean).join(" ");
    const aria = c.sortable === false ? "" : ` aria-sort="${sortKey === c.key ? sortDir : "none"}"`;
    const inner = c.sortable === false ? escapeHtml(c.label)
      : `<button type="button" class="th-sort" data-key="${escapeHtml(c.key)}">${escapeHtml(c.label)}${icon(sortKey === c.key ? (sortDir === "ascending" ? "up" : "down") : "sort")}</button>`;
    return `<th scope="col" class="${cls}"${aria}${c.title ? ` title="${escapeHtml(c.title)}"` : ""}>${inner}</th>`;
  }).join("") + "</tr>";
  if (!thead.dataset.bound) {
    thead.dataset.bound = "1";
    thead.addEventListener("click", e => {
      const b = e.target.closest(".th-sort");
      if (!b) return;
      const st = table._t, col = st.cols.find(c => c.key === b.dataset.key);
      if (st.sortKey !== col.key) { st.sortKey = col.key; st.sortDir = col.num ? "descending" : "ascending"; }
      else st.sortDir = st.sortDir === "ascending" ? "descending" : "ascending";
      drawTableHead(table); drawTableBody(table);
      table.tHead.querySelector(`.th-sort[data-key="${CSS.escape(col.key)}"]`)?.focus();
    });
  }
}
function drawTableBody(table) {
  const { cols, rows, opts, sortKey, sortDir } = table._t;
  const tbody = table.tBodies[0] || table.createTBody();
  if (rows == null) {
    tbody.innerHTML = Array.from({ length: opts.skeletonRows || 5 }, () =>
      `<tr class="skel-row">${cols.map(c => `<td class="${c.fast ? "col-fast" : ""}"><span class="skel"></span></td>`).join("")}</tr>`).join("");
    return;
  }
  let view = rows;
  if (sortKey) {
    const col = cols.find(c => c.key === sortKey);
    const val = col.sort || (r => r[col.key]);
    const dir = sortDir === "ascending" ? 1 : -1;
    view = rows.slice().sort((a, b) => {
      const x = val(a), y = val(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * dir;
    });
  }
  if (!view.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${cols.length}">${escapeHtml(opts.empty || "Nothing in this window.")}</td></tr>`;
    return;
  }
  tbody.innerHTML = view.map(r => `<tr${opts.rowClass ? ` class="${opts.rowClass(r) || ""}"` : ""}>` + cols.map(c => {
    const cls = [c.num ? "num" : "", c.fast ? "col-fast" : "", c.cls || ""].filter(Boolean).join(" ");
    return `<td${cls ? ` class="${cls}"` : ""}>${c.render ? c.render(r) : escapeHtml(r[c.key] ?? "—")}</td>`;
  }).join("") + "</tr>").join("");
  if (opts.onRow) [...tbody.rows].forEach((tr, i) => opts.onRow(tr, view[i]));
}

// Reusable cells
const estBadge = r => (r.estimated_msgs > 0)
  ? `<span class="est-badge" title="${fmtInt(r.estimated_msgs)} message${r.estimated_msgs === 1 ? "" : "s"} priced from an estimated rate">est.</span>` : "";
const costCell = (cost, r) => `${fmtMoney(cost)}${estBadge(r)}`;
const fastCell = r => (r.fast_cost_usd > 0 || r.fast_msgs > 0)
  ? `${fmtMoney(r.fast_cost_usd)}<div class="muted" style="font-size:10.5px">+${fmtMoney(r.fast_premium_usd)} premium</div>`
  : `<span class="quiet">—</span>`;
const FAST_COL = { key: "fast_cost_usd", label: "Fast", num: true, fast: true, render: fastCell,
                   title: "Spend in fast mode, and the premium over standard rates" };
const projectCell = r => `<div class="project-cell clickable" title="Filter everything to this project"><span class="name">${escapeHtml(r.project || "—")}</span><span class="path">${escapeHtml(r.project_path || "")}</span></div>`;

// ---------- charts: live re-theming + accessible summaries ----------
const charts = {};          // id -> Chart instance
const chartBuilders = {};   // id -> () => config, re-run on theme change
const chartA11y = {};       // id -> { label, summary }

function upsertChart(id, build, a11y) {
  chartBuilders[id] = build;
  if (a11y) chartA11y[id] = a11y;
  buildChart(id);
}
function buildChart(id, animate = true) {
  const canvas = document.getElementById(id);
  if (!canvas || !chartBuilders[id]) return;
  if (charts[id]) charts[id].destroy();
  Chart.getChart(canvas)?.destroy();   // a half-built chart from a failed render still owns the canvas
  const cfg = chartBuilders[id]();
  if (!animate) cfg.options = { ...cfg.options, animation: false };   // a theme switch repaints, it doesn't replay
  charts[id] = new Chart(canvas, cfg);
  const a = chartA11y[id];
  if (a) {
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", a.label);
    let desc = document.getElementById(id + "-desc");
    if (!desc) {
      desc = document.createElement("p");
      desc.id = id + "-desc"; desc.className = "sr-only";
      canvas.parentElement.after(desc);
      canvas.setAttribute("aria-describedby", desc.id);
    }
    desc.textContent = a.summary;
  }
}
const axisTitle = text => ({ display: true, text, color: T.muted, font: { size: 10.5, weight: "600" } });

// ---------- cross-filters: one chip bar, mirrored in the URL ----------
// Filters: window (header control), day drill, account, project, model. Each setter
// goes through setFilters(), which updates the URL (shareable, survives a reload),
// redraws the chip bar and the header controls, and reloads every card.
const WINDOW_IDS = ["today", "1w", "15d", "1m", "3m", "6m", "1y", "all"];
// A custom range lives in the URL as from=&to= (no window param); hour/dow as integers.
const URL_KEYS = ["window", "from", "to", "day", "account", "tool", "project", "model", "dow", "hour"];
const intIn = (v, lo, hi) => /^\d{1,2}$/.test(v || "") && +v >= lo && +v <= hi ? String(+v) : "";
// Breakdown dimensions that filter the page (?agent=Explore, ?sidechain=1, ...); see BD_DIMS.
const DIM_KEYS = ["sidechain", "agent", "skill", "mcp", "plugin", "branch", "pr", "entrypoint", "effort"];
// sidechain rides in the URL as 0|1; the breakdown rows call it main|subagent.
const SIDECHAIN_URL = { main: "0", subagent: "1" };
const dimUrlValue = (dim, v) => dim === "sidechain" ? (SIDECHAIN_URL[v] ?? v) : v;
const dimRowValue = (dim, v) => dim === "sidechain" ? ({ 0: "main", 1: "subagent" }[v] ?? v) : v;
function readUrl() {
  const q = new URLSearchParams(location.search);
  const w = q.get("window");
  STATE.window = WINDOW_IDS.includes(w) ? w : "all";
  const f = q.get("from") || "", t = q.get("to") || "";
  if (validDay(f) && validDay(t)) {
    STATE.window = "custom";
    [STATE.from, STATE.to] = f <= t ? [f, t] : [t, f];
  } else { STATE.from = ""; STATE.to = ""; }
  const d = q.get("day") || "";
  STATE.day = validDay(d) ? d : "";
  STATE.account = q.get("account") || "";
  STATE.tool = q.get("tool") || "";
  STATE.project = q.get("project") || "";
  STATE.model = q.get("model") || "";
  STATE.dims = {};
  for (const k of DIM_KEYS) { const v = q.get(k); if (v) STATE.dims[k] = k === "sidechain" ? dimUrlValue(k, v) : v; }
  STATE.hour = intIn(q.get("hour"), 0, 23);
  STATE.dow = intIn(q.get("dow"), 0, 6);
}
function syncUrl(push) {
  const u = new URL(location.href);
  const custom = STATE.window === "custom";
  for (const k of URL_KEYS) {
    const v = STATE[k];
    const keep = k === "window" ? v && v !== "all" && !custom
      : (k === "from" || k === "to") ? custom && v : v !== "" && v != null;
    if (keep) u.searchParams.set(k, v); else u.searchParams.delete(k);
  }
  for (const k of DIM_KEYS) { if (STATE.dims[k]) u.searchParams.set(k, STATE.dims[k]); else u.searchParams.delete(k); }
  if (u.href !== location.href) history[push ? "pushState" : "replaceState"](null, "", u);
}
// "Wed · 16:00–17:00", "Wednesdays", "16:00–17:00" (local time).
function whenLabel(dow = STATE.dow, hour = STATE.hour) {
  if (dow !== "" && hour !== "") return `${DOW_NAMES[+dow]} · ${hourRange(+hour)}`;
  if (dow !== "") return DOW_LONG[+dow];
  if (hour !== "") return hourRange(+hour);
  return "";
}
function activeFilters() {
  const f = [];
  if (STATE.day) f.push({ key: "day", kind: "Viewing", label: fmtRange(STATE.day) });
  if (STATE.tool) f.push({ key: "tool", kind: "Tool", label: toolName(STATE.tool), title: STATE.tool });
  if (STATE.account) f.push({ key: "account", kind: "Account", label: acctName(STATE.account), title: STATE.account });
  if (STATE.hour !== "" || STATE.dow !== "") f.push({ key: "when", kind: "When", label: whenLabel(), title: "Local time" });
  if (STATE.model) f.push({ key: "model", kind: "Model", label: shortModel(STATE.model), title: STATE.model });
  if (STATE.project) f.push({ key: "project", kind: "Project", label: STATE.project });
  for (const k of DIM_KEYS) {
    const v = STATE.dims[k];
    if (v) f.push({ key: "dim:" + k, kind: BD_DIMS[k].col, label: bdValue(k, dimRowValue(k, v)).text, title: v });
  }
  return f;
}
function renderFilterBar() {
  const bar = document.getElementById("filter-bar");
  if (!bar) return;
  const f = activeFilters();
  bar.hidden = !f.length;
  if (!f.length) { bar.innerHTML = ""; return; }
  const back = windowName();
  bar.innerHTML = `<span class="filter-bar-k">Filtered</span>` + f.map(x => x.key === "day"
    ? `<button type="button" class="chip chip-day" data-clear="day" title="Back to ${escapeHtml(back)}" aria-label="Viewing ${escapeHtml(x.label)}. Back to ${escapeHtml(back)}">${icon("back")}<span>Viewing <b>${escapeHtml(x.label)}</b></span><span class="chip-sep" aria-hidden="true">·</span><span class="chip-back">back</span></button>`
    : `<button type="button" class="chip" data-clear="${x.key}"${x.title ? ` title="${escapeHtml(x.title)}"` : ""} aria-label="Remove ${x.kind.toLowerCase()} filter: ${escapeHtml(x.label)}"><span class="chip-k">${x.kind}</span><b>${escapeHtml(x.label)}</b>${icon("close")}</button>`
  ).join("") + `<button type="button" class="chip-clear" data-clear="all">Clear all</button>`;
}
// Header controls follow STATE (after a popstate, a reload from the URL, or a chip removal).
function applyControls() {
  document.querySelectorAll("#windows button").forEach(x => {
    x.classList.toggle("on", x.dataset.w === STATE.window); x.setAttribute("aria-pressed", String(x.dataset.w === STATE.window));
  });
  const custom = STATE.window === "custom";
  const cb = document.getElementById("custom-btn");
  if (cb) cb.textContent = custom ? fmtRange(STATE.from, STATE.to) : "Custom…";
  const ws = document.getElementById("window-select");
  if (ws) {
    const opt = ws.querySelector('option[value="custom"]');
    if (opt) opt.textContent = custom ? fmtRange(STATE.from, STATE.to) : "Custom…";
    // with a range active, picking it again fires no change, so offer an explicit edit entry
    let edit = ws.querySelector('option[value="custom-edit"]');
    if (custom && !edit) { edit = new Option("Edit range…", "custom-edit"); ws.add(edit); }
    else if (!custom && edit) edit.remove();
    ws.value = STATE.window;
  }
  document.querySelectorAll("#accounts button").forEach(x => {
    x.classList.toggle("on", x.dataset.acct === STATE.account); x.setAttribute("aria-pressed", String(x.dataset.acct === STATE.account));
  });
  const as = document.getElementById("accounts-select");
  if (as && [...as.options].some(o => o.value === STATE.account)) as.value = STATE.account;
}
function setFilters(patch, { push = true } = {}) {
  const viewChanged = ("window" in patch && patch.window !== STATE.window) || ("day" in patch && patch.day !== STATE.day)
    || ("from" in patch && patch.from !== STATE.from) || ("to" in patch && patch.to !== STATE.to);
  Object.assign(STATE, patch);
  if (viewChanged) STATE.heatView = "auto";
  syncUrl(push);
  applyControls();
  renderFilterBar();
  reloadAll();
}
const toggle = (cur, v) => (v && v !== cur) ? v : "";
function setProjectFilter(name) { setFilters({ project: toggle(STATE.project, name) }); }
function setModelFilter(id) { setFilters({ model: toggle(STATE.model, id) }); }
function setDay(iso) { if (/^\d{4}-\d{2}-\d{2}$/.test(iso || "")) setFilters({ day: toggle(STATE.day, iso) }); }
// Toggle one breakdown filter; `value` is the breakdown row value ("main", "(none)", "Explore").
function setDimFilter(dim, value) {
  const v = dimUrlValue(dim, value);
  const dims = { ...STATE.dims };
  if (dims[dim] === v) delete dims[dim]; else dims[dim] = v;
  setFilters({ dims });
}
// Heatmap cross-filter: a weekday, an hour, or both. Picking the active one again clears it.
function setWhen(dow, hour) {
  dow = dow == null ? "" : String(dow); hour = hour == null ? "" : String(hour);
  const same = dow === STATE.dow && hour === STATE.hour;
  setFilters(same ? { dow: "", hour: "" } : { dow, hour });
}
function clearFilter(key) {
  if (key === "all") setFilters({ day: "", model: "", project: "", account: "", tool: "", dims: {}, dow: "", hour: "" });
  else if (key === "when") setFilters({ dow: "", hour: "" });
  else if (key.startsWith("dim:")) { const dims = { ...STATE.dims }; delete dims[key.slice(4)]; setFilters({ dims }); }
  else setFilters({ [key]: "" });
}
function bindFilterBar() {
  document.getElementById("filter-bar")?.addEventListener("click", e => {
    const b = e.target.closest("button[data-clear]");
    if (b) clearFilter(b.dataset.clear);
  });
  window.addEventListener("popstate", () => { readUrl(); STATE.heatView = "auto"; applyControls(); renderFilterBar(); reloadAll(); });
}

// ---------- custom period: from/to local days ----------
// "Custom…" in the window controls (a button on wide screens, a <select> option on phones)
// opens a small popover with two native date inputs, quick presets and Apply. The range
// lands in the URL as from=&to= and the API reads it as local calendar days.
const customPop = () => document.getElementById("custom-pop");
let customAnchor = null;
function customPreset(kind) {
  const t = new Date(), y = t.getFullYear(), m = t.getMonth();
  const today = isoLocal(t);
  switch (kind) {
    case "month": return [isoLocal(new Date(y, m, 1)), today];
    case "last-month": return [isoLocal(new Date(y, m - 1, 1)), isoLocal(new Date(y, m, 0))];
    case "quarter": return [isoLocal(new Date(y, m - m % 3, 1)), today];
    case "ytd": return [isoLocal(new Date(y, 0, 1)), today];
  }
  return null;
}
function placeCustom() {
  const pop = customPop();
  if (!pop || pop.hidden || !customAnchor) return;
  const r = customAnchor.getBoundingClientRect();
  const vw = document.documentElement.clientWidth, w = pop.offsetWidth, m = 16;
  const left = Math.max(m, Math.min(r.right - w, vw - w - m));
  pop.style.left = `${Math.round(left + scrollX)}px`;
  pop.style.top = `${Math.round(r.bottom + scrollY + 6)}px`;
}
function openCustom() {
  const pop = customPop();
  if (!pop) return;
  const btn = document.getElementById("custom-btn"), sel = document.getElementById("window-select");
  customAnchor = [btn, sel].find(el => el && el.getClientRects().length) || btn;
  const fromI = document.getElementById("custom-from"), toI = document.getElementById("custom-to");
  const today = isoLocal(new Date());
  // start from what the page shows now (a long "all" view starts from the last 30 days instead)
  let f = STATE.from, t = STATE.to;
  if (STATE.window !== "custom") {
    if (STATE.day) f = t = STATE.day;
    else if (WIN_DAYS[STATE.window]) { const s = windowSpan(); f = isoLocal(s.from); t = isoLocal(s.to); }
    else { f = isoLocal(addDays(today, -29)); t = today; }
  }
  fromI.value = f; toI.value = t;
  fromI.max = toI.max = today;
  pop.querySelectorAll("[data-preset]").forEach(b => {
    const p = customPreset(b.dataset.preset);
    b.setAttribute("aria-pressed", String(p[0] === f && p[1] === t));
  });
  const err = document.getElementById("custom-err");
  err.hidden = true; err.textContent = "";
  pop.hidden = false;
  btn?.setAttribute("aria-expanded", "true");
  placeCustom();
  fromI.focus();
}
function closeCustom(restoreFocus = true) {
  const pop = customPop();
  if (!pop || pop.hidden) return;
  pop.hidden = true;
  document.getElementById("custom-btn")?.setAttribute("aria-expanded", "false");
  applyControls();   // a cancelled pick puts the <select> back on the current window
  if (restoreFocus) customAnchor?.focus();
}
function applyCustom() {
  const fromI = document.getElementById("custom-from"), toI = document.getElementById("custom-to");
  const err = document.getElementById("custom-err");
  let f = fromI.value, t = toI.value;
  const fail = msg => { err.textContent = msg; err.hidden = false; };
  if (!validDay(f) || !validDay(t)) return fail("Pick both a start and an end date.");
  if (f > t) [f, t] = [t, f];
  closeCustom();
  setFilters({ window: "custom", from: f, to: t, day: "" });
}
function bindCustom() {
  const pop = customPop();
  if (!pop) return;
  const tz = document.getElementById("custom-tz");
  try { tz.textContent = `local days · ${Intl.DateTimeFormat().resolvedOptions().timeZone}`; } catch {}
  pop.addEventListener("click", e => {
    const p = e.target.closest("[data-preset]");
    if (p) {
      const [f, t] = customPreset(p.dataset.preset);
      document.getElementById("custom-from").value = f;
      document.getElementById("custom-to").value = t;
      pop.querySelectorAll("[data-preset]").forEach(b => b.setAttribute("aria-pressed", String(b === p)));
      return;
    }
    if (e.target.closest("[data-pop-cancel]")) closeCustom();
    if (e.target.closest("#custom-apply")) applyCustom();
  });
  pop.addEventListener("input", () => {
    pop.querySelectorAll("[data-preset]").forEach(b => b.setAttribute("aria-pressed", "false"));
    document.getElementById("custom-err").hidden = true;
  });
  pop.addEventListener("keydown", e => {
    if (e.key === "Escape") { e.preventDefault(); closeCustom(); }
    else if (e.key === "Enter" && e.target.matches("input")) { e.preventDefault(); applyCustom(); }
  });
  document.addEventListener("pointerdown", e => {
    if (!pop.hidden && !pop.contains(e.target) && !customAnchor?.contains(e.target)) closeCustom(false);
  });
  addEventListener("resize", placeCustom);
}

// ---------- accounts switcher ----------
async function loadAccounts() {
  const box = document.getElementById("accounts");
  const selWrap = document.getElementById("accounts-select-wrap");
  const sel = document.getElementById("accounts-select");
  if (!box) return;
  let accounts = [];
  try { accounts = (await api("/api/accounts")).accounts || []; } catch {}
  if (accounts.length < 2) {
    box.hidden = true; selWrap.hidden = true;
    if (STATE.account) { STATE.account = ""; syncUrl(false); renderFilterBar(); }
    return;
  }
  const opts = [["All accounts", ""], ...accounts.map(a => [acctName(a.name), a.name])];
  box.innerHTML = opts.map(([label, v]) =>
    `<button data-acct="${escapeHtml(v)}" class="${STATE.account === v ? "on" : ""}" aria-pressed="${STATE.account === v}">${escapeHtml(label)}</button>`).join("");
  sel.innerHTML = opts.map(([label, v]) => `<option value="${escapeHtml(v)}">${escapeHtml(label)}</option>`).join("");
  sel.value = STATE.account;
  if (sel.value !== STATE.account) sel.value = "";   // an account from a shared URL that isn't on this machine
  box.hidden = false; selWrap.hidden = false;
  if (!box.dataset.bound) {
    box.dataset.bound = "1";
    box.addEventListener("click", e => {
      const b = e.target.closest("button[data-acct]");
      if (b) setAccount(b.dataset.acct);
    });
    sel.addEventListener("change", () => setAccount(sel.value));
  }
}
function setAccount(v) { setFilters({ account: v || "" }); }

// ---------- KPI ----------
function setKpi(id, text, animateNumber = null) {
  const el = document.getElementById(id);
  el.removeAttribute("data-loading");
  if (animateNumber == null) { el.textContent = text; return; }
  const prev = parseFloat((el.dataset.value ?? "0"));
  el.dataset.value = String(animateNumber);
  countUp(el, prev, animateNumber, text);
}
function countUp(el, from, to, finalText) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) { el.textContent = finalText; return; }
  const duration = 600, start = performance.now();
  const isMoney = /^[+-]?\$/.test(finalText);
  const sign = finalText.startsWith("+") ? "+" : (finalText.startsWith("-") ? "-" : "");
  const fmt = isMoney ? v => sign + fmtMoney(Math.abs(v)) : v => fmtCompact(v);
  function tick(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = t < 1 ? fmt(from + (to - from) * eased) : finalText;
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}
// Period-over-period chip: arrow + signed percent, neutral ink.
function setDelta(id, cur, prev) {
  const el = document.getElementById(id);
  if (!el) return;
  const label = STATE.day ? "prev day" : PREV_LABEL[STATE.window];
  if (prev == null || cur == null || !label) { el.hidden = true; return; }
  let body;
  if (prev === 0 && cur === 0) body = `<span>no change</span>`;
  else if (prev === 0) body = `${icon("up")}<span>new</span>`;
  else {
    const pct = (cur - prev) / Math.abs(prev) * 100;
    if (Math.abs(pct) < 0.5) body = `<span>flat</span>`;
    else {
      const a = Math.abs(pct);
      const shown = a >= 1000 ? fmtCompact(a) : a.toFixed(a < 10 ? 1 : 0);
      body = `${icon(pct > 0 ? "up" : "down")}<span>${pct > 0 ? "+" : "−"}${shown}%</span>`;
    }
  }
  el.innerHTML = `${body}<span class="vs">vs ${label}</span>`;
  const r = prevWindowRange(STATE.window);
  el.title = `${STATE.day ? "Previous day" : "Previous window"}${r ? ` (${fmtRange(r.from, r.to)})` : ""}: ${typeof prev === "number" && prev % 1 ? fmtMoney(prev) : fmtInt(prev)}`;
  el.hidden = false;
}

// Out-of-pocket vs plan value. paid_by_you_usd / paid_by_employer_usd / savings_you come from
// servers that know employer-paid plans; otherwise everything subscribed is "you".
function paidSplit(s) {
  const plan = s.subscription_cost || 0;
  const employer = s.paid_by_employer_usd || 0;
  const you = s.paid_by_you_usd ?? Math.max(0, plan - employer);
  return { plan, employer, you, savingsYou: s.savings_you ?? (employer ? (s.api_cost || 0) - you : s.savings),
           employerAccounts: s.employer_accounts || [] };
}
async function loadSummary() {
  const range = prevWindowRange(STATE.window);
  const [s, p, byModel] = await Promise.all([
    api("/api/summary", { window: STATE.window }),
    range ? api("/api/summary", { from: range.from, to: range.to }).catch(() => null) : Promise.resolve(null),
    api("/api/by_model", { window: STATE.window }).catch(() => null),
  ]);
  setKpi("kpi-tokens", fmtCompact(s.total_tokens), s.total_tokens);
  // Thinking tokens are billed as output, so they sit inside "out".
  const thinking = s.thinking_tokens > 0 ? ` (${fmtCompact(s.thinking_tokens)} thinking)` : "";
  document.getElementById("kpi-tokens-sub").textContent =
    `in ${fmtCompact(s.input_tokens)} · out ${fmtCompact(s.output_tokens)}${thinking} · cache ${fmtCompact(s.cache_5m_write + s.cache_1h_write + s.cache_read)}`;
  setKpi("kpi-msgs", fmtInt(s.msgs), s.msgs);
  document.getElementById("kpi-msgs-sub").textContent =
    `${fmtInt(s.sessions)} sessions · ${fmtInt(s.projects)} projects`;
  setKpi("kpi-api", fmtMoney(s.api_cost), s.api_cost);
  const extras = [];
  // Gateway accounts can run other providers' models; those are priced at that provider's list price.
  const nonClaude = (byModel?.rows || []).filter(r => isNonClaude(r.model)).reduce((t, r) => t + (r.cost || 0), 0);
  if (nonClaude > 0) extras.push(`incl. ${fmtMoney(nonClaude)} of non-Claude models at their providers' list prices`);
  if (s.unpriced_msgs > 0) extras.push(`${fmtInt(s.unpriced_msgs)} msgs unpriced`);
  if (s.web_search_fees_usd > 0) extras.push(`incl. ${fmtMoney(s.web_search_fees_usd)} web search`);
  if (s.estimated_msgs > 0) extras.push(`${fmtInt(s.estimated_msgs)} msgs estimated`);
  document.getElementById("kpi-api-sub").textContent = ["at standard API rates", ...extras].join(" · ");

  setDelta("kpi-api-delta", s.api_cost, p?.api_cost);
  setDelta("kpi-tokens-delta", s.total_tokens, p?.total_tokens);
  setDelta("kpi-msgs-delta", s.msgs, p?.msgs);

  // Fast mode: the card exists only when the window has fast-mode spend.
  const fastCard = document.getElementById("kpi-fast-card");
  const hasFast = (s.fast_cost_usd || 0) > 0 || (s.fast_msgs || 0) > 0;
  fastCard.hidden = !hasFast;
  if (hasFast) {
    setKpi("kpi-fast", fmtMoney(s.fast_premium_usd), s.fast_premium_usd);
    const share = s.api_cost ? (s.fast_cost_usd / s.api_cost) * 100 : 0;
    document.getElementById("kpi-fast-sub").textContent =
      `${fmtPct(share)} of spend in fast mode · ${fmtInt(s.fast_msgs)} msgs`;
    setDelta("kpi-fast-delta", s.fast_premium_usd, p?.fast_premium_usd);
  }

  const hasSub = s.subscription_cost > 0;
  const subCard = document.getElementById("kpi-sub").closest(".kpi");
  const savCard = document.getElementById("kpi-savings").closest(".kpi");
  const unknownOnly = !hasSub && (s.actual_cost_unknown_accounts || []).length > 0;
  subCard.hidden = !hasSub && !unknownOnly; savCard.hidden = !hasSub;
  if (unknownOnly) {
    // every account in view is subscription-paid but has no receipt yet: say so, never "$0"
    setKpi("kpi-sub", "Unknown", null);
    document.getElementById("kpi-sub-detail").textContent =
      `${s.actual_cost_unknown_accounts.map(acctName).join(", ")}: no receipt yet · add one under Receipts`;
    document.getElementById("kpi-sub-delta").hidden = true;
  }
  if (hasSub) {
    // "You paid" is out of pocket: an employer-paid seat counts toward plan value, not toward you.
    // Older servers send only subscription_cost (the total), which is then what you paid.
    const m = paidSplit(s), mp = p ? paidSplit(p) : null;
    setKpi("kpi-sub", fmtMoney(m.you), m.you);
    const uniq = a => a.filter((v, i) => v && a.indexOf(v) === i);
    const charges = s.billing_charges || [];
    const empSet = new Set(m.employerAccounts);
    const yourPlans = uniq(charges.filter(c => !empSet.has(c.account)).map(c => c.plan));
    const empPlans = uniq(charges.filter(c => empSet.has(c.account)).map(c => c.plan));
    const unknownPaid = s.actual_cost_unknown_accounts || [];
    document.getElementById("kpi-sub-detail").textContent = [
      ...(m.you > 0 || !m.employer ? [yourPlans.join(" · ") || "prorated to this window"] : []),
      ...(m.employer > 0 ? [`not counted: ${fmtMoney(m.employer)} ${empPlans.join(" · ") || "plan"}, paid by your employer`] : []),
      ...(unknownPaid.length ? [`${unknownPaid.map(acctName).join(", ")} paid unknown`] : []),
      ...(s.plan_cost_basis === "allocated_by_usage" ? ["allocated by usage share"] : [])].join(" · ");
    subCard.title = m.employer > 0
      ? `You paid ${fmtMoney(m.you)}. Plan value ${fmtMoney(m.plan)}, of which ${fmtMoney(m.employer)} is paid by your employer.` : "";
    const sav = m.savingsYou;
    setKpi("kpi-savings", (sav >= 0 ? "+" : "-") + fmtMoney(Math.abs(sav)), Math.abs(sav));
    // savings_you counts only the accounts you pay for: their API-rate cost minus what you paid.
    // The subtitle walks from the "Would have cost" figure to this one, so the two cards reconcile.
    const selfApi = m.employer > 0 ? m.savingsYou + m.you : s.api_cost;
    const empApi = Math.max(s.api_cost - selfApi, 0);
    const empNames = m.employerAccounts.map(acctName).join(", ") || "employer accounts";
    document.getElementById("kpi-mult").textContent = m.employer > 0
      ? `${fmtMoney(s.api_cost)} at API rates − ${fmtMoney(empApi)} on ${empNames} (employer-paid) − ${fmtMoney(m.you)} you paid`
      : `${fmtMoney(s.api_cost)} at API rates − ${fmtMoney(m.you)} you paid`;
    savCard.title = m.employer > 0
      ? `Counts only the accounts you pay for: ${fmtMoney(selfApi)} at API rates minus the ${fmtMoney(m.you)} you paid. ` +
        `${empNames} (${fmtMoney(empApi)} at API rates, ${fmtMoney(m.employer)} plan) is left out because your employer pays for it. ` +
        `Counting every account, ${fmtMoney(s.api_cost)} at API rates against ${fmtMoney(m.plan)} of plans saves ` +
        `${s.savings >= 0 ? "+" : "-"}${fmtMoney(Math.abs(s.savings))}${s.multiplier ? `, ${s.multiplier}× cheaper than API` : ""}.`
      : "";
    const badge = document.getElementById("kpi-savings-badge");
    const mult = m.employer > 0 ? (m.you > 0 ? Math.round(selfApi / m.you * 10) / 10 : null) : s.multiplier;
    if (mult) { badge.textContent = `${mult}× cheaper than API`; badge.hidden = false; }
    else if (m.employer > 0 && m.you === 0) { badge.textContent = "paid by employer"; badge.hidden = false; }
    else badge.hidden = true;
    setDelta("kpi-sub-delta", m.you, mp?.you);
    setDelta("kpi-savings-delta", m.savingsYou, p?.subscription_cost > 0 ? mp.savingsYou : null);
  }
  document.body.classList.toggle("no-billing", !hasSub);

  // Header range: the days this view covers ("Feb 7 – Oct 7, 2026"), not the raw timestamps.
  const metaLine = document.getElementById("meta-line");
  if (s.first_ts || STATE.day) {
    const span = windowSpan(s.first_ts && new Date(s.first_ts));
    lastRangeText = fmtRange(span.from, span.to);
    metaLine.textContent = lastRangeText;
    metaLine.title = s.first_ts
      ? `Activity in this view: ${fmtDate(s.first_ts)} to ${fmtDate(s.last_ts)}` : "No activity in this view";
  } else { lastRangeText = ""; metaLine.textContent = "no data yet"; metaLine.removeAttribute("title"); }
  return s;
}

// ---------- Daily activity: tokens by type, or $ by model or by token type ----------
// A window shows one bar per day; clicking a day drills into it. Views of two days or
// less (today, a drilled day, a short custom range) show one bar per hour instead.
// The axis is continuous, so quiet days (or hours) read as zero.
const inArea = (chart, e) => { const a = chart.chartArea; return a && e.x >= a.left && e.x <= a.right && e.y >= a.top && e.y <= a.bottom; };
const indexAt = (chart, e) => inArea(chart, e) ? chart.getElementsAtEventForMode(e, "index", { intersect: false }, false)[0] : null;
const hourLabel = b => `${b.slice(11, 13)}:00`;
const hourSpan = b => { const h = +b.slice(11, 13); return `${String(h).padStart(2, "0")}:00–${String((h + 1) % 24).padStart(2, "0")}:00`; };
async function loadTimeline() {
  const hourly = isHourly();
  const params = { window: STATE.window, bucket: hourly ? "hour" : "day" };
  const by = { cost: "model", cost_type: "token_type" }[STATE.metric];
  const [base, bySplit] = await Promise.all([
    api("/api/timeseries", params),
    by ? api("/api/timeseries", { ...params, by }).catch(() => null) : Promise.resolve(null),
  ]);
  const byBucket = new Map(base.rows.map(r => [r.bucket, r]));
  let labels = [];
  if (hourly) labels = hourBuckets();
  else if (base.rows.length) {
    const span = windowSpan(base.rows[0].bucket);
    const lastRow = toDate(base.rows[base.rows.length - 1].bucket);
    const end = lastRow > span.to ? lastRow : span.to;
    for (let d = new Date(span.from); d <= end; d.setDate(d.getDate() + 1)) labels.push(isoLocal(d));
  }
  const rows = labels.map(b => byBucket.get(b) || { bucket: b });
  const active = rows.filter(r => r.msgs > 0 || r.cost > 0).length;
  const unit = hourly ? "hour" : "day";
  const dayTok = r => (r.input_tokens || 0) + (r.output_tokens || 0) + (r.cache_write || 0) + (r.cache_read || 0);
  const totals = new Map(rows.map(r => [r.bucket, { tokens: dayTok(r), cost: r.cost || 0 }]));
  const many = labels.length > 60;
  const barCommon = { stack: "s", borderWidth: many ? 0 : { top: 1.5 }, borderColor: () => T.panel, maxBarThickness: 24, borderSkipped: false };
  const footer = items => {
    const t = totals.get(labels[items[0]?.dataIndex]);
    if (!t) return "";
    return [`${hourly ? "Hour" : "Day"} total: ${fmtCompact(t.tokens)} tokens · ${fmtMoney(t.cost)}`,
            ...(hourly ? [] : ["Click to view this day"])];
  };
  const title = items => {
    const b = labels[items[0].dataIndex];
    return hourly ? `${fmtDayLong(b.slice(0, 10))} · ${hourSpan(b)}` : fmtDayLong(b);
  };
  // Hour axis: "00:00 03:00 …"; over two days each midnight tick names its day instead.
  const multiDay = hourly && labels.length > 24;
  const hourTick = b => multiDay && b.endsWith("T00") ? DF.md.format(toDate(b.slice(0, 10))) : hourLabel(b);
  const xTicks = dayTicks(labels, hourly ? hourTick : null);
  // Click a day to drill into it (the chip bar offers the way back).
  const drill = hourly ? {} : {
    onClick: (e, _els, chart) => { const p = indexAt(chart, e); if (p) setDay(labels[p.index]); },
    onHover: (e, _els, chart) => { e.native.target.style.cursor = indexAt(chart, e) ? "pointer" : "default"; },
  };
  const hint = document.getElementById("timeline-hint");
  document.getElementById("timeline-title").textContent = hourly ? "Activity by hour" : "Daily activity";
  const where = viewLabel();

  if (STATE.metric === "tokens") {
    hint.textContent = hourly ? "tokens per hour by type" : "tokens per day by type · click a day to drill in";
    const peak = rows.reduce((m, r) => dayTok(r) > (m ? dayTok(m) : -1) ? r : m, null);
    upsertChart("chart-timeline", () => ({
      type: "bar",
      data: { labels, datasets: [
        { label: "Input",       data: rows.map(r => r.input_tokens || 0),  backgroundColor: T.series[0], ...barCommon },
        { label: "Output",      data: rows.map(r => r.output_tokens || 0), backgroundColor: T.series[1], ...barCommon },
        { label: "Cache write", data: rows.map(r => r.cache_write || 0),   backgroundColor: T.series[2], ...barCommon },
        { label: "Cache read",  data: rows.map(r => r.cache_read || 0),    backgroundColor: T.series[3], ...barCommon },
      ]},
      options: {
        responsive: true, maintainAspectRatio: false, ...drill,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { stacked: true, grid: { display: false }, ticks: xTicks },
          y: { stacked: true, grid: { color: T.grid }, border: { display: false }, ticks: { callback: v => fmtCompact(v) }, title: axisTitle(`Tokens per ${unit}`) },
        },
        plugins: {
          tooltip: { callbacks: { title, label: ctx => `${ctx.dataset.label}: ${fmtCompact(ctx.parsed.y)}`, footer } },
          legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10 } },
        },
      },
    }), {
      label: `Tokens per ${unit} by type, ${where}`,
      summary: active
        ? `${active} active ${unit}s of ${rows.length}. Total ${fmtCompact(rows.reduce((s, r) => s + dayTok(r), 0))} tokens. Busiest ${unit} ${hourly ? title([{ dataIndex: labels.indexOf(peak.bucket) }]) : fmtDayLong(peak.bucket)} with ${fmtCompact(dayTok(peak))} tokens.`
        : "No activity in this window.",
    });
    return;
  }

  // Cost mode. Per-model stacks need /api/timeseries?by=model (rows carry `model`), per-type
  // stacks ?by=token_type (rows carry `token_type`); an older server ignores or rejects `by`,
  // and the chart falls back to one "All models" series.
  const splitKey = by === "token_type" ? "token_type" : "model";
  const split = bySplit && bySplit.rows.length && splitKey in bySplit.rows[0];
  const byModel = split && by === "model" ? bySplit : null;
  const datasets = [];
  const fastByDay = new Map();
  if (split && by === "token_type") {
    // Fast-mode spend sits inside each type here, so there is no hatched fast series.
    const series = new Map();      // type -> Map(bucket -> $)
    for (const r of bySplit.rows) {
      if (!series.has(r.token_type)) series.set(r.token_type, new Map());
      series.get(r.token_type).set(r.bucket, (series.get(r.token_type).get(r.bucket) || 0) + (r.cost || 0));
    }
    for (const t of TOKEN_TYPES.filter(t => series.has(t.key))) {
      datasets.push({ label: t.label, _slot: t.slot, data: labels.map(d => series.get(t.key).get(d) || 0) });
    }
    hint.textContent = `API-rate cost per ${unit} by token type${hourly ? "" : " · click a day to drill in"}`;
  } else if (byModel) {
    const costBy = new Map();
    for (const r of byModel.rows) costBy.set(r.model, (costBy.get(r.model) || 0) + (r.cost || 0));
    const ranked = [...costBy.entries()].filter(([, c]) => c > 0).sort((a, b) => b[1] - a[1]).map(([m]) => m);
    const slots = modelSlots(ranked);
    const series = new Map();      // key -> Map(bucket -> $)
    for (const r of byModel.rows) {
      const key = slots.has(r.model) ? r.model : "__other";
      if (!series.has(key)) series.set(key, new Map());
      const std = (r.cost || 0) - (r.fast_cost_usd || 0);
      series.get(key).set(r.bucket, (series.get(key).get(r.bucket) || 0) + std);
      fastByDay.set(r.bucket, (fastByDay.get(r.bucket) || 0) + (r.fast_cost_usd || 0));
    }
    for (const m of ranked.filter(m => slots.has(m))) {
      datasets.push({ label: shortModel(m), _slot: slots.get(m), data: labels.map(d => series.get(m)?.get(d) || 0) });
    }
    if (series.has("__other")) datasets.push({ label: "Other", _slot: null, data: labels.map(d => series.get("__other").get(d) || 0) });
    hint.textContent = `API-rate cost per ${unit} by model · top five, rest as other${hourly ? "" : " · click a day to drill in"}`;
  } else {
    for (const r of rows) fastByDay.set(r.bucket, r.fast_cost_usd || 0);
    datasets.push({ label: "All models", _slot: 0, data: rows.map(r => (r.cost || 0) - (r.fast_cost_usd || 0)) });
    hint.textContent = `API-rate cost per ${unit}${hourly ? "" : " · click a day to drill in"}`;
  }
  const fastData = labels.map(d => fastByDay.get(d) || 0);
  const hasFast = fastData.some(v => v > 0);
  let cum = 0;
  const cumCost = rows.map(r => (cum += r.cost || 0));
  const peak = rows.reduce((m, r) => (r.cost || 0) > (m?.cost ?? -1) ? r : m, null);

  upsertChart("chart-timeline", () => ({
    type: "bar",
    data: { labels, datasets: [
      ...datasets.map(d => ({ ...d, backgroundColor: slotColor(d._slot), ...barCommon, order: 2 })),
      ...(hasFast ? [{ label: "Fast mode (hatched)", data: fastData, backgroundColor: hatch(alpha(T.accent, .28)), ...barCommon, order: 2 }] : []),
      { type: "line", label: "Cumulative", data: cumCost, yAxisID: "y2", order: 1,
        borderColor: T.text2, backgroundColor: T.text2, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.2 },
    ]},
    options: {
      responsive: true, maintainAspectRatio: false, ...drill,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: xTicks },
        y: { stacked: true, grid: { color: T.grid }, border: { display: false }, ticks: { callback: v => "$" + fmtCompact(v) }, title: axisTitle(`USD per ${unit}`) },
        y2: { position: "right", grid: { drawOnChartArea: false }, border: { display: false }, ticks: { callback: v => "$" + fmtCompact(v) }, title: axisTitle("Cumulative USD") },
      },
      plugins: {
        tooltip: {
          filter: ctx => ctx.dataset.type === "line" || ctx.parsed.y > 0,
          callbacks: { title, label: ctx => `${ctx.dataset.label}: ${fmtMoney(ctx.parsed.y)}`, footer },
        },
        legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10 } },
      },
    },
  }), {
    label: `API-rate cost per ${unit}${split ? (by === "token_type" ? " by token type" : " by model") : ""}, ${where}`,
    summary: active
      ? `${active} active ${unit}s of ${rows.length}. Total ${fmtMoney(cum)}. Most expensive ${unit} ${hourly ? title([{ dataIndex: labels.indexOf(peak.bucket) }]) : fmtDayLong(peak.bucket)} at ${fmtMoney(peak.cost)}.` +
        (hasFast ? ` Fast mode ${fmtMoney(fastData.reduce((a, b) => a + b, 0))}.` : "")
      : "No activity in this window.",
  });
}

// ---------- When you work: hour × weekday (short windows) or a calendar (long ones) ----------
// Windows of 31 days or less show when in the week you work; longer ones show the
// calendar, coloured by cost or tokens. A toggle overrides the automatic choice.
let heatData = null;   // { days: /api/heatmap, dist: /api/distributions }
async function loadHeatmap() {
  const [days, dist] = await Promise.all([
    api("/api/heatmap", { window: STATE.window }),
    // the filter source: every hour stays visible and the selected one is highlighted
    api("/api/distributions", { window: STATE.window }, { skipWhen: true }).catch(() => null),
  ]);
  heatData = { days, dist };
  renderHeatmap();
}
const DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DOW_LONG = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"];
const hh = h => `${String(h).padStart(2, "0")}:00`;
const hourRange = h => `${hh(h)}–${hh((h + 1) % 24)}`;
// Scale-free log ramp, so cents and billions of tokens both spread over the five steps.
const heatLevel = (v, max) => (!v || !(max > 0)) ? 0 : Math.min(5, Math.max(1, Math.ceil(Math.log1p(99 * v / max) / Math.log1p(99) * 5)));
// The server's hour × weekday matrix: rows of { dow (0 = Monday), hour, msgs, tokens, cost? }.
function hourDowCells(dist) {
  const m = dist?.hour_dow || dist?.dow_hour || dist?.matrix || dist?.cells;
  return Array.isArray(m) && m.length && m[0] && typeof m[0] === "object" && "hour" in m[0] && "dow" in m[0] ? m : null;
}
function setSegOn(id, attr, value) {
  document.querySelectorAll(`#${id} button[${attr}]`).forEach(b => {
    const on = b.getAttribute(attr) === value;
    b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on));
  });
}
const svgNS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs = {}, styles = {}, text) {
  const el = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  Object.assign(el.style, styles);
  if (text != null) el.textContent = text;
  return el;
}
const metricFmt = m => m === "cost" ? fmtMoney : fmtCompact;
const metricWord = m => m === "cost" ? "API-rate cost" : "tokens";

// ---------- tooltips: instant, follow the pointer, keyboard and touch ----------
// Any element with data-tip ("first line\nmore lines") gets one, with no delay: it shows on
// pointer enter, follows the pointer and stays until the pointer leaves. Keyboard focus shows
// it above the element. On touch the first tap shows it and a second tap on the same element
// goes through as a click (so a tap never filters the page by surprise).
const tip = (() => {
  let el = null, cur = null, lastPointer = "mouse";
  const node = () => {
    if (!el) {
      el = document.createElement("div");
      el.id = "dtip"; el.className = "dtip"; el.setAttribute("role", "tooltip");
      document.body.appendChild(el);
    }
    return el;
  };
  function place(at) {
    const n = el, w = n.offsetWidth, h = n.offsetHeight, vw = document.documentElement.clientWidth, vh = innerHeight, m = 8;
    let left, top;
    if (at.rect) {   // anchored above the element, below when there's no room
      const r = at.rect;
      left = r.left + r.width / 2 - w / 2; top = r.top - h - 8;
      if (top < m) top = r.bottom + 8;
    } else {         // beside the pointer, flipped at the viewport edges
      left = at.x + 14; top = at.y + 18;
      if (left + w > vw - m) left = at.x - w - 14;
      if (top + h > vh - m) top = at.y - h - 12;
    }
    left = Math.max(m, Math.min(left, vw - m - w));
    top = Math.max(m, Math.min(top, vh - m - h));
    n.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  }
  function show(target, at) {
    const n = node();
    n.innerHTML = String(target.dataset.tip || "").split("\n")
      .map((l, i) => `<div class="${i ? "tip-sub" : "tip-main"}">${escapeHtml(l)}</div>`).join("");
    cur = target;
    n.classList.add("on");
    place(at || { rect: target.getBoundingClientRect() });
  }
  function hide() { cur = null; el?.classList.remove("on"); }
  const tipOf = t => t && t.closest ? t.closest("[data-tip]") : null;
  document.addEventListener("pointerover", e => {
    if (e.pointerType === "touch") return;
    const t = tipOf(e.target);
    if (t && t !== cur) show(t, { x: e.clientX, y: e.clientY });
  });
  document.addEventListener("pointermove", e => {
    if (e.pointerType === "touch" || !cur) return;
    if (cur.contains(e.target)) place({ x: e.clientX, y: e.clientY });
  }, { passive: true });
  document.addEventListener("pointerout", e => {
    if (e.pointerType === "touch" || !cur) return;
    if (!cur.contains(e.relatedTarget)) hide();
  });
  document.addEventListener("pointerdown", e => {
    lastPointer = e.pointerType || "mouse";
    if (lastPointer === "touch" && cur && !cur.contains(e.target)) hide();
  }, { capture: true, passive: true });
  document.addEventListener("click", e => {
    if (lastPointer !== "touch") return;
    const t = tipOf(e.target);
    if (!t) return;
    if (t !== cur) {   // first tap: show, and keep the tap from activating
      show(t);
      if (t.dataset.act) { e.preventDefault(); e.stopPropagation(); }
    } else hide();     // second tap: let it through
  }, true);
  document.addEventListener("focusin", e => {
    const t = tipOf(e.target);
    if (t && t === e.target && t.matches(":focus-visible")) show(t);
  });
  document.addEventListener("focusout", e => { if (cur && cur === e.target) hide(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && cur) hide(); });
  addEventListener("scroll", () => { if (cur && lastPointer === "touch") hide(); }, { passive: true, capture: true });
  return { show, hide, within: box => !!cur && box.contains(cur), get current() { return cur; } };
})();
// The touch wording for a tooltip's action line.
const tapOrClick = () => matchMedia("(hover: none)").matches ? "Tap again" : "Click";
// data-tip + an accessible name for an svg cell. lines[0] is the figure, the rest are hints.
function tipCell(el, lines, { act = false, pressed = null } = {}) {
  const ls = lines.filter(Boolean);
  el.dataset.tip = ls.join("\n");
  el.setAttribute("aria-label", ls.join(". "));
  el.setAttribute("role", act ? "button" : "img");
  if (act) el.dataset.act = "1";
  if (pressed != null) el.setAttribute("aria-pressed", String(pressed));
  return el;
}
// Roving focus over cells with data-r/data-c: one tab stop per grid, arrows move, Home/End
// jump along the row, Enter or Space activates. Pointer clicks on data-act cells activate too.
function bindGridKeys(svg, onActivate, focusKey) {
  const cells = [...svg.querySelectorAll("[data-r]")];
  if (!cells.length) return;
  const key = c => `${c.dataset.r},${c.dataset.c}`;
  const at = new Map(cells.map(c => [key(c), c]));
  const byKey = k => k && cells.find(c => c.dataset.key === k);
  const start = byKey(focusKey) || svg.querySelector("[data-sel]") || cells.find(c => c.dataset.act) || cells[0];
  cells.forEach(c => c.setAttribute("tabindex", c === start ? "0" : "-1"));
  const move = (from, to) => { from.setAttribute("tabindex", "-1"); to.setAttribute("tabindex", "0"); to.focus(); };
  svg.addEventListener("keydown", e => {
    const c = e.target.closest?.("[data-r]");
    if (!c) return;
    if (e.key === "Enter" || e.key === " ") { if (c.dataset.act) { e.preventDefault(); onActivate(c); } return; }
    const r = +c.dataset.r, col = +c.dataset.c;
    let next = null;
    const d = { ArrowLeft: [0, -1], ArrowRight: [0, 1], ArrowUp: [-1, 0], ArrowDown: [1, 0] }[e.key];
    if (d) for (let i = 1; i <= 60 && !next; i++) next = at.get(`${r + d[0] * i},${col + d[1] * i}`);
    else if (e.key === "Home" || e.key === "End") {
      const row = cells.filter(x => +x.dataset.r === r).sort((a, b) => a.dataset.c - b.dataset.c);
      next = e.key === "Home" ? row[0] : row[row.length - 1];
    } else return;
    e.preventDefault();
    if (next && next !== c) move(c, next);
  });
  svg.addEventListener("click", e => { const c = e.target.closest?.("[data-act]"); if (c) onActivate(c); });
  if (byKey(focusKey)) byKey(focusKey).focus({ preventScroll: true });
}
const anyWhen = () => STATE.dow !== "" || STATE.hour !== "";
// Is (dow, hour) inside the active weekday/hour filter? null = "any" for a total bar.
const whenSel = (dow, hour) => anyWhen()
  && (STATE.dow === "" || (dow != null && +STATE.dow === dow))
  && (STATE.hour === "" || (hour != null && +STATE.hour === hour));
// Is it exactly the active filter (clicking it again clears the filter)?
const whenExact = (dow, hour) => STATE.dow === (dow == null ? "" : String(dow)) && STATE.hour === (hour == null ? "" : String(hour));
const filterHint = (exact, what) => exact ? `${tapOrClick()} to clear this filter` : `${tapOrClick()} to filter the page to ${what}`;
const activateWhen = c => setWhen(c.dataset.dow ?? null, c.dataset.hour ?? null);

function renderHeatmap() {
  const container = document.getElementById("cal-heatmap");
  const side = document.getElementById("heatmap-side");
  if (!heatData || !container) return;
  // keep keyboard focus on the same cell across a re-render (a click reloads the page's data)
  const focusKey = container.contains(document.activeElement) ? document.activeElement.dataset?.key : null;
  if (tip.within(container)) tip.hide();
  const dayRows = heatData.days.rows || [];
  const span = windowSpan(dayRows[0]?.day);
  const view = STATE.heatView !== "auto" ? STATE.heatView : (span.days <= 31 ? "hours" : "calendar");
  setSegOn("heat-view", "data-view", view);
  setSegOn("heat-metric", "data-metric", STATE.heatMetric);
  document.getElementById("heatmap-title").textContent = view === "hours" ? "When you work" : "Activity calendar";
  container.innerHTML = "";
  const dist = heatData.dist;
  const distActive = (hourDowCells(dist) || dist?.hours || []).some(c => (c.msgs || 0) > 0);
  if (view === "hours" ? !distActive && !dayRows.length : !dayRows.length) {
    container.innerHTML = `<div class="muted heat-empty">No activity ${STATE.day ? "on this day" : "in this window"}.</div>`;
    side.innerHTML = "";
    document.getElementById("heatmap-hint").textContent = "";
    return;
  }
  if (view === "hours" && span.days === 1) renderHourStrip(container, dist, isoLocal(span.from), focusKey);
  else if (view === "hours") renderHourGrid(container, dist, focusKey);
  else renderCalendar(container, dayRows, span, focusKey);
  renderHeatSide(side, dayRows, span, dist);
}

function heatLegend(note) {
  const div = document.createElement("div");
  div.className = "heatmap-summary";
  div.innerHTML = `<span class="muted">${note}</span><div class="heatmap-legend" aria-hidden="true"><span>less</span>${
    [0, 1, 2, 3, 4, 5].map(i => `<span class="sq" style="background:var(--heat-${i})"></span>`).join("")}<span>more</span></div>`;
  return div;
}
const heatFill = (v, max, dim) => ({ fill: `var(--heat-${heatLevel(v, max)})`, opacity: dim ? .3 : 1 });
const selRing = { stroke: "var(--text)", strokeWidth: "1.5px" };

// One day: a single 24-hour strip (a weekday grid would be six empty rows).
function renderHourStrip(container, dist, day, focusKey) {
  const want = STATE.heatMetric;
  const hasCost = (dist?.hours || []).some(r => "cost" in r);
  const metric = want === "cost" && !hasCost ? "tokens" : want;
  const fmt = metricFmt(metric);
  const width = container.getBoundingClientRect().width || 900;
  const narrow = width < 500, gut = narrow ? 2 : 3;
  const cell = Math.max(10, Math.min(40, Math.floor((width - 2) / 24) - gut));
  const ch = narrow ? 30 : 38, top = 6;
  const rowW = 24 * (cell + gut) - gut;
  const w = rowW + 2, h = top + ch + 20;
  const svg = svgEl("svg", { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: "group" }, { fontFamily: "inherit" });
  const hours = Array.from({ length: 24 }, (_, hr) => dist?.hours?.find(r => r.hour === hr) || { hour: hr });
  const max = Math.max(0, ...hours.map(r => r[metric] || 0));
  const ink = { fill: "var(--muted)" };
  const step = narrow ? 6 : 3;
  hours.forEach((r, hr) => {
    const x = 1 + hr * (cell + gut);
    const exact = whenExact(null, hr), sel = STATE.hour !== "" && +STATE.hour === hr;
    const act = (r.msgs || 0) > 0 || exact;
    const rect = svgEl("rect", { x, y: top, width: cell, height: ch, rx: Math.max(2, Math.round(cell * .18)) },
      { ...heatFill(r[metric], max, STATE.hour !== "" && !sel), ...(sel ? selRing : {}) });
    Object.assign(rect.dataset, { r: 0, c: hr, key: `s${hr}`, hour: hr });
    if (sel) rect.dataset.sel = "1";
    tipCell(rect, [
      r.msgs ? `${fmtDayLong(day)} · ${hourRange(hr)}: ${fmtMoney(r.cost || 0)} · ${fmtCompact(r.tokens || 0)} tokens · ${fmtInt(r.msgs)} msgs`
             : `${fmtDayLong(day)} · ${hourRange(hr)}: no activity`,
      act ? filterHint(exact, hourRange(hr)) : "",
    ], { act, pressed: act ? exact : null });
    svg.appendChild(rect);
    if (hr % step === 0) svg.appendChild(svgEl("text", { x, y: top + ch + 14, "font-size": 9.5, "aria-hidden": "true" }, ink, hh(hr)));
  });
  const peak = hours.reduce((m, r) => (r[metric] || 0) > (m[metric] || 0) ? r : m, hours[0]);
  svg.setAttribute("aria-label", `${metricWord(metric)} by hour on ${fmtDayLong(day)}, local time. Busiest hour ${hourRange(peak.hour)} at ${fmt(peak[metric] || 0)}${metric === "cost" ? "" : " tokens"}. Arrow keys move between hours; Enter filters the page.`);
  container.appendChild(svg);
  container.appendChild(heatLegend("one cell per hour · click an hour to filter the page"));
  document.getElementById("heatmap-hint").textContent = `${metricWord(metric)} by hour, ${fmtRange(day)}, local time`;
  bindGridKeys(svg, activateWhen, focusKey);
}

// Hour × weekday grid with totals per weekday (right) and per hour (bottom). Without the
// server's matrix it falls back to the two totals as heat strips: nothing is estimated.
// Every cell and total bar filters the page (weekday, hour, or both) and shows its tooltip.
function renderHourGrid(container, dist, focusKey) {
  const cells = hourDowCells(dist);
  const want = STATE.heatMetric;
  const hint = document.getElementById("heatmap-hint");
  const hasCost = cells ? cells.some(c => "cost" in c) : (dist?.hours || []).some(r => "cost" in r);
  const metric = want === "cost" && !hasCost ? "tokens" : want;
  const fmt = metricFmt(metric);
  const unitSfx = metric === "cost" ? "" : " tokens";
  const note = want === "cost" && !hasCost ? " · cost by hour needs a server update, showing tokens" : "";
  const width = container.getBoundingClientRect().width || 900;
  const svg = svgEl("svg", { role: "group" }, { fontFamily: "inherit" });
  const ink = { fill: "var(--muted)" };

  if (!cells) {
    // Fallback: hour totals and weekday totals, each as one heat strip.
    const hours = Array.from({ length: 24 }, (_, h) => dist?.hours?.find(r => r.hour === h) || { hour: h });
    const dows = Array.from({ length: 7 }, (_, d) => dist?.dows?.find(r => r.dow === d) || { dow: d });
    // Strip labels sit above each strip, so the 24 cells get the full width on a phone.
    const padL = 0, gut = width < 500 ? 2 : 3, top1 = 30;
    const cell = Math.max(8, Math.min(34, Math.floor((width - 4) / 24) - gut));
    const rowW = 24 * (cell + gut) - gut, dowW = Math.floor((rowW + gut) / 7) - gut;
    const top2 = top1 + cell + 30;
    const w = rowW + 4, h = top2 + cell + 18;
    svg.setAttribute("width", w); svg.setAttribute("height", h); svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    const hMax = Math.max(...hours.map(r => r[metric] || 0)), dMax = Math.max(...dows.map(r => r[metric] || 0));
    svg.appendChild(svgEl("text", { x: 0, y: 10, "font-size": 10.5, "font-weight": 600, "aria-hidden": "true" }, ink, "By hour of day"));
    hours.forEach((r, h) => {
      const x = padL + h * (cell + gut);
      if (h % 3 === 0) svg.appendChild(svgEl("text", { x, y: top1 - 5, "font-size": 9.5, "aria-hidden": "true" }, ink, String(h).padStart(2, "0")));
      const exact = whenExact(null, h);
      const rect = svgEl("rect", { x, y: top1, width: cell, height: cell, rx: 3 }, { ...heatFill(r[metric], hMax, anyWhen() && !exact), ...(exact ? selRing : {}) });
      Object.assign(rect.dataset, { r: 0, c: h, key: `h${h}`, hour: h });
      if (exact) rect.dataset.sel = "1";
      svg.appendChild(tipCell(rect, [`${hourRange(h)}: ${fmt(r[metric] || 0)}${unitSfx} · ${fmtInt(r.msgs || 0)} msgs`, filterHint(exact, hourRange(h))], { act: true, pressed: exact }));
    });
    const y2 = top2;
    svg.appendChild(svgEl("text", { x: 0, y: y2 - 6, "font-size": 10.5, "font-weight": 600, "aria-hidden": "true" }, ink, "By weekday"));
    dows.forEach((r, d) => {
      const x = padL + d * (dowW + gut);
      const exact = whenExact(d, null);
      const rect = svgEl("rect", { x, y: y2, width: dowW, height: cell, rx: 3 }, { ...heatFill(r[metric], dMax, anyWhen() && !exact), ...(exact ? selRing : {}) });
      Object.assign(rect.dataset, { r: 1, c: d, key: `d${d}`, dow: d });
      if (exact) rect.dataset.sel = "1";
      svg.appendChild(tipCell(rect, [`${DOW_LONG[d]}: ${fmt(r[metric] || 0)}${unitSfx} · ${fmtInt(r.msgs || 0)} msgs`, filterHint(exact, DOW_LONG[d])], { act: true, pressed: exact }));
      svg.appendChild(svgEl("text", { x: x + dowW / 2, y: y2 + cell + 13, "font-size": 9.5, "text-anchor": "middle", "aria-hidden": "true" }, ink, DOW_NAMES[d]));
    });
    const peakH = hours.reduce((m, r) => (r[metric] || 0) > (m[metric] || 0) ? r : m, hours[0]);
    const peakD = dows.reduce((m, r) => (r[metric] || 0) > (m[metric] || 0) ? r : m, dows[0]);
    svg.setAttribute("aria-label", `${metricWord(metric)} by hour and by weekday. Busiest hour ${hourRange(peakH.hour)}, busiest weekday ${DOW_NAMES[peakD.dow]}.`);
    container.appendChild(svg);
    container.appendChild(heatLegend("hour × weekday detail arrives with the server update"));
    hint.textContent = `${metricWord(metric)} by hour and weekday, local time${note}`;
    bindGridKeys(svg, activateWhen, focusKey);
    return;
  }

  // Full matrix.
  const grid = Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => ({ msgs: 0, tokens: 0, cost: 0 })));
  for (const c of cells) {
    const g = grid[c.dow]?.[c.hour];
    if (!g) continue;
    g.msgs += c.msgs || 0; g.tokens += c.tokens || 0; g.cost += c.cost || 0;
  }
  const val = g => g[metric] || 0;
  const rowTot = grid.map(r => r.reduce((s, g) => s + val(g), 0));
  const colTot = Array.from({ length: 24 }, (_, h) => grid.reduce((s, r) => s + val(r[h]), 0));
  const rowMsgs = grid.map(r => r.reduce((s, g) => s + g.msgs, 0));
  const colMsgs = Array.from({ length: 24 }, (_, h) => grid.reduce((s, r) => s + r[h].msgs, 0));
  const max = Math.max(...grid.flat().map(val));
  const narrow = width < 500;
  const padL = 30, padT = 16, margR = narrow ? 36 : 64, gut = narrow ? 2 : 3;
  const cell = Math.max(7, Math.min(30, Math.floor((width - padL - margR) / 24) - gut));
  const ch = Math.max(cell, 13);   // rows stay tall enough for their weekday label on a phone
  const gridW = 24 * (cell + gut) - gut, gridH = 7 * (ch + gut) - gut;
  const barH = Math.max(18, Math.round(cell * 1.1));
  const w = padL + gridW + margR, h = padT + gridH + 8 + barH + 4;
  svg.setAttribute("width", w); svg.setAttribute("height", h); svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  for (let hr = 0; hr < 24; hr += 3)
    svg.appendChild(svgEl("text", { x: padL + hr * (cell + gut), y: 10, "font-size": 9.5, "aria-hidden": "true" }, ink, String(hr).padStart(2, "0")));
  const rMax = Math.max(...rowTot, 0), cMax = Math.max(...colTot, 0);
  const peakRow = rowTot.indexOf(rMax), peakCol = colTot.indexOf(cMax);
  const mark = (el, exact, data) => {
    Object.assign(el.dataset, data);
    if (exact) el.dataset.sel = "1";
    return el;
  };
  grid.forEach((row, d) => {
    const y = padT + d * (ch + gut);
    svg.appendChild(svgEl("text", { x: 0, y: y + ch / 2 + 3.5, "font-size": 9.5, "aria-hidden": "true" }, ink, DOW_NAMES[d]));
    row.forEach((g, hr) => {
      const exact = whenExact(d, hr), sel = whenSel(d, hr);
      const rect = svgEl("rect", { x: padL + hr * (cell + gut), y, width: cell, height: ch, rx: Math.max(2, Math.round(cell * .18)) },
        { ...heatFill(val(g), max, anyWhen() && !sel), ...(exact ? selRing : {}) });
      mark(rect, exact, { r: d, c: hr, key: `${d},${hr}`, dow: d, hour: hr });
      const act = g.msgs > 0 || exact;
      svg.appendChild(tipCell(rect, [
        g.msgs ? `${DOW_NAMES[d]} ${hourRange(hr)}: ${fmtMoney(g.cost)} · ${fmtCompact(g.tokens)} tokens · ${fmtInt(g.msgs)} msgs`
               : `${DOW_NAMES[d]} ${hourRange(hr)}: no activity`,
        g.msgs ? `all ${DOW_LONG[d]} in view` : "",
        act ? filterHint(exact, `${DOW_NAMES[d]} · ${hourRange(hr)}`) : "",
      ], { act, pressed: act ? exact : null }));
    });
    // weekday total, as a bar to the right of the row; the hit area spans the whole margin
    const exact = whenExact(d, null);
    const bw = rMax ? Math.max(rowTot[d] ? 2 : 0, (rowTot[d] / rMax) * (margR - 14)) : 0;
    const hit = svgEl("rect", { x: padL + gridW + 4, y, width: margR - 6, height: ch, rx: 3 },
      { fill: "transparent", cursor: rowMsgs[d] || exact ? "pointer" : "default" });
    mark(hit, exact, { r: d, c: 24, key: `${d},24`, dow: d });
    svg.appendChild(tipCell(hit, [`${DOW_LONG[d]}: ${fmt(rowTot[d])}${unitSfx} · ${fmtInt(rowMsgs[d])} msgs`,
      rowMsgs[d] || exact ? filterHint(exact, DOW_LONG[d]) : ""], { act: !!rowMsgs[d] || exact, pressed: exact }));
    svg.appendChild(svgEl("rect", { x: padL + gridW + 8, y: y + ch * .2, width: bw, height: ch * .6, rx: 2, "aria-hidden": "true" },
      { fill: d === peakRow && rowTot[d] ? "var(--heat-5)" : "var(--heat-3)", opacity: anyWhen() && !whenSel(d, null) ? .35 : 1,
        pointerEvents: "none", ...(exact ? selRing : {}) }));
  });
  // hour totals, as bars under each column; the hit area is the full column height
  const by = padT + gridH + 8;
  svg.appendChild(svgEl("text", { x: 0, y: by + barH - 2, "font-size": 9.5, "aria-hidden": "true" }, ink, "All"));
  colTot.forEach((v, hr) => {
    const exact = whenExact(null, hr);
    const bh = cMax ? Math.max(v ? 2 : 0, (v / cMax) * barH) : 0;
    const x = padL + hr * (cell + gut);
    const hit = svgEl("rect", { x, y: by, width: cell, height: barH, rx: 2 },
      { fill: "transparent", cursor: colMsgs[hr] || exact ? "pointer" : "default" });
    mark(hit, exact, { r: 7, c: hr, key: `7,${hr}`, hour: hr });
    svg.appendChild(tipCell(hit, [`${hourRange(hr)}, any day: ${fmt(v)}${unitSfx} · ${fmtInt(colMsgs[hr])} msgs`,
      colMsgs[hr] || exact ? filterHint(exact, hourRange(hr)) : ""], { act: !!colMsgs[hr] || exact, pressed: exact }));
    svg.appendChild(svgEl("rect", { x, y: by + barH - bh, width: cell, height: bh, rx: 2, "aria-hidden": "true" },
      { fill: hr === peakCol && v ? "var(--heat-5)" : "var(--heat-3)", opacity: anyWhen() && !whenSel(null, hr) ? .35 : 1,
        pointerEvents: "none", ...(exact ? selRing : {}) }));
  });
  svg.setAttribute("aria-label", `${metricWord(metric)} by weekday and hour of day, local time. Busiest weekday ${DOW_NAMES[peakRow]}, busiest hour ${hourRange(peakCol)}. Arrow keys move between cells; Enter filters the page.`);
  container.appendChild(svg);
  container.appendChild(heatLegend("bars total each weekday (right) and hour (bottom) · click any cell to filter"));
  hint.textContent = `${metricWord(metric)} by weekday and hour, local time${note}`;
  bindGridKeys(svg, activateWhen, focusKey);
}

// Calendar: one cell per day, Monday-first weeks. Click an active day to drill into it.
function renderCalendar(container, dayRows, span, focusKey) {
  const metric = STATE.heatMetric;
  const byDay = new Map(dayRows.map(r => [r.day, r]));
  const first = new Date(span.from);
  first.setDate(first.getDate() - (first.getDay() + 6) % 7);
  const days = [];
  for (let d = new Date(first); d <= span.to; d.setDate(d.getDate() + 1)) {
    const iso = isoLocal(d);
    days.push({ date: new Date(d), iso, inSpan: d >= span.from, ...byDay.get(iso) });
  }
  const max = days.reduce((m, d) => Math.max(m, d[metric] || 0), 0);
  const colCount = Math.ceil(days.length / 7);
  const containerWidth = container.getBoundingClientRect().width || 1000;
  const padLeft = 28, padTop = 20, padRight = 12;
  const availW = Math.max(200, containerWidth - padLeft - padRight);
  const cell = Math.min(28, Math.max(8, Math.floor((availW / colCount) / 1.22)));
  const gutter = Math.max(2, Math.round(cell * 0.22));
  const w = padLeft + colCount * (cell + gutter) + padRight;
  const h = padTop + 7 * (cell + gutter);
  const svg = svgEl("svg", { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: "group" }, { fontFamily: "inherit" });
  ["Mon", "", "Wed", "", "Fri", "", ""].forEach((lab, i) => {
    if (lab) svg.appendChild(svgEl("text", { x: 0, y: padTop + i * (cell + gutter) + cell - 2, "font-size": "9.5", "aria-hidden": "true" }, { fill: "var(--muted)" }, lab));
  });
  // Month labels; the year rides on the first label and on each January when the span crosses years.
  const multiYear = span.from.getFullYear() !== span.to.getFullYear();
  let lastMonth = -1, lastLabelX = -Infinity, firstLabel = true;
  days.forEach((d, idx) => {
    const x = padLeft + Math.floor(idx / 7) * (cell + gutter);
    if (d.date.getDate() <= 7 && d.date.getMonth() !== lastMonth) {
      lastMonth = d.date.getMonth();
      const withYear = multiYear && (firstLabel || d.date.getMonth() === 0);
      if (x - lastLabelX < (withYear ? 56 : 30)) return;
      lastLabelX = x; firstLabel = false;
      svg.appendChild(svgEl("text", { x, y: 12, "font-size": "10", "font-weight": "500", "aria-hidden": "true" }, { fill: "var(--muted)" },
        (withYear ? DF.mony : DF.mon).format(d.date)));
    }
  });
  const scope = whenLabel();
  days.forEach((d, idx) => {
    const active = d.inSpan && (d.msgs > 0 || d.tokens > 0);
    const rect = svgEl("rect", {
      x: padLeft + Math.floor(idx / 7) * (cell + gutter), y: padTop + (idx % 7) * (cell + gutter),
      width: cell, height: cell, rx: Math.max(2, Math.round(cell * 0.18)),
    }, { fill: `var(--heat-${heatLevel(d.inSpan ? d[metric] : 0, max)})`, opacity: d.inSpan ? 1 : .35 });
    Object.assign(rect.dataset, { r: idx % 7, c: Math.floor(idx / 7), key: `cal${d.iso}` });
    if (active) { rect.dataset.day = d.iso; rect.classList.add("active"); }
    tipCell(rect, active
      ? [`${fmtDayLong(d.iso)}: ${fmtMoney(d.cost)} · ${fmtCompact(d.tokens)} tokens · ${fmtInt(d.msgs)} msgs`,
         scope ? `${scope} only` : "", `${tapOrClick()} to view this day`]
      : [`${fmtDayLong(d.iso)}: ${d.inSpan ? "no activity" : "outside this window"}`], { act: active });
    svg.appendChild(rect);
  });
  const act = days.filter(d => d.inSpan && d.msgs > 0);
  svg.setAttribute("aria-label", `Calendar of ${metricWord(metric)} per day: ${act.length} active days of ${span.days}, ${fmtRange(span.from, span.to)}. Arrow keys move between days; Enter views a day.`);
  container.appendChild(svg);
  container.appendChild(heatLegend(`${metricWord(metric)} per day · click a day to view it`));
  document.getElementById("heatmap-hint").textContent = `${metricWord(metric)} per day, ${fmtRange(span.from, span.to)}${scope ? ` · ${scope}` : ""}`;
  bindGridKeys(svg, c => setDay(c.dataset.day), focusKey);
}

// Side stats: plain sentences about this window. Streaks live in a tooltip only.
function renderHeatSide(side, dayRows, span, dist) {
  if (!side) return;
  const inSpan = dayRows.filter(r => { const d = toDate(r.day); return d >= span.from && d <= span.to; });
  const act = inSpan.filter(r => (r.msgs || 0) > 0);
  const n = act.length, N = span.days;
  const cost = act.reduce((s, r) => s + (r.cost || 0), 0);
  const tok = act.reduce((s, r) => s + (r.tokens || 0), 0);
  const msgs = act.reduce((s, r) => s + (r.msgs || 0), 0);
  const busiest = act.reduce((m, r) => (r.cost || 0) > (m?.cost ?? -1) ? r : m, null);
  // longest run of consecutive active days, for the tooltip
  const activeSet = new Set(act.map(r => r.day));
  let run = 0, best = 0;
  for (let d = new Date(span.from); d <= span.to; d.setDate(d.getDate() + 1)) {
    run = activeSet.has(isoLocal(d)) ? run + 1 : 0; best = Math.max(best, run);
  }
  const plural = (k, w) => `${k} ${w}${k === 1 ? "" : "s"}`;
  const tiles = [];
  tiles.push(`<div class="side-stat" tabindex="0" data-tip="Longest run of consecutive active days: ${plural(best, "day")}">
      <span class="lbl">Active on</span><span class="val">${n} of ${plural(N, "day")}</span>
      <span class="sub">${STATE.day ? "the day you're viewing" : `in this window, ${escapeHtml(fmtRange(span.from, span.to))}`}</span></div>`);
  tiles.push(`<div class="side-stat"><span class="lbl">Average per active day</span><span class="val">${n ? fmtMoney(cost / n) : "—"}</span>
      <span class="sub">${n ? `${fmtCompact(tok / n)} tokens · ${fmtInt(msgs / n)} msgs, at API rates` : "no activity"}</span></div>`);
  if (busiest) {
    const sub = STATE.day ? escapeHtml(fmtDayLong(busiest.day))
      : `<button type="button" class="link-btn" data-day="${escapeHtml(busiest.day)}" title="View this day">${escapeHtml(fmtDayLong(busiest.day))}</button>`;
    tiles.push(`<div class="side-stat"><span class="lbl">Busiest day</span><span class="val">${fmtMoney(busiest.cost)}</span>
      <span class="sub">${sub} · ${fmtCompact(busiest.tokens)} tokens</span></div>`);
  }
  // Busiest hour and weekday, from the matrix when present, else the two totals.
  const cells = hourDowCells(dist);
  const key = STATE.heatMetric === "cost" && (cells || dist?.hours || []).some(r => "cost" in r) ? "cost" : "tokens";
  const hours = Array(24).fill(0), dows = Array(7).fill(0);
  if (cells) for (const c of cells) { hours[c.hour] += c[key] || 0; dows[c.dow] += c[key] || 0; }
  else { for (const r of dist?.hours || []) hours[r.hour] = r[key] || 0; for (const r of dist?.dows || []) dows[r.dow] = r[key] || 0; }
  if (Math.max(...hours) > 0) {
    const ph = hours.indexOf(Math.max(...hours)), pd = dows.indexOf(Math.max(...dows));
    tiles.push(`<div class="side-stat"><span class="lbl">Busiest hour</span><span class="val">${hourRange(ph)}</span>
      <span class="sub">${STATE.day ? "local time" : `local time · ${DOW_LONG[pd]} are the busiest`} · by ${key === "cost" ? "cost" : "tokens"}</span></div>`);
  }
  side.innerHTML = tiles.join("");
  side.querySelectorAll("button[data-day]").forEach(b => b.addEventListener("click", () => setDay(b.dataset.day)));
}

// ---------- Models: donut (share) beside the table (detail); both filter ----------
// Top five models by spend, then "Other". A model in the model filter is always its own
// slice, pulled out of Other, so its share is visible. Clicking Other (slice or legend)
// expands it into its members; "Collapse other" folds them back. Hovering a slice, a legend
// entry or a table row shows that model's $ and share in the centre; clicking filters.
let modelsView = null;   // { rows, slices, total, folded: Set, colorOf: Map }
let modelsData = null;   // last /api/by_model rows, so expanding Other needs no refetch
const fmtShare = pct => pct >= 0.1 || pct === 0 ? fmtPct(pct) : "<0.1%";
const selectedModels = () => new Set((STATE.model || "").split(",").filter(Boolean));
const sliceOf = model => modelsView ? modelsView.slices.findIndex(s => s.model === model || s.members?.includes(model)) : -1;
function setDonutCenter(i, model) {
  const el = document.getElementById("donut-center");
  if (!el || !modelsView) return;
  const { slices, total, rows } = modelsView;
  const rowOf = m => rows.find(r => r.model === m && r.cost > 0);
  const sel = [...selectedModels()].find(rowOf);
  let label = null, cost = 0;
  if (model && rowOf(model)) { label = shortModel(model); cost = rowOf(model).cost; }
  else if (i != null && slices[i]) { label = slices[i].label; cost = slices[i].cost; }
  else if (sel) { label = shortModel(sel); cost = rowOf(sel).cost; }
  el.innerHTML = label != null
    ? `<span class="dc-val">${fmtMoneyShort(cost)}</span><span class="dc-sub">${escapeHtml(label)}</span><span class="dc-sub">${fmtShare(total ? cost / total * 100 : 0)} of spend</span>`
    : `<span class="dc-val">${fmtMoneyShort(total)}</span><span class="dc-sub">all models</span><span class="dc-sub">at API rates</span>`;
}
// i: slice index (or null); model: a specific model (a table row or legend entry), which can
// sit inside the Other slice.
function highlightModel(i, source, model = null) {
  if (!modelsView) return;
  setDonutCenter(i, model);
  document.querySelectorAll("#models-table tbody tr[data-model]").forEach(tr =>
    tr.classList.toggle("hl", model ? tr.dataset.model === model : i != null && Number(tr.dataset.slice) === i));
  document.querySelectorAll("#donut-legend [data-slice]").forEach(b =>
    b.classList.toggle("hl", i != null && Number(b.dataset.slice) === i));
  const chart = charts["chart-models"];
  if (chart && source !== "chart") {
    chart.setActiveElements(i == null || i < 0 ? [] : [{ datasetIndex: 0, index: i }]);
    chart.update();
  }
}
function setOtherOpen(open) {
  STATE.otherOpen = open;
  renderModels();
  document.querySelector(open ? "#donut-legend .dl-collapse" : "#donut-legend .dl-other")?.focus({ preventScroll: true });
}
async function loadModels() {
  // The filter source: never filtered by model itself, the selection is highlighted instead.
  const j = await api("/api/by_model", { window: STATE.window }, { skipModel: true });
  modelsData = j.rows || [];
  renderModels();
}
function renderModels() {
  const rows = (modelsData || []).slice().sort((a, b) => (b.cost - a.cost) || (b.tokens - a.tokens));
  const ranked = rows.filter(r => r.cost > 0).sort((a, b) => b.cost - a.cost).map(r => r.model);
  const slots = modelSlots(ranked);
  const total = rows.reduce((s, r) => s + (r.cost || 0), 0);
  const fastTotal = rows.reduce((s, r) => s + (r.fast_cost_usd || 0), 0);
  const premTotal = rows.reduce((s, r) => s + (r.fast_premium_usd || 0), 0);
  document.getElementById("models-hint").textContent = (fastTotal > 0
    ? `share of spend at API rates · fast mode ${fmtMoney(fastTotal)} (${fmtMoney(premTotal)} premium)`
    : "share of spend at API rates") + " · click a model to filter";
  const sel = selectedModels();
  // paint(a) -> the slice colour from the live theme; css -> the same colour for the table swatch
  const slices = ranked.filter(m => slots.has(m)).map(m => {
    const r = rows.find(x => x.model === m), slot = slots.get(m);
    return { model: m, label: shortModel(m), cost: r.cost, paint: a => alpha(slotColor(slot), a), css: slotVar(slot) };
  });
  const rest = rows.filter(r => !slots.has(r.model) && r.cost > 0);
  for (const r of rest.filter(r => sel.has(r.model)))   // the filtered model, pulled out of Other
    slices.push({ model: r.model, label: shortModel(r.model), cost: r.cost, pulled: true, paint: a => alpha(T.text2, a), css: "var(--text-2)" });
  const folded = rest.filter(r => !sel.has(r.model));
  const open = STATE.otherOpen && folded.length > 0;
  if (open) {
    // members in steps of the "other" grey, darkest (costliest) first
    folded.forEach((r, k) => {
      const t = folded.length > 1 ? 1 - k * .6 / (folded.length - 1) : 1;
      slices.push({ model: r.model, label: shortModel(r.model), cost: r.cost, member: true,
                    paint: a => alpha(T.other, a * t), css: `color-mix(in srgb, var(--chart-other) ${Math.round(t * 100)}%, transparent)` });
    });
  } else if (folded.length) {
    slices.push({ model: null, other: true, label: `Other (${folded.length})`, cost: folded.reduce((s, r) => s + r.cost, 0),
                  members: folded.map(r => r.model), paint: a => alpha(T.other, a), css: "var(--chart-other)" });
  }
  const colorOf = new Map(slices.filter(s => s.model).map(s => [s.model, s.css]));
  modelsView = { rows, slices, total, folded: new Set(open ? [] : folded.map(r => r.model)), colorOf };
  const dimmed = s => sel.size > 0 && !(s.model && sel.has(s.model));
  const wrap = document.getElementById("models-donut");
  if (wrap) wrap.hidden = !slices.length;
  if (slices.length) {
    upsertChart("chart-models", () => ({
      type: "doughnut",
      data: { labels: slices.map(s => s.label), datasets: [{
        data: slices.map(s => s.cost),
        backgroundColor: slices.map(s => s.paint(dimmed(s) ? .28 : 1)),
        hoverBackgroundColor: slices.map(s => s.paint(1)),
        borderColor: T.panel, hoverBorderColor: T.panel, borderWidth: 2, hoverOffset: 7,
        offset: slices.map(s => s.pulled ? 8 : 0),
      }] },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: "70%", layout: { padding: 10 },
        onHover: (e, els) => {
          const i = els.length ? els[0].index : null;
          highlightModel(i, "chart");
          e.native.target.style.cursor = i != null ? "pointer" : "default";
        },
        // deferred: re-rendering destroys this chart, which must finish handling its event first
        onClick: (_e, els) => {
          const s = els.length ? slices[els[0].index] : null;
          if (s?.other) setTimeout(() => setOtherOpen(true));
          else if (s?.model) setTimeout(() => setModelFilter(s.model));
        },
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
      },
    }), {
      label: "Share of API-rate spend by model",
      summary: slices.map(s => `${s.label} ${fmtPct(total ? s.cost / total * 100 : 0)}`).join(", ") + ". The same figures are in the table.",
    });
    const canvas = document.getElementById("chart-models");
    if (!canvas.dataset.bound) {
      canvas.dataset.bound = "1";
      canvas.addEventListener("mouseleave", () => highlightModel(null, "chart"));
    }
  }
  renderDonutLegend(slices, total, sel, open);
  setDonutCenter(null);
  renderTable("models-table", MODEL_COLS(slots, total || 1, modelsView), rows, {
    showFast: true, empty: "No model usage in this window.",
    rowClass: r => ["clickable", sel.size ? (sel.has(r.model) ? "sel" : "dim") : ""].filter(Boolean).join(" "),
    onRow: (tr, r) => {
      const i = sliceOf(r.model);
      if (i >= 0) tr.dataset.slice = i;
      tr.dataset.model = r.model;
      tr.tabIndex = 0;
      tr.setAttribute("aria-pressed", String(sel.has(r.model)));
      tr.title = sel.has(r.model) ? "Click to clear the model filter" : "Click to filter every card to this model";
      tr.addEventListener("mouseenter", () => highlightModel(i >= 0 ? i : null, "table", r.model));
      tr.addEventListener("mouseleave", () => highlightModel(null, "table"));
      tr.addEventListener("focus", () => highlightModel(i >= 0 ? i : null, "table", r.model));
      tr.addEventListener("blur", () => highlightModel(null, "table"));
      tr.addEventListener("click", e => { if (!e.target.closest("a")) setModelFilter(r.model); });
      tr.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setModelFilter(r.model); } });
    },
  });
}
// The donut's legend: one button per slice (filter, or expand Other), plus "Collapse other".
// It is also the keyboard route into the donut, which is a canvas.
function renderDonutLegend(slices, total, sel, open) {
  const box = document.getElementById("donut-legend");
  if (!box) return;
  box.innerHTML = slices.map((s, i) => {
    const pct = fmtShare(total ? s.cost / total * 100 : 0);
    const sw = `<span class="swatch" style="background:${s.css}"></span>`;
    return s.other
      ? `<button type="button" class="dl-item dl-other" data-slice="${i}" aria-expanded="false" title="Show the ${s.members.length} models in Other">${sw}<span class="dl-name">${escapeHtml(s.label)}</span><span class="dl-pct">${pct}</span>${icon("chevron", "dl-chev")}</button>`
      : `<button type="button" class="dl-item${sel.has(s.model) ? " on" : ""}" data-slice="${i}" data-model="${escapeHtml(s.model)}" aria-pressed="${sel.has(s.model)}" title="${escapeHtml(s.model)}">${sw}<span class="dl-name">${escapeHtml(s.label)}</span><span class="dl-pct">${pct}</span></button>`;
  }).join("") + (open ? `<button type="button" class="link-btn dl-collapse" aria-expanded="true">Collapse other</button>` : "");
  box.hidden = !slices.length;
  if (box.dataset.bound) return;
  box.dataset.bound = "1";
  box.addEventListener("click", e => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.classList.contains("dl-collapse")) setOtherOpen(false);
    else if (b.classList.contains("dl-other")) setOtherOpen(true);
    else if (b.dataset.model) setModelFilter(b.dataset.model);
  });
  const hl = e => {
    const b = e.target.closest?.("[data-slice]");
    highlightModel(b ? Number(b.dataset.slice) : null, "legend", b?.dataset.model || null);
  };
  box.addEventListener("pointerover", hl);
  box.addEventListener("focusin", hl);
  box.addEventListener("pointerleave", () => highlightModel(null, "legend"));
  box.addEventListener("focusout", e => { if (!box.contains(e.relatedTarget)) highlightModel(null, "legend"); });
}
const MODEL_COLS = (slots = new Map(), total = 1, view = null) => {
  const sw = m => view?.colorOf.get(m) || slotVar(slots.get(m));
  const inOther = m => view?.folded.has(m)
    ? ` <span class="in-other" tabindex="0" data-tip="Folded into Other in the chart. Click Other to expand it.">in Other</span>` : "";
  return [
    { key: "model", label: "Model", sort: r => shortModel(r.model), render: r =>
        `<span class="model-cell" title="${escapeHtml(r.model)}"><span class="swatch" style="background:${sw(r.model)}"></span>${escapeHtml(shortModel(r.model))}${estBadge(r)}${inOther(r.model)}</span>` },
    { key: "msgs", label: "Msgs", num: true, render: r => fmtInt(r.msgs) },
    { key: "tokens", label: "Tokens", num: true, render: r => fmtCompact(r.tokens) },
    { key: "cost", label: "Cost", num: true, render: r => fmtMoney(r.cost) },
    { key: "share", label: "Share of spend", sort: r => r.cost, num: true, render: r => {
        const pct = (r.cost || 0) / total * 100;
        return `<div class="share"><span class="share-track"><span class="share-bar" style="width:${Math.max(pct, pct > 0 ? 1 : 0)}%;background:${sw(r.model)}"></span></span><span class="share-pct">${pct >= 0.1 || pct === 0 ? fmtPct(pct) : "<0.1%"}</span></div>`;
      } },
    FAST_COL,
  ];
};

// ---------- top projects: chart or table ----------
const PROJECT_COLS = [
  { key: "project", label: "Project", render: projectCell },
  { key: "msgs", label: "Msgs", num: true, render: r => fmtInt(r.msgs) },
  { key: "sessions", label: "Sessions", num: true, render: r => fmtInt(r.sessions) },
  { key: "tokens", label: "Tokens", num: true, render: r => fmtCompact(r.tokens) },
  { key: "cost", label: "Cost", num: true, render: r => costCell(r.cost, r) },
  FAST_COL,
  { key: "last_ts", label: "Last active", num: true, render: r => fmtDay(r.last_ts?.slice(0, 10)) },
];
async function loadProjects() {
  // The filter source stays unfiltered; the selected bar is highlighted instead.
  const j = await api("/api/by_project", { window: STATE.window, limit: 15 }, { skipProject: true });
  const rows = j.rows;
  renderTable("projects-table", PROJECT_COLS, rows, {
    empty: "No projects in this window.",
    rowClass: r => STATE.project && r.project !== STATE.project ? "dim" : "",
    onRow: (tr, r) => tr.querySelector(".project-cell")?.addEventListener("click", () => setProjectFilter(r.project)),
  });
  const by = STATE.projBy;
  const fmt = by === "cost" ? fmtMoney : fmtCompact;
  const labels = rows.map(r => r.project);
  const top = rows.slice().sort((a, b) => b[by] - a[by])[0];
  fitHBar("chart-projects", rows.length, 220);
  upsertChart("chart-projects", () => ({
    type: "bar",
    data: { labels, datasets: [{
      label: by, data: rows.map(r => r[by]),
      backgroundColor: rows.map(r => alpha(T.series[0], !STATE.project || r.project === STATE.project ? 1 : .3)),
      borderWidth: 0, borderRadius: 4, borderSkipped: "start", maxBarThickness: 22,
    }]},
    options: {
      indexAxis: "y", responsive: true, maintainAspectRatio: false,
      onClick: (_e, els) => { if (els.length) setProjectFilter(rows[els[0].index].project); },
      onHover: (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; },
      scales: {
        x: { grid: { color: T.grid }, border: { display: false }, ticks: { callback: v => by === "cost" ? "$" + fmtCompact(v) : fmtCompact(v) },
             title: axisTitle({ tokens: "Tokens", cost: "USD at API rates", msgs: "Messages", sessions: "Sessions" }[by]) },
        y: hAxisY(),
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: {
          title: ctx => rows[ctx[0].dataIndex].project_path || labels[ctx[0].dataIndex],
          label: ctx => `${by}: ${fmt(ctx.parsed.x)}`,
          footer: () => "click to filter",
        } },
      },
    },
  }), {
    label: `Top projects by ${by}`,
    summary: top ? `${rows.length} projects. Top: ${top.project} with ${fmt(top[by])} ${by}. The same data is in the table view.` : "No projects in this window.",
  });
}

// ---------- sessions, days, accounts tables ----------
const safeUrl = u => /^https:\/\//.test(u || "") ? u : null;
const sessCell = r => {
  const title = r.title
    ? `<span class="title" title="${escapeHtml(r.title)}${r.title_source === "ai" ? " (title written by Claude)" : ""}">${escapeHtml(r.title)}</span>`
    : `<span class="title fallback" title="Untitled session ${escapeHtml(r.session_id)}">${escapeHtml((r.session_id || "").slice(0, 8))}</span>`;
  const proj = r.project ? `<span class="proj" title="${escapeHtml(r.project_path || r.project)}: click to filter">${escapeHtml(r.project)}</span> · ` : "";
  return `<div class="sess-cell">${title}<span class="meta">${proj}${fmtInt(r.msgs)} msgs · ${fmtCompact(r.tokens)} tokens</span></div>`;
};
const mixCell = (r, slots) => {
  const mix = r.model_mix || [];
  if (!mix.length) return `<span class="quiet">—</span>`;
  const top = r.top_model || mix[0].model;
  const tip = mix.map(m => `${shortModel(m.model)} ${m.share != null ? fmtPct(m.share * 100) : ""} (${fmtMoney(m.cost)})`).join(", ");
  const segs = mix.filter(m => (m.share || 0) > 0).map(m =>
    `<span style="flex:${m.share} 1 0;background:${slotVar(slots.get(m.model))}"></span>`).join("");
  const extra = mix.length > 1 ? `<span class="muted" style="font-size:10.5px">+${mix.length - 1}</span>` : "";
  return `<div class="mix-cell" tabindex="0" data-tip="${escapeHtml(tip)}">
    <span class="model-cell"><span class="swatch" style="background:${slotVar(slots.get(top))}"></span>${escapeHtml(shortModel(top))}${extra}</span>
    ${segs ? `<span class="mix-bar" role="img" aria-label="Model mix by cost: ${escapeHtml(tip)}">${segs}</span>` : ""}
  </div>`;
};
const activeCell = r => {
  if (r.active_minutes == null) return `<span class="quiet">—</span>`;
  const span = r.active_source === "span";
  const main = fmtDur(Math.round(r.active_minutes));
  if (span) return `${main}<span class="approx" title="No turn timings in this transcript: this is the first-to-last message span, so it overstates active time">≈ span</span>`;
  return `<span title="Sum of turn durations">${main}</span>` +
    (r.duration_minutes > r.active_minutes ? `<div class="muted" style="font-size:10.5px;white-space:nowrap">of ${fmtDur(r.duration_minutes)} span</div>` : "");
};
const prCell = r => {
  const prs = (r.prs || []).filter(p => safeUrl(p.url));
  if (!prs.length) return `<span class="quiet">—</span>`;
  const shown = prs.slice(0, 3).map(p =>
    `<a href="${escapeHtml(p.url)}" target="_blank" rel="noopener" title="${escapeHtml(p.repo || "")}#${escapeHtml(p.number)}">#${escapeHtml(p.number)}</a>`).join("");
  const rest = prs.slice(3);
  return `<div class="pr-links">${shown}${rest.length ? `<span class="more" title="${escapeHtml(rest.map(p => `${p.repo}#${p.number}`).join(", "))}">+${rest.length}</span>` : ""}</div>`;
};
const SESSION_COLS = (slots = new Map()) => [
  { key: "title", label: "Session", sort: r => (r.title || r.session_id || "").toLowerCase(), render: sessCell },
  { key: "top_model", label: "Models", sort: r => shortModel(r.top_model), render: r => mixCell(r, slots) },
  { key: "active_minutes", label: "Active", num: true, title: "Time spent in turns; ≈ span marks sessions without turn timings",
    sort: r => r.active_minutes, render: activeCell },
  { key: "cost", label: "Cost", num: true, render: r => costCell(r.cost, r) },
  { key: "subagent_cost", label: "Subagents", num: true, title: "Part of the cost spent in subagent (sidechain) turns",
    render: r => r.subagent_cost > 0
      ? `${fmtMoney(r.subagent_cost)}<div class="muted" style="font-size:10.5px">${fmtPct(r.cost ? r.subagent_cost / r.cost * 100 : 0, 0)} of session</div>`
      : `<span class="quiet">—</span>` },
  FAST_COL,
  { key: "prs", label: "PRs", sort: r => (r.prs || []).length || null, render: prCell },
  { key: "branch", label: "Branch", render: r => r.branch ? `<span class="branch" title="${escapeHtml(r.branch)}">${escapeHtml(r.branch)}</span>` : `<span class="quiet">—</span>` },
  { key: "last_ts", label: "Last active", num: true, render: r => fmtDay(r.last_ts?.slice(0, 10)) },
];
const SESS_HINT = { cost: "API-rate cost", tokens: "tokens", msgs: "messages", active: "active time",
                    duration: "first-to-last message span", recent: "most recent activity" };
async function loadTopSessions() {
  const [j, bm] = await Promise.all([
    api("/api/sessions", { window: STATE.window, by: STATE.sessBy, limit: 50 }),
    api("/api/by_model", { window: STATE.window }).catch(() => ({ rows: [] })),
  ]);
  const rows = j.rows || [];
  // Same ranking as the Models table, so a model wears the same colour everywhere.
  const slots = modelSlots(bm.rows.filter(r => r.cost > 0).sort((a, b) => b.cost - a.cost).map(r => r.model));
  document.getElementById("sess-hint").textContent = `top ${rows.length || 50} by ${SESS_HINT[STATE.sessBy] || STATE.sessBy}`;
  renderTable("sessions-table", SESSION_COLS(slots), rows, {
    empty: "No sessions in this window.",
    onRow: (tr, r) => { if (r.project) tr.querySelector(".sess-cell .proj")?.addEventListener("click", () => setProjectFilter(r.project)); },
  });
}

const DAY_COLS = [
  { key: "day", label: "Day", render: r => `<span title="${escapeHtml(fmtDayLong(r.day))}">${escapeHtml(fmtDay(r.day))}</span>` },
  { key: "msgs", label: "Msgs", num: true, render: r => fmtInt(r.msgs) },
  { key: "sessions", label: "Sessions", num: true, render: r => fmtInt(r.sessions) },
  { key: "tokens", label: "Tokens", num: true, render: r => fmtCompact(r.tokens) },
  { key: "cost", label: "Cost", num: true, render: r => costCell(r.cost, r) },
  FAST_COL,
];
async function loadTopDays() {
  const j = await api("/api/top_days", { window: STATE.window, limit: 10 });
  renderTable("topdays-table", DAY_COLS, j.rows, {
    empty: "No activity in this window.",
    rowClass: r => STATE.day ? (r.day === STATE.day ? "sel" : "") : "clickable",
    onRow: (tr, r) => {
      if (STATE.day) return;
      tr.tabIndex = 0; tr.title = "View this day";
      tr.addEventListener("click", () => setDay(r.day));
      tr.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setDay(r.day); } });
    },
  });
}

// Accounts: one row per config dir, with how it is billed and what it actually paid.
const MODE_LABEL = { subscription: "Subscription", api: "API", cloud: "Cloud", gateway: "Gateway" };
const acctCell = r => {
  const mode = r.billing_mode;
  const badge = mode
    ? `<span class="mode-badge" title="${escapeHtml(r.detected === false ? "Billing mode set in config.json" : `Detected from ${(r.signals || []).join(", ") || "the config dir"}`)}">${escapeHtml(MODE_LABEL[mode] || mode)}${
        mode === "gateway" && r.gateway_host ? ` <span class="host">via ${escapeHtml(r.gateway_host)}</span>` : ""}</span>`
    : "";
  return `<div class="acct-cell"><span class="acct-pill ${escapeHtml(r.account)}" title="${escapeHtml(r.account)}">${escapeHtml(acctName(r.account))}</span>${badge}${
    paidByLine(r)}</div>`;
};
// Out of pocket for one account row: you_paid_usd when the server splits employer-paid plans.
const youPaid = r => r.payer === "employer" ? (r.you_paid_usd ?? 0) : (r.you_paid_usd ?? r.actual_cost_usd);
// The plan an account is paid by. Accounts on one shared plan (paid_by) name the others; the
// rest keep the server's basis, minus the file name ("billing.json receipts" -> "receipts").
function paidByLine(r) {
  if (r.payer === "employer") {
    const mo = r.plan_monthly_usd;
    return `<span class="basis plan-line employer" title="Your employer pays this plan; it counts toward plan value, not toward what you paid">Paid by employer${
      mo ? ` · plan ${escapeHtml(fmtMoney(mo))}/mo` : r.plan_value_usd != null ? ` · plan value ${escapeHtml(fmtMoney(r.plan_value_usd))}` : ""}</span>`;
  }
  if (r.paid_by) {
    const others = (r.plan_members || []).filter(m => m !== r.account).map(acctName);
    const plan = `${PLAN_NAMES[r.paid_by] || providerName(r.paid_by)} plan`;
    return `<span class="basis plan-line" title="One ${escapeHtml(plan)} pays for ${escapeHtml([r.account, ...(r.plan_members || []).filter(m => m !== r.account)].map(acctName).join(", "))}; its receipts are split by usage">${
      escapeHtml(plan)}${others.length ? ` · shared with ${escapeHtml(others.join(", "))}` : ""}</span>`;
  }
  if (!r.payment_basis) return "";
  const basis = r.payment_basis.replace(/\bbilling\.json receipts\b/, "from receipts");
  return `<span class="basis">${escapeHtml(basis)}</span>`;
}
// Data-quality warnings, short in the table; the full sentence is in the notes below it.
// info: true = neutral fact (nothing understated); estimated usage is amber but not an alert.
function accountWarnings(r) {
  const w = [];
  if (r.usage_unreported_msgs > 0) w.push({ chip: `${fmtCompact(r.usage_unreported_msgs)} msgs without usage`,
    note: `${fmtInt(r.usage_unreported_msgs)} messages had no usage reported by the gateway, so cost is understated.` });
  if (r.usage_estimated_msgs > 0) {
    const v = r.usage_estimate?.validation;
    w.push({ amber: true, chip: `${fmtCompact(r.usage_estimated_msgs)} msgs estimated (proxy didn't report usage) · ≈${fmtMoney(r.estimated_cost_usd)}`,
      note: `${fmtInt(r.usage_estimated_msgs)} messages came back from the proxy without usage. Their usage is estimated from neighbouring messages and their own length (≈${fmtMoney(r.estimated_cost_usd)}), kept out of the API-equivalent total${
        v?.n ? ` (checked on ${fmtInt(v.n)} messages with real usage: median cost error ${v.median_abs_pct_err}%, total bias ${v.total_bias_pct > 0 ? "+" : ""}${v.total_bias_pct}%)` : ""}.${
        r.gateway_hint ? ` ${r.gateway_hint}.` : ""}` });
  }
  if (r.failed_msgs > 0) w.push({ info: true, chip: `${fmtCompact(r.failed_msgs)} failed/aborted requests · no usage billed`,
    note: `${fmtInt(r.failed_msgs)} requests were aborted or failed before any usage was reported; they are not counted as messages.` });
  if (r.internal_msgs > 0) w.push({ info: true, chip: `${fmtCompact(r.internal_msgs)} internal-model msgs (not priced)`,
    note: `${fmtInt(r.internal_msgs)} messages used ${(r.internal_models || []).join(", ") || "an internal model"}, which has no public API price and is covered by the plan. Map it under "model_aliases" in config.json to price it as an API model.` });
  if (r.unpriced_msgs > 0) w.push({ chip: `${fmtCompact(r.unpriced_msgs)} unpriced`,
    note: `${fmtInt(r.unpriced_msgs)} messages used a model with no known rate and count as $0, so cost is understated.` });
  if (r.cache_tokens_reported === false) w.push({ chip: "no cache tokens",
    note: "The client reports no cache tokens, so cached prompt tokens are priced as fresh input; cost is likely overstated." });
  return w;
}
const ACCOUNT_COLS = [
  { key: "account", label: "Account", render: acctCell },
  { key: "msgs", label: "Msgs", num: true, render: r => fmtInt(r.msgs) },
  { key: "tokens", label: "Tokens", num: true, render: r => fmtCompact(r.tokens) },
  { key: "api_cost", label: "API-equivalent", num: true, title: "What the usage costs at each model's list price",
    render: r => costCell(r.api_cost, r) },
  FAST_COL,
  { key: "actual_cost_usd", label: "You paid", num: true, sort: r => youPaid(r),
    title: "What you paid out of pocket. A plan your employer pays shows its value underneath.", render: r => {
      if (r.payer === "employer") {
        const v = r.plan_value_usd ?? r.actual_cost_usd;
        return fmtMoney(r.you_paid_usd ?? 0) + `<div class="muted" style="font-size:10.5px;white-space:nowrap">plan value ${fmtMoney(v)}</div>`;
      }
      return youPaid(r) != null
        ? fmtMoney(youPaid(r)) + (r.payment === "per_token" && r.markup_pct ? `<div class="muted" style="font-size:10.5px">list +${r.markup_pct}%</div>` : "")
        : `<span class="unknown-paid">Unknown</span><span class="sub">add a receipt</span>`;
    } },
  { key: "savings", label: "Saved", num: true, sort: r => r.payer === "employer" ? null : youPaid(r) != null ? r.api_cost - youPaid(r) : null, render: r => {
      if (youPaid(r) == null || r.payment !== "subscription") return `<span class="quiet">—</span>`;
      if (r.payer === "employer") {
        // Not your saving (you paid nothing); shown against the plan value, like the KPI tooltip.
        const v = r.plan_value_usd ?? r.actual_cost_usd, sav = r.api_cost - v;
        return `<span class="muted" title="Your employer pays this plan, so it is left out of “Saved by subscribing”">${(sav >= 0 ? "+" : "-") + fmtMoney(Math.abs(sav))}${r.multiplier ? ` (${r.multiplier}×)` : ""}<div style="font-size:10.5px">vs plan value</div></span>`;
      }
      const sav = r.api_cost - youPaid(r);
      return (sav >= 0 ? "+" : "-") + fmtMoney(Math.abs(sav)) + (r.multiplier ? ` (${r.multiplier}×)` : "");
    } },
  { key: "warnings", label: "Data", sortable: false, render: r => {
      const w = accountWarnings(r);
      return w.length ? `<div class="chips">${w.map(x => x.info
        ? `<span class="warn-chip" style="color:var(--muted);background:var(--panel-2);border-color:var(--border)" title="${escapeHtml(x.note)}">${escapeHtml(x.chip)}</span>`
        : `<span class="warn-chip" title="${escapeHtml(x.note)}">${x.amber ? "" : icon("alert")}${escapeHtml(x.chip)}</span>`).join("")}</div>`
                      : `<span class="quiet">—</span>`;
    } },
  { key: "last_ts", label: "Active", num: true, render: r => tsRange(r.first_ts, r.last_ts) },
];
async function loadAccountsTable() {
  const [j, info, rc] = await Promise.all([
    api("/api/by_account", { window: STATE.window }),
    api("/api/accounts").catch(() => ({ accounts: [] })),
    fetch("/api/receipts").then(r => r.ok ? r.json() : null).catch(() => null),
  ]);
  const byName = new Map((info.accounts || []).map(a => [a.name, a]));
  // Monthly price of each account's latest plan, for "plan $125/mo" on employer-paid rows.
  const monthly = new Map();
  for (const c of (rc?.receipts || []).slice().sort((a, b) => String(a.start).localeCompare(String(b.start)))) {
    const days = c.recurring || !c.end ? 30 : (toDate(c.end) - toDate(c.start)) / 864e5;
    if (days >= 27 && days <= 32) monthly.set(c.account, c.amount_usd);
  }
  // Window figures win over the all-time ones from /api/accounts (api_cost, msgs, span).
  const rows = (j.rows || []).map(r => {
    const a = byName.get(r.account) || {};
    return { ...a, ...r, api_cost_all_time: a.api_cost, plan_monthly_usd: monthly.get(r.account) };
  });
  renderTable("accounts-table", ACCOUNT_COLS, rows, {
    empty: "No activity in this window.",
    rowClass: r => STATE.account && STATE.account !== r.account ? "dim" : "",
    // The account pill filters every card, like the header switcher (only with 2+ accounts).
    onRow: (tr, r) => {
      if (rows.length < 2) return;
      const pill = tr.querySelector(".acct-pill");
      if (!pill) return;
      pill.classList.add("clickable"); pill.tabIndex = 0; pill.setAttribute("role", "button");
      pill.title = STATE.account === r.account ? "Clear the account filter" : "Filter every card to this account";
      const go = () => setFilters({ account: toggle(STATE.account, r.account) });
      pill.addEventListener("click", go);
      pill.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    },
  });
  const notes = [];
  const receiptsBtn = `<button type="button" class="link-btn" data-open-receipts>Receipts</button>`;
  const gmailBtn = `<button type="button" class="link-btn" data-open-gmail>Connect Gmail</button>`;
  // Accounts on one shared plan get one note between them, not one each.
  const planDone = new Set();
  for (const r of rows) {
    const who = `<b>${escapeHtml(acctName(r.account))}</b>`;
    if (r.actual_cost_usd == null && r.paid_by) {
      if (planDone.has(r.paid_by)) continue;
      planDone.add(r.paid_by);
      const members = rows.filter(x => x.paid_by === r.paid_by && x.actual_cost_usd == null).map(x => acctName(x.account));
      const plan = PLAN_NAMES[r.paid_by] || providerName(r.paid_by);
      const store = r.paid_by === "openai_chatgpt" ? "Google Play/OpenAI" : plan;
      notes.push(`<li class="warn">${icon("alert")}<span><b>${escapeHtml(members.join(", "))}</b>: No ${escapeHtml(plan)} plan receipts yet. ${gmailBtn} to import ${escapeHtml(store)} receipts, or add one under ${receiptsBtn}.</span></li>`);
    } else if (r.actual_cost_usd == null) {
      notes.push(`<li class="warn">${icon("alert")}<span>${who}: actually paid is unknown. Add a receipt under ${receiptsBtn}, or, for a pay-per-token router, set <code>"markup_pct"</code> for it under <code>accounts</code> in <code>config.json</code>.</span></li>`);
    }
  }
  for (const r of rows) {
    for (const w of accountWarnings(r)) notes.push(`<li${w.info || w.amber ? "" : ' class="warn"'}>${icon("alert")}<span><b>${escapeHtml(acctName(r.account))}</b>: ${escapeHtml(w.note)}</span></li>`);
  }
  const ul = document.getElementById("accounts-notes");
  ul.innerHTML = notes.join("");
  ul.hidden = !notes.length;
}

async function loadFooter() {
  const m = await api("/api/meta");
  const last = m.last_index_at ? new Date(m.last_index_at).toLocaleString() : "never";
  const priced = m.pricing?.fetched_at ? ` · rates fetched ${new Date(m.pricing.fetched_at).toLocaleDateString()}` : "";
  const footMeta = document.getElementById("footer-meta");
  footMeta.textContent = `${fmtInt(m.rows)} rows in DB · indexed ${last}${priced}`;
  if (m.pricing?.source) footMeta.title = `Pricing sources: ${m.pricing.source}`;
  applyMeta(m);
  const s = await api("/api/summary", { window: "all" });
  if (s.billing_coverage?.last) {
    document.getElementById("footer-billing").textContent = `billing covers ${fmtRange(s.billing_coverage.first, s.billing_coverage.last)}`;
  }
  await refreshGmailStatus();
}

// ---------- extras (Activity & tools) ----------
async function loadExtras() {
  const status = await api("/api/extras/status").catch(() => ({enabled:false}));
  const rootEl = document.getElementById("extras-root");
  if (!rootEl) return;
  STATE.extrasReady = !!status.enabled;
  const tabBtn = document.getElementById("tab-activity-btn");
  if (!STATE.extrasReady) { rootEl.hidden = true; if (tabBtn) tabBtn.hidden = true; return; }
  rootEl.hidden = false;
  if (tabBtn) tabBtn.hidden = false;

  const ov = await api("/api/extras/overview", { window: STATE.window });
  setKpi("ex-kpi-tools",  fmtInt(ov.tool_calls || 0), ov.tool_calls || 0);
  document.getElementById("ex-kpi-tools-sub").textContent =
    `${fmtInt(ov.distinct_tools||0)} distinct · ${fmtInt(ov.errors||0)} errors`;
  setKpi("ex-kpi-skills", fmtInt(ov.distinct_skills || 0), ov.distinct_skills || 0);
  setKpi("ex-kpi-agents", fmtInt(ov.distinct_agents || 0), ov.distinct_agents || 0);
  setKpi("ex-kpi-mcp",    fmtInt(ov.distinct_mcp || 0),    ov.distinct_mcp || 0);
  setKpi("ex-kpi-slash",  fmtInt(ov.slash_prompts || 0),   ov.slash_prompts || 0);
  document.getElementById("ex-kpi-slash-sub").textContent = `${fmtInt(ov.distinct_slash_commands||0)} distinct`;

  await Promise.allSettled([
    runCard("skills", loadExtrasSkills),
    runCard("tools", loadExtrasTools),
    runCard("mcp", loadExtrasMcp),
    runCard("agents", loadExtrasAgents),
    runCard("bash", loadExtrasBash),
    runCard("slash", loadExtrasSlash),
    runCard("errors", loadExtrasErrors),
    runCard("files", loadExtrasFiles),
    runCard("calls", () => loadExtrasCalls()),
  ]);
}

// Horizontal category axes: every row gets a label (no autoSkip), long names are
// truncated with the full name in the tooltip, and the chart is as tall as its rows.
const hLabelMax = () => (window.innerWidth < 640 ? 18 : 30);
function hAxisY() {
  return { grid: { display: false }, ticks: { color: T.text2, autoSkip: false,
    callback(v) { return ellip(String(this.getLabelForValue(v)), hLabelMax()); } } };
}
function fitHBar(id, n, min = 140) {
  const wrap = document.getElementById(id)?.parentElement;
  if (wrap) wrap.style.height = `${Math.max(min, n * 26 + 64)}px`;
}

// One-series horizontal bar, shared by the extras charts.
function hbar(id, labels, data, { label, xTitle, tooltip, precision } = {}) {
  fitHBar(id, labels.length);
  upsertChart(id, () => ({
    type: "bar",
    data: { labels, datasets: [{ label: label || "uses", data, backgroundColor: T.series[0], borderWidth: 0, borderRadius: 4, borderSkipped: "start", maxBarThickness: 22 }] },
    options: {
      indexAxis: "y", responsive: true, maintainAspectRatio: false,
      scales: { x: { grid: { color: T.grid }, border: { display: false }, ticks: precision === 0 ? { precision: 0 } : { callback: v => fmtCompact(v) }, title: axisTitle(xTitle || "Uses") },
                y: hAxisY() },
      plugins: { legend: { display: false }, tooltip: { callbacks: { title: items => labels[items[0].dataIndex], ...(tooltip ? { label: tooltip } : {}) } } },
    },
  }), { label: `${label || "Uses"}: ${labels.length} items`, summary: labels.slice(0, 5).map((l, i) => `${l} ${fmtInt(data[i])}`).join(", ") || "No data in this window." });
}

async function loadExtrasSkills() {
  const j = await api("/api/extras/skills", { window: STATE.window });
  const rows = (j.rows || []).slice();
  const view = STATE.skillsBy === "most" ? rows.sort((a,b) => b.uses - a.uses).slice(0, 25) : rows.slice(0, 25);
  hbar("chart-skills", view.map(r => r.skill), view.map(r => r.uses), { label: "Skill uses", precision: 0,
    tooltip: ctx => `${ctx.parsed.x} use${ctx.parsed.x === 1 ? "" : "s"}` });
}
async function loadExtrasTools() {
  const j = await api("/api/extras/tools", { window: STATE.window });
  populateCallsToolFilter(j.rows || []);
  const rows = (j.rows || []).slice(0, 20);
  hbar("chart-tools", rows.map(r => r.tool_name), rows.map(r => r.uses), { label: "Tool calls",
    tooltip: ctx => { const r = rows[ctx.dataIndex]; return r.errors ? `${fmtInt(ctx.parsed.x)} uses · ${fmtInt(r.errors)} err` : `${fmtInt(ctx.parsed.x)} uses`; } });
}
async function loadExtrasMcp() {
  const j = await api("/api/extras/mcp", { window: STATE.window });
  const rows = (j.rows || []).slice(0, 20);
  hbar("chart-mcp", rows.map(r => r.mcp_server), rows.map(r => r.uses), { label: "MCP calls", precision: 0 });
}
async function loadExtrasAgents() {
  const j = await api("/api/extras/agents", { window: STATE.window });
  // Top five + other, so colours never cycle into a collision.
  const all = (j.rows || []).slice().sort((a, b) => b.uses - a.uses);
  const top = all.slice(0, 5), rest = all.slice(5).reduce((s, r) => s + r.uses, 0);
  const labels = top.map(r => r.subagent_type).concat(rest ? ["Other"] : []);
  const data = top.map(r => r.uses).concat(rest ? [rest] : []);
  upsertChart("chart-agents", () => ({
    type: "doughnut",
    data: { labels: labels.length ? labels : ["none"], datasets: [{ data: data.length ? data : [1],
      backgroundColor: data.length ? labels.map((_, i) => i < 5 ? T.series[i] : T.other) : [T.panel2],
      borderColor: T.panel, borderWidth: 2 }] },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: "58%",
      plugins: {
        legend: { position: "right", labels: { boxWidth: 10, boxHeight: 10 } },
        tooltip: { enabled: data.length > 0, callbacks: { label: ctx => `${ctx.label}: ${fmtInt(ctx.parsed)}` } },
      },
    },
  }), { label: "Sub-agent types by use", summary: labels.map((l, i) => `${l} ${fmtInt(data[i])}`).join(", ") || "No sub-agents in this window." });
}
async function loadExtrasBash() {
  const j = await api("/api/extras/bash", { window: STATE.window, limit: 18 });
  const rows = j.rows || [];
  hbar("chart-bash", rows.map(r => r.verb), rows.map(r => r.uses), { label: "Bash verbs", precision: 0 });
}
async function loadExtrasSlash() {
  const j = await api("/api/extras/slash", { window: STATE.window });
  renderTable("slash-table", [
    { key: "command", label: "Command", render: r => "/" + escapeHtml(r.command) },
    { key: "uses", label: "Uses", num: true, render: r => fmtInt(r.uses) },
    { key: "last_ts", label: "Last used", num: true, render: r => fmtDate(r.last_ts) },
  ], (j.rows || []).slice(0, 25), { empty: "No slash commands in this window." });
}
async function loadExtrasErrors() {
  const j = await api("/api/extras/errors", { window: STATE.window });
  renderTable("errors-table", [
    { key: "tool_name", label: "Tool" },
    { key: "errors", label: "Errors", num: true, render: r => fmtInt(r.errors) },
    { key: "total", label: "Total", num: true, render: r => fmtInt(r.total) },
    { key: "error_pct", label: "Error %", num: true, render: r => fmtPct(r.error_pct ?? 0) },
  ], j.rows || [], { empty: "No tool errors in this window." });
}
async function loadExtrasFiles() {
  const j = await api("/api/extras/files", { window: STATE.window, limit: 30 });
  renderTable("files-table", [
    { key: "path", label: "Path", render: r => { const s = r.path && r.path.length > 80 ? "…" + r.path.slice(-79) : r.path; return `<span title="${escapeHtml(r.path)}">${escapeHtml(s)}</span>`; } },
    { key: "edits", label: "Touches", num: true, render: r => fmtInt(r.edits) },
    { key: "edit_calls", label: "Edits", num: true, render: r => fmtInt(r.edit_calls) },
    { key: "write_calls", label: "Writes", num: true, render: r => fmtInt(r.write_calls) },
    { key: "read_calls", label: "Reads", num: true, render: r => fmtInt(r.read_calls) },
  ], j.rows || [], { empty: "No file activity in this window." });
}

// ---------- tool call log ----------
const CALLS_PAGE = 50;
function populateCallsToolFilter(rows) {
  const sel = document.getElementById("calls-tool");
  if (!sel) return;
  const current = STATE.callsTool;
  sel.innerHTML = `<option value="">all tools</option>` + rows.map(r =>
    `<option value="${escapeHtml(r.tool_name)}">${escapeHtml(r.tool_name)} (${fmtInt(r.uses)})</option>`).join("");
  sel.value = current;
  if (sel.value !== current) { sel.value = ""; STATE.callsTool = ""; }
}
function callDetail(r) {
  if (r.tool_name === "Bash" && r.command_verb) return r.command_verb;
  if (r.skill) return "/" + r.skill;
  if (r.subagent_type) return r.subagent_type;
  if (r.target_path) return r.target_path;
  if (r.mcp_server) return r.mcp_server;
  return "";
}
async function loadExtrasCalls(append = false) {
  const offset = append ? STATE.callsShown : 0;
  const j = await api("/api/extras/calls", {
    window: STATE.window, tool: STATE.callsTool || null, status: STATE.callsStatus || null, limit: CALLS_PAGE, offset,
  });
  const tbody = document.querySelector("#calls-table tbody");
  if (!append) tbody.innerHTML = "";
  for (const r of j.rows || []) {
    const tr = document.createElement("tr");
    const detail = callDetail(r);
    const short = detail.length > 70 ? "…" + detail.slice(-69) : detail;
    tr.innerHTML = `
      <td class="nowrap">${fmtDate(r.ts)}</td>
      <td>${escapeHtml(r.tool_name)}${r.is_error ? ' <span class="err-badge">err</span>' : ""}</td>
      <td title="${escapeHtml(detail)}">${escapeHtml(short) || "—"}</td>
      <td>${escapeHtml(r.project || "—")}</td>`;
    tbody.appendChild(tr);
  }
  STATE.callsShown = offset + (j.rows || []).length;
  if (!STATE.callsShown) tbody.innerHTML = `<tr class="empty-row"><td colspan="4">No tool calls in this window.</td></tr>`;
  document.getElementById("calls-count").textContent =
    j.total ? `showing ${fmtInt(STATE.callsShown)} of ${fmtInt(j.total)} calls` : "";
  document.getElementById("calls-more").hidden = STATE.callsShown >= j.total;
}

// ---------- Breakdown: where the spend went ----------
// Rows whose transcript was deleted before attribution was indexed come back as
// GONE_VALUE. That bucket is large, so it is shown on its own, never hidden.
const GONE_VALUE = "(transcript gone)";
const BD_DIMS = {
  // A message carries several token types at once, so this one splits cost but cannot filter.
  token_type: { col: "Token type",  hint: "token type", named: "Priced", noFilter: true },
  sidechain:  { col: "Thread",      hint: "main thread vs subagents", named: "Attributed" },
  agent:      { col: "Agent type",  hint: "agent type", named: "Attributed" },
  skill:      { col: "Skill",       hint: "skill in use", none: "No skill", named: "Used a skill" },
  mcp:        { col: "MCP server",  hint: "MCP server", none: "No MCP server", named: "Used an MCP server" },
  plugin:     { col: "Plugin",      hint: "plugin", none: "No plugin", named: "Used a plugin" },
  branch:     { col: "Branch",      hint: "git branch", none: "No branch", named: "On a branch" },
  pr:         { col: "PR",          hint: "linked pull request", named: "Linked to a PR" },
  entrypoint: { col: "Entry point", hint: "entry point (CLI, SDK…)", none: "Unknown entry point", named: "Known entry point" },
  effort:     { col: "Effort",      hint: "reasoning effort", named: "Effort set" },
};
const BD_SENTINEL = {
  "(main thread)": "Main thread", "(subagent, unattributed)": "Subagent, type unknown",
  "(unset)": "Effort not set", "(no PR)": "No linked PR",
};
// -> { text, sentinel, href? }
function bdValue(dim, v) {
  if (dim === "token_type") return { text: TOKEN_TYPE[v]?.label || v, sentinel: false, slot: TOKEN_TYPE[v]?.slot ?? null };
  if (v === GONE_VALUE) return { text: "Not attributable (transcript deleted)", sentinel: true, gone: true };
  if (dim === "sidechain") return { text: v === "main" ? "Main thread" : v === "subagent" ? "Subagents" : v, sentinel: false };
  if (v === "(none)") return { text: BD_DIMS[dim]?.none || "None", sentinel: true, none: true };
  if (v === "(unset)" || v === "(no PR)") return { text: BD_SENTINEL[v], sentinel: true, none: true };
  if (BD_SENTINEL[v]) return { text: BD_SENTINEL[v], sentinel: true };
  if (dim === "pr") {
    const m = /^([\w.-]+\/[\w.-]+)#(\d+)$/.exec(v || "");
    if (m) return { text: v, sentinel: false, href: `https://github.com/${m[1]}/pull/${m[2]}` };
  }
  return { text: v ?? "—", sentinel: false };
}
const ellip = (s, n) => s.length > n ? s.slice(0, n - 1) + "…" : s;

async function loadBreakdown() {
  const dim = STATE.bdDim;
  document.getElementById("bd-hint").textContent = `API-rate cost by ${BD_DIMS[dim].hint}`;
  const j = await api("/api/breakdown", { window: STATE.window, dim, limit: 50 });
  const total = j.total_cost || 0;
  const rows = j.rows || [];
  const gone = rows.find(r => r.value === GONE_VALUE);
  const known = rows.filter(r => r.value !== GONE_VALUE);
  const sel = STATE.dims[dim] ? dimRowValue(dim, STATE.dims[dim]) : "";   // this dimension's active filter
  const noFilter = !!BD_DIMS[dim].noFilter;
  document.getElementById("bd-hint").textContent = `API-rate cost by ${BD_DIMS[dim].hint}${noFilter ? "" : " · click a row to filter"}`;

  // One 100% bar: spend with a named value, spend with none ("No skill", "No linked PR"),
  // and spend whose transcript was deleted. The chart then shows only the named values,
  // so a large "none" bucket can't flatten them.
  const isNone = r => bdValue(dim, r.value).none;
  const named = known.filter(r => !isNone(r)), none = known.filter(isNone);
  const sum = rs => rs.reduce((t, r) => t + (r.cost || 0), 0);
  const segs = dim === "token_type" ? known.map(r => {
    const v = bdValue(dim, r.value);
    return { cls: "", sw: `style="background:${slotVar(v.slot)}"`, bg: slotVar(v.slot), label: v.text, cost: r.cost || 0 };
  }).filter(x => x.cost > 0) : [
    { cls: "known", sw: `style="background:var(--chart-1)"`, label: BD_DIMS[dim].named, cost: sum(named) },
    { cls: "none", sw: `style="background:var(--chart-other)"`, label: none.length ? bdValue(dim, none[0].value).text : "", cost: sum(none) },
    { cls: "gone", sw: `class="swatch gone"`, label: "Not attributable, transcript deleted", cost: gone?.cost || 0, msgs: gone?.msgs },
  ].filter(x => x.cost > 0);
  const attrib = document.getElementById("bd-attrib");
  if (total > 0 && segs.length > 1) {
    const segSum = segs.reduce((t, x) => t + x.cost, 0);
    attrib.innerHTML = `
      <div class="attrib-bar" role="img" aria-label="${escapeHtml(segs.map(x => `${x.label} ${fmtPct(x.cost / total * 100)}`).join(", "))}">
        ${segs.map(x => `<span class="${x.cls}" style="flex:${x.cost / segSum} 1 0${x.bg ? `;background:${x.bg}` : ""}"></span>`).join("")}
      </div>
      <div class="attrib-legend">${segs.map(x => `<span class="item"><span ${x.cls === "gone" ? x.sw : `class="swatch" ${x.sw}`}></span>${escapeHtml(x.label)} <b>${fmtMoneyShort(x.cost)}</b> <span class="muted">${fmtPct(x.cost / total * 100)}${x.msgs ? ` · ${fmtInt(x.msgs)} msgs` : ""}</span></span>`).join("")}</div>`;
  } else attrib.innerHTML = "";

  // Bar chart: named values, top 12.
  const top = named.slice().sort((a, b) => b.cost - a.cost).slice(0, 12);
  const wrap = document.getElementById("bd-chart-wrap");
  wrap.hidden = !top.length;
  wrap.style.height = `${Math.max(140, top.length * 30 + 56)}px`;
  const labels = top.map(r => bdValue(dim, r.value).text);
  const shortLabel = l => ellip(dim === "pr" ? l.replace(/^[^/]+\//, "") : l, 30);
  if (top.length) {
    upsertChart("chart-breakdown", () => ({
      type: "bar",
      data: { labels: labels.map(shortLabel), datasets: [{
        label: "Cost", data: top.map(r => r.cost),
        backgroundColor: top.map(r => { const v = bdValue(dim, r.value);
          return alpha(dim === "token_type" ? slotColor(v.slot) : v.sentinel ? T.other : T.series[0], sel && r.value !== sel ? .3 : 1); }),
        borderWidth: 0, borderRadius: 4, borderSkipped: "start", maxBarThickness: 20,
      }]},
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false,
        onClick: (_e, els) => { if (els.length && !noFilter) setDimFilter(dim, top[els[0].index].value); },
        onHover: (e, els) => { e.native.target.style.cursor = els.length && !noFilter ? "pointer" : "default"; },
        scales: {
          x: { grid: { color: T.grid }, border: { display: false }, ticks: { callback: v => "$" + fmtCompact(v) }, title: axisTitle("USD at API rates") },
          y: { grid: { display: false }, ticks: { color: T.text2, autoSkip: false } },
        },
        plugins: { legend: { display: false }, tooltip: { callbacks: {
          title: items => labels[items[0].dataIndex],
          label: ctx => { const r = top[ctx.dataIndex]; return `${fmtMoney(r.cost)} · ${fmtPct(total ? r.cost / total * 100 : 0)} of spend`; },
          afterLabel: ctx => { const r = top[ctx.dataIndex];
            if (dim === "token_type") return r.value === "web_search" ? `${fmtInt(r.web_search_requests)} searches` : `${fmtCompact(r.tokens)} tokens`;
            return `${fmtInt(r.msgs)} msgs · ${fmtInt(r.sessions)} sessions`; },
          footer: () => noFilter ? "" : "click to filter",
        } } },
      },
    }), {
      label: `API-rate cost by ${BD_DIMS[dim].hint}, top ${top.length}`,
      summary: top.slice(0, 5).map((r, i) => `${labels[i]} ${fmtMoney(r.cost)}`).join(", ") + ". The same data is in the table below.",
    });
  }

  const notes = [];
  if (dim === "token_type") notes.push("Each message's cost is split across its token types at that model's rates, so the rows add up to the total. "
    + "A message carries several types, so Msgs and Sessions overlap across rows.");
  else if (j.overlapping) notes.push(dim === "pr"
    ? "A session linked to several PRs counts toward each of them, so shares can add up to more than 100%."
    : "A message can count toward several values, so shares can add up to more than 100%.");
  if (j.truncated) notes.push(`Showing the top ${rows.length} values by cost; smaller ones are left out.`);
  if (!known.length && gone) notes.push("Nothing in this window can be attributed: every transcript in it has been deleted.");
  const ul = document.getElementById("bd-notes");
  ul.innerHTML = notes.map(n => `<li>${icon("alert")}<span>${escapeHtml(n)}</span></li>`).join("");
  ul.hidden = !notes.length;

  // Each row filters the whole page to its value (the chip bar shows it; click again to clear).
  const canFilter = r => !noFilter && !(dim === "pr" && r.value === GONE_VALUE);
  renderTable("breakdown-table", BD_COLS(dim, total), rows, {
    empty: "No spend in this window.",
    rowClass: r => [r.value === GONE_VALUE ? "row-gone" : "", canFilter(r) ? "clickable" : "",
                    sel ? (r.value === sel ? "sel" : "dim") : ""].filter(Boolean).join(" "),
    onRow: (tr, r) => {
      if (!canFilter(r)) return;
      const on = r.value === sel, label = bdValue(dim, r.value).text;
      tr.tabIndex = 0;
      tr.setAttribute("aria-pressed", String(on));
      tr.title = on ? "Click to clear this filter" : `Filter every card to ${label}`;
      const go = () => setDimFilter(dim, r.value);
      tr.addEventListener("click", e => { if (!e.target.closest("a")) go(); });
      tr.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    },
  });
}
const bdVar = (dim, v) => dim === "token_type" ? slotVar(v.slot) : v.sentinel ? "var(--chart-other)" : "var(--chart-1)";
const BD_COLS = (dim, total = 1) => [
  { key: "value", label: BD_DIMS[dim].col, sort: r => bdValue(dim, r.value).text.toLowerCase(), render: r => {
      const v = bdValue(dim, r.value);
      const sw = v.gone ? `<span class="swatch gone"></span>` : `<span class="swatch" style="background:${bdVar(dim, v)}"></span>`;
      const inner = v.href ? `<a href="${escapeHtml(v.href)}" target="_blank" rel="noopener">${escapeHtml(v.text)}</a>` : escapeHtml(v.text);
      return `<span class="value-cell${v.sentinel ? " sentinel" : ""}" title="${escapeHtml(r.value)}">${sw}<span class="txt${dim === "branch" && !v.sentinel ? " mono" : ""}">${inner}</span></span>`;
    } },
  { key: "cost", label: "Cost", num: true, render: r => costCell(r.cost, r) },
  { key: "share", label: "Share", num: true, sort: r => r.cost, render: r => {
      const pct = total ? (r.cost || 0) / total * 100 : 0;
      const v = bdValue(dim, r.value);
      const bar = `<span class="share-bar${v.gone ? " gone" : ""}" style="width:${Math.min(100, Math.max(pct, pct > 0 ? 1 : 0))}%;${v.gone ? "" : `background:${bdVar(dim, v)}`}"></span>`;
      return `<div class="share"><span class="share-track">${bar}</span><span class="share-pct">${pct >= 0.1 || pct === 0 ? fmtPct(pct) : "<0.1%"}</span></div>`;
    } },
  { key: "msgs", label: "Msgs", num: true, render: r => fmtInt(r.msgs) },
  { key: "sessions", label: "Sessions", num: true, render: r => fmtInt(r.sessions) },
  { key: "tokens", label: "Tokens", num: true, render: r => dim === "token_type" && r.value === "web_search"
      ? `${fmtInt(r.web_search_requests)} <span class="muted">searches</span>` : fmtCompact(r.tokens) },
  { key: "thinking_tokens", label: "Thinking", num: true, title: "Thinking tokens, billed as output (none are recorded for deleted transcripts)",
    render: r => r.thinking_tokens ? fmtCompact(r.thinking_tokens) : `<span class="quiet">—</span>` },
  FAST_COL,
];
function setBreakdownDim(dim) {
  if (!BD_DIMS[dim]) return;
  STATE.bdDim = dim;
  document.querySelectorAll("#bd-seg button").forEach(x => { x.classList.toggle("on", x.dataset.dim === dim); x.setAttribute("aria-pressed", String(x.dataset.dim === dim)); });
  document.getElementById("bd-select").value = dim;
  // Subtitle follows the button at once; runCard puts the card in its held (dimmed) state
  // synchronously, so the old chart never reads as current for the new dimension.
  document.getElementById("bd-hint").textContent = `API-rate cost by ${BD_DIMS[dim].hint}`;
  runCard("breakdown", loadBreakdown).catch(() => {});
}

// ---------- API errors and retries ----------
const REL_KINDS = [["rate_limit", "Rate limit"], ["server_error", "Server error"], ["retry", "Retry"], ["quota", "Quota"], ["other_error", "Other"]];
const relColor = i => i < 4 ? T.series[i] : T.other;
const relVar = i => i < 4 ? `var(--chart-${i + 1})` : "var(--chart-other)";
async function loadReliability() {
  const j = await api("/api/reliability", { window: STATE.window });
  const rows = j.rows || [];
  const byDay = new Map(rows.map(r => [r.day, r]));
  // A continuous day axis, so quiet days read as zero instead of vanishing.
  const days = [];
  if (rows.length) {
    const span = windowSpan(rows[0].day);
    const lastRow = toDate(rows[rows.length - 1].day);
    const end = lastRow > span.to ? lastRow : span.to;
    for (let d = new Date(span.from); d <= end; d.setDate(d.getDate() + 1)) days.push(isoLocal(d));
  }
  const totals = j.totals || {};
  const all = REL_KINDS.reduce((s, [k]) => s + (totals[k] || 0), 0);
  document.getElementById("rel-hint").textContent = all
    ? `${fmtInt(all)} events in this window, from the transcripts · days in ${j.timezone || "local time"}`
    : "events per day, from the transcripts";
  document.getElementById("rel-legend").innerHTML = all ? REL_KINDS.map(([k, label], i) =>
    `<span class="item"><span class="swatch" style="background:${relVar(i)}"></span>${label} <b>${fmtInt(totals[k] || 0)}</b></span>`).join("") : "";
  const many = days.length > 60;
  upsertChart("chart-reliability", () => ({
    type: "bar",
    data: { labels: days, datasets: REL_KINDS.map(([k, label], i) => ({
      label, data: days.map(d => byDay.get(d)?.[k] || 0), backgroundColor: relColor(i),
      stack: "s", borderWidth: many ? 0 : { top: 1.5 }, borderColor: T.panel, borderSkipped: false, maxBarThickness: 18,
    })) },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: dayTicks(days) },
        y: { stacked: true, grid: { color: T.grid }, border: { display: false }, ticks: { precision: 0 }, title: axisTitle("Events per day") },
      },
      plugins: {
        legend: { display: false },
        tooltip: { filter: ctx => ctx.parsed.y > 0, callbacks: { title: items => fmtDayLong(items[0].label), label: ctx => `${ctx.dataset.label}: ${fmtInt(ctx.parsed.y)}` } },
      },
    },
  }), {
    label: "API errors and retries per day, by kind",
    summary: all ? REL_KINDS.map(([k, l]) => `${l} ${fmtInt(totals[k] || 0)}`).join(", ") + "." : "No API errors or retries recorded in this window.",
  });
  const kindIdx = Object.fromEntries(REL_KINDS.map(([k], i) => [k, i]));
  renderTable("reliability-table", [
    { key: "kind", label: "Kind", sort: r => kindIdx[r.kind] ?? 9, render: r => {
        const i = kindIdx[r.kind] ?? 4;
        return `<span class="model-cell"><span class="swatch" style="background:${relVar(i)}"></span>${escapeHtml(REL_KINDS[i][1])}</span>`;
      } },
    { key: "detail", label: "Detail", render: r => `<span class="mono">${escapeHtml(r.detail || "—")}</span>` },
    { key: "n", label: "Count", num: true, render: r => fmtInt(r.n) },
    { key: "share", label: "Share", num: true, sort: r => r.n, render: r => fmtPct(all ? r.n / all * 100 : 0) },
  ], j.details || [], { empty: "No API errors or retries in this window." });
}

// ---------- Claude Code's own tally vs ours ----------
async function loadReconciliation() {
  const j = await api("/api/reconciliation", { window: STATE.window, limit: 50 });
  const t = j.totals || {};
  const ours = rk(t, "ours") || 0, theirs = rk(t, "theirs") || 0;
  const gap = rk(t, "gap") ?? (theirs - ours);
  const compared = rk(t, "comparedSessions") || 0, totalS = rk(t, "totalSessions") || 0;
  const hasSplit = rk(t, "gapUnexplained") != null;
  const signed = v => `${v >= 0 ? "+" : "−"}${fmtMoney(Math.abs(v))}`;
  const stat = (cls, lbl, val, sub) => `<div class="recon-stat ${cls}"><span class="lbl">${lbl}</span><span class="val">${val}</span><span class="sub">${sub}</span></div>`;
  document.getElementById("recon-stats").innerHTML = [
    stat("", "Comparable sessions", `${fmtInt(compared)} <span class="muted" style="font-size:14px;font-family:var(--font-ui)">of ${fmtInt(totalS)}</span>`,
      `${fmtPct(rk(t, "sessionCoverage") ?? 0)} of sessions · ${fmtPct(rk(t, "costCoverage") ?? 0)} of our spend`),
    stat("", "Ours", fmtMoney(ours), "from the transcripts, same sessions"),
    stat("", "Claude Code's tally", fmtMoney(theirs), "its own cost record"),
    stat("gap", "Gap vs Claude Code's own tally", signed(gap),
      ours ? `${fmtPct(Math.abs(gap) / ours * 100)} ${gap >= 0 ? "above" : "below"} ours` : "no comparable spend"),
  ].join("");
  // Where the gap comes from, when the server splits it. Parts can be negative, so a list, not a stacked bar.
  const split = document.getElementById("recon-split");
  if (hasSplit && compared) {
    const ws = rk(t, "gapWebSearch") || 0;
    const parts = [
      ["Calls the transcript never logs", rk(t, "gapUnlogged") || 0,
        ws > 0 ? `incl. ${fmtMoney(ws)} web search fees (${fmtInt(rk(t, "webSearches") || 0)} searches)` : "Haiku-model usage and web search fees"],
      ["Subagent output logged before its final usage", rk(t, "gapPartial") || 0, "output tokens priced at list rate"],
      ["Not explained yet", rk(t, "gapUnexplained") || 0, "mostly input and cache tokens on the main models"],
    ];
    split.innerHTML = parts.map(([l, v, sub]) =>
      `<li><span class="lbl">${escapeHtml(l)}</span><b>${signed(v)}</b><span class="muted">${escapeHtml(sub)}</span></li>`).join("");
    split.hidden = false;
  } else { split.innerHTML = ""; split.hidden = true; }
  document.getElementById("recon-note").innerHTML = compared
    ? `Only sessions with a Claude Code cost record can be compared: ${fmtInt(compared)} of ${fmtInt(totalS)} here (${fmtPct(rk(t, "sessionCoverage") ?? 0, 0)}). ` +
      (hasSplit
        ? `Part of the gap has a known cause, listed above; the rest is not explained yet. `
        : `Part of the gap is calls the transcript never logs, such as web search fees; the rest is not explained yet. `) +
      `None of it is added to any other figure on this page.`
    : `No session in this window has a Claude Code cost record, so there is nothing to compare. Claude Code writes one only for some sessions (about a fifth of them overall).`;
  renderTable("reconciliation-table", [
    { key: "project", label: "Session", sort: r => (r.project || "").toLowerCase(), render: r =>
        `<div class="sess-cell"><span class="title">${escapeHtml(r.project || "—")}</span><span class="meta"><span class="mono" title="${escapeHtml(r.session_id)}">${escapeHtml((r.session_id || "").slice(0, 8))}</span> · ${escapeHtml(tsRange(r.first_ts, r.last_ts))}</span></div>` },
    { key: "ours", label: "Ours", num: true, sort: r => rk(r, "rowOurs"), render: r => fmtMoney(rk(r, "rowOurs")) },
    { key: "theirs", label: "Claude Code's", num: true, sort: r => rk(r, "rowTheirs"), render: r => fmtMoney(rk(r, "rowTheirs")) },
    { key: "delta", label: "Delta", num: true, sort: r => rk(r, "rowDelta"), title: "Claude Code's figure minus ours", render: r => {
        const d = rk(r, "rowDelta") ?? 0, o = rk(r, "rowOurs") || 0;
        const tip = rk(r, "rowUnexplained") != null
          ? `Never logged ${fmtMoney(rk(r, "rowUnlogged") || 0)} · partial subagent output ${fmtMoney(rk(r, "rowPartial") || 0)} · not explained ${fmtMoney(rk(r, "rowUnexplained") || 0)}` : "";
        return `<span class="${d >= 0 ? "delta-pos" : "delta-neg"}"${tip ? ` tabindex="0" data-tip="${escapeHtml(tip)}"` : ""}>${d >= 0 ? "+" : "−"}${fmtMoney(Math.abs(d))}</span>` +
          (o ? `<div class="muted" style="font-size:10.5px">${d >= 0 ? "+" : "−"}${fmtPct(Math.abs(d) / o * 100, 0)}</div>` : "");
      } },
    { key: "ws", label: "Web searches", num: true, sort: r => rk(r, "rowWebSearches"), render: r => rk(r, "rowWebSearches") ? fmtInt(rk(r, "rowWebSearches")) : `<span class="quiet">—</span>` },
  ], j.rows || [], { empty: "No comparable sessions in this window." });
}

// ---------- orchestration ----------
const SECTIONS = [
  ["summary", loadSummary],
  ["accounts", loadAccountsTable],
  ["timeline", loadTimeline],
  ["heatmap", loadHeatmap],
  ["models", loadModels],
  ["projects", loadProjects],
  ["sessions", loadTopSessions],
  ["topdays", loadTopDays],
  ["breakdown", loadBreakdown],
  ["reliability", loadReliability],
  ["reconciliation", loadReconciliation],
];
async function reloadAll() {
  // Each section loads, fails and retries on its own. The banner is only for
  // a total failure, which usually means the server is down or stale.
  const results = await Promise.allSettled(SECTIONS.map(([key, fn]) => runCard(key, fn)));
  loadFooter().catch(err => console.error("[footer]", err));
  loadExtras().catch(err => console.error("[extras]", err));
  const banner = document.getElementById("error-banner");
  if (!banner) return;
  const allFailed = results.every(r => r.status === "rejected");
  banner.hidden = !allFailed;
  if (allFailed) {
    banner.textContent = "Couldn't reach the dashboard server. If it was just updated, restart it (./start.sh) and reload this page.";
  }
}

// First paint: skeleton rows in every table until data arrives.
function paintSkeletons() {
  renderTable("accounts-table", ACCOUNT_COLS, null, { skeletonRows: 2 });
  renderTable("models-table", MODEL_COLS(), null, { showFast: true });
  renderTable("projects-table", PROJECT_COLS, null);
  renderTable("sessions-table", SESSION_COLS(), null);
  renderTable("topdays-table", DAY_COLS, null);
  renderTable("breakdown-table", BD_COLS(STATE.bdDim), null);
  renderTable("reliability-table", [{ key: "kind", label: "Kind" }, { key: "detail", label: "Detail" }, { key: "n", label: "Count", num: true }], null, { skeletonRows: 3 });
  renderTable("reconciliation-table", [{ key: "s", label: "Session" }, { key: "o", label: "Ours", num: true }, { key: "c", label: "Claude Code's", num: true }, { key: "d", label: "Delta", num: true }], null);
}

// ---------- auto-refresh (the server watches the logs; we watch the index) ----------
const META_POLL_MS = 60000;
let lastIndexAt = null;
const fmtAgo = iso => {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
};
function applyMeta(m) {
  lastIndexAt = m.last_index_at || lastIndexAt;
  const el = document.getElementById("watch-status");
  if (!el) return;
  const w = m.watcher;
  if (!w || !w.enabled) { el.hidden = true; return; }
  const mins = Math.max(1, Math.round((w.interval_s || 0) / 60));
  el.hidden = false;
  el.textContent = `auto-refresh on · checked ${fmtAgo(w.last_check_at)}`;
  el.title = `checking every ~${mins} min${w.on_battery ? " (on battery)" : ""}`;
}
async function pollMeta() {
  if (document.visibilityState !== "visible") return;
  let m;
  try { m = await api("/api/meta"); } catch { return; }
  const moved = m.last_index_at && lastIndexAt && m.last_index_at !== lastIndexAt;
  applyMeta(m);
  if (moved) { await loadAccounts(); await reloadAll(); }
}
setInterval(pollMeta, META_POLL_MS);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") pollMeta(); });

// ---------- wiring ----------
function bindSeg(id, attr, onPick) {
  const seg = document.getElementById(id);
  if (!seg) return;
  seg.addEventListener("click", e => {
    const b = e.target.closest(`button[${attr}]`);
    if (!b) return;
    seg.querySelectorAll("button").forEach(x => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", String(x === b)); });
    onPick(b.getAttribute(attr));
  });
}
// Picking a window also leaves a drilled-in day.
function setWindow(w) {
  if (w === "custom" || w === "custom-edit") { openCustom(); return; }
  setFilters({ window: WINDOW_IDS.includes(w) ? w : "all", day: "", from: "", to: "" });
}
function bindControls() {
  document.getElementById("windows").addEventListener("click", e => {
    const b = e.target.closest("button[data-w]");
    if (b) setWindow(b.dataset.w);
  });
  document.getElementById("window-select").addEventListener("change", e => setWindow(e.target.value));
  bindSeg("metric-seg", "data-metric", v => { STATE.metric = v; runCard("timeline", loadTimeline).catch(() => {}); });
  bindSeg("heat-view", "data-view", v => { STATE.heatView = v; renderHeatmap(); });
  bindSeg("heat-metric", "data-metric", v => { STATE.heatMetric = v; renderHeatmap(); });
  bindSeg("proj-seg", "data-by", v => { STATE.projBy = v; runCard("projects", loadProjects).catch(() => {}); });
  bindSeg("proj-view", "data-view", v => {
    STATE.projView = v;
    document.getElementById("proj-chart-wrap").hidden = v !== "chart";
    document.getElementById("proj-table-wrap").hidden = v !== "table";
    document.getElementById("proj-seg").hidden = v !== "chart";
    if (v === "chart") requestAnimationFrame(() => charts["chart-projects"]?.resize());
  });
  bindSeg("sess-seg", "data-by", v => { STATE.sessBy = v; runCard("sessions", loadTopSessions).catch(() => {}); });
  bindSeg("bd-seg", "data-dim", setBreakdownDim);
  document.getElementById("bd-select")?.addEventListener("change", e => setBreakdownDim(e.target.value));
  bindSeg("skills-seg", "data-by", v => { STATE.skillsBy = v; runCard("skills", loadExtrasSkills).catch(() => {}); });
  bindSeg("calls-seg", "data-status", v => { STATE.callsStatus = v; loadExtrasCalls(); });
  document.getElementById("calls-tool")?.addEventListener("change", e => { STATE.callsTool = e.target.value; loadExtrasCalls(); });
  document.getElementById("calls-more")?.addEventListener("click", () => loadExtrasCalls(true));

  document.getElementById("refresh").addEventListener("click", async e => {
    const btn = e.currentTarget;
    const label = btn.querySelector(".btn-label");
    btn.disabled = true; btn.classList.add("is-busy"); label.textContent = "Indexing…";
    try { await fetch("/refresh", { method: "POST" }); await loadAccounts(); await reloadAll(); }
    finally { btn.disabled = false; btn.classList.remove("is-busy"); label.textContent = "Refresh"; }
  });

  const tt = document.getElementById("theme-toggle");
  tt.addEventListener("click", e => {
    const b = e.target.closest("button[data-theme-mode]");
    if (b) setThemeMode(b.dataset.themeMode);
  });
  tt.querySelectorAll("button").forEach(b => b.setAttribute("aria-checked", String(b.dataset.themeMode === themeMode())));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (themeMode() === "system") retheme(); });
}

// ---------- Gmail: connection state + dialog ----------
// /api/gmail/status says whether a token file exists. An expired token is reported when the
// server says so (expired / token_valid=false), or remembered from a scrape that failed to
// authenticate, so the button reads "Gmail expired · Reconnect" instead of "connected".
const GMAIL_EXPIRED_KEY = "cud-gmail-expired";
const GMAIL_AUTH_ERR = /expired|revoked|invalid_grant|could not be refreshed|reconnect|re-?authori[sz]e/i;
let gmailStatus = null;
const GMAIL_EXPIRY_WHY = "Google expires test-mode sign-ins after 7 days; publishing the OAuth app to Production stops this.";
function gmailExpiredHint(v) {
  try {
    if (v === undefined) return sessionStorage.getItem(GMAIL_EXPIRED_KEY) === "1";
    if (v) sessionStorage.setItem(GMAIL_EXPIRED_KEY, "1"); else sessionStorage.removeItem(GMAIL_EXPIRED_KEY);
  } catch {}
  return !!v;
}
// -> "connected" | "expired" | "error" (couldn't check) | "disconnected" (credentials, no token) | "setup" (no credentials.json)
function gmailState(g = gmailStatus) {
  if (!g) return "disconnected";
  // refresh_ok=false: a token exists but Google refused to refresh it (invalid_grant), so the
  // server reports connected=false. That is an expired sign-in, not a never-connected inbox.
  // A network failure while checking is "error" (retry later), not an expired sign-in.
  if (g.refresh_ok === false) return !g.error || GMAIL_AUTH_ERR.test(g.error) || /refresh token|not valid/i.test(g.error) ? "expired" : "error";
  if (g.connected) {
    const expired = g.expired === true || g.token_expired === true || g.valid === false || g.token_valid === false
      || g.state === "expired" || g.status === "expired" || gmailExpiredHint();
    return expired ? "expired" : "connected";
  }
  return g.credentials_present ? "disconnected" : "setup";
}
const GMAIL_BTN = {
  connected:    { ic: "check", label: "Gmail connected", aria: "Gmail connected. Sync receipts", title: "Gmail connected: import new receipts" },
  expired:      { ic: "alert", label: "Gmail expired · Reconnect", aria: "Gmail sign-in expired. Reconnect", title: `Gmail sign-in expired — reconnect. ${GMAIL_EXPIRY_WHY}` },
  error:        { ic: "alert", label: "Gmail check failed", aria: "Gmail sign-in could not be checked", title: "Gmail sign-in could not be checked right now" },
  disconnected: { ic: "mail", label: "Connect Gmail", aria: "Connect Gmail", title: "Connect Gmail to import Anthropic, OpenAI and Google Play receipts" },
  setup:        { ic: "mail", label: "Connect Gmail", aria: "Connect Gmail", title: "Connect Gmail to import Anthropic, OpenAI and Google Play receipts" },
};
function renderGmailButton() {
  const btn = document.getElementById("gmail-btn");
  if (!btn) return;
  const st = gmailState(), b = GMAIL_BTN[st];
  btn.classList.toggle("connected", st === "connected");
  btn.classList.toggle("expired", st === "expired" || st === "error");
  btn.innerHTML = `${icon(b.ic)}<span class="btn-label">${escapeHtml(b.label)}</span>`;
  btn.setAttribute("aria-label", b.aria);
  btn.title = st === "error" && gmailStatus?.error ? `${b.title}: ${gmailStatus.error}` : b.title;
  renderReceiptsGmail();
}
async function refreshGmailStatus() {
  try { gmailStatus = await api("/api/gmail/status"); } catch { return; }
  renderGmailButton();
}

const SETUP_HTML = `
<p>This imports your receipts (Anthropic, OpenAI and Google Play) straight from Gmail into the Receipts list. Receipts you add by hand are kept.</p>
<p><b>One-time setup</b> (~5 minutes):</p>
<ol>
  <li>Go to the <a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noopener">Google Cloud Console</a> and create a new project.</li>
  <li>Open <a href="https://console.cloud.google.com/apis/library/gmail.googleapis.com" target="_blank" rel="noopener">Gmail API</a> and click <b>Enable</b>.</li>
  <li>Open <a href="https://console.cloud.google.com/apis/credentials/consent" target="_blank" rel="noopener">OAuth consent screen</a>, pick <b>External</b>, fill the basics, add your Gmail as a <b>Test user</b>.</li>
  <li>Open <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">Credentials</a> → <b>Create credentials → OAuth client ID</b> → <b>Desktop app</b> → download the JSON.</li>
  <li>Save the downloaded file as <code>credentials.json</code> in the dashboard folder, then reopen this dialog.</li>
</ol>
<p class="muted">All tokens stay on your machine. Scope is read-only Gmail.</p>
<div class="row"><button class="secondary" data-close>OK, I'll do that</button></div>
`;
const gmailBody = (lead, action) => `
${lead}
<p class="muted">Read-only scope · token saved locally to <code>token.json</code> · revoke any time at <a href="https://myaccount.google.com/permissions" target="_blank" rel="noopener">myaccount.google.com/permissions</a>.</p>
<div class="row">
  <button class="primary" id="gmail-go" data-label="${escapeHtml(action)}">${escapeHtml(action)}</button>
  <button class="secondary" data-close>Cancel</button>
</div>
<div id="gmail-result" style="margin-top:12px;" aria-live="polite"></div>
`;
const GMAIL_DIALOG = {
  disconnected: ["Connect Gmail", gmailBody(`<p>Authorize this dashboard to read your Anthropic, OpenAI and Google Play receipts from Gmail. A browser window opens for Google's consent.</p>`, "Authorize and import")],
  connected: ["Sync Gmail receipts", gmailBody(`<p>Import new receipts from Gmail. Receipts you added by hand are never overwritten.</p>`, "Import receipts")],
  error: ["Sync Gmail receipts", gmailBody(`<p class="gmail-expired">${icon("alert")}<span><b>Gmail sign-in could not be checked.</b> This is usually a network problem; importing retries the sign-in.</span></p>`, "Import receipts")],
  expired: ["Reconnect Gmail", gmailBody(`<p class="gmail-expired">${icon("alert")}<span><b>Gmail sign-in expired — reconnect.</b> New receipts are not imported until you do. (${escapeHtml(GMAIL_EXPIRY_WHY)})</span></p>
<p>Reconnecting opens Google's consent page in a browser window, then imports receipts.</p>`, "Reconnect and import")],
};
async function openGmailDialog() {
  const dlg = document.getElementById("gmail-dialog");
  const body = document.getElementById("gmail-dlg-body");
  const title = document.getElementById("gmail-dlg-title");
  try { gmailStatus = await api("/api/gmail/status"); renderGmailButton(); } catch {}
  const st = gmailState();
  if (st === "setup") {
    title.textContent = "Set up Gmail import";
    body.innerHTML = SETUP_HTML;
  } else {
    [title.textContent, body.innerHTML] = GMAIL_DIALOG[st];
    document.getElementById("gmail-go").addEventListener("click", runScrape);
  }
  if (!dlg.open) dlg.showModal();
}
async function runScrape() {
  const btn = document.getElementById("gmail-go");
  const out = document.getElementById("gmail-result");
  btn.disabled = true; btn.textContent = "Working…";
  out.innerHTML = `<span class="muted">A browser window opens for Google's consent if needed, then receipts are fetched. This can take a minute.</span>`;
  try {
    const r = await fetch("/api/gmail/scrape", { method: "POST" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = j.detail || j.error || "Import failed";
      if (GMAIL_AUTH_ERR.test(detail)) {
        gmailExpiredHint(true); renderGmailButton();
        out.innerHTML = `<span class="err">Gmail sign-in expired — reconnect. ${escapeHtml(GMAIL_EXPIRY_WHY)}</span><div class="muted gmail-detail">${escapeHtml(detail)}</div>`;
      } else out.innerHTML = `<span class="err">${escapeHtml(detail)}</span>`;
    } else {
      gmailExpiredHint(false);
      out.innerHTML = `<span class="ok">Found ${fmtInt(j.found)} receipt${j.found === 1 ? "" : "s"} · added ${fmtInt(j.added)}, updated ${fmtInt(j.updated)}.</span>`;
      await refreshGmailStatus();
      if (document.getElementById("receipts-dialog")?.open) loadReceipts();
      afterReceiptsChange();
    }
  } catch (e) {
    out.innerHTML = `<span class="err">${escapeHtml(String(e))}</span>`;
  } finally {
    btn.disabled = false; btn.textContent = btn.dataset.label || "Import receipts";
  }
}
function bindGmail() {
  document.getElementById("gmail-btn").addEventListener("click", openGmailDialog);
  const dlg = document.getElementById("gmail-dialog");
  dlg.addEventListener("click", e => {
    if (e.target.closest("[data-close]") || e.target === dlg) dlg.close();
  });
}

// ---------- Accounts dialog: Claude profiles, shared folders, aliases, other agents ----------
// Folder sharing per profile, from ccm's sharing report.
const SHARE_STATUS = {
  source:   { cls: "src", label: "shared from here", tip: "The shared copy lives in this profile; other profiles link to it" },
  linked:   { cls: "lnk", label: "uses shared",      tip: "A link to the shared copy" },
  separate: { cls: "sep", label: "own copy",         tip: "This profile's own folder, not shared" },
  missing:  { cls: "off", label: "not used",         tip: "No such folder here; nothing to share" },
  broken:   { cls: "bad", label: "broken link",      tip: "A link whose target is gone: Claude Code can't read this folder" },
};
const SHARE_ORDER = ["source", "linked", "separate", "missing", "broken"];
const shareStatus = s => SHARE_STATUS[s] ? s : (s === "real-dir" ? "separate" : s === "ok" ? "linked" : "separate");
const listWords = a => a.length < 2 ? (a[0] || "") : `${a.slice(0, -1).join(", ")} and ${a[a.length - 1]}`;
const capFirst = s => s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
let profilesHome = "";
const tilde = p => (p && profilesHome && (p === profilesHome || p.startsWith(profilesHome + "/"))) ? "~" + p.slice(profilesHome.length) : (p || "");

function shareChip(dir, status, srcPath) {
  const s = SHARE_STATUS[shareStatus(status)];
  const from = status === "linked" && srcPath ? ` (${tilde(srcPath)})` : "";
  return `<span class="p-chip ${s.cls}" title="${escapeHtml(`${dir}: ${s.label}${from}. ${s.tip}`)}"><span class="p-chip-dot" aria-hidden="true"></span>${escapeHtml(dir)}<span class="sr-only">: ${escapeHtml(s.label)}</span></span>`;
}
function shareLegend(statuses) {
  const shown = SHARE_ORDER.filter(s => statuses.has(s));
  if (!shown.length) return "";
  return `<div class="share-legend" aria-label="Folder status legend">${shown.map(s =>
    `<span class="item"><span class="p-chip-dot ${SHARE_STATUS[s].cls}" aria-hidden="true"></span>${escapeHtml(SHARE_STATUS[s].label)}</span>`).join("")}</div>`;
}

// One sentence about sharing, or (when nothing is shared yet) the action behind a confirm step.
function sharingSummary(j) {
  const profs = j.profiles || [], dirs = j.shared_dirs || [];
  const st = (p, d) => shareStatus((p.shared || {})[d] || "missing");
  const inPlace = profs.some(p => dirs.some(d => ["source", "linked"].includes(st(p, d))));
  if (inPlace) {
    const sharedDirs = dirs.filter(d => profs.some(p => ["source", "linked"].includes(st(p, d))));
    const unused = dirs.filter(d => profs.every(p => st(p, d) === "missing"));
    const src = profs.filter(p => sharedDirs.some(d => st(p, d) === "source"));
    const from = src.length
      ? `<code>${escapeHtml(tilde(src[0].path))}</code> (${escapeHtml(src[0].name)})`
      : `<code>${escapeHtml(tilde(j.shared_root))}</code>`;
    const users = profs.filter(p => !src.includes(p) && sharedDirs.some(d => st(p, d) === "linked")).map(p => p.name);
    const own = profs.filter(p => sharedDirs.some(d => st(p, d) === "separate")).map(p => p.name);
    const broken = profs.filter(p => dirs.some(d => st(p, d) === "broken")).map(p => p.name);
    const parts = [`${escapeHtml(capFirst(listWords(sharedDirs)))} are shared from ${from}.`];
    if (users.length) parts.push(`${escapeHtml(capFirst(listWords(users)))} use${users.length === 1 ? "s" : ""} the same copies.`);
    if (own.length) parts.push(`${escapeHtml(capFirst(listWords(own)))} still keep${own.length === 1 ? "s" : ""} an own copy of some.`);
    if (unused.length) parts.push(`${escapeHtml(capFirst(listWords(unused)))}: not used by any profile.`);
    if (broken.length) parts.push(`<span class="err">${escapeHtml(capFirst(listWords(broken)))} ha${broken.length === 1 ? "s" : "ve"} a broken link.</span>`);
    return `<p>${parts.join(" ")}</p>`;
  }
  // Not shared yet: say what "share" would do, folder by folder, before doing it.
  const def = profs.find(p => p.name === "default");
  const root = j.shared_root;
  if (!def) return `<p>Each profile keeps its own ${escapeHtml(listWords(dirs))}.</p>`;
  const moves = dirs.map(d => {
    const s = st(def, d);
    if (s === "separate") return `<li><code>${escapeHtml(tilde(def.path))}/${escapeHtml(d)}</code> moves to <code>${escapeHtml(tilde(root))}/${escapeHtml(d)}</code>; a link is left in its place</li>`;
    if (s === "missing") return `<li><code>${escapeHtml(tilde(root))}/${escapeHtml(d)}</code> is created empty (default has no ${escapeHtml(d)} folder)</li>`;
    return `<li><code>${escapeHtml(d)}</code> is already a link in default, so it is left alone</li>`;
  }).join("");
  return `<p>Each profile keeps its own ${escapeHtml(listWords(dirs))}. Sharing them means one copy that every profile links to.</p>
    <button type="button" class="p-btn" id="share-start">Share them across profiles…</button>
    <div class="share-confirm" id="share-confirm" hidden>
      <p><b>This changes folders on disk:</b></p>
      <ul>${moves}</ul>
      <p class="muted">Claude Code keeps working in default; other profiles can then link to the shared folders with “Use shared folders”.</p>
      <div class="row"><button type="button" class="p-btn primary" id="share-go">Move and share</button><button type="button" class="p-btn" id="share-cancel">Cancel</button></div>
    </div>`;
}

function profileCard(p, j) {
  const dirs = j.shared_dirs || [];
  const chips = dirs.map(d => shareChip(d, (p.shared || {})[d] || "missing", (p.shared_source || {})[d])).join("");
  let usage;
  if (p.merged_into) {
    usage = p.name === "default"
      ? `History before the account split — counted under ${escapeHtml(p.merged_into)}`
      : `Counted under ${escapeHtml(p.merged_into)} (same account)`;
  } else if (p.usage) {
    usage = `${fmtInt(p.usage.msgs)} msgs · ${fmtCompact(p.usage.tokens)} tokens · ${fmtMoney(p.usage.cost)} at API rates` +
      (p.usage.last_ts ? ` · last active ${escapeHtml(fmtDay(p.usage.last_ts.slice(0, 10)))}` : "");
  } else usage = "No usage indexed yet. Refresh to pick it up.";
  // ccm names profiles by folder; the index files rows under the canonical account.
  const acct = p.account_differs && !p.merged_into
    ? `<span class="p-acct" title="Usage from this profile is indexed as ${escapeHtml(p.account)}">${escapeHtml(p.name)} → ${escapeHtml(p.account)}</span>` : "";
  const al = p.alias || {};
  const prim = al.primary || { shell: j.shell || "zsh", rc_file: j.rc_file, line: al.zsh };
  const rc = tilde(prim.rc_file || j.rc_file || "");
  const installed = p.alias_installed ?? al.installed;
  const instName = p.alias_installed_name || al.installed_name;
  const others = Object.entries(al.other || {}).filter(([sh, line]) => line && sh !== prim.shell);
  const canLink = dirs.some(d => (j.shared_sources || {})[d] && ["separate", "missing", "broken"].includes(shareStatus((p.shared || {})[d] || "missing")));
  return `
    <div class="profile-card${p.merged_into ? " merged" : ""}" data-name="${escapeHtml(p.name)}">
      <div class="p-head">
        <span class="p-name">${escapeHtml(p.name)}</span>
        ${acct}
        ${p.credentials ? `<span class="p-cred ok">signed in</span>` : ""}
      </div>
      <div class="p-path">${escapeHtml(tilde(p.path))}</div>
      <div class="p-usage">${usage}</div>
      <div class="p-chips">${chips}</div>
      <div class="p-alias">
        ${installed ? `<span class="p-installed">${icon("check")}${instName ? `<code>${escapeHtml(instName)}</code> ` : ""}installed in ${escapeHtml(rc || "your shell")}</span>` : ""}
        <button type="button" class="p-btn${installed ? "" : " primary"}" data-copy="${escapeHtml(prim.line || "")}">Copy alias (${escapeHtml(prim.shell)})</button>
        ${others.length ? `<details class="p-other"><summary>Other shells</summary><div class="p-other-list">${others.map(([sh, line]) =>
          `<button type="button" class="p-btn" data-copy="${escapeHtml(line)}">Copy alias (${escapeHtml(sh === "powershell" ? "PowerShell" : sh)})</button>`).join("")}</div></details>` : ""}
      </div>
      ${canLink ? `<div class="p-actions"><button type="button" class="p-btn" data-act="link">Use shared folders</button></div>` : ""}
    </div>`;
}

function otherToolsSection(tools) {
  if (!tools?.length) return "";
  const rows = tools.map(t => {
    const u = t.usage;
    const usage = u ? `${fmtInt(u.msgs)} msgs · ${fmtCompact(u.tokens)} tokens · ${fmtMoney(u.cost)} at API rates` : "found, no usage data";
    const plan = t.paid_by ? `<span class="p-acct">${escapeHtml(PLAN_NAMES[t.paid_by] || providerName(t.paid_by))} plan</span>` : "";
    return `<div class="tool-row${u ? "" : " nodata"}">
      <div class="tool-head"><span class="tool-name">${escapeHtml(toolName(t.tool))}</span>${plan}</div>
      <div class="p-path">${escapeHtml(tilde(t.path))}</div>
      <div class="p-usage">${usage}</div>
    </div>`;
  }).join("");
  return `<h4 class="dlg-sub">Other coding agents</h4><div class="tools-list">${rows}</div>`;
}

function profilesLog(text) {
  const log = document.getElementById("profiles-log");
  if (!text) { log.hidden = true; return; }
  log.hidden = false;
  log.textContent = text;
}
async function profilesAction(promise) {
  let r;
  try { r = await promise; } catch (e) { profilesLog(String(e)); return; }
  profilesLog(r.output || (r.ok ? "Done." : "Failed."));
  await renderProfiles();
}
async function copyText(btn, text) {
  try {
    await navigator.clipboard.writeText(text);
    const prev = btn.textContent; btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = prev; }, 1200);
  } catch { profilesLog(text); }
}
async function renderProfiles() {
  const sharedEl = document.getElementById("profiles-shared");
  const list = document.getElementById("profiles-list");
  const toolsEl = document.getElementById("profiles-tools");
  let j;
  try { j = await api("/api/profiles"); }
  catch { list.innerHTML = `<div class="muted">Could not load accounts.</div>`; return; }
  profilesHome = (j.shared_root || "").replace(/\/[^/]+\/?$/, "");
  const statuses = new Set();
  for (const p of j.profiles || []) for (const d of j.shared_dirs || []) statuses.add(shareStatus((p.shared || {})[d] || "missing"));
  sharedEl.innerHTML = sharingSummary(j) + shareLegend(statuses) +
    `<p class="alias-note">An alias starts Claude with this profile's config folder (<code>CLAUDE_CONFIG_DIR</code>).</p>`;
  list.innerHTML = (j.profiles || []).map(p => profileCard(p, j)).join("")
    || `<div class="muted">No Claude config folders found in your home directory.</div>`;
  toolsEl.innerHTML = otherToolsSection(j.other_tools);
  const conf = document.getElementById("share-confirm");
  document.getElementById("share-start")?.addEventListener("click", e => { conf.hidden = false; e.currentTarget.hidden = true; document.getElementById("share-go").focus(); });
  document.getElementById("share-cancel")?.addEventListener("click", () => { conf.hidden = true; const s = document.getElementById("share-start"); s.hidden = false; s.focus(); });
  document.getElementById("share-go")?.addEventListener("click", () =>
    profilesAction(fetch("/api/profiles/init_shared?from=default", { method: "POST" }).then(r => r.json())));
  list.querySelectorAll(".profile-card button").forEach(btn => {
    btn.addEventListener("click", () => {
      const name = btn.closest(".profile-card").dataset.name;
      if (btn.dataset.act === "link") profilesAction(fetch(`/api/profiles/link?name=${encodeURIComponent(name)}`, { method: "POST" }).then(r => r.json()));
      else if (btn.dataset.copy != null) copyText(btn, btn.dataset.copy);
    });
  });
}
function openProfiles() {
  const dlg = document.getElementById("profiles-dialog");
  profilesLog(null);
  if (!dlg.open) dlg.showModal();
  renderProfiles();
}
function bindProfiles() {
  const dlg = document.getElementById("profiles-dialog");
  document.getElementById("profiles-btn").addEventListener("click", openProfiles);
  dlg.addEventListener("click", e => {
    if (e.target.closest("[data-close]") || e.target === dlg) dlg.close();
    if (e.target.closest("[data-open-receipts]")) { dlg.close(); openReceipts(); }
  });
  document.getElementById("profile-create").addEventListener("click", () => {
    const input = document.getElementById("profile-new-name");
    const name = input.value.trim();
    if (!name) { input.focus(); return; }
    profilesAction(fetch(`/api/profiles/create?name=${encodeURIComponent(name)}`, { method: "POST" }).then(r => r.json()))
      .then(() => { input.value = ""; loadAccounts(); });
  });
  if (location.hash === "#accounts") openProfiles();
}

// ---------- Receipts: what each plan actually cost ----------
// GET /api/receipts lists billing.json (scraped from Gmail, or added here by hand). Manual
// receipts can be added, edited and deleted; Gmail ones are read-only (a re-import restores them).
let receiptsData = null;      // { receipts, providers, path }
let receiptsAccounts = [];    // /api/accounts rows, for the account picker and plan defaults
let receiptEditing = null;    // receipt_id being edited, or null for a new one
const PROVIDER_ORDER = ["anthropic", "openai_chatgpt", "openai_api", "google_play", "other"];
const recurringLabel = r => `monthly from ${fmtDay(r.start)}${r.end ? ` until ${fmtDay(r.end)}` : ""}`;
const receiptPeriod = r => r.recurring ? recurringLabel(r) : (r.end ? fmtRange(r.start, r.end) : fmtDay(r.start));
function receiptRow(r) {
  const manual = r.source === "manual";
  const orig = r.currency && r.currency !== "USD" && r.amount_original != null
    ? `<span class="rc-orig">${escapeHtml(r.currency)} ${escapeHtml(Number(r.amount_original).toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 }))}</span>` : "";
  const via = r.store || r.merchant ? ` · via ${escapeHtml(r.store || r.merchant)}` : "";
  const plan = r.raw_plan && r.raw_plan !== r.plan ? ` title="${escapeHtml(r.raw_plan)}"` : "";
  return `<li class="rc-row" data-id="${escapeHtml(r.receipt_id)}">
    <div class="rc-main">
      <span class="rc-plan"${plan}>${escapeHtml(r.plan || "—")}</span>
      <span class="rc-meta">${escapeHtml(receiptPeriod(r))} · ${escapeHtml(acctName(r.account))}${via}</span>
      ${r.note ? `<span class="rc-note">${escapeHtml(r.note)}</span>` : ""}
    </div>
    <div class="rc-side">
      <span class="rc-amt">${fmtMoney(r.amount_usd)}${r.recurring ? `<span class="rc-per">/mo</span>` : ""}</span>
      ${orig}
      <span class="rc-src ${manual ? "manual" : "gmail"}">${manual ? "manual" : "Gmail"}</span>
    </div>
    ${manual ? `<div class="rc-acts">
      <button type="button" class="p-btn" data-rc="edit">Edit</button>
      <button type="button" class="p-btn danger" data-rc="delete">Delete</button>
    </div>` : ""}
  </li>`;
}
function renderReceiptsList() {
  const el = document.getElementById("receipts-list");
  if (!el || !receiptsData) return;
  const rows = receiptsData.receipts || [];
  if (!rows.length) { el.innerHTML = `<p class="muted">No receipts yet. Connect Gmail to import them, or add one above.</p>`; return; }
  const groups = new Map();
  for (const r of rows) {
    const p = r.provider || "other";
    if (!groups.has(p)) groups.set(p, []);
    groups.get(p).push(r);
  }
  const order = [...groups.keys()].sort((a, b) => (PROVIDER_ORDER.indexOf(a) + 1 || 99) - (PROVIDER_ORDER.indexOf(b) + 1 || 99));
  el.innerHTML = order.map(p => {
    const list = groups.get(p).slice().sort((a, b) => String(b.start).localeCompare(String(a.start)));
    const total = list.filter(r => !r.recurring).reduce((s, r) => s + (r.amount_usd || 0), 0);
    const rec = list.filter(r => r.recurring).reduce((s, r) => s + (r.amount_usd || 0), 0);
    return `<section class="rc-group">
      <h4 class="rc-group-h"><span>${escapeHtml(providerName(p))}</span><span class="muted">${list.length} receipt${list.length === 1 ? "" : "s"} · ${fmtMoney(total)}${rec ? ` + ${fmtMoney(rec)}/mo` : ""}</span></h4>
      <ul class="rc-list">${list.map(receiptRow).join("")}</ul>
    </section>`;
  }).join("");
}
function renderReceiptsGmail() {
  const el = document.getElementById("receipts-gmail");
  if (!el) return;
  const st = gmailState();
  const msg = {
    connected: [`${icon("check")}<span>Gmail connected · receipts import from your inbox</span>`, "Import new"],
    expired: [`${icon("alert")}<span><b>Gmail sign-in expired — reconnect</b> (${escapeHtml(GMAIL_EXPIRY_WHY.replace(/\.$/, ""))})</span>`, "Reconnect"],
    error: [`${icon("alert")}<span><b>Gmail sign-in could not be checked</b>${gmailStatus?.error ? ` · ${escapeHtml(gmailStatus.error)}` : ""}</span>`, "Try import"],
    disconnected: [`${icon("mail")}<span>Gmail not connected · connect it to import Anthropic, OpenAI and Google Play receipts</span>`, "Connect Gmail"],
    setup: [`${icon("mail")}<span>Gmail not set up · import Anthropic, OpenAI and Google Play receipts automatically</span>`, "Set up"],
  }[st];
  el.className = `rc-gmail ${st === "error" ? "expired" : st}`;
  el.innerHTML = `<span class="rc-gmail-txt">${msg[0]}</span><button type="button" class="p-btn${st === "expired" ? " primary" : ""}" data-open-gmail>${msg[1]}</button>`;
}
function receiptAccountOptions(selected) {
  const names = receiptsAccounts.map(a => a.name);
  for (const r of receiptsData?.receipts || []) if (r.account && !names.includes(r.account)) names.push(r.account);
  if (selected && !names.includes(selected)) names.push(selected);
  return names.map(n => `<option value="${escapeHtml(n)}"${n === selected ? " selected" : ""}>${escapeHtml(acctName(n))}</option>`).join("");
}
// The account a new receipt for this provider most likely belongs to.
function defaultAccountFor(provider) {
  const byPaid = receiptsAccounts.find(a => a.paid_by === provider);
  if (byPaid) return byPaid.name;
  if (provider === "anthropic") return (receiptsAccounts.find(a => a.provider === "anthropic") || {}).name || "personal";
  return receiptsAccounts[0]?.name || "personal";
}
function planHint(provider, account) {
  const a = receiptsAccounts.find(x => x.name === account);
  if (a?.paid_by && (a.plan_members || []).length > 1)
    return `One ${PLAN_NAMES[a.paid_by] || providerName(a.paid_by)} plan pays for ${listWords(a.plan_members.map(acctName))}; this receipt is split between them by usage.`;
  return "";
}
function openReceiptForm(r = null) {
  const form = document.getElementById("receipt-form");
  const add = document.getElementById("receipt-add");
  receiptEditing = r ? r.receipt_id : null;
  const providers = receiptsData?.providers?.length ? receiptsData.providers : ["anthropic", "openai_chatgpt", "openai_api", "other"];
  const prov = r?.provider || "anthropic";
  const acct = r?.account || defaultAccountFor(prov);
  const today = isoLocal(new Date());
  form.querySelector("#rc-provider").innerHTML = providers.map(p => `<option value="${escapeHtml(p)}"${p === prov ? " selected" : ""}>${escapeHtml(providerName(p))}</option>`).join("");
  form.querySelector("#rc-account").innerHTML = receiptAccountOptions(acct);
  form.querySelector("#rc-plan").value = r?.plan || "";
  form.querySelector("#rc-amount").value = r?.amount_usd ?? "";
  form.querySelector("#rc-orig").value = r?.amount_original ?? "";
  form.querySelector("#rc-currency").value = r?.currency && r.currency !== "USD" ? r.currency : "";
  form.querySelector("#rc-start").value = r?.start || today;
  form.querySelector("#rc-end").value = r?.end || isoLocal(addDays(r?.start || today, 30));
  form.querySelector("#rc-recurring").checked = !!r?.recurring;
  form.querySelector("#rc-note").value = r?.note || "";
  form.querySelector("#rc-title").textContent = r ? "Edit receipt" : "Add a receipt";
  form.querySelector("#rc-save").textContent = r ? "Save changes" : "Add receipt";
  syncReceiptForm();
  document.getElementById("rc-error").textContent = "";
  form.hidden = false; add.hidden = true;
  form.querySelector("#rc-plan").focus();
}
function closeReceiptForm() {
  const form = document.getElementById("receipt-form");
  form.hidden = true; receiptEditing = null;
  const add = document.getElementById("receipt-add");
  add.hidden = false;
}
function syncReceiptForm() {
  const form = document.getElementById("receipt-form");
  const rec = form.querySelector("#rc-recurring").checked;
  const endLabel = form.querySelector("#rc-end-label");
  endLabel.querySelector(".lbl").textContent = rec ? "Ends (optional)" : "End";
  form.querySelector("#rc-end").required = !rec;
  if (rec && !receiptEditing) form.querySelector("#rc-end").value = "";
  form.querySelector("#rc-plan-hint").textContent = planHint(form.querySelector("#rc-provider").value, form.querySelector("#rc-account").value);
}
async function saveReceipt(e) {
  e.preventDefault();
  const form = e.currentTarget;
  const err = document.getElementById("rc-error");
  const v = id => form.querySelector(id).value.trim();
  const body = {
    provider: v("#rc-provider"), account: v("#rc-account"), plan: v("#rc-plan"),
    amount_usd: Number(v("#rc-amount")), start: v("#rc-start"),
  };
  if (receiptEditing) body.receipt_id = receiptEditing;
  if (form.querySelector("#rc-recurring").checked) body.recurring = true;
  if (v("#rc-end")) body.end = v("#rc-end");
  if (v("#rc-orig") !== "") body.amount_original = Number(v("#rc-orig"));
  if (v("#rc-currency")) body.currency = v("#rc-currency").toUpperCase();
  if (body.amount_original != null && !body.currency) { err.textContent = "Pick the currency of the original amount."; return; }
  if (v("#rc-note")) body.note = v("#rc-note");
  const btn = form.querySelector("#rc-save");
  btn.disabled = true; err.textContent = "";
  try {
    const r = await fetch("/api/receipts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) { err.textContent = j.detail || j.error || `Could not save (${r.status})`; return; }
    closeReceiptForm();
    await loadReceipts(j.receipt?.receipt_id);
    afterReceiptsChange();
  } catch (x) { err.textContent = String(x); }
  finally { btn.disabled = false; }
}
async function deleteReceipt(row, id) {
  // Inline confirm: the row turns into "Delete this receipt? Delete · Keep".
  if (!row.querySelector(".rc-confirm")) {
    const r = (receiptsData?.receipts || []).find(x => x.receipt_id === id);
    row.querySelector(".rc-acts").hidden = true;
    row.insertAdjacentHTML("beforeend", `<div class="rc-confirm" role="group" aria-label="Confirm delete">
      <span>Delete ${escapeHtml(r?.plan || "this receipt")} (${escapeHtml(r ? fmtMoney(r.amount_usd) : "")})? This can't be undone.</span>
      <button type="button" class="p-btn danger" data-rc="confirm-delete">Delete</button>
      <button type="button" class="p-btn" data-rc="cancel-delete">Keep</button></div>`);
    row.querySelector('[data-rc="cancel-delete"]').focus();
    return;
  }
  try {
    const r = await fetch(`/api/receipts?receipt_id=${encodeURIComponent(id)}`, { method: "DELETE" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { row.querySelector(".rc-confirm span").textContent = j.detail || j.error || `Could not delete (${r.status})`; return; }
    await loadReceipts();
    afterReceiptsChange();
  } catch (x) { row.querySelector(".rc-confirm span").textContent = String(x); }
}
async function loadReceipts(flashId) {
  const el = document.getElementById("receipts-list");
  try {
    const [rc, acc] = await Promise.all([
      fetch("/api/receipts").then(r => { if (!r.ok) throw new Error(`/api/receipts ${r.status}`); return r.json(); }),
      fetch("/api/accounts").then(r => r.ok ? r.json() : { accounts: [] }).catch(() => ({ accounts: [] })),
    ]);
    receiptsData = rc; receiptsAccounts = acc.accounts || [];
  } catch (e) { el.innerHTML = `<p class="err">Could not load receipts. ${escapeHtml(e.message || "")}</p>`; return; }
  document.getElementById("receipts-path").textContent = receiptsData.path ? tildeAny(receiptsData.path) : "";
  renderReceiptsList();
  if (flashId) el.querySelector(`.rc-row[data-id="${CSS.escape(flashId)}"]`)?.classList.add("flash");
}
// ~ for a path under the user's home, when the home is known (from /api/profiles).
const tildeAny = p => profilesHome ? tilde(p) : p;
// KPIs and the Accounts table read billing.json: reload them after any receipt change.
function afterReceiptsChange() {
  reloadAll();
}
function openReceipts() {
  const dlg = document.getElementById("receipts-dialog");
  closeReceiptForm();
  renderReceiptsGmail();
  if (!dlg.open) dlg.showModal();
  loadReceipts();
}
function bindReceipts() {
  const dlg = document.getElementById("receipts-dialog");
  if (!dlg) return;
  dlg.addEventListener("click", e => {
    if (e.target.closest("[data-close]") || e.target === dlg) { dlg.close(); return; }
    const b = e.target.closest("button[data-rc]");
    if (!b) return;
    const row = b.closest(".rc-row"), id = row?.dataset.id;
    const act = b.dataset.rc;
    if (act === "edit") openReceiptForm((receiptsData?.receipts || []).find(x => x.receipt_id === id));
    else if (act === "delete" || act === "confirm-delete") deleteReceipt(row, id);
    else if (act === "cancel-delete") { row.querySelector(".rc-confirm")?.remove(); const a = row.querySelector(".rc-acts"); a.hidden = false; a.querySelector("button")?.focus(); }
  });
  document.getElementById("receipt-add").addEventListener("click", () => openReceiptForm());
  const form = document.getElementById("receipt-form");
  form.addEventListener("submit", saveReceipt);
  form.querySelector("#rc-cancel").addEventListener("click", closeReceiptForm);
  form.querySelector("#rc-recurring").addEventListener("change", syncReceiptForm);
  form.querySelector("#rc-account").addEventListener("change", syncReceiptForm);
  form.querySelector("#rc-provider").addEventListener("change", e => {
    if (!receiptEditing) form.querySelector("#rc-account").innerHTML = receiptAccountOptions(defaultAccountFor(e.target.value));
    syncReceiptForm();
  });
  document.getElementById("receipts-btn")?.addEventListener("click", openReceipts);
  // "Receipts" / "Connect Gmail" links anywhere on the page (account notes, dialogs).
  document.addEventListener("click", e => {
    if (e.target.closest("#profiles-dialog")) return;   // handled there (closes that dialog first)
    if (e.target.closest("[data-open-receipts]")) openReceipts();
    else if (e.target.closest("[data-open-gmail]")) openGmailDialog();
  });
  if (location.hash === "#receipts") openReceipts();
}

function bindTabs() {
  const bar = document.getElementById("tabs");
  if (!bar) return;
  bar.addEventListener("click", e => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    bar.querySelectorAll("button[data-tab]").forEach(x => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", String(x === b)); });
    STATE.tab = b.dataset.tab;
    document.querySelector("main").dataset.tab = STATE.tab;
    // Charts sized while hidden come back at 0×0; resize them once visible.
    requestAnimationFrame(() => {
      Object.values(charts).forEach(c => { try { c.resize(); } catch {} });
      if (STATE.tab === "overview") renderHeatmap();
    });
  });
  const m = location.hash.match(/^#tab=(\w+)$/);
  if (m) {
    const btn = bar.querySelector(`button[data-tab="${m[1]}"]`);
    if (btn) { btn.hidden = false; btn.click(); }
  }
}

// ---------- Export menu (Canvas-rendered PNG, see exporter.js) ----------
function bindExport() {
  const btn = document.getElementById("export-btn");
  const menu = document.getElementById("export-menu");
  if (!btn || !menu) return;
  const setOpen = open => { menu.hidden = !open; btn.setAttribute("aria-expanded", String(open)); if (open) menu.querySelector("button")?.focus(); };
  btn.addEventListener("click", e => { e.stopPropagation(); setOpen(menu.hidden); });
  document.addEventListener("click", e => { if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) setOpen(false); });
  menu.addEventListener("keydown", e => { if (e.key === "Escape") { setOpen(false); btn.focus(); } });
  menu.addEventListener("click", async e => {
    const item = e.target.closest("button[data-mode]");
    if (!item) return;
    setOpen(false);
    const label = btn.querySelector(".btn-label");
    btn.disabled = true; label.textContent = "Rendering…";
    try { await window.__exporter.run(item.dataset.mode); }
    catch (err) { console.error(err); alert("Export failed: " + (err.message || err)); }
    finally { btn.disabled = false; label.textContent = "Export"; }
  });
}

// ---------- scroll cues + sticky-header offset ----------
// Containers that scroll sideways get an edge fade on whichever side has more to show.
function bindScrollFade(el) {
  if (!el || el._fade) return;
  el._fade = true;
  const update = () => {
    const max = el.scrollWidth - el.clientWidth;
    const l = max > 2 && el.scrollLeft > 2, r = max > 2 && el.scrollLeft < max - 2;
    el.toggleAttribute("data-fade-l", l);
    el.toggleAttribute("data-fade-r", r);
    el.toggleAttribute("data-fade", l || r);
  };
  el.addEventListener("scroll", update, { passive: true });
  const ro = new ResizeObserver(update);
  ro.observe(el);
  [...el.children].forEach(c => ro.observe(c));
  new MutationObserver(() => { [...el.children].forEach(c => ro.observe(c)); update(); }).observe(el, { childList: true });
  update();
}
function bindScrollCues() {
  document.querySelectorAll(".table-wrap, .tabs, #cal-heatmap").forEach(bindScrollFade);
}
// --header-h tracks the real sticky header height (0 when it scrolls away on phones),
// so scroll-margin-top on cards always clears it.
function bindHeaderOffset() {
  const bar = document.querySelector(".topbar");
  if (!bar) return;
  const set = () => {
    const sticky = getComputedStyle(bar).position === "sticky";
    document.documentElement.style.setProperty("--header-h", sticky ? `${bar.offsetHeight}px` : "0px");
  };
  new ResizeObserver(set).observe(bar);
  window.addEventListener("resize", set);
  set();
}

// ---------- boot ----------
readUrl();
syncUrl(false);        // normalise a hand-edited or stale query string
readTheme();
applyControls();
renderFilterBar();
bindFilterBar();
bindControls();
bindScrollCues();
bindHeaderOffset();
bindGmail();
bindTabs();
bindProfiles();
bindReceipts();
bindExport();
bindCustom();
paintSkeletons();

// Reflow the heatmap on resize from cached data (debounced, no refetch).
let _resizeT;
window.addEventListener("resize", () => {
  clearTimeout(_resizeT);
  _resizeT = setTimeout(renderHeatmap, 180);
});

loadAccounts().finally(reloadAll);
