"use strict";
/* PriceWatch UI: plain JS, no build step, no external requests. Every dynamic value goes through esc(). */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => (n == null ? "-" : "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: Math.abs(n % 1) > 0.004 ? 2 : 0, maximumFractionDigits: 2 }));
const nowS = () => Date.now() / 1000;
const ago = (ts) => {
  if (!ts) return "never";
  const s = Math.max(0, nowS() - ts);
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  if (s < 129600) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
};
const until = (ts) => {
  const s = ts - nowS();
  if (s <= 30) return "any moment";
  if (s < 5400) return "in " + Math.round(s / 60) + "m";
  return "in " + Math.round(s / 3600) + "h";
};
const srcLabel = (s) => (s.startsWith("custom:") ? s.slice(7) : { ebay: "eBay", craigslist: "Craigslist" }[s] || s);

async function api(path, opts = {}) {
  const r = await fetch("/api" + path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (e) { /* keep statusText */ }
    console.error(`PriceWatch: ${opts.method || "GET"} /api${path} failed (HTTP ${r.status}): ${msg}`);
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

let toastTimer;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), bad ? 6000 : 2600);
}
const guard = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };

/* ---------- router ---------- */
let refreshTimer, pollTimer, currentRoute = "";
const ds = { id: null, status: "active" };   // detail-page state that survives refreshes

