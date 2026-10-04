"""Page bots for the 🧭 Flow Map.

Safety rules, in the order they are checked:
  1. HARD STOP. While it is on, bots notice nothing, ask for nothing and change nothing. Turning it on cancels
     every request still waiting for approval.
  2. A flow only works after you approve that exact version. Editing a flow makes a new version that waits for you.
  3. A bot can only do what its page's permission toggles allow. All toggles start off.
  4. Even then a bot only ASKS. Nothing is changed until you press Approve on the request.
Every change (flows, toggles, box positions, hard stop, approvals) is saved as a new numbered version and written
to the change log, so you can see what changed and go back to any earlier version."""
import json
import threading
import time
from datetime import datetime

SETUP = {"db": None, "lock": None}
_lock = threading.RLock()
_stop_event = threading.Event()
WHO = "You (this computer)"

TRIGGERS = {
    "lead_added": "A new customer is added",
    "stage_changed": "A customer moves to a box",
    "checkin_due": "A check-in text comes due",
    "email_due": "A monthly email comes due",
    "daily": "Every day at a set time",
}
ACTIONS = {
    "move": "Move the customer to a box",
    "note": "Add a note to the customer",
}
FIELDS = {
    "stage": "Box", "project": "Project", "homeowner": "Owns the home", "timeline": "Wants to start",
    "budget_low": "Budget (lowest $)", "job_value": "Job worth ($)", "location": "Where they live",
    "days_in_stage": "Days in this box",
}
OPS = {"is": "is", "is_not": "is not", "at_least": "is at least", "at_most": "is at most", "contains": "contains"}


def setup(db, lock):
    """Creates the tables for bot settings, the change log and bot requests."""
    SETUP["db"], SETUP["lock"] = db, lock
    with SETUP["lock"], _db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS flow_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, key TEXT, version INTEGER,
            value TEXT, ts TEXT, note TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS flow_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, who TEXT, action TEXT, target TEXT,
            detail TEXT, before TEXT, after TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS flow_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, flow_id TEXT, flow_version INTEGER, bot TEXT,
            lead_id INTEGER, lead_name TEXT, reason TEXT, actions TEXT, dedupe TEXT UNIQUE,
            status TEXT, decided_at TEXT, result TEXT)""")


def _db():
    return SETUP["db"]()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- versioned store + change log

def get(kind, key, default=None):
    """Loads the newest version of a setting."""
    with _db() as conn:
        r = conn.execute("SELECT value FROM flow_versions WHERE kind=? AND key=? ORDER BY version DESC LIMIT 1",
                         (kind, key)).fetchone()
    return json.loads(r["value"]) if r else default


def _version(kind, key):
    """Finds the newest version number of a setting."""
    with _db() as conn:
        r = conn.execute("SELECT MAX(version) AS v FROM flow_versions WHERE kind=? AND key=?", (kind, key)).fetchone()
    return r["v"] or 0


def put(kind, key, value, action, detail, who=WHO):
    """Saves a new version (never overwrites) and writes the change log."""
    with _lock:
        before = get(kind, key)
        if before == value:
            return _version(kind, key)
        version = _version(kind, key) + 1
        with SETUP["lock"], _db() as conn:
            conn.execute("INSERT INTO flow_versions (kind, key, version, value, ts, note) VALUES (?,?,?,?,?,?)",
                         (kind, key, version, json.dumps(value), now_iso(), detail))
        log(action, f"{kind}:{key} v{version}", detail, before, value, who)
        return version


def log(action, target, detail, before=None, after=None, who=WHO):
    """Writes one line in the change log."""
    with SETUP["lock"], _db() as conn:
        conn.execute("INSERT INTO flow_log (ts, who, action, target, detail, before, after) VALUES (?,?,?,?,?,?,?)",
                     (now_iso(), who, action, target, detail,
                      None if before is None else json.dumps(before), None if after is None else json.dumps(after)))


def read_log(limit=200):
    """Loads the change log, newest first."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM flow_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def history(kind, key):
    """Lists every saved version of a setting, newest first."""
    with _db() as conn:
        rows = conn.execute("SELECT version, value, ts, note FROM flow_versions WHERE kind=? AND key=? ORDER BY version DESC",
                            (kind, key)).fetchall()
    return [{**dict(r), "value": json.loads(r["value"])} for r in rows]


