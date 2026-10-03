// 🔗 Sync Hub: your CRMs, what each one is missing (waiting for your approval), disagreements, and the master records.
// Uses $, api, esc, toast, fmtDT from index.html and openLog / closeFlowEditor from flowmap.js.

const HUB = { data: null, timer: null };

async function renderHub() {
  try { HUB.data = await api("/api/hub"); } catch (e) { toast(e.message); return; }
  const d = HUB.data;
  $("#hubStopBanner").hidden = !d.hard_stop;
  $("#hubAuto").textContent = `Switched-on CRMs are read again every ${d.auto_minutes} minutes while the app is open.`;
  $("#hubSummary").innerHTML = [
    [d.records, "master records"], [d.systems.filter(s => s.enabled).length, "CRMs syncing"],
    [d.outbox.length, "changes waiting for you"], [d.conflicts.length, "disagreements to settle"],
  ].map(([n, t]) => `<div class="stat"><b>${n}</b><small>${t}</small></div>`).join("");
  hubBadge(d);
  drawConflicts(d);
  drawOutbox(d);
  drawSystems(d);
  drawRules(d);
  drawRecords();
  clearInterval(HUB.timer);
  HUB.timer = setInterval(() => { if ($("#page-hub").classList.contains("on")) renderHub(); else clearInterval(HUB.timer); }, 60000);
}

function hubBadge(d) {
  const n = d.outbox.length + d.conflicts.length;
  $("#hubBadge").hidden = !n;
  $("#hubBadge").textContent = n;
}

function fieldVals(fields) {
  return Object.entries(fields).map(([k, v]) => `<span class="hub-val"><b>${esc(HUB.data.fields[k] || k)}:</b> ${esc(v)}</span>`).join(" ");
}

function drawOutbox(d) {
  if (!d.outbox.length) { $("#hubOutbox").innerHTML = ""; return; }
  const items = d.outbox.map(o => `<div class="fm-req"><div class="what">🔗 Write into <b>${esc(o.system_label)}</b> for <b>${esc(o.name)}</b>
      ${o.ext_id ? "" : `<span class="hub-tag">new there</span>`}<br>${fieldVals(o.fields)}
      <div class="muted" style="font-size:13px">${fmtDT(o.ts)}</div></div>
      <button class="btn small green" data-hubok="${o.id}">✅ Approve</button><button class="btn small ghost" data-hubno="${o.id}">✖ No</button></div>`).join("");
  $("#hubOutbox").innerHTML = `<div class="row" style="justify-content:space-between;margin:6px 0">
      <h2 class="section-title" style="margin:0">Waiting for your approval</h2>
      <div class="row"><button class="btn small green" onclick="hubAll(true)" data-tip="Writes every change below into its CRM.">✅ Approve all</button>
      <button class="btn small ghost" onclick="hubAll(false)">✖ No to all</button></div></div>${items}`;
}

function drawConflicts(d) {
  if (!d.conflicts.length) { $("#hubConflicts").innerHTML = ""; return; }
  const sys = id => (d.systems.find(s => s.id === id) || {}).label || id;
  $("#hubConflicts").innerHTML = `<h2 class="section-title">⚖ Two CRMs disagree</h2>` + d.conflicts.map(c => `<div class="fm-req">
      <div class="what"><b>${esc(c.name)}</b>: ${esc(c.field_label)}</div>
      <button class="btn small ghost" data-hubpick="current" data-cid="${c.id}" data-tip="Keep this value everywhere.">“${esc(c.current.v)}” <span class="muted">from ${esc(sys(c.current.src))}</span></button>
      <button class="btn small ghost" data-hubpick="incoming" data-cid="${c.id}" data-tip="Keep this value everywhere.">“${esc(c.incoming.v)}” <span class="muted">from ${esc(sys(c.incoming.src))}</span></button></div>`).join("");
}

