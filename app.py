"""Remod Flow CRM - local web app. Runs on http://127.0.0.1:8765 using only Python's standard library."""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import bots
import flowmap
import guide
import videos
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("REMODFLOW_DB") or os.path.join(HERE, "remodflow.db")
PORT = int(os.environ.get("REMODFLOW_PORT") or 8765)

STAGES = [
    "New Lead",
    "Pre-Sales Video Viewed",
    "Pre-Qualified",
    "Discovery Call Scheduled",
    "Estimating/Design",
    "Proposal Sent",
    "Closed-Won",
    "Nurture/Follow-Up",
]

NEXT_STEP = {
    "New Lead": "We're missing some info. Open \"Change their info\" and fill in if they own the home, their budget, and when they want to start. Then press \"Save and re-sort\".",
    "Pre-Sales Video Viewed": "They watched your video. Ask them their budget and when they want to start.",
    "Pre-Qualified": "Send them the first text below right now. Fast replies win jobs!",
    "Discovery Call Scheduled": "Call or text them the day before to remind them about your phone call.",
    "Estimating/Design": "Measure the space, plan the design, and write up the price.",
    "Proposal Sent": "Check in with them in 2 days to answer any questions about the price.",
    "Closed-Won": "Get the deposit, sign the contract, and put the job on your calendar.",
    "Nurture/Follow-Up": "Nothing to do today. Send their monthly email when it shows up on your To-Do page.",
}

NURTURE = [
    {
        "month": 1,
        "title": "Cost Education",
        "subject": "What a $35,000 Colorado kitchen really pays for",
        "body": (
            "Hi {first},\n\n"
            "One of the most common questions we get is \"where does the money actually go?\" "
            "Here's a simple breakdown of a realistic $35,000 kitchen remodel here in Colorado:\n\n"
            "- Cabinets and hardware: $[amount]\n"
            "- Countertops: $[amount]\n"
            "- Labor and installation: $[amount]\n"
            "- Appliances: $[amount]\n"
            "- Flooring, lighting and plumbing fixtures: $[amount]\n"
            "- Permits, design and contingency: $[amount]\n\n"
            "Knowing this up front makes it much easier to plan a budget that fits. "
            "Whenever you're ready to talk through your own {project}, just reply to this email.\n\n"
            "The Remod Flow design team"
        ),
    },
    {
        "month": 2,
        "title": "Design Smart",
        "subject": "The layout trick that makes kitchens and small baths work",
        "body": (
            "Hi {first},\n\n"
            "Great remodels start with smart layouts. In kitchens we use the \"work triangle\": "
            "the sink, stove and fridge placed so you can move between them in a few easy steps, "
            "with no traffic cutting through.\n\n"
            "In small bathrooms, the biggest wins are a curbless shower, a wall-hung vanity, "
            "and a pocket door to free up floor space.\n\n"
            "If you'd like ideas for your own {project}, reply and we'll send a few layout options.\n\n"
            "The Remod Flow design team"
        ),
    },
    {
        "month": 3,
        "title": "Contractor Guard",
        "subject": "Your checklist before hiring any Colorado contractor",
        "body": (
            "Hi {first},\n\n"
            "Before you hire anyone for your {project}, ask for these:\n\n"
            "1. Proof of residential general liability insurance\n"
            "2. Proof of workers' compensation coverage for their crew\n"
            "3. A local contractor license where your city requires one\n"
            "4. Confirmation they will pull the building permits\n"
            "5. A written scope of work and payment schedule\n\n"
            "We're happy to show you all of ours. When you're ready to start planning, just reply.\n\n"
            "The Remod Flow design team"
        ),
    },
]