def restore(kind, key, version):
    """Going back is itself a new version, so nothing in the history is ever lost.
    Only switches, flows and box positions can go back. The hard stop and a flow's on/off state can't, so going
    back can never quietly turn a bot on."""
    if kind not in ("perms", "flow", "layout"):
        return {"error": "That can't be put back to an older version."}
    old = next((h for h in history(kind, key) if h["version"] == int(version)), None)
    if not old:
        return {"error": "That version wasn't found."}
    value = old["value"]
    if kind == "flow":
        _save_flow_def(key, value, f"Went back to version {version}")
        return {"ok": True}
    put(kind, key, value, "restore", f"Went back to version {version}")
    return {"ok": True}


# ---------------------------------------------------------------- hard stop

def hard_stopped():
    """Checks whether the hard stop is on."""
    return bool((get("hardstop", "all") or {}).get("on"))


_stop_hooks = []


def on_hard_stop(fn):
    """Other parts of the app (like the Sync Hub) register here to cancel their own waiting work."""
    if fn not in _stop_hooks:
        _stop_hooks.append(fn)


def set_hard_stop(on):
    """Turns the hard stop on or off. Turning it on cancels everything waiting for approval."""
    with _lock:
        put("hardstop", "all", {"on": bool(on)}, "hard-stop", "⛔ HARD STOP turned ON" if on else "▶ Hard stop turned off")
        if on:
            with SETUP["lock"], _db() as conn:
                n = conn.execute("UPDATE flow_runs SET status='stopped', decided_at=?, result=? WHERE status='pending'",
                                 (now_iso(), "Cancelled by hard stop")).rowcount
            if n:
                log("hard-stop", "requests", f"Cancelled {n} request(s) that were waiting for approval")
            for fn in _stop_hooks:
                fn()
    return {"on": hard_stopped()}


# ---------------------------------------------------------------- permissions

def perm_catalog(page):
    """The toggles a page's bot has, built from what the code scan says that page can do."""
    out = []
    # bots never get switches for their own settings, log or requests: they can't change their own rules
    page = {**page, "reads": [r for r in page["reads"] if not r.startswith("table:flow_")],
            "saves": [r for r in page["saves"] if not r.startswith("table:flow_")]}
    for r in page["reads"]:
        out.append({"id": f"watch:{r}", "label": f"Watch {page['_labels'].get(r, r)}",
                    "tip": "Lets the bot notice changes here so a flow can start."})
    for r in page["saves"]:
        out.append({"id": f"change:{r}", "label": f"Change {page['_labels'].get(r, r)}",
                    "tip": "Lets the bot ASK to change this. You still approve every change."})
    for o in page["outside"]:
        if o["kind"] != "mentioned":
            out.append({"id": f"use:{o['name']}", "label": f"Use {o['name']}",
                        "tip": "No flow action uses this yet. Kept off unless you turn it on."})
    return out


def perms(page_id):
    """Loads a page bot's switches."""
    return get("perms", page_id, {}) or {}


def set_perm(page_id, perm, on, page_label, perm_label=None):
    """Turns one of a page bot's switches on or off, saved as a new version."""
    cur = dict(perms(page_id))
    cur[perm] = bool(on)
    put("perms", page_id, cur, "permission", f"{page_label} bot: {'allowed' if on else 'blocked'} \"{perm_label or perm}\"")
    return cur


# ---------------------------------------------------------------- flows

def flows(include_deleted=False):
    """Lists all your flows."""
    with _db() as conn:
        keys = [r["key"] for r in conn.execute("SELECT DISTINCT key FROM flow_versions WHERE kind='flow'")]
    out = []
    for k in keys:
        f = flow(k)
        if f and (include_deleted or not f["deleted"]):
            out.append(f)
    return sorted(out, key=lambda f: f["id"])