async function route() {
  clearInterval(refreshTimer);
  const h = location.hash || "#/";
  const key = h.split("?")[0];
  $$("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.nav === (key.startsWith("#/monitor") ? "home" : key.slice(2) || "home")));
  const view = $("#view");
  const y = window.scrollY;
  try {
    let m;
    if ((m = key.match(/^#\/monitor\/(\d+)$/))) {
      const id = Number(m[1]);
      if (ds.id !== id) { ds.id = id; ds.status = "active"; }
      await viewMonitor(id);
      refreshTimer = setInterval(() => { if (!document.hidden && !$("#dlg").open && !isTyping()) route(); }, 30000);
    } else if (key === "#/check") await viewCheck();
    else if (key === "#/alerts") await viewAlerts();
    else if (key === "#/settings") await viewSettings();
    else {
      await viewDashboard();
      refreshTimer = setInterval(() => { if (!document.hidden && !$("#dlg").open) route(); }, 20000);
    }
    if (currentRoute === key) window.scrollTo(0, y); else window.scrollTo(0, 0);
    currentRoute = key;
  } catch (e) {
    view.innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
  }
}
const isTyping = () => ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
window.addEventListener("hashchange", route);

async function setBanner() {
  const s = await api("/status");
  const notes = [];
  if (!s.ebay_configured) notes.push(`<div class="note">eBay isn't connected yet. Add your free eBay API keys in <a href="#/settings">Settings</a> to search eBay (Craigslist and custom sites work without keys).</div>`);
  if (!s.channels.length) notes.push(`<div class="note">No notification channel is turned on, so deals will only show on the <a href="#/alerts">Alerts</a> page. Set one up in <a href="#/settings">Settings</a>.</div>`);
  if (!s.auth) notes.push(`<div class="note">No password is set (PRICEWATCH_PASSWORD). Fine on localhost; set one before exposing this to the internet.</div>`);
  $("#banner").innerHTML = notes.join("");
  return s;
}

/* ---------- shared pieces ---------- */
function tile(k, v, s = "", cls = "") {
  return `<div class="tile ${cls}"><div class="k">${esc(k)}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
}

function marketTiles(d, mon) {
  const m = d.market, s = d.suggested;
  if (!m) return `<div class="note">No priced listings yet.</div>`;
  const tiles = [
    tile("Going price", money(m.median), `typical ${money(m.p25)} to ${money(m.p75)}`),
    tile("Good deal", "≤ " + money(s.good_deal), "cheapest quarter", "good"),
    tile("Great deal", "≤ " + money(s.great_deal), "cheapest tenth", "good"),
    tile("Lowest real price", money(m.min), `${m.n} listings counted`),
  ];
  if (mon && mon.auto_target) tiles.push(tile("Your alert line", "≤ " + money(s.target), `cheapest ${mon.target_percentile}%`));
  else if (mon && mon.max_price != null) tiles.push(tile("Your alert line", "≤ " + money(mon.max_price), "max price"));
  let extra = "";
  if (m.confidence === "low") extra += `<div class="note">Still learning this market: only ${m.n} usable listing${m.n === 1 ? "" : "s"} so far, so the numbers below are rough and automatic deal alerts start at 8.</div>`;
  if (m.removed) extra += `<p class="hint">${m.removed} listing${m.removed === 1 ? " was" : "s were"} left out of these stats as outliers (accessories, parts, bait or absurd asking prices).</p>`;
  return `<div class="tiles">${tiles.join("")}</div>${extra}`;
}

function histSvg(hist, s) {
  if (!hist.length) return `<p class="muted">No data yet.</p>`;
  const W = 640, H = 230, L = 28, R = 12, T = 50, B = 30;
  const lo = hist[0].lo, hi = hist[hist.length - 1].hi || lo + 1, span = hi - lo || 1;
  const maxN = Math.max(...hist.map((b) => b.n));
  const x = (v) => L + ((v - lo) / span) * (W - L - R);
  const y = (n) => H - B - (n / maxN) * (H - T - B);
  const bw = hist.length === 1 ? 30 : null;
  let g = "";
  hist.forEach((b) => {
    const x0 = hist.length === 1 ? x(b.lo) - 15 : x(b.lo) + 1;
    const w = hist.length === 1 ? 30 : Math.max(1, x(b.hi) - x(b.lo) - 2);
    g += `<rect class="bar" x="${x0.toFixed(1)}" y="${y(b.n).toFixed(1)}" width="${w.toFixed(1)}" height="${(H - B - y(b.n)).toFixed(1)}" rx="2"><title>${esc(money(b.lo))} to ${esc(money(b.hi))}: ${b.n} listing${b.n === 1 ? "" : "s"}</title></rect>`;
  });
  const mark = (v, color, label, row) => {
    if (v == null || v < lo || v > hi) return "";
    const px = x(v), ty = 12 + row * 13;
    return `<line x1="${px}" x2="${px}" y1="${ty + 3}" y2="${H - B}" stroke="${color}" stroke-width="2" stroke-dasharray="${row ? "4 3" : "0"}"/>` +
      `<text x="${Math.min(Math.max(px, 60), W - 60)}" y="${ty}" text-anchor="middle" style="fill:${color};font-weight:600">${esc(label)} ${esc(money(v))}</text>`;
  };
  let ticks = "";
  for (let i = 0; i <= 4; i++) {
    const v = lo + (span * i) / 4;
    ticks += `<text x="${x(v)}" y="${H - 10}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${esc(money(Math.round(v)))}</text>`;
  }
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Price distribution">
    <line x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}" stroke="var(--line)"/>
    ${g}${mark(s.going_price, "var(--blue)", "Going", 0)}${mark(s.good_deal, "var(--accent)", "Good", 1)}${mark(s.great_deal, "var(--purple)", "Great", 2)}${ticks}
  </svg></div>
  <div class="legend"><span><i style="background:var(--blue)"></i>going price (median)</span><span><i style="background:var(--accent)"></i>good deal (cheapest 25%)</span><span><i style="background:var(--purple)"></i>great deal (cheapest 10%)</span></div>`;
}

function trendSvg(points) {
  if (points.length < 2) return `<p class="muted">The price trend appears after a few runs. Each run adds a point.</p>`;
  const W = 640, H = 220, L = 52, R = 12, T = 12, B = 28;
  const t0 = points[0].ts, t1 = points[points.length - 1].ts, ts = t1 - t0 || 1;
  const vals = points.flatMap((p) => [p.median, p.p25, p.min]).filter((v) => v != null);
  let vmin = Math.min(...vals), vmax = Math.max(...vals);
  if (vmax === vmin) { vmax += 1; vmin -= 1; }
  const pad = (vmax - vmin) * 0.08; vmin -= pad; vmax += pad;
  const x = (t) => L + ((t - t0) / ts) * (W - L - R);
  const y = (v) => T + (1 - (v - vmin) / (vmax - vmin)) * (H - T - B);
  const line = (k, color, dash) => `<polyline fill="none" stroke="${color}" stroke-width="2" ${dash ? 'stroke-dasharray="5 4"' : ""} points="${points.filter((p) => p[k] != null).map((p) => `${x(p.ts).toFixed(1)},${y(p[k]).toFixed(1)}`).join(" ")}"/>`;
  const fmt = (t) => new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Price trend over time">
    ${[0, 0.5, 1].map((f) => { const v = vmin + (vmax - vmin) * f; return `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line)"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${esc(money(Math.round(v)))}</text>`; }).join("")}
    ${line("median", "var(--blue)")}${line("p25", "var(--accent)", true)}${line("min", "var(--muted)", true)}
    <text x="${L}" y="${H - 8}">${esc(fmt(t0))}</text><text x="${W - R}" y="${H - 8}" text-anchor="end">${esc(fmt(t1))}</text>
  </svg></div>
  <div class="legend"><span><i style="background:var(--blue)"></i>going price</span><span><i style="background:var(--accent)"></i>good-deal line</span><span><i style="background:var(--muted)"></i>lowest</span></div>`;
}

function bySourceHtml(rows) {
  if (!rows.length) return `<p class="muted">No data yet.</p>`;
  return `<div class="tablewrap"><table><thead><tr><th>Source</th><th class="num">Listings</th><th class="num">Going price</th><th class="num">Lowest</th></tr></thead><tbody>` +
    rows.map((r) => `<tr><td>${esc(srcLabel(r.source))}</td><td class="num">${r.n}</td><td class="num">${money(r.median)}</td><td class="num">${money(r.min)}</td></tr>`).join("") + `</tbody></table></div>`;
}

const FLAGS = {
  outlier_low: ["bad", "likely parts or bait"],
  suspicious: ["warn", "too good? verify"],
  below_range: ["good", "far below normal"],
  outlier_high: ["warn", "overpriced"],
};

function mountListings(container, listings, { statusSel = false, onStatus } = {}) {
  const st = { q: "", source: "", sort: "price", shown: 100 };
  const sources = [...new Set(listings.map((l) => l.source))];
  container.innerHTML = `
    <div class="row" style="margin-bottom:10px">
      <input data-k="q" type="search" placeholder="Filter titles…" style="flex:1;min-width:160px">
      <select data-k="source" style="width:auto"><option value="">All sources</option>${sources.map((s) => `<option value="${esc(s)}">${esc(srcLabel(s))}</option>`).join("")}</select>
      <select data-k="sort" style="width:auto"><option value="price">Cheapest first</option><option value="deal">Best deal first</option><option value="new">Newest first</option></select>
      ${statusSel ? `<select data-k="status" style="width:auto"><option value="active">Active now</option><option value="gone">Gone (sold or removed)</option><option value="all">Everything</option></select>` : ""}
    </div>
    <div class="tablewrap"><table><thead><tr><th></th><th>Listing</th><th class="num">Price</th><th>Deal</th><th>Source</th><th>Seen</th></tr></thead><tbody></tbody></table></div>
    <div class="row" style="margin-top:10px;justify-content:space-between"><span class="muted small" data-k="count"></span><button class="btn sm" data-k="more" hidden>Show more</button></div>`;
  if (statusSel) $('[data-k="status"]', container).value = ds.status;
  const body = $("tbody", container);
  const draw = () => {
    let rows = listings.filter((l) => (!st.source || l.source === st.source) && (!st.q || l.title.toLowerCase().includes(st.q)));
    if (st.sort === "deal") rows = [...rows].sort((a, b) => (b.deal_pct ?? -1e9) - (a.deal_pct ?? -1e9));
    if (st.sort === "new") rows = [...rows].sort((a, b) => (b.first_seen || 0) - (a.first_seen || 0));
    body.innerHTML = rows.slice(0, st.shown).map((l) => {
      const f = FLAGS[l.flag];
      let deal = `<span class="muted">-</span>`;
      if (l.flag !== "outlier_low" && l.deal_pct != null) {
        if (l.deal_pct >= 5) deal = `<span class="badge good">${Math.round(l.deal_pct)}% under</span>`;
        else if (l.deal_pct <= -5) deal = `<span class="badge bad">${Math.round(-l.deal_pct)}% over</span>`;
        else deal = `<span class="muted">about average</span>`;
      }
      if (f) deal += ` <span class="badge ${f[0]}">${f[1]}</span>`;
      const sub = [];
      if (l.shipping) sub.push(`${money(l.price)} + ${money(l.shipping)} ship`);
      if (l.first_total && l.first_total > l.total * 1.005) sub.push(`↓ was ${money(l.first_total)}`);
      const seen = l.first_seen ? (l.active === 0 ? `gone ${ago(l.gone_at)}` : (nowS() - l.first_seen < 86400 ? `<span class="badge new">NEW</span> ` : "") + ago(l.first_seen)) : "";
      return `<tr><td>${l.image ? `<img class="thumb" loading="lazy" referrerpolicy="no-referrer" src="${esc(l.image)}" alt="">` : `<div class="thumb"></div>`}</td>
        <td><a class="ltitle" href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.title)}</a><div class="muted small">${esc(l.location || "")}${l.condition ? " · " + esc(l.condition) : ""}</div></td>
        <td class="num"><b>${money(l.total)}</b><div class="muted small">${sub.map(esc).join("<br>")}</div></td><td>${deal}</td>
        <td>${esc(srcLabel(l.source))}</td><td class="small">${seen}</td></tr>`;
    }).join("") || `<tr><td colspan="6" class="muted">Nothing matches.</td></tr>`;
    $('[data-k="count"]', container).textContent = `Showing ${Math.min(st.shown, rows.length)} of ${rows.length}`;
    $('[data-k="more"]', container).hidden = rows.length <= st.shown;
  };
  container.addEventListener("input", (e) => {
    const k = e.target.dataset.k;
    if (k === "q") st.q = e.target.value.toLowerCase();
    else if (k === "source" || k === "sort") st[k] = e.target.value;
    else return;
    st.shown = 100; draw();
  });
  container.addEventListener("change", (e) => { if (e.target.dataset.k === "status") { ds.status = e.target.value; onStatus && onStatus(); } });
  container.addEventListener("click", (e) => { if (e.target.dataset.k === "more") { st.shown += 200; draw(); } });
  draw();
}

/* ---------- source editor (used by the monitor dialog and Price check) ---------- */
const CL_CATS = [["sss", "All for sale"], ["ela", "Electronics"], ["sya", "Computers"], ["vga", "Video games"], ["cta", "Cars & trucks"], ["bia", "Bicycles"], ["fua", "Furniture"], ["tla", "Tools"], ["sga", "Sporting goods"], ["msa", "Musical instruments"], ["ppa", "Appliances"], ["pta", "Auto parts"], ["jwa", "Jewelry"]];
const opt = (v, label, cur) => `<option value="${esc(v)}"${v === cur ? " selected" : ""}>${esc(label)}</option>`;

function customBlock(c = {}) {
  const f = (k, label, ph = "") => `<label class="f">${label}<input data-f="${k}" value="${esc(c[k] || "")}" placeholder="${esc(ph)}"></label>`;
  return `<div class="custom"><div class="row" style="justify-content:space-between"><b>Custom site</b><button type="button" class="btn sm danger" data-act="rm-custom">Remove</button></div>
    <div class="cols">${f("name", "Name", "My local shop")}${f("url", "Search URL (use {query})", "https://example.com/search?q={query}")}</div>
    <div class="cols">${f("item_selector", "Result item (CSS)", "div.product")}${f("title_selector", "Title (CSS)", "h2")}${f("price_selector", "Price (CSS)", ".price")}</div>
    <div class="cols">${f("link_selector", "Link (CSS, optional)", "a")}${f("image_selector", "Image (CSS, optional)", "img")}
      <label class="f">Page type<select data-f="render">${opt("http", "Normal page", c.render)}${opt("browser", "Needs JavaScript (browser)", c.render)}</select></label></div></div>`;
}

function sourcesEditorHtml(sources = [{ type: "ebay" }]) {
  const eb = sources.find((s) => s.type === "ebay"), cl = sources.find((s) => s.type === "craigslist");
  const gens = sources.filter((s) => s.type === "generic");
  return `<fieldset data-src="ebay"><legend>Where to look</legend>
    <label class="check"><input type="checkbox" data-s="ebay" ${eb ? "checked" : ""}> <b>eBay</b> <span class="hint">official API, needs keys in Settings</span></label>
    <div class="cols" style="margin:8px 0 12px 26px"><label class="f">Condition<select data-s="ebay_condition">${opt("any", "Any", eb?.condition)}${opt("new", "New", eb?.condition)}${opt("used", "Used", eb?.condition)}</select></label>
      <label class="f">Listing type<select data-s="ebay_buying">${opt("fixed", "Buy it now (best for pricing)", eb?.buying)}${opt("any", "Buy it now + auctions", eb?.buying)}${opt("auction", "Auctions only", eb?.buying)}</select></label></div>
    <label class="check"><input type="checkbox" data-s="cl" ${cl ? "checked" : ""}> <b>Craigslist</b> <span class="hint">reads the public search page, politely</span></label>
    <div class="cols" style="margin:8px 0 12px 26px"><label class="f">Regions (subdomains, comma separated)<input data-s="cl_regions" value="${esc((cl?.regions || []).join(", "))}" placeholder="tampa, orlando"></label>
      <label class="f">Category<select data-s="cl_category">${CL_CATS.map(([v, l]) => opt(v, l, cl?.category || "sss")).join("")}</select></label></div>
    <div><b>Other sites</b> <span class="hint">any site whose results are in the page HTML</span></div>
    <div data-s="customs">${gens.map(customBlock).join("")}</div>
    <button type="button" class="btn sm" data-act="add-custom" style="margin-top:8px">+ Add a custom site</button></fieldset>`;
}

function readSources(root) {
  const g = (k) => $(`[data-s="${k}"]`, root);
  const out = [];
  if (g("ebay").checked) out.push({ type: "ebay", condition: g("ebay_condition").value, buying: g("ebay_buying").value });
  if (g("cl").checked) out.push({ type: "craigslist", regions: g("cl_regions").value, category: g("cl_category").value });
  $$(".custom", root).forEach((b) => {
    const o = { type: "generic" };
    $$("[data-f]", b).forEach((i) => (o[i.dataset.f] = i.value.trim()));
    out.push(o);
  });
  return out;
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("[data-act]");
  if (!a) return;
  if (a.dataset.act === "add-custom") {
    const box = $('[data-s="customs"]', a.closest("form") || document);
    box.insertAdjacentHTML("beforeend", customBlock());
  } else if (a.dataset.act === "rm-custom") a.closest(".custom").remove();
});

/* ---------- dashboard ---------- */
function monCard(m) {
  const snap = m.snapshot;
  const state = m.running ? "running" : !m.enabled ? "" : m.last_status === "ok" ? "ok" : m.last_status || "";
  const statusText = m.running ? "Running now…" : !m.enabled ? "Paused" : m.last_run_at ? `Last run ${ago(m.last_run_at)}, next ${until(m.next_run_at)}` : "Waiting for first run";
  const chips = m.sources.map((s) => `<span class="chip">${s.type === "ebay" ? "eBay" : s.type === "craigslist" ? "Craigslist: " + esc(s.regions.join(", ")) : esc(s.name)}</span>`).join("");
  const goal = m.auto_target ? `auto: cheapest ${m.target_percentile}%` : "";
  const lim = m.max_price != null ? `≤ ${money(m.max_price)}` : "";
  return `<article class="card" data-id="${m.id}">
    <div class="mon-head"><a class="mon-name" href="#/monitor/${m.id}">${esc(m.name)}</a><span class="dot ${state}" title="${esc(m.last_error || statusText)}"></span></div>
    <div class="muted">${esc(m.query)}</div><div class="chips">${chips}${[goal, lim].filter(Boolean).map((t) => `<span class="chip">alert ${esc(t)}</span>`).join("")}</div>
    <div class="tiles">${tile("Going price", snap ? money(snap.median) : "-", snap ? `${snap.n} counted` : "no data yet")}${tile("Best now", snap ? money(snap.min) : "-", snap ? `good deal ≤ ${money(snap.p25)}` : "", "good")}${tile("New today", m.new_24h, `${m.alerts_24h} alert${m.alerts_24h === 1 ? "" : "s"}`)}</div>
    <div class="small ${m.last_status === "error" ? "pill-bad" : "muted"}">${esc(statusText)}${m.last_error ? " · " + esc(m.last_error.slice(0, 140)) : ""}</div>
    <div class="actions"><a class="btn sm primary" href="#/monitor/${m.id}">Open</a><button class="btn sm" data-a="run">Run now</button><button class="btn sm" data-a="toggle">${m.enabled ? "Pause" : "Resume"}</button><button class="btn sm" data-a="edit">Edit</button><button class="btn sm danger" data-a="delete">Delete</button></div></article>`;
}

let monsCache = [];
async function viewDashboard() {
  const [mons] = await Promise.all([api("/monitors"), setBanner()]);
  monsCache = mons;
  $("#view").innerHTML = `<div class="pagehead"><div><h1>Monitors</h1><div class="sub">Each monitor searches your marketplaces on a schedule and alerts you when something is a real deal.</div></div>
    <div class="row"><a class="btn" href="#/check">Price check</a><button class="btn primary" id="new-mon">+ New monitor</button></div></div>` +
    (mons.length ? `<div class="grid">${mons.map(monCard).join("")}</div>` :
      `<div class="card empty"><h2>No monitors yet</h2><p>Create one to start tracking prices. Want to know what something is worth first? Try a <a href="#/check">price check</a>.</p><button class="btn primary" id="new-mon-2">+ New monitor</button></div>`);
  $("#new-mon, #new-mon-2")?.addEventListener("click", () => openEditor());
  $("#new-mon-2")?.addEventListener("click", () => openEditor());
  if (mons.some((m) => m.running)) pollSoon();
}
function pollSoon() { clearTimeout(pollTimer); pollTimer = setTimeout(() => { if (!$("#dlg").open) route(); }, 2500); }

document.addEventListener("click", guard(async (e) => {
  const b = e.target.closest("button[data-a]");
  if (!b) return;
  const id = Number(b.closest("[data-id]")?.dataset.id || ds.id);
  const act = b.dataset.a;
  if (act === "run") { const r = await api(`/monitors/${id}/run`, { method: "POST" }); toast(r.started ? "Run started" : "Already running"); setTimeout(route, 600); }
  else if (act === "toggle") { await api(`/monitors/${id}/toggle`, { method: "POST" }); route(); }
  else if (act === "edit") { const m = monsCache.find((x) => x.id === id) || (await api(`/monitors/${id}`)).monitor; openEditor(m); }
  else if (act === "delete") {
    const m = monsCache.find((x) => x.id === id);
    if (confirm(`Delete "${m ? m.name : "this monitor"}" and all its history? This can't be undone.`)) {
      await api(`/monitors/${id}`, { method: "DELETE" }); toast("Deleted");
      if (location.hash.startsWith("#/monitor")) location.hash = "#/"; else route();
    }
  }
}));