db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, phone TEXT, email TEXT, location TEXT,
                project TEXT, homeowner TEXT, budget TEXT, budget_low INTEGER,
                job_value INTEGER, timeline TEXT, stage TEXT, reason TEXT,
                notes TEXT, raw TEXT,
                created_at TEXT, stage_changed_at TEXT,
                protocol_a_sent_at TEXT, protocol_b_sent_at TEXT,
                nurture_start TEXT, nurture_sent TEXT DEFAULT ''
            )"""
        )
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")


def get_layout():
    with db() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key='layout'").fetchone()
    saved = json.loads(r["value"]) if r else {}
    order = [s for s in saved.get("order", []) if s in STAGES]
    order += [s for s in STAGES if s not in order]
    labels = {k: v for k, v in (saved.get("labels") or {}).items() if k in STAGES and str(v).strip()}
    hidden = [s for s in saved.get("hidden", []) if s in STAGES]
    return {"order": order, "labels": labels, "hidden": hidden}


def save_layout(data):
    if data.get("reset"):
        with db_lock, db() as conn:
            conn.execute("DELETE FROM settings WHERE key='layout'")
        fresh = get_layout()
        guide.save_markdown(fresh)
        return fresh
    layout = {
        "order": [s for s in data.get("order", []) if s in STAGES],
        "labels": {k: str(v).strip()[:40] for k, v in (data.get("labels") or {}).items() if k in STAGES and str(v).strip()},
        "hidden": [s for s in data.get("hidden", []) if s in STAGES],
    }
    with db_lock, db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('layout', ?)", (json.dumps(layout),))
    fresh = get_layout()
    guide.save_markdown(fresh)
    return fresh


# ---------------------------------------------------------------- parsing

CO_CITIES = ["Denver", "Aurora", "Lakewood", "Littleton", "Centennial", "Highlands Ranch", "Parker", "Castle Rock",
             "Englewood", "Arvada", "Westminster", "Thornton", "Broomfield", "Boulder", "Longmont", "Golden",
             "Wheat Ridge", "Lone Tree", "Greenwood Village", "Northglenn", "Brighton", "Commerce City",
             "Erie", "Louisville", "Lafayette", "Superior", "Colorado Springs", "Fort Collins", "Loveland",
             "Greeley", "Pueblo", "Monument", "Evergreen", "Conifer"]

LABEL_LINE = r"(?im)^[\s*\-•]*(?:{labels})\b[^:=\n]*?(?:[:=]|\s[-–]\s)\s*(.+?)\s*$"


def grab(text, labels):
    m = re.search(LABEL_LINE.format(labels="|".join(labels)), text)
    return m.group(1).strip() if m else ""


def money_values(s, need_marker=False):
    vals = []
    for m in re.finditer(r"(\$)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|K|thousand|grand)?", s):
        dollar, num, k = m.groups()
        if need_marker and not (dollar or k):
            continue
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        if k:
            v *= 1000
        elif v < 1000 and (dollar or not need_marker):
            # "$20-40k" style: bare small numbers next to a k later are thousands
            if re.search(r"\d\s*(k|K)", s):
                v *= 1000
        if v >= 1000:
            vals.append(int(v))
    return vals


def parse_budget(text):
    """Returns (display, low, value). low is used for qualifying, value for job value."""
    field = grab(text, ["estimated budget", "budget", "anticipated project budget", "project budget", "price range"])
    src = field or text
    vals = money_values(src, need_marker=not field)
    if not vals:
        return field, None, None
    under = re.search(r"under|less than|below|<", src, re.I)
    if under and len(vals) == 1:
        return field or f"Under ${vals[0]:,}", vals[0] - 1, int(vals[0] * 0.75)
    low, high = min(vals), max(vals)
    display = field or (f"${low:,}" if low == high else f"${low:,} - ${high:,}")
    return display, low, (low + high) // 2


def parse_timeline(text):
    field = grab(text, ["timeline", "target start date", "start date", "wants to start", "start", "when"])
    src = (field or text).lower()
    if re.search(r"research|just looking|browsing|not sure|someday|next year|1\+\s*y|12\+|a year|years? (out|away|from now)|over a year", src):
        return "Researching / 1+ Yr"
    if re.search(r"immediate|asap|right away|\bnow\b|this month|urgent", src):
        return "Immediate"
    if re.search(r"6\s*\+|6\s*(mo|months)?\s*(or more|plus)|over 6|more than 6|6\s*-\s*12", src):
        return "6+ Mo"
    nums = [int(n) for n in re.findall(r"(\d+)\s*(?:-|to|–)?\s*(?=\d*\s*(?:mo|month))", src)]
    nums += [int(n) for n in re.findall(r"(\d+)\s*(?:mo|month)", src)]
    if nums:
        top = max(nums)
        if top <= 3:
            return "1-3 Mo"
        if top <= 6:
            return "3-6 Mo"
        return "6+ Mo"
    if re.search(r"week", src):
        return "Immediate"
    return field


def parse_homeowner(text):
    field = grab(text, ["homeowner status", "homeowner", "do you own", "owns the home", "owns home", "own the home", "property owner", "owner", "own home"])
    src = field.lower()
    if field:
        if re.search(r"\b(no|renter|rent|renting|tenant|n)\b", src):
            return "No"
        if re.search(r"\b(yes|y|own|owner|homeowner)\b", src):
            return "Yes"
    t = text.lower()
    if re.search(r"\b(renter|renting|i rent|we rent|tenant|landlord)\b", t):
        return "No"
    if re.search(r"\b(homeowner|i own|we own|own the home|own our home|own my home)\b", t):
        return "Yes"
    return field


def parse_lead(text):
    email_m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    text = text.replace("_", " ")
    name = grab(text, ["lead name", "full name", "name", "customer", "client", "contact name"])
    if not name:
        nm = re.search(r"\b(?:this is|my name is|i'm|i am|name's)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", text)
        name = nm.group(1) if nm else ""
    phone_m = re.search(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b", text)
    location = grab(text, ["location", "lives in", "city", "zip", "address", "area", "town"])
    if not location:
        zm = re.search(r"\b8[01]\d{3}\b", text)
        cm = re.search(r"\b(" + "|".join(CO_CITIES) + r")\b", text, re.I)
        location = cm.group(1).title() if cm else zm.group(0) if zm else ""
    location = re.sub(r"\s*,?\s*\bCO\b\.?$", "", location).strip() or location

    pfield = grab(text, ["project type", "project", "service", "remodel type", "wants"])
    ptext = (pfield if re.search(r"kitchen|bath|both", pfield, re.I) else text).lower()
    kitchen = "kitchen" in ptext
    bath = bool(re.search(r"bath", ptext))
    project = "Both" if (kitchen and bath) or "both" in ptext else "Kitchen" if kitchen else "Bathroom" if bath else ""

    budget, budget_low, value = parse_budget(text)
    lead = {
        "name": name,
        "phone": phone_m.group(0).strip() if phone_m else "",
        "email": email_m.group(0) if email_m else "",
        "location": location,
        "project": project,
        "homeowner": parse_homeowner(text),
        "budget": budget,
        "budget_low": budget_low,
        "job_value": value,
        "timeline": parse_timeline(text),
    }
    lead["stage"], lead["reason"] = route(lead)
    return lead


def route(lead):
    """Applies the Remod Flow qualification rules."""
    low = lead.get("budget_low")
    ho = lead.get("homeowner")
    tl = lead.get("timeline") or ""
    short = tl in ("Immediate", "1-3 Mo", "3-6 Mo")
    long_tail = tl == "Researching / 1+ Yr"

    cold = []
    if low is not None and low < 20000:
        cold.append("budget under $20k")
    if ho == "No":
        cold.append("renter")
    if long_tail:
        cold.append("timeline 1+ years / researching")
    if cold:
        return "Nurture/Follow-Up", "Long-tail match: " + ", ".join(cold) + ". Text alerts suppressed; nurture emails queued."
    if ho == "Yes" and low is not None and low >= 20000 and short:
        return "Pre-Qualified", "High-intent match: homeowner, $20k+ budget, under 6 months. Send Protocol A now."
    missing = []
    if ho not in ("Yes", "No"):
        missing.append("homeowner status")
    if low is None:
        missing.append("budget")
    if not tl:
        missing.append("timeline")
    elif tl == "6+ Mo":
        missing.append("timeline is 6-12 months, decide by hand")
    return "New Lead", "Needs review: " + (", ".join(missing) or "details") + "."


# ---------------------------------------------------------------- drafts

def first_name(lead):
    n = (lead.get("name") or "").strip()
    return n.split()[0] if n else "there"


def project_word(lead, cap=True):
    p = lead.get("project") or ""
    word = {"Kitchen": "Kitchen", "Bathroom": "Bathroom", "Both": "Kitchen and Bathroom"}.get(p, "remodeling")
    return word if cap else word.lower()


def drafts(lead):
    loc = re.sub(r"[,\s]*(\bCO\b|Colorado)?[,\s]*\d{5}(-\d{4})?\s*$", "", lead.get("location") or "").strip(" ,")
    loc = re.sub(r",\s*(CO|Colorado)$", "", loc) or "your"
    a = (
        f"Hi {first_name(lead)}, this is the design team at Remod Flow. I saw you just reviewed our "
        f"{project_word(lead)} project pricing video for the {loc} area. Your scope looks fantastic. "
        "I have two openings tomorrow morning for your fast-track discovery call: 9:30 AM or 11:15 AM. "
        "Which one works best to lock in your design slot?"
    )
    b = (
        f"Hey {first_name(lead)}, are you still planning on remodeling your {project_word(lead, False)} this year, "
        "or have you put the project on hold for now? Let me know so I can keep your local material "
        "and pricing estimate active."
    )
    return {"protocol_a": a, "protocol_b": b}


def nurture_schedule(lead):
    if not lead.get("nurture_start"):
        return []
    start = datetime.fromisoformat(lead["nurture_start"])
    sent = set(filter(None, (lead.get("nurture_sent") or "").split(",")))
    proj = project_word(lead, False) if lead.get("project") else "remodel"
    out = []
    for i, n in enumerate(NURTURE):
        due = start + timedelta(days=30 * i)
        out.append({
            "month": n["month"], "title": n["title"], "subject": n["subject"],
            "body": n["body"].format(first=first_name(lead), project=proj),
            "due": due.date().isoformat(), "sent": str(n["month"]) in sent,
        })
    return out


def enrich(row):
    lead = dict(row)
    lead.update(drafts(lead))
    lead["next_step"] = NEXT_STEP.get(lead["stage"], "")
    lead["nurture"] = nurture_schedule(lead)
    now = datetime.now()
    lead["protocol_b_due"] = None
    if lead["protocol_a_sent_at"] and not lead["protocol_b_sent_at"] and lead["stage"] == "Pre-Qualified":
        lead["protocol_b_due"] = (datetime.fromisoformat(lead["protocol_a_sent_at"]) + timedelta(hours=96)).isoformat(timespec="minutes")
    lead["protocol_b_ready"] = bool(lead["protocol_b_due"] and datetime.fromisoformat(lead["protocol_b_due"]) <= now)
    return lead


# ---------------------------------------------------------------- data ops

def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def all_leads():
    with db() as conn:
        return [enrich(r) for r in conn.execute("SELECT * FROM leads ORDER BY created_at DESC")]


def get_lead(conn, lid):
    r = conn.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
    return enrich(r) if r else None


def add_lead(raw):
    lead = parse_lead(raw)
    ts = now_iso()
    with db_lock, db() as conn:
        cur = conn.execute(
            """INSERT INTO leads (name, phone, email, location, project, homeowner, budget, budget_low,
               job_value, timeline, stage, reason, notes, raw, created_at, stage_changed_at, nurture_start)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lead["name"] or "Unnamed lead", lead["phone"], lead["email"], lead["location"], lead["project"],
             lead["homeowner"], lead["budget"], lead["budget_low"], lead["job_value"], lead["timeline"],
             lead["stage"], lead["reason"], "", raw, ts, ts,
             ts if lead["stage"] == "Nurture/Follow-Up" else None),
        )
        new = get_lead(conn, cur.lastrowid)
    bots.event("lead_added", new)
    return new


