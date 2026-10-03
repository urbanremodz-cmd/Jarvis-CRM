// 🧭 Flow Map: draws every page as a box (from the server's scan of the app's own code), joins them with
// square-cornered lines that go around boxes, and lets you set up page bots and your own flows.
// Uses $, api, esc, toast, hideTip and fmtDT from index.html.

const FM = { map: null, status: null, pos: {}, size: {}, sel: null, timer: null, draft: null };
const FM_W = 260, FM_M = 18, FM_GRID = 8;   // box width, clear space kept around every box, snap size

async function renderFlowMap() {
  try { FM.map = await api("/api/flowmap"); }
  catch (e) { $("#fmPanel").innerHTML = `<p>Couldn't load the map: ${esc(e.message)}</p>`; return; }
  FM.pos = {...(FM.map.layout || {})};
  if (FM.status) FM.status.hard_stop = FM.map.hard_stop;  // so nothing below shows an old hard-stop state
  drawBoxes();
  renderRequests();
  if (!FM.sel || !fmPage(FM.sel)) FM.sel = null;
  renderPanel();
  await refreshStatus();
  clearInterval(FM.timer);
  FM.timer = setInterval(() => { if ($("#page-flowmap").classList.contains("on")) refreshStatus(); }, 5000);
}

const fmPage = id => FM.map.scan.pages.find(p => p.id === id);
const fmRes = r => FM.map.scan.resources[r] || r;
const fmName = id => { const p = fmPage(id); return p ? `${p.icon} ${p.label}` : id; };

// ---------------------------------------------------------------- boxes
function boxHtml(p) {
  const bot = FM.map.bots[p.id];
  const chips = (list, cls = "") => list.length ? list.map(r => `<span class="fm-chip ${cls}">${esc(fmRes(r))}</span>`).join("") : "<i>nothing</i>";
  const outside = p.outside.map(o => o.kind === "mentioned" ? `${esc(o.name)} (talks about)` : esc(o.name));
  return `<div class="fm-head" data-tip="Drag here to move this box. Click to see all its details.">
      <span class="grip">⠿</span><span class="name">${p.icon} ${esc(p.label)}</span><span class="fm-dot" data-dot></span></div>
    <div class="fm-live" data-live>Checking…</div>
    <div class="fm-row" style="margin-top:4px">📖 <b>Reads:</b> ${chips(p.reads)}</div>
    <div class="fm-row">💾 <b>Saves:</b> ${chips(p.saves, "w")}</div>
    <div class="fm-row">➡ <b>Feeds:</b> ${p.feeds.length ? p.feeds.map(f => esc(fmName(f))).join(", ") : "<i>no other page</i>"}</div>
    <div class="fm-row">⏰ <b>Schedules:</b> ${p.schedules.length ? p.schedules.map(s => esc(s.label)).join("; ") : "<i>none</i>"}</div>
    <div class="fm-row">🌐 <b>Outside:</b> ${outside.length ? outside.join(", ") : "<i>none</i>"}</div>
    ${bot ? `<div class="fm-bot" data-botline data-tip="Click to set what this page's bot may do.">🤖 <span data-bot>${botLine(p.id)}</span></div>`
          : `<div class="fm-bot" style="cursor:default">🖥 Runs by itself in the background</div>`}`;
}

function botLine(id) {
  const b = FM.map.bots[id];
  if (FM.status && FM.status.hard_stop) return "<b>Stopped</b> (hard stop)";
  const state = b.active ? `<b>Watching</b> · ${b.active} flow${b.active > 1 ? "s" : ""} on` : b.allowed ? "<b>Ready</b> · no flows on" : "<b>Off</b>";
  return `${state} · ${b.allowed}/${b.perms.length} allowed${b.pending ? ` · <b>${b.pending} waiting</b>` : ""}`;
}

function drawBoxes() {
  const canvas = $("#fmCanvas");
  canvas.querySelectorAll(".fm-box").forEach(b => b.remove());
  for (const p of FM.map.scan.pages) {
    const el = document.createElement("div");
    el.className = "fm-box" + (p.background ? " bg" : "") + (FM.sel === p.id ? " sel" : "");
    el.dataset.id = p.id;
    el.innerHTML = boxHtml(p);
    canvas.appendChild(el);
    wireDrag(el);
  }
  canvas.querySelectorAll(".fm-box").forEach(el => FM.size[el.dataset.id] = {w: el.offsetWidth, h: el.offsetHeight});
  const missing = FM.map.scan.pages.some(p => !FM.pos[p.id]);
  if (missing) tidy(false);
  // boxes can grow taller after a re-scan: push any overlap apart
  for (const p of FM.map.scan.pages) if (!fits(p.id, FM.pos[p.id])) FM.pos[p.id] = freeSpot(p.id, FM.pos[p.id]);
  placeAll();
  route();
  applyStatus();
}

function placeAll() {
  let maxX = 0, maxY = 0;
  document.querySelectorAll("#fmCanvas .fm-box").forEach(el => {
    const p = FM.pos[el.dataset.id], s = FM.size[el.dataset.id];
    el.style.left = p.x + "px"; el.style.top = p.y + "px";
    maxX = Math.max(maxX, p.x + s.w); maxY = Math.max(maxY, p.y + s.h);
  });
  const c = $("#fmCanvas"), w = maxX + 300, h = maxY + 300;
  c.style.width = w + "px"; c.style.height = h + "px";
  $("#fmEdges").setAttribute("width", w); $("#fmEdges").setAttribute("height", h);
}

