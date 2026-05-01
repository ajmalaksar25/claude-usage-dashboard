// Claude Usage Dashboard — single-page client.
// Pure ES module-free script, talks to /api/* endpoints.

const fmtInt    = n => (n == null) ? "—" : n.toLocaleString();
const fmtCompact= n => (n == null) ? "—" : Intl.NumberFormat("en", {notation:"compact", maximumFractionDigits:1}).format(n);
const fmtMoney  = n => (n == null) ? "—" : "$" + (Math.round(n*100)/100).toLocaleString("en", {minimumFractionDigits:2, maximumFractionDigits:2});
const fmtDate   = s => s ? new Date(s).toLocaleString() : "—";
const fmtDay    = s => s ? new Date(s + "T00:00:00").toLocaleDateString(undefined,{month:"short", day:"numeric"}) : "—";
const fmtDur    = m => { if (m == null) return "—"; if (m < 60) return m + "m"; const h = Math.floor(m/60), mm = m%60; return mm ? `${h}h ${mm}m` : `${h}h`; };

const COLORS = [
  getCSS("--chart-1"), getCSS("--chart-2"), getCSS("--chart-3"), getCSS("--chart-4"),
  getCSS("--chart-5"), getCSS("--chart-6"), getCSS("--chart-7"), getCSS("--chart-8"),
];
function getCSS(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function alpha(c, a) {
  // c is hex like "#abcdef" — return rgba string with alpha a (0..1)
  if (!c.startsWith("#")) return c;
  const r = parseInt(c.slice(1,3),16), g = parseInt(c.slice(3,5),16), b = parseInt(c.slice(5,7),16);
  return `rgba(${r},${g},${b},${a})`;
}

Chart.defaults.color = getCSS("--muted");
Chart.defaults.borderColor = getCSS("--border");
Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
Chart.defaults.font.size = 11;
Chart.defaults.plugins.legend.labels.color = getCSS("--text");
Chart.defaults.plugins.tooltip.backgroundColor = "#0d0f12";
Chart.defaults.plugins.tooltip.borderColor = getCSS("--border");
Chart.defaults.plugins.tooltip.borderWidth = 1;
Chart.defaults.plugins.tooltip.titleColor = getCSS("--text");
Chart.defaults.plugins.tooltip.bodyColor = getCSS("--text");

// State
const STATE = {
  window: "all",
  projBy: "tokens",
  sessBy: "tokens",
  showCost: true,
};

const charts = {};       // id -> Chart instance

async function api(path, params={}) {
  const u = new URL(path, location.origin);
  for (const [k,v] of Object.entries(params)) if (v != null) u.searchParams.set(k, v);
  const r = await fetch(u);
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json();
}

// ---------- KPI ----------
function setKpi(id, text, animateNumber=null) {
  const el = document.getElementById(id);
  el.removeAttribute("data-loading");
  if (animateNumber == null) {
    el.textContent = text;
    return;
  }
  // Count-up: animate from current displayed value to the target
  const prev = parseFloat((el.dataset.value ?? "0"));
  el.dataset.value = String(animateNumber);
  countUp(el, prev, animateNumber, text);
}

function countUp(el, from, to, finalText) {
  const duration = 600;
  const start = performance.now();
  const isMoney = finalText.startsWith("$") || finalText.startsWith("+$") || finalText.startsWith("-$");
  const sign = finalText.startsWith("+") ? "+" : (finalText.startsWith("-") ? "-" : "");
  const fmt = isMoney ? (v) => sign + fmtMoney(Math.abs(v)) : (v) => fmtCompact(v);
  function tick(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3); // cubic ease-out
    const v = from + (to - from) * eased;
    el.textContent = t < 1 ? fmt(v) : finalText;
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

async function loadSummary() {
  const s = await api("/api/summary", { window: STATE.window });
  setKpi("kpi-tokens", fmtCompact(s.total_tokens), s.total_tokens);
  document.getElementById("kpi-tokens-sub").textContent =
    `in ${fmtCompact(s.input_tokens)} · out ${fmtCompact(s.output_tokens)} · cache ${fmtCompact(s.cache_5m_write + s.cache_1h_write + s.cache_read)}`;
  setKpi("kpi-msgs", fmtInt(s.msgs), s.msgs);
  document.getElementById("kpi-msgs-sub").textContent =
    `${fmtInt(s.sessions)} sessions · ${fmtInt(s.projects)} projects`;
  setKpi("kpi-api", fmtMoney(s.api_cost), s.api_cost);

  const hasSub = s.subscription_cost > 0;
  const subCard = document.getElementById("kpi-sub").closest(".kpi");
  const savCard = document.getElementById("kpi-savings").closest(".kpi");
  if (hasSub) {
    subCard.hidden = false; savCard.hidden = false;
    setKpi("kpi-sub", fmtMoney(s.subscription_cost), s.subscription_cost);
    const planList = (s.billing_charges||[]).map(c=>c.plan).filter((v,i,a)=>a.indexOf(v)===i);
    document.getElementById("kpi-sub-detail").textContent = planList.join(" · ");
    const sav = s.savings;
    setKpi("kpi-savings", (sav>=0?"+":"") + fmtMoney(sav), Math.abs(sav));
    document.getElementById("kpi-mult").textContent = `vs ${fmtMoney(s.api_cost)} at API rates`;
    const badge = document.getElementById("kpi-savings-badge");
    if (s.multiplier) { badge.textContent = `${s.multiplier}× cheaper than API`; badge.hidden = false; }
    else { badge.hidden = true; }
  } else {
    subCard.hidden = true; savCard.hidden = true;
  }
  document.body.classList.toggle("no-billing", !hasSub);

  document.getElementById("meta-line").textContent =
    s.first_ts ? `${new Date(s.first_ts).toLocaleDateString()} → ${new Date(s.last_ts).toLocaleDateString()}` : "no data yet";
  return s;
}

// ---------- Timeline ----------
async function loadTimeline() {
  const j = await api("/api/timeseries", { window: STATE.window, bucket: "day" });
  const labels = j.rows.map(r => r.bucket);
  const inp    = j.rows.map(r => r.input_tokens);
  const out    = j.rows.map(r => r.output_tokens);
  const cw     = j.rows.map(r => r.cache_write);
  const cr     = j.rows.map(r => r.cache_read);
  let cum = 0;
  const cumCost = j.rows.map(r => (cum += r.cost));

  const datasets = [
    { label: "input",        data: inp, backgroundColor: alpha(COLORS[0], .9), stack: "tok", borderWidth: 0 },
    { label: "output",       data: out, backgroundColor: alpha(COLORS[1], .9), stack: "tok", borderWidth: 0 },
    { label: "cache write",  data: cw,  backgroundColor: alpha(COLORS[2], .8), stack: "tok", borderWidth: 0 },
    { label: "cache read",   data: cr,  backgroundColor: alpha(COLORS[3], .65), stack: "tok", borderWidth: 0 },
  ];
  if (STATE.showCost) {
    datasets.push({
      type: "line", label: "cumulative cost ($)", data: cumCost,
      borderColor: COLORS[4], backgroundColor: COLORS[4],
      tension: 0.25, yAxisID: "y2", borderWidth: 2, pointRadius: 0,
    });
  }
  const cfg = {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { stacked: true, grid: { color: "rgba(255,255,255,.04)" }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } },
        y: { stacked: true, grid: { color: "rgba(255,255,255,.04)" }, ticks: { callback: v => fmtCompact(v) } },
        y2: { display: STATE.showCost, position: "right", grid: { drawOnChartArea: false }, ticks: { callback: v => "$"+fmtCompact(v) } },
      },
      plugins: {
        tooltip: { callbacks: {
          label: ctx => ctx.dataset.label === "cumulative cost ($)" ? `cum cost ${fmtMoney(ctx.parsed.y)}` : `${ctx.dataset.label}: ${fmtInt(ctx.parsed.y)}`,
        }},
        legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10 } },
      },
    },
  };
  upsertChart("chart-timeline", cfg);
}