/* ---------- monitor editor ---------- */
function openEditor(m = null, prefill = null) {
  const v = m || prefill || {};
  const dlg = $("#dlg");
  const n = (x, d = "") => (x == null ? d : x);
  dlg.innerHTML = `<form class="form" id="mon-form" method="dialog" novalidate>
    <div class="dlg-head"><h2 style="margin:0">${m ? "Edit monitor" : "New monitor"}</h2><button type="button" class="btn sm" id="dlg-x">Close</button></div>
    <div class="dlg-body form">
      <label class="f">Name<input name="name" value="${esc(n(v.name))}" placeholder="optional, defaults to the search"></label>
      <label class="f">What are you looking for?<input name="query" required value="${esc(n(v.query))}" placeholder='e.g. rtx 3080 -laptop  (use "quotes" for phrases, -word to exclude)'></label>
      ${sourcesEditorHtml(v.sources || [{ type: "ebay" }])}
      <fieldset><legend>Alert me when (any of these is true)</legend><div class="form">
        <label class="check"><input type="checkbox" name="auto_target" ${v.auto_target === false ? "" : "checked"}> Price is in the cheapest <input name="target_percentile" type="number" min="1" max="50" step="1" value="${n(v.target_percentile, 10)}" style="width:70px"> % of the market <span class="hint">(learns the going price automatically)</span></label>
        <div class="cols"><label class="f">Price at or under ($)<input name="max_price" type="number" min="0" step="any" value="${n(v.max_price)}" placeholder="optional"></label>
          <label class="f">At least this % below going price<input name="min_discount_pct" type="number" min="1" max="99" step="any" value="${n(v.min_discount_pct)}" placeholder="optional"></label></div></div></fieldset>
      <fieldset><legend>Filters</legend><div class="form"><div class="cols">
        <label class="f">Ignore listings under ($)<input name="min_price" type="number" min="0" step="any" value="${n(v.min_price)}" placeholder="e.g. 50 to skip accessories"></label>
        <label class="f">Title must also contain<input name="include_words" value="${esc((v.include_words || []).join(", "))}" placeholder="12gb, founders"></label>
        <label class="f">Title must not contain<input name="exclude_words" value="${esc((v.exclude_words || []).join(", "))}" placeholder="case, box only"></label></div>
        <label class="check"><input type="checkbox" name="require_all_terms" ${v.require_all_terms === false ? "" : "checked"}> Every search word must appear in the title <span class="hint">(keeps loose matches out of your stats)</span></label>
        <label class="check"><input type="checkbox" name="skip_junk" ${v.skip_junk === false ? "" : "checked"}> Skip "for parts", "broken", "untested" and similar</label></div></fieldset>
      <div class="cols"><label class="f">Check every (minutes)<input name="interval_minutes" type="number" min="10" max="1440" value="${n(v.interval_minutes, 30)}"></label>
        <label class="f">Max deals per notification<input name="max_alerts_per_run" type="number" min="1" max="20" value="${n(v.max_alerts_per_run, 5)}"></label></div>
      <div class="err" id="mon-err"></div>
    </div>
    <div class="dlg-foot"><button type="button" class="btn" id="dlg-cancel">Cancel</button><button class="btn primary" id="mon-save" type="submit">${m ? "Save changes" : "Create monitor"}</button></div></form>`;
  dlg.showModal();
  const close = () => dlg.close();
  $("#dlg-x").onclick = close; $("#dlg-cancel").onclick = close;
  $("#mon-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const f = ev.target.elements, num = (k) => (f[k].value.trim() === "" ? null : Number(f[k].value));
    const body = {
      name: f.name.value.trim(), query: f.query.value.trim(), sources: readSources(ev.target),
      min_price: num("min_price"), max_price: num("max_price"), min_discount_pct: num("min_discount_pct"),
      auto_target: f.auto_target.checked, target_percentile: Number(f.target_percentile.value || 10),
      include_words: f.include_words.value, exclude_words: f.exclude_words.value,
      require_all_terms: f.require_all_terms.checked, skip_junk: f.skip_junk.checked,
      interval_minutes: Number(f.interval_minutes.value || 30), max_alerts_per_run: Number(f.max_alerts_per_run.value || 5),
      enabled: m ? m.enabled : true, notify_channels: m ? m.notify_channels : null,
    };
    const bad = checkSources(body);
    if (bad) { $("#mon-err").textContent = bad; return; }
    $("#mon-save").disabled = true;
    try {
      const saved = m ? await api(`/monitors/${m.id}`, { method: "PUT", body }) : await api("/monitors", { method: "POST", body });
      close(); toast(m ? "Saved" : "Monitor created, first check is starting");
      if (!m) { await api(`/monitors/${saved.id}/run`, { method: "POST" }).catch(() => {}); location.hash = `#/monitor/${saved.id}`; }
      route();
    } catch (err) { $("#mon-err").textContent = err.message; $("#mon-save").disabled = false; }
  };
}