// neat rows, pages in menu order, "Behind the scenes" last
function tidy(save = true) {
  const ids = FM.map.scan.pages.map(p => p.id), cols = 3, gapX = 120, gapY = 90;
  let y = 40;
  for (let r = 0; r * cols < ids.length; r++) {
    const row = ids.slice(r * cols, r * cols + cols);
    row.forEach((id, i) => FM.pos[id] = {x: 40 + i * (FM_W + gapX), y});
    y += Math.max(...row.map(id => FM.size[id].h)) + gapY;
  }
  if (save) { placeAll(); route(); fmSaveLayout("Tidied the boxes into rows"); }
}

const rectOf = (id, p = FM.pos[id]) => ({l: p.x, t: p.y, r: p.x + FM.size[id].w, b: p.y + FM.size[id].h});
// a box fits when its clear space doesn't touch any other box's clear space
function fits(id, p) {
  if (!p || p.x < 0 || p.y < 0) return false;
  const a = rectOf(id, p);
  return FM.map.scan.pages.every(q => {
    if (q.id === id || !FM.pos[q.id]) return true;
    const b = rectOf(q.id);
    return a.r + 2 * FM_M <= b.l || b.r + 2 * FM_M <= a.l || a.b + 2 * FM_M <= b.t || b.b + 2 * FM_M <= a.t;
  });
}
function freeSpot(id, p) {
  p = p || {x: 40, y: 40};
  for (let dy = 0; dy < 4000; dy += FM_GRID * 4) if (fits(id, {x: p.x, y: p.y + dy})) return {x: p.x, y: p.y + dy};
  return {x: p.x, y: p.y + 4000};
}

// ---------------------------------------------------------------- dragging
function wireDrag(el) {
  const id = el.dataset.id, head = el.querySelector(".fm-head");
  head.addEventListener("pointerdown", e => {
    if (e.button !== 0) return;
    hideTip();
    head.setPointerCapture(e.pointerId);
    const start = {...FM.pos[id]}, sx = e.clientX, sy = e.clientY;
    let moved = false, frame = 0;
    const move = ev => {
      const dx = ev.clientX - sx, dy = ev.clientY - sy;
      if (!moved && Math.abs(dx) + Math.abs(dy) < 5) return;
      moved = true; el.classList.add("dragging");
      const snap = v => Math.max(0, Math.round(v / FM_GRID) * FM_GRID);
      FM.pos[id] = {x: snap(start.x + dx), y: snap(start.y + dy)};
      el.style.left = FM.pos[id].x + "px"; el.style.top = FM.pos[id].y + "px";
      el.classList.toggle("bad", !fits(id, FM.pos[id]));
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; route(); });
    };
    const up = () => {
      head.removeEventListener("pointermove", move); head.removeEventListener("pointerup", up); head.removeEventListener("pointercancel", up);
      el.classList.remove("dragging", "bad"); hideTip();
      if (!moved) return selectPage(id);
      if (!fits(id, FM.pos[id])) { FM.pos[id] = start; toast("Boxes need a little space between them. It went back."); }
      placeAll(); route();
      if (FM.pos[id].x !== start.x || FM.pos[id].y !== start.y) fmSaveLayout(`Moved the ${fmName(id)} box`);
    };
    head.addEventListener("pointermove", move); head.addEventListener("pointerup", up); head.addEventListener("pointercancel", up);
  });
  el.addEventListener("click", e => {
    if (e.target.closest(".fm-head")) return;
    if (e.target.closest("[data-botline]")) return selectPage(id, true);
    selectPage(id);
  });
}