// ---------- Heatmap (custom SVG, GitHub-style, fits container width) ----------
async function loadHeatmap() {
  const j = await api("/api/heatmap", { window: STATE.window });
  const byDay = new Map(j.rows.map(r => [r.day, r]));
  const container = document.getElementById("cal-heatmap");
  container.innerHTML = "";
  if (!j.rows.length) {
    container.innerHTML = '<div class="muted" style="padding:14px 0">No activity in window.</div>';
    return;
  }

  // Determine date span
  const winDays = ({today:1, "1w":7, "15d":15, "1m":30, "3m":90, "6m":180, "1y":365})[STATE.window];
  const last = new Date();
  const first = new Date();
  if (winDays != null) {
    first.setDate(last.getDate() - winDays + 1);
  } else {
    first.setTime(new Date(j.rows[0].day + "T00:00:00").getTime());
  }
  // Snap to Monday
  const dow = (first.getDay() + 6) % 7;
  first.setDate(first.getDate() - dow);

  // Build day list
  const days = [];
  for (let d = new Date(first); d <= last; d.setDate(d.getDate() + 1)) {
    const iso = d.toISOString().slice(0,10);
    days.push({ date: new Date(d), iso, ...byDay.get(iso) });
  }

  // Color stops — dynamic so short windows still get a full range
  const max = days.reduce((m, d) => Math.max(m, d.tokens || 0), 0);
  const colors = ["#181c22", "#3b261a", "#5a341e", "#9c4d23", "#c46938", "#e07a4f"];
  const colorFor = v => {
    if (!v) return colors[0];
    if (max <= 1) return colors[1];
    const t = Math.log10(1 + v) / Math.log10(1 + max); // log scale handles huge spread
    const i = Math.min(colors.length - 1, Math.max(1, Math.ceil(t * (colors.length - 1))));
    return colors[i];
  };

  // Layout — compute cell size to fill container
  const colCount = Math.ceil(days.length / 7);
  const containerWidth = container.getBoundingClientRect().width || 1000;
  const padLeft = 28, padTop = 20;
  // Available width for cell grid
  const availW = Math.max(200, containerWidth - padLeft - 4);
  // Pick cell size: scale to width, capped so cells stay readable but fill the heatmap column
  const cellMax = 22, cellMin = 10, gutterRatio = 0.22;
  let cell = Math.min(cellMax, Math.max(cellMin, Math.floor((availW / colCount) / (1 + gutterRatio))));
  let gutter = Math.max(2, Math.round(cell * gutterRatio));

  const w = padLeft + colCount * (cell + gutter);
  const h = padTop + 7 * (cell + gutter);

  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.style.fontFamily = "inherit";

  // Weekday labels
  ["Mon","","Wed","","Fri","",""].forEach((lab,i) => {
    if (!lab) return;
    const t = document.createElementNS(svgNS, "text");
    t.setAttribute("x", 0);
    t.setAttribute("y", padTop + i*(cell+gutter) + cell - 2);
    t.setAttribute("fill", getCSS("--muted"));
    t.setAttribute("font-size", "9.5");
    t.textContent = lab;
    svg.appendChild(t);
  });

  // Month labels
  let lastMonth = -1;
  days.forEach((d, idx) => {
    const col = Math.floor(idx / 7);
    if (d.date.getDate() <= 7 && d.date.getMonth() !== lastMonth) {
      lastMonth = d.date.getMonth();
      const t = document.createElementNS(svgNS, "text");
      t.setAttribute("x", padLeft + col*(cell+gutter));
      t.setAttribute("y", 12);
      t.setAttribute("fill", getCSS("--muted"));
      t.setAttribute("font-size", "10");
      t.setAttribute("font-weight", "500");
      const showYear = STATE.window === "all" || STATE.window === "1y";
      const monthLabel = d.date.toLocaleDateString(undefined, { month: "short" });
      t.textContent = showYear ? `${monthLabel} '${String(d.date.getFullYear()).slice(2)}` : monthLabel;
      svg.appendChild(t);
    }
  });

  // Cells
  let activeDays = 0, peakDay = null, totalTokens = 0, totalCost = 0;
  days.forEach((d, idx) => {
    if (d.tokens) {
      activeDays++; totalTokens += d.tokens; totalCost += d.cost || 0;
      if (!peakDay || (d.tokens > peakDay.tokens)) peakDay = d;
    }
    const col = Math.floor(idx / 7);
    const row = idx % 7;
    const rect = document.createElementNS(svgNS, "rect");
    rect.setAttribute("x", padLeft + col*(cell+gutter));
    rect.setAttribute("y", padTop + row*(cell+gutter));
    rect.setAttribute("width", cell);
    rect.setAttribute("height", cell);
    rect.setAttribute("rx", Math.max(2, Math.round(cell * 0.18)));
    rect.setAttribute("fill", colorFor(d.tokens || 0));
    const tip = d.tokens
      ? `${d.iso}: ${fmtCompact(d.tokens)} tokens · ${fmtInt(d.msgs)} msgs · ${fmtMoney(d.cost)}`
      : `${d.iso}: no activity`;
    const title = document.createElementNS(svgNS, "title");
    title.textContent = tip;
    rect.appendChild(title);
    svg.appendChild(rect);
  });

  container.appendChild(svg);

  // Streak + weekday stats, computed once for the side panel
  let curStreak = 0, longestStreak = 0, longestEnd = null;
  for (const d of days) {
    if (d.tokens) {
      curStreak += 1;
      if (curStreak > longestStreak) { longestStreak = curStreak; longestEnd = d.iso; }
    } else curStreak = 0;
  }
  // Active streak ending today (or most recent active day)
  let activeStreak = 0;
  for (let i = days.length - 1; i >= 0; i--) {
    if (days[i].tokens) activeStreak += 1;
    else if (activeStreak > 0) break;
  }
  const dowTotals = [0,0,0,0,0,0,0];
  const dowDays   = [0,0,0,0,0,0,0];
  for (const d of days) {
    const dow = (d.date.getDay() + 6) % 7; // 0=Mon
    if (d.tokens) { dowTotals[dow] += d.tokens; dowDays[dow] += 1; }
  }
  const dowNames = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
  let busy = 0; for (let i=1;i<7;i++) if (dowTotals[i] > dowTotals[busy]) busy = i;

  // Bottom summary (still useful — full width below heatmap)
  const summary = document.createElement("div");
  summary.className = "heatmap-summary";
  const peakLine = peakDay ? `${fmtDay(peakDay.iso)} · ${fmtCompact(peakDay.tokens)}` : "—";
  summary.innerHTML = `
    <div class="stat"><span class="lbl">Active days</span><span class="val">${activeDays} / ${days.length}</span></div>
    <div class="stat"><span class="lbl">Peak day</span><span class="val">${peakLine}</span></div>
    <div class="stat"><span class="lbl">Daily avg (active)</span><span class="val">${activeDays ? fmtCompact(totalTokens/activeDays) : "—"} tokens</span></div>
    <div class="heatmap-legend"><span>less</span>${
      colors.map(c => `<span class="sq" style="background:${c}"></span>`).join("")
    }<span>more</span></div>`;
  container.appendChild(summary);

  // Right side panel — mini chart + stat boxes
  const side = document.getElementById("heatmap-side");
  if (side) {
    const longestEndStr = longestEnd ? `ending ${fmtDay(longestEnd)}` : "";
    const busyDays = dowDays[busy], busyAvg = busyDays ? dowTotals[busy] / busyDays : 0;
    const dowMax = Math.max(...dowTotals, 1);
    const barRows = dowTotals.map((tot, i) => {
      const w = (tot / dowMax) * 100;
      const isMax = i === busy && tot > 0;
      return `
        <div class="dow-row">
          <span class="dow-name">${dowNames[i]}</span>
          <span class="dow-track"><span class="dow-bar${isMax ? " peak" : ""}" style="width:${w}%"></span></span>
          <span class="dow-val">${tot ? fmtCompact(tot) : "—"}</span>
        </div>`;
    }).join("");
    side.innerHTML = `
      <div class="side-chart">
        <div class="side-chart-head"><span class="lbl">Tokens by weekday</span><span class="muted">peak ${dowNames[busy]}</span></div>
        ${barRows}
      </div>
      <div class="side-stat streak"><span class="lbl">Current streak</span><span class="val">${activeStreak} day${activeStreak===1?"":"s"}</span><span class="sub">consecutive active days</span></div>
      <div class="side-stat"><span class="lbl">Longest streak</span><span class="val">${longestStreak} day${longestStreak===1?"":"s"}</span><span class="sub">${escapeHtml(longestEndStr)}</span></div>
      <div class="side-stat"><span class="lbl">Total this window</span><span class="val">${fmtCompact(totalTokens)}</span><span class="sub">${fmtMoney(totalCost)} at API rates</span></div>`;
  }
}