/* ---------- monitor detail ---------- */
async function viewMonitor(id) {
  const [d] = await Promise.all([api(`/monitors/${id}?status=${ds.status}`), setBanner()]);
  const m = d.monitor;
  monsCache = [m];
  const state = m.running ? "running" : !m.enabled ? "" : m.last_status === "ok" ? "ok" : m.last_status || "";
  const sold = d.sold_estimate;
  $("#view").innerHTML = `<div data-id="${m.id}">
    <div class="pagehead"><div><div class="small"><a href="#/">← All monitors</a></div><h1 style="margin-top:4px"><span class="dot ${state}" style="display:inline-block;margin-right:8px"></span>${esc(m.name)}</h1>
      <div class="sub">${esc(m.query)} · ${m.sources.map((s) => (s.type === "generic" ? esc(s.name) : s.type === "ebay" ? "eBay" : "Craigslist " + esc(s.regions.join(", ")))).join(" · ")}</div></div>
      <div class="row"><button class="btn primary" data-a="run">Run now</button><button class="btn" data-a="toggle">${m.enabled ? "Pause" : "Resume"}</button><button class="btn" data-a="edit">Edit</button><button class="btn danger" data-a="delete">Delete</button></div></div>
    <div class="muted small" style="margin-bottom:12px">${m.running ? "Running now…" : !m.enabled ? "Paused" : `Last run ${ago(m.last_run_at)} · next ${until(m.next_run_at)}`}${m.last_error ? ` · <span class="pill-bad">${esc(m.last_error)}</span>` : ""}</div>
    <div class="stack">
      <div class="card sec"><h2>Market</h2>${marketTiles(d, m)}</div>
      <div class="two"><div class="card"><h2>Price distribution</h2>${d.suggested ? histSvg(d.histogram, d.suggested) : `<p class="muted">No data yet.</p>`}</div>
        <div class="card"><h2>Going price over time</h2>${trendSvg(d.trend)}</div></div>
      <div class="two"><div class="card"><h2>By source</h2>${bySourceHtml(d.by_source)}</div>
        <div class="card"><h2>What things actually sell for</h2>${sold ? `<dl class="kv"><dt>Estimated sale price</dt><dd><b>${money(sold.median)}</b> (lowest quarter ${money(sold.p25)})</dd><dt>Listings gone (60 days)</dt><dd>${sold.n}</dd><dt>Average time listed</dt><dd>${sold.avg_days_listed ?? "-"} days</dd></dl><p class="hint">Estimated from listings that disappeared at their last seen price. A listing can vanish because it sold or because it was removed.</p>` : `<p class="muted">Needs a few listings to disappear first. PriceWatch learns this as it keeps watching.</p>`}</div></div>
      <div class="card"><h2>Listings</h2><div id="listings"></div></div>
      <div class="two"><div class="card"><h2>Recent alerts</h2>${alertsList(d.alerts) || `<p class="muted">No alerts yet.</p>`}</div>
        <div class="card"><h2>Run log</h2>${runsList(d.runs)}</div></div>
    </div></div>`;
  mountListings($("#listings"), d.listings, { statusSel: true, onStatus: route });
  if (m.running) pollSoon();
}
const alertsList = (rows) => rows.length ? `<div class="tablewrap"><table><tbody>${rows.map((a) => `<tr><td class="small" style="white-space:nowrap">${ago(a.ts)}</td><td class="num"><b>${money(a.total)}</b></td><td><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a><div class="muted small">${esc(srcLabel(a.source))} · ${esc(a.reason || "")}</div></td></tr>`).join("")}</tbody></table></div>` : "";
const runsList = (rows) => rows.length ? `<div class="tablewrap"><table><tbody>${rows.map((r) => `<tr><td class="small" style="white-space:nowrap">${ago(r.ts)}</td><td>${esc(srcLabel(r.source))}</td><td class="${r.ok ? "pill-ok" : "pill-bad"}">${r.ok ? r.count + " listings" : "failed"}</td><td class="small muted">${esc(r.error || "")}</td></tr>`).join("")}</tbody></table></div>` : `<p class="muted">No runs yet.</p>`;