EDITABLE = ["name", "phone", "email", "location", "project", "homeowner", "budget", "job_value", "timeline", "notes"]


def update_lead(lid, data, origin="you"):
    with db_lock, db() as conn:
        cur = get_lead(conn, lid)
        if not cur:
            return None
        sets, vals = [], []
        if data.get("append_note"):
            data["notes"] = ((cur["notes"] or "") + "\n" + data["append_note"]).strip()
        for k in EDITABLE:
            if k in data:
                v = data[k]
                if k == "job_value":
                    v = int(re.sub(r"[^\d]", "", str(v)) or 0) or None
                sets.append(f"{k}=?")
                vals.append(v)
        if "budget" in data:
            _, low, _ = parse_budget("Budget: " + str(data["budget"]))
            sets.append("budget_low=?")
            vals.append(low)
        if data.get("reroute"):
            merged = {**cur, **{k: data[k] for k in EDITABLE if k in data}}
            if "budget" in data:
                merged["budget_low"] = low
            stage, reason = route(merged)
            data["stage"] = stage
            sets.append("reason=?")
            vals.append(reason)
        if "stage" in data and data["stage"] in STAGES and data["stage"] != cur["stage"]:
            sets += ["stage=?", "stage_changed_at=?"]
            vals += [data["stage"], now_iso()]
            if data["stage"] == "Nurture/Follow-Up" and not cur["nurture_start"]:
                sets.append("nurture_start=?")
                vals.append(now_iso())
        for flag in ("protocol_a_sent_at", "protocol_b_sent_at"):
            if flag in data:
                sets.append(f"{flag}=?")
                vals.append(now_iso() if data[flag] else None)
        if "nurture_sent" in data:
            month = str(data["nurture_sent"])
            sent = set(filter(None, (cur["nurture_sent"] or "").split(",")))
            sent ^= {month}
            sets.append("nurture_sent=?")
            vals.append(",".join(sorted(sent)))
        if sets:
            conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", (*vals, lid))
        new = get_lead(conn, lid)
    if new["stage"] != cur["stage"]:
        bots.event("stage_changed", new, origin)
    return new