function drawSystems(d) {
  $("#hubSystems").innerHTML = d.systems.map(s => {
    const last = s.last ? `${s.last.ok ? "✅" : "⚠"} Last sync ${fmtDT(s.last.at)}: ${esc(s.last.msg)}` : "Not synced since the app started";
    const keys = s.secrets.map(k => `<div class="hub-key"><label>${esc(k.label)} ${k.set ? "✔ saved" : ""}</label>
        <input type="password" autocomplete="off" data-hubsecret="${esc(k.key)}" data-sys="${s.id}" placeholder="${k.set ? "Saved. Type to replace" : "Paste here"}" style="flex:2"></div>`).join("");
    return `<div class="vcard hub-sys"><div class="vbody">
      <h3>${esc(s.label)}</h3>
      <div class="muted" style="font-size:14px">${s.linked} customers linked · ${s.pending} waiting · ${s.ready ? last : esc(s.ready_msg)}</div>
      <div style="margin:6px 0">${s.fields.map(f => `<span class="hub-tag">${esc(f)}</span>`).join("")}</div>
      <label class="fm-toggle" data-tip="Reads this CRM into the master copy. Reading never changes anything in it."><span class="t">Sync this CRM</span>
        <span class="fm-switch"><input type="checkbox" data-hubsys="${s.id}" data-what="enabled" ${s.enabled ? "checked" : ""}><i></i></span></label>
      <label class="fm-toggle" data-tip="Lets changes you approve be written into this CRM. Off means nothing is ever written, even if you press Approve."><span class="t">Write approved changes into it</span>
        <span class="fm-switch"><input type="checkbox" data-hubsys="${s.id}" data-what="can_write" ${s.can_write ? "checked" : ""}><i></i></span></label>
      ${keys ? `<details ${s.ready ? "" : "open"}><summary style="cursor:pointer;font-weight:600">🔑 Connection</summary>${keys}
        <button class="btn small" data-hubsave="${s.id}">Save</button>
        <p class="muted" style="font-size:13px">Kept only on this computer. It's never shown again or written in the change log.</p></details>` : ""}
      <button class="btn small ghost" data-hubpull="${s.id}" ${s.enabled && s.ready && !d.hard_stop ? "" : "disabled"} style="margin-top:8px">🔄 Sync now</button>
    </div></div>`;
  }).join("");
}

function drawRules(d) {
  const opts = [...Object.entries(d.rule_names), ...d.systems.map(s => [s.id, `${s.label} is always right`])];
  $("#hubRules").innerHTML = Object.entries(d.fields).map(([f, label]) => `<tr><th>${esc(label)}</th><td>
      <select data-hubrule="${f}">${opts.map(([v, t]) => `<option value="${esc(v)}" ${d.rules[f] === v ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></td></tr>`).join("");
}

async function drawRecords() {
  const r = await api("/api/hub/records");
  const sys = id => (HUB.data.systems.find(s => s.id === id) || {}).label || id;
  const v = (rec, f) => esc((rec.data[f] || {}).v ?? "");
  $("#hubRecords").innerHTML = `<tr><th>Name</th><th>Email</th><th>Phone</th><th>Stage</th><th>In these CRMs</th><th>Changed</th></tr>` +
    (r.records.map(rec => `<tr style="cursor:pointer" data-hubrec="${rec.id}" data-tip="Click to see every version of this record.">
      <td><b>${v(rec, "name") || "#" + rec.id}</b></td><td>${v(rec, "email")}</td><td>${v(rec, "phone")}</td><td>${v(rec, "stage")}</td>
      <td>${rec.links.map(l => `<span class="hub-tag">${esc(sys(l.system))}</span>`).join("")}</td>
      <td class="muted" style="white-space:nowrap">${fmtDT(rec.updated_at)} · v${rec.version}</td></tr>`).join("")
      || `<tr><td colspan="6" class="muted">No master records yet. Switch on a CRM above and press 🔄 Sync now.</td></tr>`);
}

async function openRecord(id) {
  const d = await api(`/api/hub/record?id=${id}`);
  const sys = s => (HUB.data.systems.find(x => x.id === s) || {}).label || s;
  const rows = d.history.map(h => `<tr><td class="muted" style="white-space:nowrap">v${h.version}<br>${fmtDT(h.ts)}</td><td>${esc(h.why)}</td>
      <td>${Object.entries(h.data).map(([f, x]) => `<span class="hub-val"><b>${esc(HUB.data.fields[f] || f)}:</b> ${esc(x.v)} <span class="muted">(${esc(sys(x.src))})</span></span>`).join(" ")}</td></tr>`).join("");
  $("#fmModal").innerHTML = `<div class="row" style="justify-content:space-between"><h2 style="margin:0">📜 Master record versions</h2>
      <button class="btn ghost small" onclick="closeFlowEditor()">✖ Close</button></div>
    <p class="muted">Every version of this record, newest first, with the CRM each value came from.</p>
    <table class="fm-log"><tr><th>Version</th><th>Why</th><th>Values</th></tr>${rows}</table>`;
  $("#fmOverlay").classList.add("on");
}

async function hubDo(path, body, ok) {
  try { const r = await api(path, body); if (ok) toast(typeof ok === "function" ? ok(r) : ok); }
  catch (e) { toast(e.message); }
  renderHub();
}

function hubAll(approve) {
  if (!confirm(approve ? "Write every waiting change into its CRM?" : "Say no to every waiting change?")) return;
  hubDo("/api/hub/outbox/all", {approve}, r => `${approve ? "✅ Wrote" : "✖ Skipped"} ${r.done}` + (r.errors.length ? `. ${r.errors[0]}` : ""));
}

$("#page-hub").addEventListener("click", e => {
  const t = e.target.closest("[data-hubok],[data-hubno],[data-hubpick],[data-hubpull],[data-hubsave],[data-hubrec]");
  if (!t) return;
  if (t.dataset.hubok) hubDo("/api/hub/outbox", {id: +t.dataset.hubok, approve: true}, "✅ Written");
  else if (t.dataset.hubno) hubDo("/api/hub/outbox", {id: +t.dataset.hubno, approve: false}, "✖ Skipped");
  else if (t.dataset.hubpick) hubDo("/api/hub/conflict", {id: +t.dataset.cid, pick: t.dataset.hubpick}, "⚖ Settled");
  else if (t.dataset.hubpull) hubDo("/api/hub/pull", {system: t.dataset.hubpull}, r => `🔄 Read ${r.read}: ${r.new} new, ${r.updated} updated, ${r.conflicts} need you`);
  else if (t.dataset.hubrec) openRecord(+t.dataset.hubrec);
  else if (t.dataset.hubsave) {
    const values = {};
    document.querySelectorAll(`[data-hubsecret][data-sys="${t.dataset.hubsave}"]`).forEach(i => { if (i.value.trim()) values[i.dataset.hubsecret] = i.value.trim(); });
    hubDo("/api/hub/secrets", {system: t.dataset.hubsave, values}, "🔑 Saved on this computer");
  }
});

$("#page-hub").addEventListener("change", e => {
  const t = e.target;
  if (t.dataset.hubsys) hubDo("/api/hub/system", {system: t.dataset.hubsys, what: t.dataset.what, on: t.checked}, "Saved as a new version");
  if (t.dataset.hubrule) hubDo("/api/hub/rule", {field: t.dataset.hubrule, rule: t.value}, "Saved as a new version");
});

$("#hubSyncAll").onclick = async () => {
  const on = HUB.data.systems.filter(s => s.enabled && s.ready);
  if (!on.length) return toast("Switch on a CRM first.");
  for (const s of on) { try { await api("/api/hub/pull", {system: s.id}); } catch (e) { toast(e.message); } }
  toast("🔄 Synced");
  renderHub();
};
$("#hubLogBtn").onclick = () => openLog();