async function fmSaveLayout(why) {
  try { await api("/api/flowmap/layout", {positions: FM.pos, why}); } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- square-cornered lines that go around boxes
function route() {
  const svg = $("#fmEdges"), ids = FM.map.scan.pages.map(p => p.id);
  const obs = ids.map(id => { const r = rectOf(id); return {id, l: r.l - FM_M, t: r.t - FM_M, r: r.r + FM_M, b: r.b + FM_M}; });
  const edges = FM.map.scan.edges.map(e => {
    const fwd = e.a_to_b.length > 0, back = e.b_to_a.length > 0;
    const [from, to] = fwd ? [e.a, e.b] : [e.b, e.a];
    const via = [...new Set([...e.a_to_b, ...e.b_to_a])].map(fmRes);
    return {from, to, both: fwd && back, via, tip: (fwd ? `${fmName(e.a)} → ${fmName(e.b)}: ${e.a_to_b.map(fmRes).join(", ")}` : "")
      + (fwd && back ? "\n" : "") + (back ? `${fmName(e.b)} → ${fmName(e.a)}: ${e.b_to_a.map(fmRes).join(", ")}` : "")};
  });
  // pick a side of each box for each line, then spread lines out along that side
  const center = id => { const r = rectOf(id); return {x: (r.l + r.r) / 2, y: (r.t + r.b) / 2}; };
  const sideFor = (a, b) => {
    const ca = center(a), cb = center(b), ra = rectOf(a), rb = rectOf(b);
    if (rb.l >= ra.r + 2 * FM_M) return "R"; if (rb.r + 2 * FM_M <= ra.l) return "L";
    return cb.y > ca.y ? "B" : "T";
  };
  const opp = {L: "R", R: "L", T: "B", B: "T"};
  const ends = [];
  edges.forEach((e, i) => {
    const s = sideFor(e.from, e.to);
    ends.push({edge: i, box: e.from, side: s, other: e.to, start: true}, {edge: i, box: e.to, side: opp[s], other: e.from, start: false});
  });
  const groups = {};
  ends.forEach(en => (groups[en.box + en.side] = groups[en.box + en.side] || []).push(en));
  Object.values(groups).forEach(g => {
    const horiz = g[0].side === "T" || g[0].side === "B";
    g.sort((a, b) => horiz ? center(a.other).x - center(b.other).x : center(a.other).y - center(b.other).y);
    g.forEach((en, k) => {
      const r = rectOf(en.box), f = (k + 1) / (g.length + 1);
      const snap = v => Math.round(v);
      if (en.side === "L" || en.side === "R") {
        en.port = {x: en.side === "L" ? r.l : r.r, y: snap(r.t + 40 + (r.b - r.t - 60) * f)};
        en.stub = {x: en.port.x + (en.side === "L" ? -FM_M : FM_M), y: en.port.y};
      } else {
        en.port = {x: snap(r.l + (r.r - r.l) * f), y: en.side === "T" ? r.t : r.b};
        en.stub = {x: en.port.x, y: en.port.y + (en.side === "T" ? -FM_M : FM_M)};
      }
    });
  });
  const used = new Set();
  let html = `<defs><marker id="fmArrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0,0 L10,5 L0,10 z" fill="#8a8f98"/></marker></defs>`;
  edges.forEach((e, i) => {
    const a = ends.find(x => x.edge === i && x.start), b = ends.find(x => x.edge === i && !x.start);
    const mid = findPath(a.stub, b.stub, obs, a.side, opp[b.side], used);
    if (!mid) return; // only while a box is dragged onto another: draw nothing rather than a line behind a box
    const pts = simplify([a.port, ...mid, b.port]);
    for (let k = 1; k < pts.length; k++) used.add(segKey(pts[k - 1], pts[k]));
    const d = "M" + pts.map(p => `${p.x},${p.y}`).join(" L");
    html += `<g data-edge="${e.from}|${e.to}"><path class="line" d="${d}" marker-end="url(#fmArrow)" ${e.both ? 'marker-start="url(#fmArrow)"' : ""}
/><path class="hit" d="${d}" data-tip="${esc(e.tip)}"/></g>`;
  });
  svg.innerHTML = html;
  if (FM.sel) svg.querySelectorAll("g").forEach(g => g.querySelector(".line").classList.toggle("hot", g.dataset.edge.split("|").includes(FM.sel)));
}

const segKey = (p, q) => [p.x, p.y, q.x, q.y].join(",");
function simplify(pts) {
  const out = [];
  for (const p of pts) {
    if (out.length && out[out.length - 1].x === p.x && out[out.length - 1].y === p.y) continue;
    if (out.length >= 2) {
      const a = out[out.length - 2], b = out[out.length - 1];
      if ((a.x === b.x && b.x === p.x) || (a.y === b.y && b.y === p.y)) out.pop();
    }
    out.push(p);
  }
  return out;
}

// Shortest square-cornered path on a grid made from the box edges. A line may run in the clear space around a
// box but never through a box, so lines never pass behind one. Each corner costs extra, so paths stay simple.
function findPath(s, t, obs, startDir, endDir, used) {
  const uniq = a => [...new Set(a.map(Math.round))].sort((x, y) => x - y);
  let xs = uniq([s.x, t.x, ...obs.flatMap(o => [o.l, o.r])]), ys = uniq([s.y, t.y, ...obs.flatMap(o => [o.t, o.b])]);
  const mids = a => a.slice(1).map((v, i) => Math.round((v + a[i]) / 2));
  xs = uniq([...xs, ...mids(xs), xs[0] - 40, xs[xs.length - 1] + 40]);
  ys = uniq([...ys, ...mids(ys), ys[0] - 40, ys[ys.length - 1] + 40]);
  const inside = (x, y) => obs.some(o => x > o.l && x < o.r && y > o.t && y < o.b);
  const crosses = (x1, y1, x2, y2) => obs.some(o => x1 === x2
    ? x1 > o.l && x1 < o.r && Math.max(y1, y2) > o.t && Math.min(y1, y2) < o.b
    : y1 > o.t && y1 < o.b && Math.max(x1, x2) > o.l && Math.min(x1, x2) < o.r);
  const DIRS = {R: [1, 0], L: [-1, 0], B: [0, 1], T: [0, -1]};
  const si = [xs.indexOf(Math.round(s.x)), ys.indexOf(Math.round(s.y))], ti = [xs.indexOf(Math.round(t.x)), ys.indexOf(Math.round(t.y))];
  const key = (i, j, d) => `${i},${j},${d}`;
  const heap = [], best = new Map(), prev = new Map();
  const push = (c, item) => { heap.push([c, item]); let k = heap.length - 1; while (k > 0) { const p = (k - 1) >> 1; if (heap[p][0] <= heap[k][0]) break; [heap[p], heap[k]] = [heap[k], heap[p]]; k = p; } };
  const pop = () => { const top = heap[0], last = heap.pop(); if (heap.length) { heap[0] = last; let k = 0; for (;;) { const l = 2 * k + 1, r = l + 1; let m = k; if (l < heap.length && heap[l][0] < heap[m][0]) m = l; if (r < heap.length && heap[r][0] < heap[m][0]) m = r; if (m === k) break; [heap[m], heap[k]] = [heap[k], heap[m]]; k = m; } } return top; };
  const h = (i, j) => Math.abs(xs[i] - t.x) + Math.abs(ys[j] - t.y);
  const k0 = key(si[0], si[1], startDir);
  best.set(k0, 0); push(h(si[0], si[1]), [si[0], si[1], startDir, 0]);
  let guard = 0;
  while (heap.length && guard++ < 200000) {
    const [, [i, j, d, g]] = pop();
    if (g > (best.get(key(i, j, d)) ?? Infinity)) continue;
    if (i === ti[0] && j === ti[1]) {
      const out = []; let k = key(i, j, d);
      while (k) { const [a, b] = k.split(",").map(Number); out.unshift({x: xs[a], y: ys[b]}); k = prev.get(k); }
      return out;
    }
    for (const [nd, [dx, dy]] of Object.entries(DIRS)) {
      if (DIRS[d][0] === -dx && DIRS[d][1] === -dy) continue; // no U-turns
      const ni = i + dx, nj = j + dy;
      if (ni < 0 || nj < 0 || ni >= xs.length || nj >= ys.length) continue;
      const x1 = xs[i], y1 = ys[j], x2 = xs[ni], y2 = ys[nj];
      if (inside(x2, y2) || crosses(x1, y1, x2, y2)) continue;
      let cost = g + Math.abs(x2 - x1) + Math.abs(y2 - y1) + (nd !== d ? 30 : 0);
      if (used.has(segKey({x: x1, y: y1}, {x: x2, y: y2})) || used.has(segKey({x: x2, y: y2}, {x: x1, y: y1}))) cost += 20;
      if (ni === ti[0] && nj === ti[1] && nd !== endDir) cost += 30;
      const nk = key(ni, nj, nd);
      if (cost < (best.get(nk) ?? Infinity)) { best.set(nk, cost); prev.set(nk, key(i, j, d)); push(cost + h(ni, nj), [ni, nj, nd, cost]); }
    }
  }
  return null;
}

// ---------------------------------------------------------------- live status
async function refreshStatus() {
  try { FM.status = await api("/api/flowmap/status"); }
  catch { FM.status = {offline: true, pages: {}}; }
  applyStatus();
}
function applyStatus() {
  const st = FM.status;
  if (!st || !FM.map) return;
  document.querySelectorAll("#fmCanvas .fm-box").forEach(el => {
    const s = st.offline ? {state: "error", headline: "The app isn't answering"} : st.pages[el.dataset.id] || {state: "", headline: ""};
    const dot = el.querySelector("[data-dot]"), state = st.hard_stop && !st.offline && FM.map.bots[el.dataset.id] && s.state !== "error" ? "stopped" : s.state;
    dot.className = "fm-dot " + state;
    dot.dataset.tip = {ok: "All good", attention: "Something here needs you", error: "Something is wrong here", stopped: "Hard stop is on: this page's bot is frozen"}[state] || "";
    // only touch what changed, so a click that lands during a refresh isn't lost
    const live = el.querySelector("[data-live]"); if (live.textContent !== s.headline) live.textContent = s.headline;
    const bl = el.querySelector("[data-bot]"), html = bl && botLine(el.dataset.id);
    if (bl && bl.dataset.html !== html) { bl.innerHTML = html; bl.dataset.html = html; }
  });
  showStopState(!!st.hard_stop);
  if (!st.offline && st.pending !== FM.lastPending) { FM.lastPending = st.pending; if (FM.lastPending !== undefined) reloadRuns(); }
  const sc = FM.map.scan;
  $("#fmScanInfo").textContent = `Map read from ${sc.files.join(", ")} at ${fmtDT(sc.generated_at)}` + (st.offline ? " · ⚠ app not answering" : "");
}

function showStopState(on) {
  $("#hsPill").hidden = !on;
  const b = $("#fmStop");
  b.classList.toggle("on", on);
  b.textContent = on ? "▶ Turn hard stop off" : "⛔ HARD STOP";
  b.dataset.tip = on ? "Lets bots notice and ask again. They still can't change anything without your approval."
    : "Freezes every bot right away. Bots stop noticing, stop asking, and anything waiting for approval is cancelled.";
  $("#fmStopBanner").hidden = !on;
  $("#fmStopBanner").innerHTML = "⛔ <b>Hard stop is ON.</b> Every bot is frozen: they don't notice, don't ask, and can't change anything. Nothing can be approved until you turn it off.";
}

async function reloadRuns() {
  try { const d = await api("/api/flowmap/log"); FM.map.runs = d.runs; renderRequests(); } catch {}
}

// ---------------------------------------------------------------- requests waiting for approval
function actionText(a) {
  return a.type === "move" ? `move to <b>${esc(a.stage)}</b>` : `add the note “${esc(a.text)}”`;
}
function renderRequests() {
  const pending = (FM.map.runs || []).filter(r => r.status === "pending");
  const stopped = FM.status && FM.status.hard_stop;
  $("#fmRequests").innerHTML = pending.map(r => {
    const f = FM.map.flows.find(x => x.id === r.flow_id);
    return `<div class="fm-req"><div class="what">🤖 <b>${esc(f ? f.name : "A flow")}</b> asks to ${r.actions.map(actionText).join(", then ")} for <b>${esc(r.lead_name)}</b>
      <div class="muted" style="font-size:13px">Because: ${esc(r.reason)} · ${fmtDT(r.ts)} · ${esc(fmName(r.bot))} bot</div></div>
      <button class="btn small green" data-run="${r.id}" data-ok="1" ${stopped ? "disabled" : ""} data-tip="Do it now. This is the only way a bot's change ever happens.">✅ Approve</button>
      <button class="btn small ghost" data-run="${r.id}" data-tip="Don't do it. Nothing changes.">✖ No</button></div>`;
  }).join("");
}
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-run]");
  if (!b) return;
  try { await api("/api/flowmap/run", {id: +b.dataset.run, approve: !!b.dataset.ok}); toast(b.dataset.ok ? "✅ Done" : "✖ Skipped"); }
  catch (err) { toast(err.message); }
  await renderFlowMap();
  if (typeof load === "function") load();
});