def run_bot_action(action, lid):
    """Makes one change a bot asked for. Only called after you press Approve (see bots.decide)."""
    if action["type"] == "move":
        if action["stage"] not in STAGES:
            raise ValueError(f"there is no box called {action['stage']}")
        data = {"stage": action["stage"]}
    elif action["type"] == "note":
        data = {"append_note": f"🤖 {action['text']}"}
    else:
        raise ValueError("unknown action")
    if not update_lead(lid, data, origin="bot"):
        raise ValueError("that customer was deleted")


def delete_lead(lid):
    with db_lock, db() as conn:
        conn.execute("DELETE FROM leads WHERE id=?", (lid,))


# ---------------------------------------------------------------- flow map

STARTED = now_iso()


def resource_ok(res):
    kind, name = res.split(":", 1)
    try:
        if kind == "table":
            with db() as conn:
                conn.execute(f"SELECT COUNT(*) FROM {name}")  # name comes from the code scan, not from a person
        elif kind == "file":
            # a file that doesn't exist yet is fine: the app makes it the first time it's needed
            fpath = os.path.join(HERE, name)
            return not os.path.exists(fpath) or os.access(fpath, os.R_OK)
        return True
    except sqlite3.Error:
        return False


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def flow_status():
    """Live status for every box on the Flow Map."""
    scan = flowmap.current_map()
    leads = all_leads()
    today_s = datetime.now().date().isoformat()
    due = sum(1 for l in leads if l["protocol_b_ready"]) + sum(
        1 for l in leads if l["stage"] == "Nurture/Follow-Up" for n in l["nurture"] if not n["sent"] and n["due"] <= today_s)
    text_now = sum(1 for l in leads if l["stage"] == "Pre-Qualified" and not l["protocol_a_sent_at"])
    pending = len(bots.runs("pending"))
    vids = videos.list_videos()["videos"]
    guide_time = os.path.getmtime(guide.MD_PATH) if os.path.exists(guide.MD_PATH) else None
    live = {
        "pipeline": (plural(sum(1 for l in leads if l['stage'] not in ('Nurture/Follow-Up', 'Closed-Won')), "customer") + " in progress"
                     + (f" · {text_now} to text now" if text_now else ""), "attention" if text_now else "ok"),
        "add": (f"{sum(1 for l in leads if (l['created_at'] or '').startswith(today_s))} added today", "ok"),
        "follow": (f"{due} to send today" if due else "Nothing due", "attention" if due else "ok"),
        "videos": (f"{len(vids)} video{'s' if len(vids) != 1 else ''}", "ok"),
        "guide": ("Guide updated " + datetime.fromtimestamp(guide_time).strftime("%b %d, %I:%M %p") if guide_time
                  else "Guide not written yet", "ok" if guide_time else "attention"),
        "howto": ("Instructions only", "ok"),
        "flowmap": (f"{pending} request{'s' if pending != 1 else ''} waiting for you" if pending else "No requests waiting",
                    "attention" if pending else "ok"),
        "server": (f"Running since {datetime.fromisoformat(STARTED).strftime('%b %d, %I:%M %p')}", "ok"),
    }
    stopped = bots.hard_stopped()
    out = {}
    for p in scan["pages"]:
        bad = [scan["resources"][r] for r in set(p["reads"]) | set(p["saves"]) if not resource_ok(r)]
        headline, state = live.get(p["id"], ("Working", "ok"))
        if bad:
            headline, state = "Can't open: " + ", ".join(bad), "error"
        out[p["id"]] = {"headline": headline, "state": state}
    return {"pages": out, "hard_stop": stopped, "pending": pending, "time": now_iso()}