// ---------- by-model donuts ----------
async function loadModels() {
  const j = await api("/api/by_model", { window: STATE.window });
  const labels = j.rows.map(r => r.model);
  const tokens = j.rows.map(r => r.tokens);
  const cost   = j.rows.map(r => r.cost);
  const colors = labels.map((_,i) => COLORS[i % COLORS.length]);
  for (const [id, data, fmt] of [
    ["chart-model-tokens", tokens, fmtCompact],
    ["chart-model-cost",   cost,   fmtMoney],
  ]) {
    const cfg = {
      type: "doughnut",
      data: { labels, datasets: [{ data, backgroundColor: colors, borderColor: getCSS("--panel"), borderWidth: 2 }] },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: "60%",
        plugins: {
          legend: { position: "right", labels: { boxWidth: 10, boxHeight: 10 } },
          tooltip: { callbacks: { label: ctx => `${ctx.label}: ${fmt(ctx.parsed)}` } },
        },
      },
    };
    upsertChart(id, cfg);
  }
}

// ---------- top projects ----------
async function loadProjects() {
  const j = await api("/api/by_project", { window: STATE.window, limit: 15 });
  const labels = j.rows.map(r => r.project);
  const data   = j.rows.map(r => r[STATE.projBy]);
  const fmt = STATE.projBy === "cost" ? fmtMoney : fmtCompact;
  const cfg = {
    type: "bar",
    data: { labels, datasets: [{
      label: STATE.projBy, data,
      backgroundColor: alpha(COLORS[0], .85), borderColor: COLORS[0], borderWidth: 0,
    }]},
    options: {
      indexAxis: "y", responsive: true, maintainAspectRatio: false,
      scales: {
        x: { grid: { color: "rgba(255,255,255,.04)" }, ticks: { callback: v => STATE.projBy === "cost" ? "$"+fmtCompact(v) : fmtCompact(v) } },
        y: { grid: { display: false } },
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: {
          title: ctx => j.rows[ctx[0].dataIndex].project_path || ctx[0].label,
          label: ctx => `${STATE.projBy}: ${fmt(ctx.parsed.x)}`,
        } },
      },
    },
  };
  upsertChart("chart-projects", cfg);
}