def flow(fid):
    """Loads one flow with its version and whether it's on, paused or waiting for approval."""
    d = get("flow", fid)
    if d is None:
        return None
    st = get("flow_state", fid, {}) or {}
    version = _version("flow", fid)
    approved = st.get("approved_version") == version
    status = "deleted" if st.get("deleted") else "paused" if st.get("paused") and approved else \
        "on" if approved else "needs approval"
    return {**d, "id": fid, "version": version, "approved_version": st.get("approved_version"),
            "paused": bool(st.get("paused")), "deleted": bool(st.get("deleted")), "status": status}


def _clean_flow(data):
    trig = data.get("trigger") or {}
    if trig.get("type") not in TRIGGERS:
        raise ValueError("Pick what starts the flow.")
    conds = []
    for c in data.get("conditions") or []:
        if c.get("field") in FIELDS and c.get("op") in OPS:
            conds.append({"field": c["field"], "op": c["op"], "value": str(c.get("value", ""))[:80]})
    acts = []
    for a in data.get("actions") or []:
        if a.get("type") == "move" and a.get("stage"):
            acts.append({"type": "move", "stage": a["stage"]})
        elif a.get("type") == "note" and str(a.get("text", "")).strip():
            acts.append({"type": "note", "text": str(a["text"]).strip()[:300]})
    if not acts:
        raise ValueError("Add at least one thing for the flow to do.")
    t = {"type": trig["type"]}
    if trig["type"] == "stage_changed":
        t["stage"] = trig.get("stage", "")
    if trig["type"] == "daily":
        t["time"] = trig.get("time") or "08:00"
    return {"name": str(data.get("name") or "My flow").strip()[:60], "bot": data.get("bot") or "pipeline",
            "trigger": t, "conditions": conds, "actions": acts}


def _save_flow_def(fid, value, why):
    """Saves a flow as a new version that waits for your approval."""
    with _lock:
        v = put("flow", fid, value, "flow-edit", f"Flow \"{value['name']}\": {why}. Needs your approval.")
        st = dict(get("flow_state", fid, {}) or {})
        st["deleted"] = False
        put("flow_state", fid, st, "flow-state", f"Flow \"{value['name']}\" v{v} is waiting for approval")
    return flow(fid)


def save_flow(data):
    try:
        value = _clean_flow(data)
    except ValueError as e:
        return {"error": str(e)}
    fid = data.get("id") or f"f{int(time.time() * 1000)}"
    return _save_flow_def(fid, value, "saved" if data.get("id") else "created")


def set_flow_state(fid, what, version=None):
    """Approves, pauses, turns back on or removes a flow, and logs it."""
    f = flow(fid)
    if not f:
        return {"error": "That flow wasn't found."}
    st = dict(get("flow_state", fid, {}) or {})
    if what == "approve":
        if version is not None and int(version) != f["version"]:
            return {"error": f"This flow was changed to version {f['version']} since you looked. Check it, then approve again."}
        st.update(approved_version=f["version"], paused=False)
        msg = f"✅ Approved flow \"{f['name']}\" version {f['version']}"
    elif what == "pause":
        st["paused"] = True
        msg = f"⏸ Paused flow \"{f['name']}\""
    elif what == "resume":
        st["paused"] = False
        msg = f"▶ Turned flow \"{f['name']}\" back on"
    elif what == "delete":
        st.update(deleted=True, paused=True)
        msg = f"🗑 Removed flow \"{f['name']}\" (its history is kept)"
    else:
        return {"error": "Unknown change."}
    put("flow_state", fid, st, "flow-" + what, msg)
    if what in ("pause", "delete"):
        _cancel_pending(fid, "Flow was " + ("paused" if what == "pause" else "removed"))
    return flow(fid)


def _cancel_pending(fid, why):
    """Cancels a flow's requests that are still waiting for you."""
    with SETUP["lock"], _db() as conn:
        conn.execute("UPDATE flow_runs SET status='cancelled', decided_at=?, result=? WHERE flow_id=? AND status='pending'",
                     (now_iso(), why, fid))


def needed_perms(f):
    """Lists the switches a flow's bot needs."""
    need = {"watch:table:leads"}
    if f["actions"]:
        need.add("change:table:leads")
    return need