// ---------------------------------------------------------------- side panel
function selectPage(id, botFirst = false) {
  FM.sel = id;
  document.querySelectorAll("#fmCanvas .fm-box").forEach(el => el.classList.toggle("sel", el.dataset.id === id));
  document.querySelectorAll("#fmEdges g").forEach(g => g.querySelector(".line").classList.toggle("hot", g.dataset.edge.split("|").includes(id)));
  renderPanel();
  if (botFirst) { const b = $("#fmBotSection"); if (b) b.scrollIntoView({block: "nearest"}); }
}

function renderPanel() {
  const panel = $("#fmPanel");
  if (!FM.sel) {
    const s = FM.map.scan;
    panel.innerHTML = `<h2 style="margin:0 0 6px;font-size:18px">How to read this map</h2>
      <p>Each box is one page of Remod Flow. A line means one page <b>feeds</b> another: it saves something the other page reads. Point at a line to see what.</p>
      <ul><li><span class="fm-dot ok" style="display:inline-block"></span> all good</li>
      <li><span class="fm-dot attention" style="display:inline-block"></span> something needs you</li>
      <li><span class="fm-dot error" style="display:inline-block"></span> something is wrong</li>
      <li><span class="fm-dot stopped" style="display:inline-block"></span> hard stop is on</li></ul>
      <h3>🤖 Page bots</h3><p>Every page has a bot. Bots start <b>off</b>. Click a box to choose what its bot may do. A bot can only <b>ask</b>: you approve every change at the top of this page.</p>
      <h3>⚡ Your flows</h3>${flowList(FM.map.flows)}
      <button class="btn small" onclick="openFlowEditor()" style="margin-top:6px">➕ New flow</button>
      ${s.warnings.length ? `<h3>⚠ Scan notes</h3><ul>${s.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}`;
    return;
  }
  const p = fmPage(FM.sel), bot = FM.map.bots[p.id], st = (FM.status && FM.status.pages[p.id]) || {};
  const li = (items, f) => items.length ? `<ul>${items.map(f).join("")}</ul>` : `<p class="muted">None</p>`;
  panel.innerHTML = `<div class="row" style="justify-content:space-between"><h2 style="margin:0;font-size:19px">${p.icon} ${esc(p.label)}</h2>
      <button class="btn ghost small" onclick="FM.sel=null;selectPage(null)" data-tip="Close and go back to the map key.">✖</button></div>
    <p style="margin:6px 0"><span class="fm-dot ${st.state || ""}" style="display:inline-block;vertical-align:middle"></span> ${esc(st.headline || "")}</p>
    <h3>📖 Reads</h3>${li(p.reads, r => `<li>${esc(fmRes(r))} <span class="fm-where">${esc(r)}</span></li>`)}
    <h3>💾 Saves</h3>${li(p.saves, r => `<li>${esc(fmRes(r))} <span class="fm-where">${esc(r)}</span></li>`)}
    <h3>➡ Feeds these pages</h3>${li(p.feeds, f => `<li><a href="#" onclick="selectPage('${f}');return false">${esc(fmName(f))}</a></li>`)}
    <h3>⏰ Schedules</h3>${li(p.schedules, s => `<li>${esc(s.label)}: ${esc(s.when)} <span class="fm-where">${esc(s.where)}</span></li>`)}
    <h3>🌐 Outside services</h3>${li(p.outside, o => `<li>${esc(o.name)} <span class="fm-where">${o.kind === "mentioned" ? "only mentioned in the instructions, no connection" : esc(o.where)}</span></li>`)}
    ${p.endpoints.length ? `<details style="margin-top:10px"><summary class="muted">Server addresses this page uses (${p.endpoints.length})</summary>
      <ul>${p.endpoints.map(e => `<li><code>${e.method} ${esc(e.path)}</code> <span class="fm-where">${esc(e.server)}</span></li>`).join("")}</ul></details>` : ""}
    ${bot ? botSection(p, bot) : `<h3>🖥 Background work</h3><p class="muted">This box is the app itself running. It has no bot.</p>`}`;
}

function botSection(p, bot) {
  const stopped = FM.status && FM.status.hard_stop;
  const flowsHere = FM.map.flows.filter(f => f.bot === p.id);
  return `<div id="fmBotSection"><h3>🤖 ${esc(p.label)} bot</h3>
    ${stopped ? `<p class="fm-banner" style="font-size:14px">⛔ Hard stop is on. This bot is frozen no matter what the switches say.</p>` : ""}
    <p class="muted" style="font-size:14px">Switch on only what you want this bot to be able to ask for. Every switch is saved as a new version in the change log.</p>
    ${bot.perms.length ? bot.perms.map(t => `<label class="fm-toggle" data-tip="${esc(t.tip)}"><span class="t">${esc(t.label)}</span>
        <span class="fm-switch"><input type="checkbox" data-perm="${esc(t.id)}" data-page="${p.id}" ${t.on ? "checked" : ""}><i></i></span></label>`).join("")
      : `<p class="muted">This page doesn't read or save anything, so its bot has nothing it could do.</p>`}
    <h3>⚡ Flows this bot runs</h3>${flowList(flowsHere)}
    ${bot.can_run_flows ? `<button class="btn small" onclick="openFlowEditor(null, '${p.id}')" style="margin-top:6px">➕ New flow for this bot</button>`
      : `<p class="muted" style="font-size:14px">Flows can change customers, and this page doesn't save customers, so it can't run flows.</p>`}
    <p style="margin-top:12px"><a href="#" onclick="openHistory('perms','${p.id}');return false">📜 Switch history for this bot</a></p></div>`;
}

function flowList(list) {
  if (!list.length) return `<p class="muted">No flows yet.</p>`;
  return list.map(f => {
    const cls = f.status === "on" ? "on" : f.status === "paused" ? "paused" : "needs";
    const missing = missingPerms(f);
    return `<div class="fm-flow"><div class="row" style="justify-content:space-between"><b>${esc(f.name)}</b>
        <span class="fm-status ${cls}">${f.status === "on" ? "On" : f.status === "paused" ? "Paused" : "Needs approval"}</span></div>
      <div class="muted" style="font-size:13px">${esc(fmName(f.bot))} bot · version ${f.version}${f.approved_version && f.approved_version !== f.version ? ` (you approved v${f.approved_version})` : ""}</div>
      <div style="font-size:13.5px;margin:4px 0">${esc(flowSentence(f))}</div>
      ${missing.length ? `<div style="font-size:13px;color:#b42318">⚠ Its bot needs: ${missing.map(m => esc(permLabel(f.bot, m))).join(", ")}</div>` : ""}
      <div class="row" style="gap:6px;margin-top:4px">
        ${f.status === "needs approval" ? `<button class="btn small green" onclick="flowState('${f.id}','approve',${f.version})" data-tip="Turns on this exact version. If you edit it later, you'll be asked again.">✅ Approve v${f.version}</button>` : ""}
        ${f.status === "on" ? `<button class="btn small ghost" onclick="flowState('${f.id}','pause')" data-tip="Stops this flow from asking for anything.">⏸ Pause</button>` : ""}
        ${f.status === "paused" ? `<button class="btn small ghost" onclick="flowState('${f.id}','resume')">▶ Turn on</button>` : ""}
        <button class="btn small ghost" onclick="openFlowEditor('${f.id}')">✏️ Edit</button>
        <button class="btn small ghost" onclick="openHistory('flow','${f.id}')" data-tip="Every saved version of this flow.">📜</button>
        <button class="btn small danger" onclick="flowState('${f.id}','delete')" data-tip="Removes the flow. Its history stays in the change log.">🗑</button></div></div>`;
  }).join("");
}

const permLabel = (page, id) => ((FM.map.bots[page] || {perms: []}).perms.find(p => p.id === id) || {label: id}).label;
function missingPerms(f) {
  const b = FM.map.bots[f.bot]; if (!b) return [];
  const need = ["watch:table:leads", "change:table:leads"];
  return need.filter(n => !(b.perms.find(p => p.id === n) || {}).on);
}
function flowSentence(f) {
  const o = FM.map.options, t = f.trigger;
  const when = o.triggers[t.type] + (t.type === "stage_changed" && t.stage ? ` “${t.stage}”` : "") + (t.type === "daily" ? ` ${t.time}` : "");
  const ifs = f.conditions.map(c => `${o.fields[c.field]} ${o.ops[c.op]} ${c.value}`).join(" and ");
  const thens = f.actions.map(a => a.type === "move" ? `move to “${a.stage}”` : `add note “${a.text}”`).join(", then ");
  return `When: ${when}${ifs ? ` · Only if: ${ifs}` : ""} · Then ask to: ${thens}`;
}

async function flowState(id, what, version) {
  if (what === "delete" && !confirm("Remove this flow? Its history stays in the change log.")) return;
  try { await api("/api/flowmap/flow/state", {id, what, version}); toast({approve: "✅ Flow approved", pause: "⏸ Flow paused", resume: "▶ Flow on", delete: "🗑 Flow removed"}[what]); }
  catch (e) { toast(e.message); }
  renderFlowMap();
}

document.addEventListener("change", async e => {
  const t = e.target.closest("[data-perm]");
  if (!t) return;
  try { await api("/api/flowmap/perm", {page: t.dataset.page, perm: t.dataset.perm, on: t.checked}); toast(t.checked ? "🔓 Allowed (saved as a new version)" : "🔒 Blocked (saved as a new version)"); }
  catch (err) { toast(err.message); t.checked = !t.checked; }
  renderFlowMap();
});

// ---------------------------------------------------------------- flow editor: when → only if → then
function openFlowEditor(id, bot) {
  const f = id ? FM.map.flows.find(x => x.id === id) : null;
  const firstBot = bot || Object.keys(FM.map.bots).find(k => FM.map.bots[k].can_run_flows) || "pipeline";
  FM.draft = f ? JSON.parse(JSON.stringify({id: f.id, name: f.name, bot: f.bot, trigger: f.trigger, conditions: f.conditions, actions: f.actions}))
    : {name: "", bot: firstBot, trigger: {type: "lead_added"}, conditions: [], actions: [{type: "note", text: ""}]};
  drawEditor();
  $("#fmOverlay").classList.add("on");
}
function closeFlowEditor() { $("#fmOverlay").classList.remove("on"); hideTip(); }
$("#fmOverlay").addEventListener("click", e => { if (e.target.id === "fmOverlay") closeFlowEditor(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeFlowEditor(); });

function drawEditor() {
  const d = FM.draft, o = FM.map.options, stages = FM.map.stages;
  const opt = (obj, cur) => Object.entries(obj).map(([k, v]) => `<option value="${k}" ${k === cur ? "selected" : ""}>${esc(v)}</option>`).join("");
  const stageOpt = cur => stages.map(s => `<option ${s === cur ? "selected" : ""}>${esc(s)}</option>`).join("");
  const link = `<div class="fm-link"></div>`;
  const t = d.trigger;
  let chain = `<div class="fm-node trig"><h4>⚡ When…</h4>
      <select data-ed="trigger.type" data-tip="What makes this flow start.">${opt(o.triggers, t.type)}</select>
      ${t.type === "stage_changed" ? `<select data-ed="trigger.stage"><option value="">any box</option>${stageOpt(t.stage)}</select>` : ""}
      ${t.type === "daily" ? `<input type="time" data-ed="trigger.time" value="${esc(t.time || "08:00")}">` : ""}</div>`;
  d.conditions.forEach((c, i) => {
    chain += link + `<div class="fm-node cond"><h4>❓ Only if… <button class="fm-x" data-del="conditions.${i}" data-tip="Remove this check.">✖</button></h4>
      <select data-ed="conditions.${i}.field">${opt(o.fields, c.field)}</select>
      <select data-ed="conditions.${i}.op">${opt(o.ops, c.op)}</select>
      ${c.field === "stage" ? `<select data-ed="conditions.${i}.value">${stageOpt(c.value)}</select>` : `<input data-ed="conditions.${i}.value" value="${esc(c.value)}" placeholder="value">`}</div>`;
  });
  chain += link + `<div class="fm-node add" data-add="condition" data-tip="Add a check. The flow only goes on when every check is true.">➕ Only if…</div>`;
  d.actions.forEach((a, i) => {
    chain += link + `<div class="fm-node act"><h4>▶ Then ask to… ${d.actions.length > 1 ? `<button class="fm-x" data-del="actions.${i}">✖</button>` : ""}</h4>
      <select data-ed="actions.${i}.type">${opt(o.actions, a.type)}</select>
      ${a.type === "move" ? `<select data-ed="actions.${i}.stage">${stageOpt(a.stage)}</select>` : `<input data-ed="actions.${i}.text" value="${esc(a.text || "")}" placeholder="Note to add">`}</div>`;
  });
  chain += link + `<div class="fm-node add" data-add="action" data-tip="Add another step. Steps happen in order, after you approve.">➕ Then also…</div>`;
  const bots = Object.entries(FM.map.bots).filter(([, b]) => b.can_run_flows).map(([k]) => `<option value="${k}" ${k === d.bot ? "selected" : ""}>${esc(fmName(k))} bot</option>`).join("");
  const f = d.id ? FM.map.flows.find(x => x.id === d.id) : null;
  const missing = missingPerms({bot: d.bot});
  $("#fmModal").innerHTML = `<div class="row" style="justify-content:space-between"><h2 style="margin:0">${d.id ? "✏️ Edit flow" : "➕ Draw a new flow"}</h2>
      <button class="btn ghost small" onclick="closeFlowEditor()">✖ Close</button></div>
    <div class="grid2" style="margin-top:12px">
      <div><label>Flow name</label><input data-ed="name" value="${esc(d.name)}" placeholder="Like: Flag big kitchen jobs"></div>
      <div><label>Which page's bot runs it</label><select data-ed="bot">${bots}</select></div></div>
    <div class="fm-chain">${chain}</div>
    <p class="muted" style="margin:0 0 8px">Read it left to right. When the flow starts and every check is true, the bot <b>asks</b> you at the top of the Flow Map. Nothing changes until you press <b>Approve</b>.
      Saving makes a new version${f ? ` (now v${f.version})` : ""}, and a new version stays off until you approve it.</p>
    ${missing.length ? `<p style="color:#b42318">⚠ This bot isn't allowed to ${missing.map(m => esc(permLabel(d.bot, m).toLowerCase())).join(" or ")} yet. Turn that on in its box, or the flow won't ask for anything.</p>` : ""}
    <div class="row"><button class="btn" id="fmSaveFlow">💾 Save as a new version</button>
      <span class="muted">You'll approve it next.</span></div>`;
  $("#fmSaveFlow").onclick = saveFlow;
}

document.addEventListener("change", e => {
  const el = e.target.closest("[data-ed]");
  if (!el || !FM.draft) return;
  const path = el.dataset.ed.split("."), last = path.pop();
  let obj = FM.draft; for (const k of path) obj = obj[k];
  obj[last] = el.value;
  if (last === "type" && path[0] === "actions") Object.assign(obj, el.value === "move" ? {stage: FM.map.stages[0]} : {text: ""});
  if (last === "field") obj.value = obj.field === "stage" ? FM.map.stages[0] : "";
  if (el.tagName === "SELECT") drawEditor();
});
document.addEventListener("input", e => {
  const el = e.target.closest("input[data-ed]");
  if (!el || !FM.draft) return;
  const path = el.dataset.ed.split("."), last = path.pop();
  let obj = FM.draft; for (const k of path) obj = obj[k];
  obj[last] = el.value;
});
document.addEventListener("click", e => {
  const add = e.target.closest("[data-add]"), del = e.target.closest("[data-del]");
  if (!FM.draft || !(add || del)) return;
  if (add && add.dataset.add === "condition") FM.draft.conditions.push({field: "budget_low", op: "at_least", value: "20000"});
  if (add && add.dataset.add === "action") FM.draft.actions.push({type: "note", text: ""});
  if (del) { const [list, i] = del.dataset.del.split("."); FM.draft[list].splice(+i, 1); }
  drawEditor();
});

async function saveFlow() {
  try {
    const f = await api("/api/flowmap/flow", FM.draft);
    closeFlowEditor();
    toast(`💾 Saved as version ${f.version}. Approve it to turn it on.`);
    FM.sel = f.bot;
    await renderFlowMap();
  } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- change log and history
async function openLog() {
  const d = await api("/api/flowmap/log");
  const rows = d.log.map(l => {
    const m = /^(\w+):(.+) v(\d+)$/.exec(l.target || "");
    const canGoBack = m && ["perms", "flow", "layout"].includes(m[1]);
    return `<tr><td class="muted" style="white-space:nowrap">${fmtDT(l.ts)}</td><td>${esc(l.who)}</td><td>${esc(l.detail)}</td>
      <td>${canGoBack ? `<a href="#" onclick="openHistory('${m[1]}','${esc(m[2])}');return false">versions</a>` : ""}</td></tr>`;
  }).join("");
  const runs = d.runs.filter(r => r.status !== "pending").map(r => `<tr><td class="muted" style="white-space:nowrap">${fmtDT(r.decided_at || r.ts)}</td>
      <td>${esc(r.lead_name)}</td><td>${r.actions.map(actionText).join(", then ")}</td><td><b>${esc(r.status)}</b> ${esc(r.result || "")}</td></tr>`).join("");
  $("#fmModal").innerHTML = `<div class="row" style="justify-content:space-between"><h2 style="margin:0">📜 Change log</h2>
      <button class="btn ghost small" onclick="closeFlowEditor()">✖ Close</button></div>
    <p class="muted">Every change on the Flow Map is saved here, newest first. Nothing is ever deleted from this list.</p>
    <table class="fm-log"><tr><th>When</th><th>Who</th><th>What changed</th><th></th></tr>${rows || `<tr><td colspan="4" class="muted">No changes yet.</td></tr>`}</table>
    <h3 class="section-title">🤖 Bot requests you answered</h3>
    <table class="fm-log"><tr><th>When</th><th>Customer</th><th>Asked to</th><th>Result</th></tr>${runs || `<tr><td colspan="4" class="muted">None yet.</td></tr>`}</table>`;
  FM.draft = null;
  $("#fmOverlay").classList.add("on");
}

async function openHistory(kind, key) {
  const d = await api(`/api/flowmap/history?kind=${encodeURIComponent(kind)}&key=${encodeURIComponent(key)}`);
  const title = kind === "perms" ? `${fmName(key)} bot switches` : kind === "flow" ? `Flow versions` : "Box positions";
  const show = v => kind === "perms" ? Object.entries(v).map(([k, on]) => `${on ? "✅" : "⬜"} ${esc(permLabel(key, k))}`).join("<br>")
    : kind === "flow" ? `<b>${esc(v.name)}</b><br>${esc(flowSentence(v))}` : `${Object.keys(v).length} boxes placed`;
  $("#fmModal").innerHTML = `<div class="row" style="justify-content:space-between"><h2 style="margin:0">📜 ${esc(title)}</h2>
      <button class="btn ghost small" onclick="closeFlowEditor()">✖ Close</button></div>
    <p class="muted">Going back to an older version saves it as a new version, so nothing is lost.${kind === "flow" ? " A flow you go back to needs your approval again." : ""}</p>
    <table class="fm-log"><tr><th>Version</th><th>Saved</th><th>What it was</th><th></th></tr>
    ${d.history.map((h, i) => `<tr><td><b>v${h.version}</b>${i === 0 ? " (now)" : ""}</td><td class="muted">${fmtDT(h.ts)}<br>${esc(h.note || "")}</td><td>${show(h.value)}</td>
      <td>${i ? `<button class="btn small ghost" onclick="restoreVersion('${kind}','${esc(key)}',${h.version})">↩ Go back to this</button>` : ""}</td></tr>`).join("")}</table>`;
  FM.draft = null;
  $("#fmOverlay").classList.add("on");
}

async function restoreVersion(kind, key, version) {
  if (!confirm(`Go back to version ${version}? This is saved as a new version.`)) return;
  try { await api("/api/flowmap/restore", {kind, key, version}); toast("↩ Done. Saved as a new version."); closeFlowEditor(); renderFlowMap(); }
  catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- buttons
$("#fmStop").onclick = async () => {
  const on = !(FM.status && FM.status.hard_stop);
  if (!on && !confirm("Turn the hard stop off? Bots that are on will start noticing and asking again. They still can't change anything without your approval.")) return;
  try { await api("/api/flowmap/hardstop", {on}); toast(on ? "⛔ Hard stop is ON. Every bot is frozen." : "▶ Hard stop is off"); }
  catch (e) { toast(e.message); }
  await renderFlowMap();
};
$("#fmNewFlow").onclick = () => openFlowEditor(null, FM.sel && FM.map.bots[FM.sel] && FM.map.bots[FM.sel].can_run_flows ? FM.sel : null);
$("#fmLogBtn").onclick = openLog;
$("#fmArrange").onclick = () => tidy(true);
$("#fmScan").onclick = async () => { await api("/api/flowmap/scan", {}); await renderFlowMap(); toast("🔄 Map rebuilt from the code"); };
$("#hsPill").onclick = () => document.querySelector('nav button[data-page="flowmap"]').click();

// show the hard-stop warning in the top bar on every page
api("/api/flowmap/status").then(s => { $("#hsPill").hidden = !s.hard_stop; }).catch(() => {});