/* Catch the common mistakes in the browser, with a message that says what to fix. */
function checkSources(form) {
  if (!form.query) return "Enter what you're looking for.";
  if (!form.sources.length) return "Tick at least one place to look (eBay, Craigslist or a custom site).";
  for (const s of form.sources) {
    if (s.type === "craigslist" && !String(s.regions).trim()) return "Craigslist is ticked but no region is entered. Add one (like “tampa”) or untick Craigslist.";
    if (s.type === "generic") {
      if (!s.name) return "A custom site needs a name.";
      if (!s.url.includes("{query}")) return `Custom site “${s.name}”: the search URL must contain {query}.`;
      if (!s.item_selector || !s.title_selector || !s.price_selector) return `Custom site “${s.name}”: fill in the result item, title and price selectors.`;
    }
  }
  return "";
}

/* ---------- price check ---------- */
let lastCheck = null;
async function viewCheck() {
  await setBanner();
  const c = lastCheck?.form || {};
  $("#view").innerHTML = `<div class="pagehead"><div><h1>Price check</h1><div class="sub">Find out what something goes for right now, and what a good price looks like, without creating a monitor.</div></div></div>
    <form class="card form" id="check-form" novalidate>
      <label class="f">What are you looking for?<input name="query" required value="${esc(c.query || "")}" placeholder='e.g. iphone 14 pro 256gb -case'></label>
      ${sourcesEditorHtml(c.sources || [{ type: "ebay" }])}
      <div class="cols"><label class="f">Ignore listings under ($)<input name="min_price" type="number" min="0" step="any" value="${esc(c.min_price ?? "")}"></label>
        <label class="check" style="align-self:end"><input type="checkbox" name="skip_junk" ${c.skip_junk === false ? "" : "checked"}> Skip parts / broken / untested</label></div>
      <div class="err" id="check-err" role="alert"></div>
      <div class="row"><button class="btn primary" id="check-go" type="submit">Check market</button><span class="muted small" id="check-msg"></span></div>
    </form><div id="check-out" class="stack" style="margin-top:14px"></div>`;
  if (lastCheck) renderCheck(lastCheck.data);
  $("#check-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const f = ev.target.elements;
    const form = { query: f.query.value.trim(), sources: readSources(ev.target), skip_junk: f.skip_junk.checked,
      min_price: f.min_price.value === "" ? null : Number(f.min_price.value), name: f.query.value.trim() };
    const bad = checkSources(form);
    $("#check-err").textContent = bad || "";
    if (bad) return;
    $("#check-go").disabled = true; $("#check-msg").textContent = "Searching… this can take up to a minute.";
    try { const data = await api("/analyze", { method: "POST", body: form }); lastCheck = { form, data }; renderCheck(data); $("#check-msg").textContent = ""; }
    catch (e) { $("#check-msg").textContent = ""; $("#check-err").textContent = e.message; }
    $("#check-go").disabled = false;
  };
}
function renderCheck(d) {
  const f = lastCheck.form;
  $("#check-out").innerHTML = (d.errors || []).map((e) => `<div class="note">${esc(e)}</div>`).join("") +
    `<div class="card sec"><h2>Market for “${esc(f.query)}” <button class="btn sm primary" id="save-mon" type="button">Turn this into a monitor</button></h2>${marketTiles(d, null)}</div>
     <div class="two"><div class="card"><h2>Price distribution</h2>${d.suggested ? histSvg(d.histogram, d.suggested) : `<p class="muted">No data.</p>`}</div><div class="card"><h2>By source</h2>${bySourceHtml(d.by_source)}</div></div>
     <div class="card"><h2>Listings</h2><div id="check-list"></div></div>`;
  mountListings($("#check-list"), d.listings);
  $("#save-mon").onclick = () => openEditor(null, { ...f, name: "", target_percentile: 10 });
}

