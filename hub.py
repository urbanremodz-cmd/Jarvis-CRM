"""🔗 Sync Hub: one master copy of every customer, kept in step across all your CRMs.

How it works:
  1. Each connected CRM (Remod Flow itself, GoHighLevel, ...) is PULLED. Pulling only reads that CRM.
  2. People are matched across CRMs by the link we already know, then by email, then by phone, so the same
     person is one master record, not duplicates.
  3. Each field of a master record remembers its value, which CRM it came from and when. When two CRMs changed
     the same field, the field's rule decides: the newest edit wins, one CRM is the boss for that field,
     or you're asked.
  4. When the master changes, the hub works out what each OTHER CRM is missing and puts it in the outbox.
     Nothing is written into any CRM until you press Approve, and only if writing to that CRM is switched on.
  5. ⛔ HARD STOP (the same one as the Flow Map) freezes pulling and writing and cancels the outbox.
Every setting change, master record change and approval is versioned and written to the Flow Map change log.
API keys are kept only in this computer's database, never in the change log and never sent to the screen."""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import bots

SETUP = {"db": None, "lock": None, "local": None}
_lock = threading.RLock()
_stop_event = threading.Event()

# The hub's own field names. Every connector translates its CRM's fields to and from these.
FIELDS = {
    "name": "Name", "email": "Email", "phone": "Phone", "location": "City", "project": "Project",
    "stage": "Pipeline stage", "job_value": "Job worth ($)", "timeline": "Wants to start",
    "homeowner": "Owns the home", "budget": "Budget",
}
RULES = {"newest": "Newest edit wins", "ask": "Ask me"}  # or the id of a CRM that is always right
DEFAULT_RULE = "newest"
AUTO_PULL_MINUTES = 5