def missing_perms(f):
    """Lists the switches a flow's bot still needs turned on."""
    p = perms(f["bot"])
    return sorted(x for x in needed_perms(f) if not p.get(x))


# ---------------------------------------------------------------- noticing things -> requests

def _days_in_stage(lead):
    """Counts how many days a customer has been in their current box."""
    try:
        return (datetime.now() - datetime.fromisoformat(lead.get("stage_changed_at") or "")).days
    except ValueError:
        return 0


def _num(v):
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def matches(conds, lead):
    """Checks a flow's \"Only if\" rules against a customer."""
    for c in conds:
        have = _days_in_stage(lead) if c["field"] == "days_in_stage" else lead.get(c["field"])
        want, op = c["value"], c["op"]
        if op in ("at_least", "at_most"):
            a, b = _num(have if have is not None else ""), _num(want)
            if a is None or b is None or (a < b if op == "at_least" else a > b):
                return False
        elif op == "is" and str(have or "").lower() != want.lower():
            return False
        elif op == "is_not" and str(have or "").lower() == want.lower():
            return False
        elif op == "contains" and want.lower() not in str(have or "").lower():
            return False
    return True


def _describe(f, lead):
    """Writes a flow's actions in plain words for the request and the log."""
    bits = []
    for a in f["actions"]:
        bits.append(f"move {lead.get('name')} to \"{a['stage']}\"" if a["type"] == "move" else f"add note \"{a['text']}\"")
    return "; ".join(bits)


def _propose(f, lead, why, dedupe):
    """A bot asks. Nothing happens until you approve."""
    with _lock:  # held so the hard stop can't switch on between the check and the request being saved
        if hard_stopped():
            return
        if missing_perms(f):
            log("blocked", f"flow:{f['id']}", f"Flow \"{f['name']}\" wanted to ask about {lead.get('name')}, "
                f"but its bot isn't allowed: {', '.join(missing_perms(f))}", who="bot")
            return
        with SETUP["lock"], _db() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO flow_runs (ts, flow_id, flow_version, bot, lead_id, lead_name, reason, actions,
                   dedupe, status) VALUES (?,?,?,?,?,?,?,?,?, 'pending')""",
                (now_iso(), f["id"], f["version"], f["bot"], lead["id"], lead.get("name"), why,
                 json.dumps(f["actions"]), dedupe))
            made = cur.rowcount
    if made:
        log("request", f"flow:{f['id']}", f"🤖 \"{f['name']}\" asks to {_describe(f, lead)}. Waiting for you.", who="bot")


def active_flows():
    """Lists the flows that are approved and switched on."""
    return [f for f in flows() if f["status"] == "on"]


def event(kind, lead, origin="you"):
    """Called by the app when a customer is added or moves box. Changes made by bots don't start flows."""
    if origin == "bot" or not lead or hard_stopped():
        return
    for f in active_flows():
        t = f["trigger"]
        if t["type"] != kind or (kind == "stage_changed" and t.get("stage") and t["stage"] != lead.get("stage")):
            continue
        if matches(f["conditions"], lead):
            stamp = lead.get("stage_changed_at") if kind == "stage_changed" else lead.get("created_at")
            _propose(f, lead, TRIGGERS[kind], f"{f['id']}:{f['version']}:{lead['id']}:{kind}:{stamp}")


def tick(all_leads):
    """Checks the time-based triggers. Runs about once a minute in the background."""
    if hard_stopped():
        return
    fl = [f for f in active_flows() if f["trigger"]["type"] in ("checkin_due", "email_due", "daily")]
    if not fl:
        return
    leads = all_leads()
    today = datetime.now().date().isoformat()
    for f in fl:
        t = f["trigger"]
        if t["type"] == "daily" and datetime.now().strftime("%H:%M") < t.get("time", "08:00"):
            continue
        for lead in leads:
            if t["type"] == "checkin_due" and lead.get("protocol_b_ready") and not lead.get("protocol_b_sent_at"):
                key = "checkin"
            elif t["type"] == "email_due":
                due = [n for n in lead.get("nurture") or [] if not n["sent"] and n["due"] <= today]
                if not due:
                    continue
                key = f"email{due[0]['month']}"
            elif t["type"] == "daily":
                key = today
            else:
                continue
            if matches(f["conditions"], lead):
                _propose(f, lead, TRIGGERS[t["type"]], f"{f['id']}:{f['version']}:{lead['id']}:{key}")