// ---------- distributions ----------
async function loadDistributions() {
  const j = await api("/api/distributions", { window: STATE.window });
  const hours = Array.from({length:24}, (_,h) => (j.hours.find(r=>r.hour===h)?.tokens) || 0);
  const dows  = Array.from({length:7},  (_,d) => (j.dows.find(r=>r.dow===d)?.tokens) || 0);
  const dowLabels = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
  const hourLabels = Array.from({length:24}, (_,h) => `${h.toString().padStart(2,"0")}:00`);
  upsertChart("chart-hours", {
    type: "bar",
    data: { labels: hourLabels, datasets: [{ label: "tokens", data: hours, backgroundColor: alpha(COLORS[1], .85), borderWidth: 0 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: { grid: { display: false } }, y: { grid: { color: "rgba(255,255,255,.04)" }, ticks: { callback: v => fmtCompact(v) } } },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => fmtCompact(ctx.parsed.y) + " tokens" } } },
    },
  });
  upsertChart("chart-dows", {
    type: "bar",
    data: { labels: dowLabels, datasets: [{ label: "tokens", data: dows, backgroundColor: alpha(COLORS[2], .85), borderWidth: 0 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: { grid: { display: false } }, y: { grid: { color: "rgba(255,255,255,.04)" }, ticks: { callback: v => fmtCompact(v) } } },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => fmtCompact(ctx.parsed.y) + " tokens" } } },
    },
  });
}