def setup(db, lock, local):
    """Creates the Sync Hub's tables and connects it to the hard stop."""
    SETUP.update(db=db, lock=lock, local=local)
    CONNECTORS[local.id] = local
    with SETUP["lock"], db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS hub_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, version INTEGER, created_at TEXT, updated_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS hub_record_versions (
            record_id INTEGER, version INTEGER, data TEXT, ts TEXT, why TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS hub_links (
            system TEXT, ext_id TEXT, record_id INTEGER, seen TEXT, synced_at TEXT, PRIMARY KEY (system, ext_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS hub_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, system TEXT, record_id INTEGER, ext_id TEXT,
            fields TEXT, status TEXT, decided_at TEXT, result TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS hub_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, record_id INTEGER, field TEXT, current TEXT,
            incoming TEXT, status TEXT, decided_at TEXT)""")
        conn.execute("CREATE TABLE IF NOT EXISTS hub_secrets (system TEXT, key TEXT, value TEXT, PRIMARY KEY (system, key))")
    bots.on_hard_stop(_cancel_outbox)


def _db():
    return SETUP["db"]()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def local_time(iso):
    """A CRM's time (often UTC, like 2026-10-03T18:00:00.000Z) as this computer's local time, to compare fairly."""
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t.astimezone() if t.tzinfo else t).replace(tzinfo=None).isoformat(timespec="seconds")


def norm_email(v):
    """Tidies an email address so matches aren't missed."""
    return str(v or "").strip().lower()


def norm_phone(v):
    """Keeps just the last 10 digits of a phone number so matches aren't missed."""
    digits = re.sub(r"\D", "", str(v or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def _same(field, a, b):
    """Checks whether two values are really the same (ignoring case in emails and formatting in phone numbers)."""
    if field == "email":
        return norm_email(a) == norm_email(b)
    if field == "phone":
        return norm_phone(a) == norm_phone(b)
    return str(a if a is not None else "").strip() == str(b if b is not None else "").strip()


# ---------------------------------------------------------------- settings (versioned through bots.put)

def system_settings(sid):
    """enabled: pull this CRM. can_write: let approved changes be written into it. Both start off."""
    return {"enabled": False, "can_write": False, **(bots.get("hub_system", sid, {}) or {})}


def set_system(sid, what, on):
    if sid not in CONNECTORS or what not in ("enabled", "can_write"):
        return {"error": "Unknown setting."}
    cur = system_settings(sid)
    cur[what] = bool(on)
    label = CONNECTORS[sid].label
    words = {"enabled": "syncing", "can_write": "writing approved changes into it"}[what]
    bots.put("hub_system", sid, cur, "hub-setting", f"🔗 {label}: {words} turned {'on' if on else 'off'}")
    if what == "can_write" and not on:
        _cancel_outbox(f"Writing to {label} was turned off", system=sid)
    return cur


def rules():
    """Loads who wins for each field when CRMs disagree."""
    return {f: DEFAULT_RULE for f in FIELDS} | (bots.get("hub_rules", "fields", {}) or {})


def set_rule(field, rule):
    if field not in FIELDS or (rule not in RULES and rule not in CONNECTORS):
        return {"error": "Unknown rule."}
    cur = dict(bots.get("hub_rules", "fields", {}) or {})
    cur[field] = rule
    who = RULES.get(rule) or f"{CONNECTORS[rule].label} is always right"
    bots.put("hub_rules", "fields", cur, "hub-rule", f"🔗 {FIELDS[field]}: {who}")
    return rules()


def secret(sid, key):
    """Reads a saved key (like an API token) from this computer's database."""
    with _db() as conn:
        r = conn.execute("SELECT value FROM hub_secrets WHERE system=? AND key=?", (sid, key)).fetchone()
    return r["value"] if r else ""


def set_secrets(sid, values):
    """Saves keys like the API key. Only a note that it changed goes in the log, never the key itself."""
    c = CONNECTORS.get(sid)
    if not c:
        return {"error": "Unknown CRM."}
    changed = []
    with SETUP["lock"], _db() as conn:
        for key, label in c.secret_fields.items():
            if key in values and str(values[key]).strip():
                conn.execute("INSERT OR REPLACE INTO hub_secrets (system, key, value) VALUES (?,?,?)",
                             (sid, key, str(values[key]).strip()))
                changed.append(label)
    if changed:
        bots.log("hub-secret", f"hub_system:{sid}", f"🔑 {c.label}: saved {', '.join(changed)} (kept on this computer)")
    return {"ok": True}


# ---------------------------------------------------------------- master records

def _record(conn, rid):
    """Loads one master record."""
    r = conn.execute("SELECT * FROM hub_records WHERE id=?", (rid,)).fetchone()
    return {**dict(r), "data": json.loads(r["data"])} if r else None


def record(rid):
    with _db() as conn:
        return _record(conn, rid)


def records(limit=500):
    """Lists master records with the CRMs each one is linked to."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM hub_records ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        links = conn.execute("SELECT system, ext_id, record_id, synced_at FROM hub_links").fetchall()
    by_rec = {}
    for l in links:
        by_rec.setdefault(l["record_id"], []).append({"system": l["system"], "ext_id": l["ext_id"], "synced_at": l["synced_at"]})
    return [{**dict(r), "data": json.loads(r["data"]), "links": by_rec.get(r["id"], [])} for r in rows]


def record_history(rid):
    """Lists every version of a master record."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM hub_record_versions WHERE record_id=? ORDER BY version DESC", (rid,)).fetchall()
    return [{**dict(r), "data": json.loads(r["data"])} for r in rows]


def _save_record(conn, rid, data, why):
    """Saves a master record as a new version."""
    ts = now_iso()
    if rid is None:
        cur = conn.execute("INSERT INTO hub_records (data, version, created_at, updated_at) VALUES (?,1,?,?)",
                           (json.dumps(data), ts, ts))
        rid, version = cur.lastrowid, 1
    else:
        version = conn.execute("SELECT version FROM hub_records WHERE id=?", (rid,)).fetchone()["version"] + 1
        conn.execute("UPDATE hub_records SET data=?, version=?, updated_at=? WHERE id=?", (json.dumps(data), version, ts, rid))
    conn.execute("INSERT INTO hub_record_versions (record_id, version, data, ts, why) VALUES (?,?,?,?,?)",
                 (rid, version, json.dumps(data), ts, why))
    return rid


def _find_match(conn, sid, ext_id, fields):
    """The same person: a link we already know, then the same email, then the same phone number."""
    r = conn.execute("SELECT record_id FROM hub_links WHERE system=? AND ext_id=?", (sid, str(ext_id))).fetchone()
    if r:
        return r["record_id"], None
    email, phone = norm_email(fields.get("email")), norm_phone(fields.get("phone"))
    if not email and not phone:
        return None, None
    taken = {x["record_id"] for x in conn.execute("SELECT record_id FROM hub_links WHERE system=?", (sid,))}
    for row in conn.execute("SELECT id, data FROM hub_records"):
        if row["id"] in taken:  # already matched to another record in this same CRM
            continue
        d = json.loads(row["data"])
        if email and norm_email((d.get("email") or {}).get("v")) == email:
            return row["id"], "same email"
        if phone and len(phone) >= 7 and norm_phone((d.get("phone") or {}).get("v")) == phone:
            return row["id"], "same phone"
    return None, None


def _link(conn, sid, ext_id, rid, seen):
    """Remembers which record in a CRM is which master record, and what that CRM last had."""
    conn.execute("INSERT OR REPLACE INTO hub_links (system, ext_id, record_id, seen, synced_at) VALUES (?,?,?,?,?)",
                 (sid, str(ext_id), rid, json.dumps(seen), now_iso()))


# ---------------------------------------------------------------- pulling a CRM into the hub

def _merge(conn, rid, data, sid, changed, seen, at, rule_for):
    """Applies the fields a CRM changed to one master record. Returns (new data, what changed, conflicts).
    seen is what this CRM had the last time we looked or wrote to it."""
    data = {k: dict(v) for k, v in data.items()}
    applied, conflicts = [], []
    for f, v in changed.items():
        cur = data.get(f)
        if cur is None or cur.get("v") in (None, ""):
            data[f] = {"v": v, "src": sid, "at": at}
            applied.append(f)
            continue
        if _same(f, cur["v"], v):
            continue
        # the master holds a value from another CRM that this CRM never got, and this CRM changed it too:
        # that's a real disagreement
        both_changed = cur["src"] != sid and not _same(f, seen.get(f), cur["v"])
        rule = rule_for(f)
        if both_changed and rule == "newest" and f not in seen:
            # first time these two CRMs are matched and they already disagree: there's no fair "newest", so ask
            rule = "ask"
        if not both_changed or rule == "newest" and at >= cur["at"] or rule == sid:
            data[f] = {"v": v, "src": sid, "at": at}
            applied.append(f)
        elif rule == "ask":
            open_ = conn.execute("SELECT id FROM hub_conflicts WHERE record_id=? AND field=? AND status='open'",
                                 (rid, f)).fetchone()
            inc = json.dumps({"v": v, "src": sid, "at": at})
            if open_:
                conn.execute("UPDATE hub_conflicts SET incoming=?, ts=? WHERE id=?", (inc, now_iso(), open_["id"]))
            else:
                conn.execute("INSERT INTO hub_conflicts (ts, record_id, field, current, incoming, status) VALUES (?,?,?,?,?,'open')",
                             (now_iso(), rid, f, json.dumps(cur), inc))
            conflicts.append(f)
        # otherwise the master's value wins (a newer edit, or the boss CRM); the outbox sends it back
    return data, applied, conflicts


def pull(sid):
    """Reads one CRM and updates the master records. Never writes to any CRM."""
    if bots.hard_stopped():
        return {"error": "⛔ Hard stop is on. Syncing is frozen."}
    c = CONNECTORS.get(sid)
    if not c:
        return {"error": "Unknown CRM."}
    if not system_settings(sid)["enabled"]:
        return {"error": f"Syncing {c.label} is off."}
    try:
        items = c.fetch()
    except Exception as e:
        bots.log("hub-error", f"hub_system:{sid}", f"⚠ Couldn't read {c.label}: {e}", who="hub")
        _status[sid] = {"at": now_iso(), "ok": False, "msg": str(e)}
        return {"error": f"Couldn't read {c.label}: {e}"}
    rule_for = _rule_for(rules())
    stats = {"new": 0, "matched": 0, "updated": 0, "conflicts": 0}
    touched, notes = set(), []
    with _lock:
        for it in items:
            if bots.hard_stopped():
                break
            fields = {k: v for k, v in it["fields"].items() if k in FIELDS and k in c.fields and v not in (None, "")}
            at = it.get("updated_at") or now_iso()
            with SETUP["lock"], _db() as conn:
                link = conn.execute("SELECT * FROM hub_links WHERE system=? AND ext_id=?", (sid, str(it["id"]))).fetchone()
                seen = json.loads(link["seen"]) if link else {}
                changed = {f: v for f, v in fields.items() if not _same(f, seen.get(f), v)}
                rid, how = _find_match(conn, sid, it["id"], fields)
                if rid is None:
                    data = {f: {"v": v, "src": sid, "at": at} for f, v in fields.items()}
                    rid = _save_record(conn, None, data, f"New from {c.label}")
                    stats["new"] += 1
                    notes.append(("hub-new", f"hub_record:{rid}", f"🔗 New master record from {c.label}: {fields.get('name') or fields.get('email') or it['id']}", None, None))
                else:
                    if how:
                        stats["matched"] += 1
                        notes.append(("hub-match", f"hub_record:{rid}", f"🔗 Matched {c.label} {fields.get('name') or it['id']} to master record #{rid} ({how})", None, None))
                    if changed:
                        old = _record(conn, rid)["data"]
                        data, applied, confl = _merge(conn, rid, old, sid, changed, seen, at, rule_for)
                        stats["conflicts"] += len(confl)
                        if applied:
                            _save_record(conn, rid, data, f"{c.label} changed {', '.join(FIELDS[f] for f in applied)}")
                            stats["updated"] += 1
                            notes.append(("hub-update", f"hub_record:{rid}",
                                          f"🔗 {c.label} changed {', '.join(FIELDS[f] for f in applied)} on master record #{rid}",
                                          {f: old.get(f, {}).get("v") for f in applied}, {f: data[f]["v"] for f in applied}))
                # remember what this CRM has, so its next pull only reports real changes
                _link(conn, sid, it["id"], rid, {**seen, **fields})
            touched.add(rid)
    for action, target, detail, before, after in notes:  # logged after the database lock is let go
        bots.log(action, target, detail, before, after, who="hub")
    for rid in touched:
        plan_outbox(rid)
    _status[sid] = {"at": now_iso(), "ok": True, "msg": f"{len(items)} read"}
    if stats["new"] or stats["updated"] or stats["conflicts"]:
        bots.log("hub-pull", f"hub_system:{sid}", f"🔗 Synced {c.label}: {stats['new']} new, {stats['updated']} updated, "
                 f"{stats['conflicts']} need you", who="hub")
    return {"ok": True, **stats, "read": len(items)}


def _rule_for(rule_map):
    return lambda f: rule_map.get(f, DEFAULT_RULE)


_status = {}


# ---------------------------------------------------------------- the outbox: what each CRM is missing

def plan_outbox(rid):
    """Works out what every other enabled CRM needs so it matches the master. Only ASKS, never writes."""
    if bots.hard_stopped():
        return
    with _lock, SETUP["lock"], _db() as conn:
        rec = _record(conn, rid)
        if not rec:
            return
        links = {l["system"]: l for l in conn.execute("SELECT * FROM hub_links WHERE record_id=?", (rid,))}
        unsettled = {r["field"] for r in conn.execute("SELECT field FROM hub_conflicts WHERE record_id=? AND status='open'", (rid,))}
        for sid, c in CONNECTORS.items():
            if not system_settings(sid)["enabled"] or not c.ready()[0]:
                continue
            link = links.get(sid)
            seen = json.loads(link["seen"]) if link else {}
            want = {f: v["v"] for f, v in rec["data"].items()
                    if f in c.fields and v.get("v") not in (None, "") and f not in unsettled}  # wait until you pick
            diff = {f: v for f, v in want.items() if not _same(f, seen.get(f), v)}
            if link is None and not c.can_create(want):
                continue
            pending = conn.execute("SELECT id FROM hub_outbox WHERE system=? AND record_id=? AND status='pending'",
                                   (sid, rid)).fetchone()
            if not diff:
                if pending:  # the CRM caught up on its own
                    conn.execute("UPDATE hub_outbox SET status='cancelled', decided_at=?, result=? WHERE id=?",
                                 (now_iso(), "Not needed anymore", pending["id"]))
                continue
            said_no = conn.execute("SELECT fields FROM hub_outbox WHERE system=? AND record_id=? AND status='rejected' "
                                   "ORDER BY id DESC LIMIT 1", (sid, rid)).fetchone()
            if said_no and json.loads(said_no["fields"]) == diff:
                continue  # you already said no to exactly this; don't ask again until something changes
            if pending:  # one request per CRM per record, always showing the latest
                conn.execute("UPDATE hub_outbox SET fields=?, ext_id=?, ts=? WHERE id=?",
                             (json.dumps(diff), link["ext_id"] if link else None, now_iso(), pending["id"]))
            else:
                conn.execute("INSERT INTO hub_outbox (ts, system, record_id, ext_id, fields, status) VALUES (?,?,?,?,?,'pending')",
                             (now_iso(), sid, rid, link["ext_id"] if link else None, json.dumps(diff)))


def outbox(status="pending", limit=200):
    """Lists changes waiting for your approval before they're written into a CRM."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM hub_outbox WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        out = []
        for r in rows:
            rec = _record(conn, r["record_id"])
            out.append({**dict(r), "fields": json.loads(r["fields"]),
                        "name": ((rec or {}).get("data", {}).get("name") or {}).get("v") or f"Record #{r['record_id']}",
                        "system_label": CONNECTORS[r["system"]].label if r["system"] in CONNECTORS else r["system"]})
    return out


def decide(oid, approve):
    """The only place the hub writes into a CRM. Every safety rule is checked again right here."""
    with _lock:
        with _db() as conn:
            r = conn.execute("SELECT * FROM hub_outbox WHERE id=?", (oid,)).fetchone()
        if not r or r["status"] != "pending":
            return {"error": "That change was already handled."}
        r = dict(r)
        c = CONNECTORS.get(r["system"])
        fields = json.loads(r["fields"])
        if not approve:
            _finish(oid, "rejected", "You said no")
            bots.log("hub-reject", f"hub_record:{r['record_id']}", f"❌ Said no to writing {_fmt(fields)} into {c.label if c else r['system']}")
            return {"ok": True}
        if bots.hard_stopped():
            return {"error": "⛔ Hard stop is on. Nothing can be written until you turn it off."}
        if not c or not system_settings(r["system"])["enabled"]:
            return {"error": "Syncing that CRM is off."}
        if not system_settings(r["system"])["can_write"]:
            return {"error": f"Writing into {c.label} is switched off. Turn on \"Write approved changes\" for it first."}
        with _db() as conn:  # the person may have been matched in this CRM since the request was made
            link = conn.execute("SELECT ext_id FROM hub_links WHERE system=? AND record_id=?", (r["system"], r["record_id"])).fetchone()
        try:
            ext_id = c.write(link["ext_id"] if link else r["ext_id"], fields)
        except Exception as e:
            _finish(oid, "failed", str(e))
            bots.log("hub-failed", f"hub_record:{r['record_id']}", f"⚠ Writing into {c.label} failed: {e}")
            return {"error": f"It didn't work: {e}"}
        with SETUP["lock"], _db() as conn:
            link = conn.execute("SELECT seen FROM hub_links WHERE system=? AND record_id=?", (r["system"], r["record_id"])).fetchone()
            seen = json.loads(link["seen"]) if link else {}
            _link(conn, r["system"], ext_id, r["record_id"], {**seen, **fields})
        _finish(oid, "done", "Done")
        bots.log("hub-write", f"hub_record:{r['record_id']}", f"✅ Approved: wrote {_fmt(fields)} into {c.label}")
        return {"ok": True}


def decide_all(approve, system=None):
    done, errors = 0, []
    for r in outbox():
        if system and r["system"] != system:
            continue
        res = decide(r["id"], approve)
        if res.get("error"):
            errors.append(res["error"])
            if bots.hard_stopped():
                break
        else:
            done += 1
    return {"ok": True, "done": done, "errors": sorted(set(errors))}


def _fmt(fields):
    return ", ".join(f"{FIELDS.get(k, k)} = \"{v}\"" for k, v in fields.items())


def _finish(oid, status, result):
    with SETUP["lock"], _db() as conn:
        conn.execute("UPDATE hub_outbox SET status=?, decided_at=?, result=? WHERE id=?", (status, now_iso(), result, oid))


def _cancel_outbox(why="Cancelled by hard stop", system=None):
    """Cancels changes waiting to be written into CRMs (by the hard stop, or when writing is switched off)."""
    with SETUP["lock"], _db() as conn:
        q = "UPDATE hub_outbox SET status='stopped', decided_at=?, result=? WHERE status='pending'"
        args = [now_iso(), why]
        if system:
            q += " AND system=?"
            args.append(system)
        n = conn.execute(q, args).rowcount
    if n:
        bots.log("hub-cancel", "hub_outbox", f"Cancelled {n} change(s) waiting to be written: {why}")


# ---------------------------------------------------------------- conflicts

def conflicts(status="open"):
    """Lists the fields where two CRMs disagree and you need to pick."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM hub_conflicts WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        out = []
        for r in rows:
            rec = _record(conn, r["record_id"])
            out.append({**dict(r), "current": json.loads(r["current"]), "incoming": json.loads(r["incoming"]),
                        "field_label": FIELDS.get(r["field"], r["field"]),
                        "name": ((rec or {}).get("data", {}).get("name") or {}).get("v") or f"Record #{r['record_id']}"})
    return out


def resolve(cid, pick):
    """You pick which value is right. The master takes it and the outbox sends it to the CRMs that differ."""
    with _lock, SETUP["lock"], _db() as conn:
        r = conn.execute("SELECT * FROM hub_conflicts WHERE id=?", (cid,)).fetchone()
        if not r or r["status"] != "open":
            return {"error": "That was already settled."}
        if pick not in ("current", "incoming"):
            return {"error": "Pick one of the two values."}
        chosen = json.loads(r[pick])
        rec = _record(conn, r["record_id"])
        data = rec["data"]
        before = (data.get(r["field"]) or {}).get("v")
        data[r["field"]] = {"v": chosen["v"], "src": chosen["src"], "at": now_iso()}
        _save_record(conn, r["record_id"], data, f"You picked {FIELDS[r['field']]} = {chosen['v']}")
        conn.execute("UPDATE hub_conflicts SET status='settled', decided_at=? WHERE id=?", (now_iso(), cid))
    src = CONNECTORS[chosen["src"]].label if chosen["src"] in CONNECTORS else chosen["src"]
    bots.log("hub-resolve", f"hub_record:{r['record_id']}", f"⚖ You picked {FIELDS[r['field']]} = \"{chosen['v']}\" (from {src})",
             before, chosen["v"])
    plan_outbox(r["record_id"])
    return {"ok": True}


# ---------------------------------------------------------------- connectors

class Connector:
    id, label = "", ""
    fields = ()            # which hub fields this CRM has
    secret_fields = {}     # key -> label, typed in on the Sync Hub page

    def fetch(self):
        """Returns [{"id": its own id, "updated_at": iso time or None, "fields": {hub field: value}}]."""
        raise NotImplementedError

    def write(self, ext_id, fields):
        """Creates (ext_id None) or updates one record with hub fields. Returns its own id."""
        raise NotImplementedError

    def can_create(self, fields):
        return bool(fields.get("name") or fields.get("email") or fields.get("phone"))

    def ready(self):
        return True, "Ready"


class LocalConnector(Connector):
    """Remod Flow's own customer list. Its functions come from app.py so the hub uses the same rules as the app."""
    id, label = "remodflow", "Remod Flow (this app)"
    fields = ("name", "email", "phone", "location", "project", "stage", "job_value", "timeline", "homeowner", "budget")

    def __init__(self, all_leads, update_lead, create_lead, stages):
        self.all_leads, self.update_lead, self.create_lead, self.stages = all_leads, update_lead, create_lead, stages

    def fetch(self):
        # leads don't record when they were last edited, so a change counts from when the hub first sees it
        return [{"id": str(l["id"]), "updated_at": None,
                 "fields": {f: l.get(f) for f in self.fields}} for l in self.all_leads()]

    def write(self, ext_id, fields):
        data = {k: v for k, v in fields.items() if k in self.fields}
        if "stage" in data and data["stage"] not in self.stages:
            raise ValueError(f"Remod Flow has no box called \"{data['stage']}\". Rename the stage or say No")
        if ext_id is None:
            return str(self.create_lead(data)["id"])
        if not self.update_lead(int(ext_id), data, origin="hub"):
            raise ValueError("that customer was deleted")
        return str(ext_id)


class GoHighLevelConnector(Connector):
    """GoHighLevel (API v2, private integration token). Contacts, plus each contact's opportunity stage and worth in
    the pipeline you pick. Stages are matched by name, so name your GoHighLevel stages like your Remod Flow boxes."""
    id, label = "ghl", "GoHighLevel"
    fields = ("name", "email", "phone", "location", "stage", "job_value")
    secret_fields = {"token": "Private integration token", "location_id": "Location (sub-account) ID",
                     "pipeline_id": "Pipeline ID (optional, for stages)"}
    BASE = os.environ.get("GHL_BASE_URL") or "https://services.leadconnectorhq.com"  # override only for testing
    VERSION = "2021-07-28"

    def ready(self):
        if not secret(self.id, "token") or not secret(self.id, "location_id"):
            return False, "Add your token and location ID"
        return True, "Ready"

    def _call(self, method, path, body=None, query=None):
        """Sends one request to GoHighLevel with your token and reads the answer."""
        url = self.BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {secret(self.id, 'token')}", "Version": self.VERSION,
                                              "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            raise RuntimeError(f"GoHighLevel said {e.code}: {detail}") from None

    def _stages(self):
        """Reads your GoHighLevel pipeline's stage names."""
        pid = secret(self.id, "pipeline_id")
        if not pid:
            return None, {}
        data = self._call("GET", "/opportunities/pipelines", query={"locationId": secret(self.id, "location_id")})
        for p in data.get("pipelines", []):
            if p.get("id") == pid:
                return pid, {s["id"]: s["name"] for s in p.get("stages", [])}
        raise RuntimeError("that pipeline ID wasn't found in this location")

    def _opportunities(self, pid):
        """Reads the GoHighLevel pipeline's opportunities, to get each contact's stage and job worth."""
        out, page = {}, 1
        while pid:
            data = self._call("GET", "/opportunities/search", query={"location_id": secret(self.id, "location_id"),
                                                                     "pipeline_id": pid, "limit": 100, "page": page})
            for o in data.get("opportunities", []):
                cid = o.get("contactId") or (o.get("contact") or {}).get("id")
                if cid:
                    out.setdefault(cid, o)
            if len(data.get("opportunities", [])) < 100:
                break
            page += 1
        return out

    def fetch(self):
        loc = secret(self.id, "location_id")
        pid, stages = self._stages()
        opps = self._opportunities(pid)
        out, after = [], {}
        for _ in range(200):  # up to 20,000 contacts
            data = self._call("GET", "/contacts/", query={"locationId": loc, "limit": 100, **after})
            batch = data.get("contacts", [])
            for c in batch:
                name = (c.get("contactName") or f"{c.get('firstName') or ''} {c.get('lastName') or ''}").strip()
                f = {"name": name.title() if name.islower() else name, "email": c.get("email"), "phone": c.get("phone"),
                     "location": c.get("city")}
                o = opps.get(c["id"])
                if o:
                    f["stage"] = stages.get(o.get("pipelineStageId"))
                    f["job_value"] = int(o["monetaryValue"]) if o.get("monetaryValue") else None
                out.append({"id": c["id"], "updated_at": local_time(c.get("dateUpdated")), "fields": f})
            meta = data.get("meta") or {}
            if len(batch) < 100 or not meta.get("startAfterId"):
                break
            after = {"startAfterId": meta["startAfterId"], "startAfter": meta.get("startAfter")}
        return out

    def write(self, ext_id, fields):
        body = {}
        if "name" in fields:
            first, _, last = str(fields["name"]).strip().partition(" ")
            body.update(firstName=first, lastName=last)
        for hub_f, ghl_f in (("email", "email"), ("phone", "phone"), ("location", "city")):
            if hub_f in fields:
                body[ghl_f] = fields[hub_f]
        if ext_id is None:
            body["locationId"] = secret(self.id, "location_id")
            ext_id = self._call("POST", "/contacts/upsert", body)["contact"]["id"]
        elif body:
            self._call("PUT", f"/contacts/{ext_id}", body)
        if "stage" in fields or "job_value" in fields:
            self._write_opportunity(ext_id, fields)
        return ext_id

    def _write_opportunity(self, cid, fields):
        pid, stages = self._stages()
        if not pid:
            return
        by_name = {v.lower(): k for k, v in stages.items()}
        body = {}
        if "stage" in fields:
            sid = by_name.get(str(fields["stage"]).lower())
            if not sid:
                raise RuntimeError(f"your GoHighLevel pipeline has no stage called \"{fields['stage']}\"")
            body["pipelineStageId"] = sid
        if "job_value" in fields:
            body["monetaryValue"] = fields["job_value"]
        found = self._call("GET", "/opportunities/search", query={"location_id": secret(self.id, "location_id"),
                                                                  "pipeline_id": pid, "contact_id": cid})
        opp = (found.get("opportunities") or [None])[0]
        if opp:
            self._call("PUT", f"/opportunities/{opp['id']}", body)
        elif "pipelineStageId" in body:
            self._call("POST", "/opportunities/", {**body, "pipelineId": pid, "locationId": secret(self.id, "location_id"),
                                                   "contactId": cid, "name": fields.get("name") or "Remod Flow job",
                                                   "status": "open"})


CONNECTORS = {"ghl": GoHighLevelConnector()}


# ---------------------------------------------------------------- background pulling + what the page shows

def start_auto_pull():
    """Every few minutes, pulls each CRM that's switched on. Pulling only reads; writing always waits for you."""
    def loop():
        while not _stop_event.wait(AUTO_PULL_MINUTES * 60):
            for sid in list(CONNECTORS):
                if system_settings(sid)["enabled"] and CONNECTORS[sid].ready()[0] and not bots.hard_stopped():
                    try:
                        pull(sid)
                    except Exception as e:  # keep going even if one CRM fails
                        bots.log("hub-error", f"hub_system:{sid}", f"Sync failed: {e}", who="hub")
    threading.Thread(target=loop, daemon=True, name="hub-auto-pull").start()


def overview():
    """Gathers everything the Sync Hub page shows: CRMs, master record count, waiting changes and disagreements."""
    systems = []
    with _db() as conn:
        counts = {r["system"]: r["n"] for r in conn.execute("SELECT system, COUNT(*) AS n FROM hub_links GROUP BY system")}
        n_records = conn.execute("SELECT COUNT(*) AS n FROM hub_records").fetchone()["n"]
    pending = outbox()
    for sid, c in sorted(CONNECTORS.items(), key=lambda kv: kv[1] is not SETUP["local"]):  # this app first
        ok, why = c.ready()
        systems.append({"id": sid, "label": c.label, **system_settings(sid), "ready": ok, "ready_msg": why,
                        "linked": counts.get(sid, 0), "fields": [FIELDS[f] for f in c.fields],
                        "secrets": [{"key": k, "label": v, "set": bool(secret(sid, k))} for k, v in c.secret_fields.items()],
                        "last": _status.get(sid), "pending": sum(1 for p in pending if p["system"] == sid)})
    return {"systems": systems, "records": n_records, "outbox": pending, "conflicts": conflicts(),
            "rules": rules(), "rule_names": RULES, "fields": FIELDS, "hard_stop": bots.hard_stopped(),
            "auto_minutes": AUTO_PULL_MINUTES}