def start_watcher(all_leads):
    """Starts the background check, once a minute, for time-based flows."""
    def loop():
        while not _stop_event.is_set():
            try:
                tick(all_leads)
            except Exception as e:  # keep watching even if one check fails
                log("error", "watcher", f"Check failed: {e}", who="bot")
            _stop_event.wait(60)
    threading.Thread(target=loop, daemon=True, name="flow-watcher").start()


# ---------------------------------------------------------------- approving requests

def runs(status=None, limit=100):
    """Lists bot requests, optionally only the waiting ones."""
    with _db() as conn:
        if status:
            rows = conn.execute("SELECT * FROM flow_runs WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
        else:
            rows = conn.execute("SELECT * FROM flow_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "actions": json.loads(r["actions"] or "[]")} for r in rows]


def decide(run_id, approve, execute):
    """The only place a bot's change is ever made. Every safety rule is checked again right here."""
    with _lock:
        with _db() as conn:
            r = conn.execute("SELECT * FROM flow_runs WHERE id=?", (run_id,)).fetchone()
        if not r or r["status"] != "pending":
            return {"error": "That request was already handled."}
        r = dict(r)
        if not approve:
            _finish(run_id, "rejected", "You said no")
            log("reject", f"run:{run_id}", f"❌ Said no to \"{r['reason']}\" for {r['lead_name']}")
            return {"ok": True}
        if hard_stopped():
            return {"error": "⛔ Hard stop is on. Nothing can run until you turn it off."}
        f = flow(r["flow_id"])
        if not f or f["status"] != "on" or f["version"] != r["flow_version"]:
            _finish(run_id, "cancelled", "The flow changed or was turned off after it asked")
            return {"error": "That flow changed or was turned off after it asked, so the request was cancelled."}
        if missing_perms(f):
            return {"error": "The bot isn't allowed to do that anymore: " + ", ".join(missing_perms(f))}
        done = []
        try:
            for a in json.loads(r["actions"]):
                execute(a, r["lead_id"])
                done.append(a)
        except Exception as e:
            _finish(run_id, "failed", f"Stopped after {len(done)} step(s): {e}")
            log("failed", f"run:{run_id}", f"⚠ \"{f['name']}\" failed for {r['lead_name']}: {e}")
            return {"error": f"It didn't work: {e}"}
        _finish(run_id, "done", "Done")
        log("approve", f"run:{run_id}", f"✅ Approved: \"{f['name']}\" did {_describe(f, {'name': r['lead_name']})}")
        return {"ok": True}


def _finish(run_id, status, result):
    """Marks a bot request as done, refused, cancelled or failed."""
    with SETUP["lock"], _db() as conn:
        conn.execute("UPDATE flow_runs SET status=?, decided_at=?, result=? WHERE id=?", (status, now_iso(), result, run_id))


# ---------------------------------------------------------------- what the Flow Map page shows

def bots_for(scan):
    """Works out each page bot's switches, flows and waiting requests for the map."""
    out = {}
    fl = flows()
    pending = runs("pending")
    for p in scan["pages"]:
        if p.get("background"):
            continue
        page = {**p, "_labels": scan["resources"]}
        cat = perm_catalog(page)
        cur = perms(p["id"])
        mine = [f for f in fl if f["bot"] == p["id"]]
        out[p["id"]] = {
            "perms": [{**c, "on": bool(cur.get(c["id"]))} for c in cat],
            "allowed": sum(1 for c in cat if cur.get(c["id"])),
            "flows": [f["id"] for f in mine],
            "active": sum(1 for f in mine if f["status"] == "on"),
            "pending": sum(1 for r in pending if r["bot"] == p["id"]),
            "can_run_flows": "table:leads" in p["saves"],
        }
    return out


def options():
    """Lists the choices for the flow editor: triggers, actions, fields and checks."""
    return {"triggers": TRIGGERS, "actions": ACTIONS, "fields": FIELDS, "ops": OPS}