def flow_payload():
    scan = flowmap.current_map()
    return {"scan": scan, "bots": bots.bots_for(scan), "flows": bots.flows(), "layout": bots.get("layout", "map", {}),
            "options": bots.options(), "stages": STAGES, "runs": bots.runs(limit=40), "hard_stop": bots.hard_stopped()}


def flow_post(path, data):
    """Every change the Flow Map page can make. Each one is versioned and logged inside bots.py."""
    page_label = {p["id"]: p["label"] for p in flowmap.current_map()["pages"]}
    if path == "/api/flowmap/scan":
        flowmap.current_map(force=True)
        bots.log("scan", "code", "🔄 Re-scanned the app's code")
        return flow_payload()
    if path == "/api/flowmap/layout":
        pos = {k: {"x": int(v["x"]), "y": int(v["y"])} for k, v in (data.get("positions") or {}).items() if k in page_label}
        bots.put("layout", "map", pos, "layout", data.get("why") or "Moved boxes on the Flow Map")
        return {"ok": True}
    if path == "/api/flowmap/perm":
        if data.get("page") not in page_label:
            return {"error": "Unknown page."}
        scan = flowmap.current_map()
        page = next(p for p in scan["pages"] if p["id"] == data["page"])
        catalog = {c["id"]: c["label"] for c in bots.perm_catalog({**page, "_labels": scan["resources"]})}
        if data.get("perm") not in catalog:
            return {"error": "This page's bot doesn't have that switch."}
        return {"perms": bots.set_perm(data["page"], data["perm"], data.get("on"), page_label[data["page"]],
                                       catalog[data["perm"]])}
    if path == "/api/flowmap/flow":
        if data.get("bot") not in page_label:
            return {"error": "Pick which page's bot runs this flow."}
        return bots.save_flow(data)
    if path == "/api/flowmap/flow/state":
        return bots.set_flow_state(str(data.get("id")), data.get("what"), data.get("version"))
    if path == "/api/flowmap/run":
        return bots.decide(int(data.get("id") or 0), bool(data.get("approve")), run_bot_action)
    if path == "/api/flowmap/hardstop":
        return bots.set_hard_stop(bool(data.get("on")))
    if path == "/api/flowmap/restore":
        return bots.restore(data.get("kind"), data.get("key"), data.get("version"))
    return {"error": "not found"}


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")  # always show the newest screen after an update
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/leads":
            self.send_json({"stages": STAGES, "leads": all_leads(), "layout": get_layout()})
        elif path == "/api/videos":
            self.send_json(videos.list_videos())
        elif path.startswith("/videos/"):
            self.send_video(unquote(path[len("/videos/"):]))
        elif path == "/api/guide":
            self.send_json({"sections": guide.build(get_layout())})
        elif path == "/flowmap.js":
            with open(os.path.join(HERE, "flowmap.js"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/flowmap":
            self.send_json(flow_payload())
        elif path == "/api/flowmap/status":
            self.send_json(flow_status())
        elif path == "/api/flowmap/log":
            self.send_json({"log": bots.read_log(), "runs": bots.runs(limit=100)})
        elif path == "/api/flowmap/history":
            q = parse_qs(urlparse(self.path).query)
            self.send_json({"history": bots.history(q.get("kind", [""])[0], q.get("key", [""])[0])})
        elif path == "/api/ping":
            self.send_json({"ok": True, "app": "remodflow"})
        else:
            self.send_json({"error": "not found"}, 404)

    def send_video(self, name):
        fpath = videos.video_path(name)
        if not fpath:
            return self.send_json({"error": "not found"}, 404)
        size = os.path.getsize(fpath)
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", videos.SERVE_TYPES[os.path.splitext(fpath)[1].lower()])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with open(fpath, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            pass

    def receive_video(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            name, out = videos.start_upload(unquote(self.headers.get("X-Filename", "")))
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        with out:
            left = length
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
        videos.set_info(name, unquote(self.headers.get("X-Title", "")) or videos.nice_title(name),
                        unquote(self.headers.get("X-Description", "")), added=now_iso())
        return self.send_json({"ok": True, "file": name})

    def same_site(self):
        """Blocks other websites open in your browser from sending changes (like an approval) to this app."""
        origin = self.headers.get("Origin")
        return not origin or origin in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.same_site():
            return self.send_json({"error": "Changes can only come from this app's own page."}, 403)
        if path == "/api/videos/upload":
            return self.receive_video()
        data = self.read_json()
        if path == "/api/videos/info":
            videos.set_info(data.get("file", ""), data.get("title"), data.get("description"))
            return self.send_json({"ok": True})
        if path == "/api/videos/delete":
            videos.delete(data.get("file", ""))
            return self.send_json({"ok": True})
        if path == "/api/videos/open-folder":
            videos._ensure()
            os.startfile(videos.VIDEO_DIR)
            return self.send_json({"ok": True})
        if path == "/api/leads":
            raw = (data.get("raw") or "").strip()
            if not raw:
                return self.send_json({"error": "Paste the lead details first."}, 400)
            return self.send_json(add_lead(raw))
        if path == "/api/layout":
            return self.send_json(save_layout(data))
        if path.startswith("/api/flowmap/"):
            out = flow_post(path, data)
            return self.send_json(out, 400 if isinstance(out, dict) and out.get("error") else 200)
        if path == "/api/preview":
            return self.send_json(parse_lead(data.get("raw") or ""))
        m = re.fullmatch(r"/api/leads/(\d+)", path)
        if m:
            lead = update_lead(int(m.group(1)), data)
            return self.send_json(lead) if lead else self.send_json({"error": "not found"}, 404)
        m = re.fullmatch(r"/api/leads/(\d+)/delete", path)
        if m:
            delete_lead(int(m.group(1)))
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)


def main():
    init_db()
    bots.setup(db, db_lock)
    bots.start_watcher(all_leads)
    guide.save_markdown(get_layout())
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Remod Flow CRM running at http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