// ---------- top sessions ----------
async function loadTopSessions() {
  const j = await api("/api/top_sessions", { window: STATE.window, by: STATE.sessBy, limit: 20 });
  const tbody = document.querySelector("#sessions-table tbody");
  tbody.innerHTML = "";
  for (const r of j.rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><div class="project-cell"><span class="name">${escapeHtml(r.project || "—")}</span><span class="path">${escapeHtml(r.project_path || "")}</span></div></td>
      <td>${escapeHtml(r.model || "—")}</td>
      <td class="num">${fmtInt(r.msgs)}</td>
      <td class="num">${fmtCompact(r.tokens)}</td>
      <td class="num">${fmtMoney(r.cost)}</td>
      <td class="num">${fmtDur(r.duration_minutes)}</td>
      <td class="num">${fmtDate(r.first_ts)}</td>`;
    tbody.appendChild(tr);
  }
}

async function loadTopDays() {
  const j = await api("/api/top_days", { window: STATE.window, limit: 10 });
  const tbody = document.querySelector("#topdays-table tbody");
  tbody.innerHTML = "";
  for (const r of j.rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtDay(r.day)}</td>
      <td class="num">${fmtInt(r.msgs)}</td>
      <td class="num">${fmtInt(r.sessions)}</td>
      <td class="num">${fmtCompact(r.tokens)}</td>
      <td class="num">${fmtMoney(r.cost)}</td>`;
    tbody.appendChild(tr);
  }
}

