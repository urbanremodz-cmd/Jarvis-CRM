"""Flow Map scanner. Reads Remod Flow's own source code (no running app needed) and works out, for every page:
what it reads, what it saves, which pages it feeds, its schedules and its outside services.

Run it by itself to see the map:   python flowmap.py        (also writes flowmap.json next to the app)
The app runs the same scan when you open the 🧭 Flow Map page or press 🔄 Re-scan code."""
import ast
import json
import os
import re
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "flowmap.json")
SKIP_PY = {"flowmap.py", "launch.pyw"}

# Friendly names for things the scan finds. Anything not listed shows its raw name.
RES_LABEL = {
    "table:leads": "Customers",
    "table:settings": "Settings",
    "setting:layout": "Box layout",
    "table:flow_versions": "Bot settings & flows (every version)",
    "table:flow_log": "Change log",
    "table:flow_runs": "Bot requests",
    "file:HOW-TO-USE.md": "How-to guide file",
    "file:videos": "Video files",
    "file:videos/videos.json": "Video names",
    "file:index.html": "App screen file",
    "table:hub_records": "Master records",
    "table:hub_record_versions": "Master record versions",
    "table:hub_links": "CRM links (who is who)",
    "table:hub_outbox": "Changes waiting to be written",
    "table:hub_conflicts": "CRM disagreements",
    "table:hub_secrets": "CRM keys (this computer only)",
}
SCHEDULE_LABEL = {
    "protocol_b_due": "Check-in text comes due",
    "protocol_b_ready": "Check-in text comes due",
    "nurture": "Monthly emails come due",
}
FUNC_LABEL = {"start_watcher": "Bot watcher checks time-based flows", "start_auto_pull": "Sync Hub reads your CRMs"}
# Python calls that reach outside the app.
PY_OUTSIDE = {
    "os.startfile": ("Windows (opens a folder)", "computer"),
    "webbrowser.open": ("Web browser", "computer"),
    "subprocess.Popen": ("Starts another program", "computer"),
    "subprocess.run": ("Starts another program", "computer"),
    "smtplib.SMTP": ("Email server", "internet"),
    "urllib.request.urlopen": ("Internet request", "internet"),
    "requests.get": ("Internet request", "internet"),
    "requests.post": ("Internet request", "internet"),
    "http.client.HTTPSConnection": ("Internet request", "internet"),
}
JS_OUTSIDE = [
    (r"navigator\.clipboard|execCommand\(\s*[\"']copy", "Clipboard (copy & paste)", "computer"),
    (r"window\.print\(", "Printer", "computer"),
    (r"fetch\(\s*[`\"']https?://", "Internet request", "internet"),
    (r"new\s+WebSocket\(", "Live internet connection", "internet"),
]
# Services a page only talks about (instructions) - the code never connects to them.
MENTIONED = ["Facebook", "GoHighLevel", "Google", "Gmail", "Twilio", "Stripe", "QuickBooks", "Telegram", "Zapier", "n8n"]

SQL_PATTERNS = [
    (r"\bCREATE TABLE IF NOT EXISTS\s+(\w+)", None),
    (r"\bINSERT(?:\s+OR\s+\w+)?\s+INTO\s+(\w+)", "w"),
    (r"\bUPDATE\s+(\w+)\s+SET\b", "w"),
    (r"\bDELETE\s+FROM\s+(\w+)", "w"),
    (r"\bSELECT\b[\s\S]*?\bFROM\s+(\w+)", "r"),
]


def label_of(res):
    return RES_LABEL.get(res, res.split(":", 1)[-1])


# =====================================================================  Python side

class PyFunc:
    def __init__(self, module, name, node, src):
        self.module, self.name, self.node, self.src = module, name, node, src
        self.reads, self.writes, self.calls = set(), set(), set()
        self.outside, self.schedules = [], []
        self.doc = (ast.get_docstring(node) or "").strip()

    @property
    def key(self):
        return f"{self.module}.{self.name}"

    def where(self, node):
        return f"{self.module}.py:{node.lineno}"


def _const_paths(tree):
    """Module constants like VIDEO_DIR = os.path.join(HERE, "videos") -> {"VIDEO_DIR": "videos"}."""
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            v = node.value
            if isinstance(v, ast.Call) and ast.unparse(v.func) == "os.path.join":
                parts = []
                for a in v.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        parts.append(a.value)
                    elif isinstance(a, ast.Name) and a.id in consts:
                        parts.append(consts[a.id])
                if parts:
                    consts[node.targets[0].id] = "/".join(parts)
    return consts