/* ---------- alerts ---------- */
async function viewAlerts() {
  const [rows] = await Promise.all([api("/alerts"), setBanner()]);
  $("#view").innerHTML = `<div class="pagehead"><div><h1>Alerts</h1><div class="sub">Every deal PriceWatch has flagged, newest first.</div></div></div>` +
    (rows.length ? `<div class="card tablewrap"><table><thead><tr><th>When</th><th>Monitor</th><th class="num">Price</th><th>Listing</th><th>Sent via</th></tr></thead><tbody>${rows.map((a) =>
      `<tr><td class="small" style="white-space:nowrap">${ago(a.ts)}</td><td><a href="#/monitor/${a.monitor_id}">${esc(a.monitor_name)}</a></td><td class="num"><b>${money(a.total)}</b><div class="muted small">going ${money(a.median)}</div></td>
      <td><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a><div class="muted small">${esc(srcLabel(a.source))} · ${esc(a.reason || "")}</div></td>
      <td class="small">${Object.entries(a.channels).map(([k, v]) => `<span class="${v === "ok" ? "pill-ok" : "pill-bad"}">${esc(k)}</span>`).join(", ") || '<span class="muted">dashboard only</span>'}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="card empty"><h2>No alerts yet</h2><p>When a listing hits one of your alert rules it shows up here.</p></div>`);
}

/* ---------- settings ---------- */
const NOTIFY = [
  ["ntfy", "ntfy (free push to your phone)", [["server", "Server"], ["topic", "Topic (make it long and unguessable)"], ["token", "Access token (optional)", "password"], ["priority", "Priority", "select", ["min", "low", "default", "high", "urgent"]]]],
  ["discord", "Discord", [["webhook_url", "Webhook URL", "password"]]],
  ["slack", "Slack", [["webhook_url", "Incoming webhook URL", "password"]]],
  ["telegram", "Telegram", [["bot_token", "Bot token", "password"], ["chat_id", "Chat ID"]]],
  ["pushover", "Pushover", [["api_token", "API token", "password"], ["user_key", "User key", "password"]]],
  ["email", "Email (SMTP)", [["host", "SMTP host"], ["port", "Port", "number"], ["security", "Security", "select", ["starttls", "ssl", "none"]], ["username", "Username"], ["password", "Password", "password"], ["from_addr", "From address"], ["to_addr", "Send to"]]],
  ["webhook", "Generic webhook (JSON POST)", [["webhook_url", "URL", "password"]]],
];
function fieldHtml(path, label, type, val, optsList) {
  if (type === "select") return `<label class="f">${esc(label)}<select data-path="${path}">${optsList.map((o) => opt(o, o, val)).join("")}</select></label>`;
  return `<label class="f">${esc(label)}<input data-path="${path}" type="${type === "password" ? "password" : type === "number" ? "number" : "text"}" value="${esc(val)}" autocomplete="off"></label>`;
}
async function viewSettings() {
  const [s] = await Promise.all([api("/settings"), setBanner()]);
  const ch = NOTIFY.map(([key, title, fields]) => `<div class="card"><div class="row" style="justify-content:space-between"><label class="check"><input type="checkbox" data-path="notify.${key}.enabled" ${s.notify[key].enabled ? "checked" : ""}> <b>${esc(title)}</b></label>
    <button class="btn sm" data-test="${key}">Send test</button></div><div class="cols" style="margin-top:10px">${fields.map(([k, l, t = "text", o]) => fieldHtml(`notify.${key}.${k}`, l, t, s.notify[key][k], o)).join("")}</div></div>`).join("");
  $("#view").innerHTML = `<div class="pagehead"><div><h1>Settings</h1><div class="sub">Keys and passwords are stored on your server and never shown again after saving.</div></div><button class="btn primary" id="save-set">Save settings</button></div>
    <div class="stack">
      <div class="card"><h2>eBay API</h2><p class="muted small" style="margin-top:-6px">Free: create a keyset at <a href="https://developer.ebay.com/" target="_blank" rel="noopener noreferrer">developer.ebay.com</a>, then copy the <b>Production</b> App ID (Client ID) and Cert ID (Client Secret).</p>
        <div class="cols">${fieldHtml("ebay.app_id", "App ID (Client ID)", "text", s.ebay.app_id)}${fieldHtml("ebay.cert_id", "Cert ID (Client Secret)", "password", s.ebay.cert_id)}${fieldHtml("ebay.marketplace", "Marketplace", "select", s.ebay.marketplace, ["EBAY_US", "EBAY_GB", "EBAY_CA", "EBAY_AU", "EBAY_DE", "EBAY_FR", "EBAY_IT", "EBAY_ES"])}</div></div>
      <h2 style="margin:6px 0 -4px">Notifications</h2>${ch}
      <div class="card"><h2>General</h2><div class="cols">${fieldHtml("app_base_url", "Public address of this app (for links in alerts)", "text", s.app_base_url)}
        ${fieldHtml("http.request_delay_s", "Seconds between requests to the same site", "number", s.http.request_delay_s)}</div>
        <div class="cols" style="margin-top:10px">${fieldHtml("http.user_agent", "Browser identity sent to sites", "text", s.http.user_agent)}</div>
        <p class="hint">Keep the delay generous. Scraped sites can block you if you check too often.</p></div></div>`;
  $("#save-set").onclick = guard(async () => { await saveSettings(); toast("Settings saved"); });
  $$("[data-test]").forEach((b) => (b.onclick = guard(async () => {
    await saveSettings();
    const r = await api(`/settings/test/${b.dataset.test}`, { method: "POST" });
    const v = r.results[b.dataset.test];
    toast(v === "ok" ? "Test sent. Check your device." : `${b.dataset.test}: ${v || "not configured"}`, v !== "ok");
  })));
}
async function saveSettings() {
  const out = {};
  $$("[data-path]").forEach((el) => {
    const keys = el.dataset.path.split(".");
    let o = out;
    keys.slice(0, -1).forEach((k) => (o = o[k] = o[k] || {}));
    const k = keys[keys.length - 1];
    o[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
  });
  await api("/settings", { method: "PUT", body: out });
}

route();