async function loadFooter() {
  const m = await api("/api/meta");
  const last = m.last_index_at ? new Date(m.last_index_at).toLocaleString() : "never";
  document.getElementById("footer-meta").textContent = `${fmtInt(m.rows)} rows in DB · indexed ${last}`;
  const s = await api("/api/summary", { window: "all" });
  if (s.billing_coverage?.last) {
    document.getElementById("footer-billing").textContent = `billing covers ${s.billing_coverage.first} → ${s.billing_coverage.last}`;
  }
  // Reflect Gmail status on the button
  try {
    const g = await api("/api/gmail/status");
    const btn = document.getElementById("gmail-btn");
    if (g.connected) { btn.classList.add("connected"); btn.textContent = "✓ Gmail connected"; }
    else { btn.classList.remove("connected"); btn.textContent = "✉ Connect Gmail"; }
  } catch {}
}

function upsertChart(id, cfg) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), cfg);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

async function reloadAll() {
  await Promise.all([
    loadSummary(),
    loadTimeline(),
    loadHeatmap(),
    loadModels(),
    loadProjects(),
    loadDistributions(),
    loadTopSessions(),
    loadTopDays(),
    loadFooter(),
  ]);
}

// ---------- wiring ----------
function bindWindows() {
  document.getElementById("windows").addEventListener("click", e => {
    const b = e.target.closest("button[data-w]");
    if (!b) return;
    document.querySelectorAll("#windows button").forEach(x => x.classList.toggle("on", x === b));
    STATE.window = b.dataset.w;
    reloadAll();
  });
}
function bindProjSeg() {
  document.getElementById("proj-seg").addEventListener("click", e => {
    const b = e.target.closest("button[data-by]");
    if (!b) return;
    document.querySelectorAll("#proj-seg button").forEach(x => x.classList.toggle("on", x === b));
    STATE.projBy = b.dataset.by;
    loadProjects();
  });
}
function bindSessSeg() {
  document.getElementById("sess-seg").addEventListener("click", e => {
    const b = e.target.closest("button[data-by]");
    if (!b) return;
    document.querySelectorAll("#sess-seg button").forEach(x => x.classList.toggle("on", x === b));
    STATE.sessBy = b.dataset.by;
    loadTopSessions();
  });
}
function bindRefresh() {
  document.getElementById("refresh").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.textContent = "indexing…";
    try { await fetch("/refresh", { method: "POST" }); await reloadAll(); }
    finally { btn.disabled = false; btn.textContent = "↻ Refresh"; }
  });
}
function bindCostToggle() {
  document.getElementById("show-cost").addEventListener("change", e => {
    STATE.showCost = e.target.checked;
    loadTimeline();
  });
}