def _strings(node):
    """All string pieces inside a node (f-strings included)."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    return out


def _sql_resources(text):
    reads, writes = set(), set()
    for pat, mode in SQL_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            table = m.group(1)
            res = f"table:{table}"
            km = re.search(r"key\s*=\s*'(\w+)'|VALUES\s*\(\s*'(\w+)'", text)
            if table == "settings" and km:
                res = f"setting:{km.group(1) or km.group(2)}"
            if mode == "w":
                writes.add(res)
            elif mode == "r":
                reads.add(res)
    return reads, writes


def _file_res(expr, consts):
    for n in ast.walk(expr):
        if isinstance(n, ast.Name) and n.id in consts and consts[n.id] != "remodflow.db":
            return "file:" + consts[n.id]
    for n in ast.walk(expr):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "." in n.value and "/" not in n.value[:1]:
            return "file:" + n.value
    return None


def _duration(call):
    parts = []
    for kw in call.keywords:
        v = kw.value
        if isinstance(v, ast.Constant):
            n = v.value
            if kw.arg == "hours" and n % 24 == 0:
                parts.append(f"{n} hours ({n // 24} days)")
            else:
                parts.append(f"{n} {kw.arg}")
        elif isinstance(v, ast.BinOp) and isinstance(v.op, ast.Mult):
            c = v.left if isinstance(v.left, ast.Constant) else v.right if isinstance(v.right, ast.Constant) else None
            parts.append(f"every {c.value} {kw.arg}" if c is not None else ast.unparse(v) + " " + kw.arg)
        else:
            parts.append(f"{ast.unparse(v)} {kw.arg}")
    return ", ".join(parts) or "a set time"


def _const_numbers(tree):
    """Module number constants like AUTO_PULL_MINUTES = 5."""
    return {n.targets[0].id: n.value.value for n in tree.body
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
            and isinstance(n.value, ast.Constant) and isinstance(n.value.value, (int, float))}


def _number(node, numbers):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name):
        return numbers.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        a, b = _number(node.left, numbers), _number(node.right, numbers)
        return a * b if a is not None and b is not None else None
    return None


def _class_label(cls):
    """A connector class names its service:  id, label = "ghl", "GoHighLevel"."""
    for st in cls.body:
        if isinstance(st, ast.Assign) and isinstance(st.value, (ast.Tuple, ast.Constant)):
            names = st.targets[0].elts if isinstance(st.targets[0], ast.Tuple) else [st.targets[0]]
            vals = st.value.elts if isinstance(st.value, ast.Tuple) else [st.value]
            for n, v in zip(names, vals):
                if isinstance(n, ast.Name) and n.id == "label" and isinstance(v, ast.Constant) and v.value:
                    return v.value
    return None


def scan_python(paths):
    funcs, modules, methods = {}, {}, {}
    for path in paths:
        module = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8-sig") as f:
            src = f.read()
        tree = ast.parse(src)
        modules[module] = tree
        consts = _const_paths(tree)
        numbers = _const_numbers(tree)
        defs = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.append((node.name, node, None))
            elif isinstance(node, ast.ClassDef):
                label = _class_label(node)
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        defs.append((sub.name, sub, label))
                        methods.setdefault(module, set()).add(sub.name)
        for name, node, cls_label in defs:
            fn = PyFunc(module, name, node, src)
            for text in _strings(node):
                r, w = _sql_resources(text)
                fn.reads |= r
                fn.writes |= w
            for n in ast.walk(node):
                if isinstance(n, ast.Call):
                    callee = ast.unparse(n.func)
                    if callee == "open" and n.args:
                        res = _file_res(n.args[0], consts)
                        mode = n.args[1].value if len(n.args) > 1 and isinstance(n.args[1], ast.Constant) else "r"
                        if res and res != "file:remodflow.db":
                            (fn.writes if any(c in mode for c in "wa") else fn.reads).add(res)
                    elif callee in ("os.remove", "os.unlink") and n.args:
                        res = _file_res(n.args[0], consts)
                        fn.writes.add(res or "file:videos")
                    elif callee == "os.listdir" and n.args:
                        res = _file_res(n.args[0], consts)
                        if res:
                            fn.reads.add(res)
                    elif callee in PY_OUTSIDE:
                        name_, kind = PY_OUTSIDE[callee]
                        if cls_label and kind == "internet":
                            name_ = f"{cls_label} (over the internet)"
                        fn.outside.append({"name": name_, "kind": kind, "where": fn.where(n)})
                    elif callee.split(".")[-1] == "timedelta":
                        fn.schedules.append({"call": n, "where": fn.where(n), "when": _duration(n)})
                    elif callee in ("time.sleep",) or callee.endswith(".wait"):
                        secs = _number(n.args[0], numbers) if n.args else None
                        if secs is not None:
                            if secs >= 30 and any(isinstance(p, ast.While) for p in ast.walk(node)):
                                fn.schedules.append({"call": n, "where": fn.where(n), "when": f"every {secs:g} seconds",
                                                     "label": FUNC_LABEL.get(name, "Runs " + name.replace("_", " ")),
                                                     "background": True})
                # references to other functions (calls and functions passed along) are both links
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                    fn.calls.add(f"{n.value.id}.{n.attr}")
                elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    fn.calls.add(n.id)
            funcs[fn.key] = fn
    # resolve call names to known functions
    for fn in funcs.values():
        resolved = set()
        for c in fn.calls:
            if c.startswith("self."):
                c = c[5:]
            for cand in (f"{fn.module}.{c}", c):
                if cand in funcs:
                    resolved.add(cand)
            # obj.method() where obj is one of this module's classes (like a connector): follow the method
            meth = c.rsplit(".", 1)[-1]
            if "." in c and c.split(".")[0] not in modules and meth in methods.get(fn.module, ()):
                resolved.add(f"{fn.module}.{meth}")
        resolved.discard(fn.key)
        fn.calls = resolved
    return funcs, modules


def _closure(funcs, start):
    seen, stack = set(), [start]
    while stack:
        k = stack.pop()
        if k in seen or k not in funcs:
            continue
        seen.add(k)
        stack.extend(funcs[k].calls)
    return seen


def _schedule_names(funcs, modules):
    """Which data field each schedule produces, so we can tell which page shows it."""
    out = []
    for fn in funcs.values():
        for s in fn.schedules:
            names = set()
            if "label" not in s:
                parent = None
                for stmt in ast.walk(fn.node):
                    if isinstance(stmt, ast.Assign) and any(n is s["call"] for n in ast.walk(stmt)):
                        parent = stmt
                tgt = parent.targets[0] if parent else None
                if isinstance(tgt, ast.Subscript) and isinstance(tgt.slice, ast.Constant):
                    names.add(tgt.slice.value)
                else:
                    # local value: use the key the caller stores this function's result under
                    for tree in modules.values():
                        for a in ast.walk(tree):
                            if (isinstance(a, ast.Assign) and isinstance(a.value, ast.Call)
                                    and ast.unparse(a.value.func).split(".")[-1] == fn.name
                                    and isinstance(a.targets[0], ast.Subscript)
                                    and isinstance(a.targets[0].slice, ast.Constant)):
                                names.add(a.targets[0].slice.value)
            label = s.get("label") or next((SCHEDULE_LABEL[n] for n in names if n in SCHEDULE_LABEL), None) \
                or (", ".join(sorted(names)) or fn.name.replace("_", " "))
            out.append({"label": label, "when": s["when"], "where": s["where"], "fields": sorted(names), "func": fn.key,
                        "background": s.get("background", False)})
    return out


def scan_routes(funcs, modules):
    """Finds every web address the server answers (do_GET / do_POST) and the functions behind it."""
    routes = []
    for module, tree in modules.items():
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            for meth in cls.body:
                if not isinstance(meth, ast.FunctionDef) or meth.name not in ("do_GET", "do_POST"):
                    continue
                method = meth.name[3:]
                regex_vars = {}

                def handle(stmts):
                    for st in stmts:
                        if isinstance(st, ast.Assign) and isinstance(st.value, ast.Call) \
                                and ast.unparse(st.value.func) in ("re.fullmatch", "re.match"):
                            regex_vars[ast.unparse(st.targets[0])] = st.value.args[0].value
                        if isinstance(st, ast.If):
                            match = _route_test(st.test, regex_vars)
                            if match:
                                refs = set()
                                for n in ast.walk(ast.Module(body=st.body, type_ignores=[])):
                                    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                                        refs.add(f"{n.value.id}.{n.attr}")
                                    elif isinstance(n, ast.Name):
                                        refs.add(n.id)
                                keys = set()
                                for r in refs:
                                    r = r[5:] if r.startswith("self.") else r
                                    for cand in (f"{module}.{r}", r):
                                        if cand in funcs:
                                            keys.add(cand)
                                direct = [{"name": PY_OUTSIDE[c][0], "kind": PY_OUTSIDE[c][1], "where": f"{module}.py:{n.lineno}"}
                                          for n in ast.walk(ast.Module(body=st.body, type_ignores=[]))
                                          if isinstance(n, ast.Call) and (c := ast.unparse(n.func)) in PY_OUTSIDE]
                                routes.append({"method": method, "match": match, "funcs": sorted(keys),
                                               "line": st.lineno, "outside": direct})
                            else:
                                handle(st.body)
                            handle(st.orelse)

                handle(meth.body)
    return routes


def _route_test(test, regex_vars):
    src = ast.unparse(test)
    m = re.fullmatch(r"path == '([^']+)'", src)
    if m:
        return ("eq", [m.group(1)])
    m = re.fullmatch(r"path in \((.+)\)", src)
    if m:
        return ("eq", re.findall(r"'([^']+)'", m.group(1)))
    m = re.fullmatch(r"path\.startswith\('([^']+)'\)", src)
    if m:
        return ("prefix", [m.group(1)])
    if isinstance(test, ast.Name) and test.id in regex_vars:
        return ("regex", [regex_vars[test.id]])
    return None


def route_for(routes, method, path):
    sample = re.sub(r"\{[^}]*\}", "1", path)
    for r in routes:
        if r["method"] != method:
            continue
        kind, vals = r["match"]
        if kind == "eq" and sample in vals or kind == "prefix" and sample.startswith(vals[0]) \
                or kind == "regex" and re.fullmatch(vals[0], sample):
            return r
    return None


# =====================================================================  screen (HTML + JS) side

def _split_chunks(js):
    """Splits a script into its top-level statements and functions."""
    chunks, depth, start, i, n = [], 0, 0, 0, len(js)
    quote = None
    while i < n:
        c = js[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            elif quote == "`" and c == "$" and i + 1 < n and js[i + 1] == "{":
                # template expression: count braces so nested templates still work
                j, d = i + 2, 1
                while j < n and d:
                    d += {"{": 1, "}": -1}.get(js[j], 0)
                    j += 1
                i = j
                continue
        elif c in "\"'`":
            quote = c
        elif c == "/" and i + 1 < n and js[i + 1] == "/":
            i = js.find("\n", i)
            i = n if i < 0 else i
            continue
        elif c == "/" and (js[:i].rstrip()[-1:] or "(") in "(,=:[!&|?{};+":
            # a regex like /[&<>"']/g: skip it so its quote marks aren't read as strings
            j, in_class = i + 1, False
            while j < n and js[j] != "\n":
                if js[j] == "\\":
                    j += 2
                    continue
                if js[j] == "[":
                    in_class = True
                elif js[j] == "]":
                    in_class = False
                elif js[j] == "/" and not in_class:
                    break
                j += 1
            i = j + 1
            continue
        elif c in "{([":
            depth += 1
        elif c in "})]":
            depth -= 1
        elif c == "\n" and depth == 0:
            piece = js[start:i].strip()
            if piece and not piece.endswith((",", "=", "(", "&&", "||", "?", ":", "+")):
                chunks.append(piece)
                start = i + 1
        i += 1
    if js[start:].strip():
        chunks.append(js[start:].strip())
    out = []
    for text in chunks:
        m = re.match(r"(?:async\s+)?function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>", text)
        out.append({"name": (m.group(1) or m.group(2)) if m else None, "text": text})
    return out


def _js_api_calls(text):
    calls = []
    for m in re.finditer(r"\bapi\(\s*([`\"'])(.+?)\1\s*(,)?", text):
        calls.append(("POST" if m.group(3) else "GET", m.group(2)))
    for m in re.finditer(r"\.open\(\s*[\"'](GET|POST)[\"']\s*,\s*([`\"'])(.+?)\2", text):
        calls.append((m.group(1), m.group(3)))
    for m in re.finditer(r"\bfetch\(\s*([`\"'])(/.+?)\1", text):
        calls.append(("GET", m.group(2)))
    for m in re.finditer(r"(?:src|href)=\"(/[\w/-]+)/\$\{", text):
        calls.append(("GET", m.group(1) + "/{file}"))
    return [(meth, re.sub(r"\$\{[^}]*\}", "{id}", p).split("?")[0]) for meth, p in calls]


def scan_screen(html_path):
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    js_files = []
    for src in re.findall(r"<script\s+src=\"/?([\w.-]+\.js)[^\"]*\"", html):
        p = os.path.join(HERE, src)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                scripts.append(f.read())
            js_files.append(src)
    nav = re.findall(r"<button data-page=\"(\w+)\"(?:[^>]*?data-tip=\"([^\"]*)\")?[^>]*>([^<]+)", html)
    pages = []
    for pid, about, label in nav:
        m = re.search(r"<section class=\"page[^\"]*\" id=\"page-%s\">([\s\S]*?)</section>" % pid, html)
        body = m.group(1) if m else ""
        label = label.strip()
        icon = label.split(" ", 1)[0] if label and not label[0].isalnum() else ""
        pages.append({"id": pid, "label": label[len(icon):].strip() if icon else label, "icon": icon, "about": about,
                      "html": body, "ids": set(re.findall(r"\bid=\"([\w-]+)\"", body))})

    chunks = []
    for s in scripts:
        chunks += _split_chunks(s)
    names = {c["name"]: i for i, c in enumerate(chunks) if c["name"]}
    named = [c["name"] for c in chunks if c["name"]]
    clashes = sorted({n for n in named if named.count(n) > 1})
    # handlers listening for clicks on [data-x] belong to whoever draws data-x buttons
    delegated = {}
    for i, c in enumerate(chunks):
        for attr in re.findall(r"closest\(\s*[\"']\[data-([\w-]+)\]", c["text"]):
            delegated.setdefault(attr, set()).add(i)
    for c in chunks:
        c["ids"] = set(re.findall(r"\$\(\s*[`\"']#([\w-]+)", c["text"])) | set(re.findall(r"getElementById\(\s*[\"']([\w-]+)", c["text"]))
        c["calls"] = {names[w] for w in re.findall(r"\b(\w+)\s*\(", c["text"]) if w in names and w != c["name"]}
        for attr, idx in delegated.items():
            if re.search(r"data-%s=" % re.escape(attr), c["text"]):
                c["calls"] |= idx
        c["api"] = _js_api_calls(c["text"])

    def closure(start_idx, blocked):
        seen, stack = set(), list(start_idx)
        while stack:
            k = stack.pop()
            if k in seen or k in blocked:
                continue
            seen.add(k)
            stack.extend(chunks[k]["calls"])
        return seen

    for p in pages:
        own = {i for i, c in enumerate(chunks) if c["ids"] & p["ids"]}
        # nav switching: if (b.dataset.page === "guide") renderGuide();
        for c in chunks:
            for pg, fname in re.findall(r"dataset\.page\s*===\s*[\"'](\w+)[\"']\)\s*(\w+)\(", c["text"]):
                if pg == p["id"] and fname in names:
                    own.add(names[fname])
        # inline handlers written in the page's own markup
        for w in re.findall(r"on\w+=\"([^\"]+)\"", p["html"]):
            own |= {names[f] for f in re.findall(r"\b(\w+)\s*\(", w) if f in names}
        p["own"] = own
    for p in pages:
        # refreshing another page's view isn't this page's own work, so stop at chunks other pages own
        blocked = set().union(*(q["own"] for q in pages if q is not p)) - p["own"]
        p["chunks"] = closure(p["own"], blocked)
        text = "\n".join(chunks[i]["text"] for i in p["chunks"]) + "\n" + p["html"]
        p["api"] = sorted({a for i in p["chunks"] for a in chunks[i]["api"]})
        p["text"] = text
        p["outside"] = []
        for pat, name, kind in JS_OUTSIDE:
            if re.search(pat, text):
                p["outside"].append({"name": name, "kind": kind, "where": "screen"})
        plain = re.sub(r"<[^>]+>", " ", p["html"])
        for svc in MENTIONED:
            if re.search(r"\b%s\b" % re.escape(svc), plain):
                p["outside"].append({"name": svc, "kind": "mentioned", "where": "instructions only, no connection"})
        for ms in re.finditer(r"set(?:Interval|Timeout)\([\s\S]{0,400}?,\s*(\d+)\s*\)", text):
            if int(ms.group(1)) >= 60000:
                p.setdefault("js_schedules", []).append({"label": "Screen refresh", "when": f"every {int(ms.group(1)) // 1000} seconds", "where": "screen"})
    return pages, js_files, clashes


# =====================================================================  put it together

NOISE = {"send_json", "read_json", "send_response", "send_header", "end_headers", "now_iso"}


def _branch_keys(funcs, fn, path):
    """In a function that answers several addresses (if path == "/api/x": ...), the functions behind just this one."""
    for st in ast.walk(fn.node):
        if isinstance(st, ast.If):
            m = _route_test(st.test, {})
            if m and m[0] == "eq" and path in m[1]:
                keys = set()
                for n in ast.walk(ast.Module(body=st.body, type_ignores=[])):
                    ref = f"{n.value.id}.{n.attr}" if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                        else n.id if isinstance(n, ast.Name) else None
                    for cand in (ref and f"{fn.module}.{ref}", ref):
                        if cand in funcs:
                            keys.add(cand)
                return keys
    return None


def _steps(funcs, keys, path):
    out = []
    for k in keys:
        narrowed = _branch_keys(funcs, funcs[k], path)
        out += sorted(narrowed) if narrowed is not None else [k]
    return [k for k in dict.fromkeys(out) if funcs[k].name not in NOISE]


def _catalog(funcs, start):
    """Every function a page uses, with what it does in plain words, so the map can be broken down step by step."""
    keys = set()
    for k in start:
        keys |= _closure(funcs, k)
    out = {}
    for k in sorted(keys):
        fn = funcs[k]
        out[k] = {"name": fn.name, "file": f"{fn.module}.py", "line": fn.node.lineno,
                  "doc": fn.doc.split("\n\n")[0].replace("\n", " ")[:400],
                  "reads": sorted(fn.reads), "saves": sorted(fn.writes),
                  "outside": sorted({o["name"] for o in fn.outside}),
                  "calls": sorted(c for c in fn.calls if c in funcs and funcs[c].name not in NOISE)}
    return out


def scan():
    py_paths = sorted(os.path.join(HERE, f) for f in os.listdir(HERE)
                      if f.endswith(".py") and f not in SKIP_PY and not f.startswith("test"))
    funcs, modules = scan_python(py_paths)
    routes = scan_routes(funcs, modules)
    schedules = _schedule_names(funcs, modules)
    pages, js_files, clashes = scan_screen(os.path.join(HERE, "index.html"))
    warnings = [f"The screen code has two functions called {n}(), so one replaces the other" for n in clashes]

    def gather(keys):
        reads, writes, outside = set(), set(), []
        for k in keys:
            fn = funcs[k]
            reads |= fn.reads
            writes |= fn.writes
            outside += fn.outside
        return reads, writes, outside

    out_pages = []
    for p in pages:
        keys, endpoints, direct_outside = set(), [], []
        for meth, path in p["api"]:
            r = route_for(routes, meth, path)
            if not r:
                warnings.append(f"{p['label']}: the screen asks for {meth} {path} but the server has no such address")
                continue
            endpoints.append({"method": meth, "path": path, "server": f"app.py:{r['line']}",
                              "steps": _steps(funcs, r["funcs"], path)})
            for f in r["funcs"]:
                keys |= _closure(funcs, f)
            direct_outside += r["outside"]
        reads, writes, outside = gather(keys)
        outside += direct_outside
        reads -= {"table:settings"}
        outside += p["outside"]
        sched = [s for s in schedules if s["func"] in keys and any(re.search(r"\.%s\b" % re.escape(n), p["text"]) for n in s["fields"])]
        sched += p.get("js_schedules", [])
        out_pages.append({
            "id": p["id"], "label": p["label"], "icon": p["icon"], "about": p["about"],
            "reads": sorted(reads), "saves": sorted(writes), "endpoints": endpoints,
            "schedules": _dedupe(sched, ("label", "when")), "outside": _dedupe(outside, ("name",)),
        })

    # the server's own background work (start-up and timers) gets its own box
    main_keys = set()
    for k, fn in funcs.items():
        if fn.name == "main":
            main_keys |= _closure(funcs, k)
    bg = [s for s in schedules if s["background"]]
    for s in bg:
        main_keys |= _closure(funcs, s["func"])
    reads, writes, outside = gather(main_keys)
    reads -= {"table:settings"}
    writes -= {"table:settings"}
    bg_actions = [{"method": "RUN", "path": "start-up", "server": funcs[k].where(funcs[k].node), "steps": [k]}
                  for k, fn in funcs.items() if fn.name == "main"]
    bg_actions += [{"method": "RUN", "path": s["label"], "server": s["where"], "steps": [s["func"]]} for s in bg]
    out_pages.append({"id": "server", "label": "Behind the scenes", "icon": "🖥", "reads": sorted(reads),
                      "about": "The app itself running: starting up, and the jobs it does on a timer without anyone clicking.",
                      "saves": sorted(writes), "endpoints": bg_actions, "schedules": _dedupe(bg, ("label",)),
                      "outside": _dedupe(outside, ("name",)), "background": True})

    # page A feeds page B when A saves something B reads
    edges = {}
    for a in out_pages:
        for b in out_pages:
            if a is b:
                continue
            via = sorted(set(a["saves"]) & set(b["reads"]))
            if via:
                key = tuple(sorted((a["id"], b["id"])))
                e = edges.setdefault(key, {"a": key[0], "b": key[1], "a_to_b": [], "b_to_a": []})
                e["a_to_b" if a["id"] == key[0] else "b_to_a"] = via
    for p in out_pages:
        p["feeds"] = sorted({e["b"] if e["a"] == p["id"] else e["a"] for e in edges.values()
                             if (e["a"] == p["id"] and e["a_to_b"]) or (e["b"] == p["id"] and e["b_to_a"])})
    functions = _catalog(funcs, {k for p in out_pages for e in p["endpoints"] for k in e["steps"]})
    resources = sorted({r for p in out_pages for r in p["reads"] + p["saves"]}
                       | {r for f in functions.values() for r in f["reads"] + f["saves"]})
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": [os.path.basename(p) for p in py_paths] + ["index.html"] + js_files,
        "pages": out_pages,
        "edges": list(edges.values()),
        "resources": {r: label_of(r) for r in resources},
        "routes": len(routes),
        "warnings": warnings,
        "functions": functions,
    }


def _dedupe(items, keys):
    seen, out = set(), []
    for it in items:
        k = tuple(it.get(x) for x in keys)
        if k not in seen:
            seen.add(k)
            out.append({x: v for x, v in it.items() if x not in ("func", "fields", "background")})
    return out


_cache = {"sig": None, "map": None}


def current_map(force=False):
    """The scan, redone only when a source file has changed (or when forced)."""
    sig = tuple(sorted((f, os.path.getmtime(os.path.join(HERE, f))) for f in os.listdir(HERE)
                       if f.endswith((".py", ".html", ".js"))))
    if force or sig != _cache["sig"]:
        _cache["map"], _cache["sig"] = scan(), sig
        try:
            with open(OUT_PATH, "w", encoding="utf-8") as f:
                json.dump(_cache["map"], f, indent=2, ensure_ascii=False)
        except OSError:
            pass
    return _cache["map"]


if __name__ == "__main__":
    m = current_map(force=True)
    print(f"Scanned {', '.join(m['files'])} ({m['routes']} server addresses)\n")
    for p in m["pages"]:
        print(f"{p['icon']} {p['label']}")
        print("   reads:    ", ", ".join(m["resources"][r] for r in p["reads"]) or "-")
        print("   saves:    ", ", ".join(m["resources"][r] for r in p["saves"]) or "-")
        print("   feeds:    ", ", ".join(p["feeds"]) or "-")
        print("   schedules:", "; ".join(f"{s['label']} ({s['when']})" for s in p["schedules"]) or "-")
        print("   outside:  ", ", ".join(o["name"] for o in p["outside"]) or "-")
    for w in m["warnings"]:
        print("WARNING:", w)
    print(f"\nSaved {OUT_PATH}")