// ---------- Gmail dialog ----------
const SETUP_HTML = `
<p>This pulls your Anthropic receipts straight from Gmail and fills <code>billing.json</code> automatically. Manual entries are preserved.</p>
<p><b>One-time setup</b> (~5 minutes):</p>
<ol>
  <li>Go to the <a href="https://console.cloud.google.com/projectcreate" target="_blank">Google Cloud Console</a> and create a new project.</li>
  <li>Open <a href="https://console.cloud.google.com/apis/library/gmail.googleapis.com" target="_blank">Gmail API</a> and click <b>Enable</b>.</li>
  <li>Open <a href="https://console.cloud.google.com/apis/credentials/consent" target="_blank">OAuth consent screen</a>, pick <b>External</b>, fill the basics, add your Gmail as a <b>Test user</b>.</li>
  <li>Open <a href="https://console.cloud.google.com/apis/credentials" target="_blank">Credentials</a> → <b>Create credentials → OAuth client ID</b> → <b>Desktop app</b> → download the JSON.</li>
  <li>Save the downloaded file as <code>credentials.json</code> in the dashboard folder, then reopen this dialog.</li>
</ol>
<p class="muted">All tokens stay on your machine. Scope is read-only Gmail.</p>
<div class="row"><button class="secondary" data-close>OK, I'll do that</button></div>
`;

const CONNECT_HTML = `
<p>Click below to authorize this dashboard to read Anthropic receipts from your Gmail. A browser window will open for Google's consent.</p>
<p class="muted">Read-only scope · token saved locally to <code>token.json</code> · revoke any time at <a href="https://myaccount.google.com/permissions" target="_blank">myaccount.google.com/permissions</a>.</p>
<div class="row">
  <button class="primary" id="gmail-go">Authorize and scrape</button>
  <button class="secondary" data-close>Cancel</button>
</div>
<div id="gmail-result" style="margin-top:12px;"></div>
`;

async function openGmailDialog() {
  const dlg = document.getElementById("gmail-dialog");
  const body = document.getElementById("gmail-dlg-body");
  const title = document.getElementById("gmail-dlg-title");
  const status = await api("/api/gmail/status");
  if (!status.credentials_present) {
    title.textContent = "Set up Gmail integration";
    body.innerHTML = SETUP_HTML;
  } else {
    title.textContent = status.connected ? "Sync Gmail receipts" : "Connect Gmail";
    body.innerHTML = CONNECT_HTML;
    document.getElementById("gmail-go").addEventListener("click", runScrape);
  }
  dlg.showModal();
}

async function runScrape() {
  const btn = document.getElementById("gmail-go");
  const out = document.getElementById("gmail-result");
  btn.disabled = true; btn.textContent = "working...";
  out.innerHTML = `<span class="muted">Opening browser for consent (first time only)... then fetching receipts. This can take a minute.</span>`;
  try {
    const r = await fetch("/api/gmail/scrape", { method: "POST" });
    const j = await r.json();
    if (!r.ok) {
      out.innerHTML = `<span class="err">${escapeHtml(j.detail || j.error || "scrape failed")}</span>`;
    } else {
      out.innerHTML = `<span class="ok">✓ Found ${j.found} receipt(s) · added ${j.added}, updated ${j.updated} · billing.json now has ${j.total} entries.</span>`;
      await reloadAll();
    }
  } catch (e) {
    out.innerHTML = `<span class="err">${escapeHtml(String(e))}</span>`;
  } finally {
    btn.disabled = false; btn.textContent = "Authorize and scrape";
  }
}

function bindGmail() {
  document.getElementById("gmail-btn").addEventListener("click", openGmailDialog);
  document.getElementById("gmail-dialog").addEventListener("click", e => {
    if (e.target.matches("[data-close]") || e.target.id === "gmail-dialog") {
      document.getElementById("gmail-dialog").close();
    }
  });
}

bindWindows();
bindProjSeg();
bindSessSeg();
bindRefresh();
bindCostToggle();
bindGmail();

// Reflow heatmap on window resize (debounced)
let _resizeT;
window.addEventListener("resize", () => {
  clearTimeout(_resizeT);
  _resizeT = setTimeout(() => loadHeatmap(), 180);
});

// ---------- Export to PNG ----------
async function exportImage() {
  const btn = document.getElementById("export-btn");
  if (!window.html2canvas) {
    alert("Export library not loaded yet. Try again in a second.");
    return;
  }
  btn.disabled = true; const orig = btn.textContent; btn.textContent = "rendering…";
  // To preserve Chart.js canvas content we render the live DOM. Hide everything
  // we don't want in the export, then capture, then restore.
  const main = document.querySelector("main");
  const sections = Array.from(main.children);
  const keep = sections.slice(0, 3); // KPI row, Daily activity, Heatmap
  const hideEls = [
    document.querySelector("header.topbar"),
    document.querySelector(".footer"),
    ...sections.slice(3),
  ].filter(Boolean);
  const prev = hideEls.map(el => [el, el.style.display]);
  // Stamp banner that explains the export
  const banner = document.createElement("div");
  banner.id = "export-banner";
  banner.style.cssText = `display:flex;align-items:center;gap:12px;padding:18px 24px;border-bottom:1px solid ${getCSS('--border-soft')};margin:-18px -24px 18px;background:${getCSS('--panel')};`;
  banner.innerHTML = `
    <div style="width:32px;height:32px;border-radius:8px;background:linear-gradient(135deg, ${getCSS('--accent')}, ${getCSS('--accent-2')});box-shadow:0 4px 14px rgba(217,119,87,.28)"></div>
    <div style="display:flex;flex-direction:column;">
      <div style="font-size:18px;font-weight:600;letter-spacing:-0.01em;color:${getCSS('--text')};">Claude Usage · ${STATE.window}</div>
      <div style="font-size:12px;color:${getCSS('--muted')};">${escapeHtml(document.getElementById('meta-line').textContent)}</div>
    </div>
    <div style="margin-left:auto;font-size:11px;color:${getCSS('--muted')};text-align:right;">runs locally · no data leaves your machine<br/>generated ${new Date().toLocaleString()}</div>`;
  try {
    hideEls.forEach(el => el.style.display = "none");
    main.insertBefore(banner, main.firstChild);
    await new Promise(r => requestAnimationFrame(r));
    const canvas = await window.html2canvas(main, {
      backgroundColor: getCSS("--bg"),
      scale: 2, useCORS: true, logging: false, foreignObjectRendering: false,
    });
    const url = canvas.toDataURL("image/png");
    const a = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 10);
    a.href = url; a.download = `claude-usage-${STATE.window}-${stamp}.png`;
    document.body.appendChild(a); a.click(); a.remove();
  } catch (e) {
    console.error(e);
    alert("Export failed: " + (e.message || e));
  } finally {
    if (banner.parentNode) banner.parentNode.removeChild(banner);
    prev.forEach(([el, d]) => el.style.display = d);
    btn.disabled = false; btn.textContent = orig;
  }
}

document.getElementById("export-btn").addEventListener("click", exportImage);

reloadAll();
