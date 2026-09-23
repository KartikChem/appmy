"""
Campus Notices - a web app that collects, sorts and prioritises the notices
your college sends every day.

Run:
    pip install flask
    python app.py
Then open http://127.0.0.1:5000

Features
  * Home screen  -> everything received TODAY + upcoming deadlines
  * Top-left ⋮  -> drawer with the 5 categories (General, Assignment,
                    Events / Fest, Online Forms, Exam Section)
  * Top-right   -> student icon -> personal information page
  * Each notice is auto-categorised from its text, and any date in it is
    picked up as a deadline / exam date / event date.
  * Add notices by pasting them in (+ button) or send them from another
    program (WhatsApp/e-mail forwarder etc.) to  POST /api/ingest
"""

import os
import re
import sqlite3
from datetime import date, datetime, time, timedelta

from flask import Flask, g, jsonify, redirect, render_template, request, url_for
from jinja2 import DictLoader

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "notices.db")

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
CATEGORIES = {
    "general": {
        "name": "General",
        "icon": "📢",
        "color": "#4f5bd5",
        "keywords": [],
    },
    "assignment": {
        "name": "Assignment",
        "icon": "📝",
        "color": "#d98a00",
        "keywords": [
            "assignment", "submission", "submit", "homework", "lab record",
            "journal", "project report", "worksheet", "quiz", "mini project",
            "presentation", "upload", "record book", "file submission",
        ],
    },
    "events": {
        "name": "Events / Fest",
        "icon": "🎉",
        "color": "#d6336c",
        "keywords": [
            "fest", "event", "competition", "hackathon", "workshop", "seminar",
            "cultural", "sports", "webinar", "club", "annual day", "guest lecture",
            "celebration", "orientation", "farewell", "contest", "concert",
            "tournament", "trophy",
        ],
    },
    "forms": {
        "name": "Online Forms",
        "icon": "🧾",
        "color": "#0f9d76",
        "keywords": [
            "form", "fill", "google form", "apply", "application", "scholarship",
            "registration", "register", "enrol", "enroll", "portal",
            "fee payment", "fees", "online form", "feedback", "verification",
            "link",
        ],
    },
    "exam": {
        "name": "Exam Section",
        "icon": "🎓",
        "color": "#d64545",
        "keywords": [
            "exam", "examination", "timetable", "time table", "hall ticket",
            "admit card", "internal", "mid-sem", "midsem", "mid semester",
            "mid-semester", "end-sem", "end semester", "result", "revaluation",
            "backlog", "viva", "practical", "seating", "question paper",
        ],
    },
}

# Order used to break ties (most specific first).
CLASSIFY_ORDER = ("exam", "forms", "assignment", "events")

# Precompile whole-word keyword patterns ("form" must not match "information").
_KW_PATTERNS = {
    slug: [
        re.compile(r"\b" + re.escape(k) + r"(?:s|es|ed|ing)?\b")
        for k in CATEGORIES[slug]["keywords"]
    ]
    for slug in CLASSIFY_ORDER
}

IMPORTANT_RE = re.compile(
    r"\b(urgent|urgently|important|mandatory|compulsory|last date|deadline|"
    r"immediately|asap|final notice|strictly|must|last chance|penalty|"
    r"not be accepted|no extension)\b"
)


def classify(title: str, body: str) -> str:
    """Pick the category whose keywords appear most often (title counts double)."""
    text = f"{title} {title} {body}".lower()
    best, best_score = "general", 0
    for slug in CLASSIFY_ORDER:
        score = sum(len(p.findall(text)) for p in _KW_PATTERNS[slug])
        if score > best_score:
            best, best_score = slug, score
    return best


# ---------------------------------------------------------------------------
# Deadline detection
# ---------------------------------------------------------------------------
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_MON = "(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"

_NUMERIC = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4}|\d{2})\b")
_DAY_MON = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?" + _MON + r"\b\.?,?\s*(\d{4})?"
)
_MON_DAY = re.compile(
    r"\b" + _MON + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*:)(?:,?\s*(\d{4}))?"
)


def _make_date(day, month, year, base):
    try:
        if year is None:
            d = date(base.year, month, day)
            if (base - d).days > 60:  # "10 Jan" seen in December -> next year
                d = date(base.year + 1, month, day)
            return d
        year = int(year)
        if year < 100:
            year += 2000
        return date(year, month, day)
    except ValueError:
        return None


def extract_deadline(text: str, base: date):
    """Return the most relevant date mentioned in the text (or None)."""
    t = text.lower()
    found = set()

    for m in _NUMERIC.finditer(t):  # day-first, e.g. 25/09/2026
        d = _make_date(int(m.group(1)), int(m.group(2)), m.group(3), base)
        if d:
            found.add(d)
    for m in _DAY_MON.finditer(t):  # 25 Sep 2026
        d = _make_date(int(m.group(1)), _MONTHS[m.group(2)], m.group(3), base)
        if d:
            found.add(d)
    for m in _MON_DAY.finditer(t):  # Sep 25, 2026
        d = _make_date(int(m.group(2)), _MONTHS[m.group(1)], m.group(3), base)
        if d:
            found.add(d)

    if not found:
        if re.search(r"\btomorrow\b", t):
            found.add(base + timedelta(days=1))
        elif re.search(r"\b(today|tonight)\b", t):
            found.add(base)

    if not found:
        return None
    upcoming = sorted(d for d in found if d >= base)
    return upcoming[0] if upcoming else max(found)


def is_important(text: str, category: str, deadline) -> bool:
    if IMPORTANT_RE.search(text.lower()):
        return True
    if deadline and category in ("assignment", "forms", "exam"):
        return True
    if deadline and 0 <= (deadline - date.today()).days <= 3:
        return True
    return False


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
app = Flask(__name__)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL,
    deadline    TEXT,
    important   INTEGER NOT NULL DEFAULT 0,
    is_read     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS student (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    name      TEXT, roll_no TEXT, branch TEXT, year TEXT,
    division  TEXT, email TEXT, phone TEXT, college TEXT
);
"""


def add_notice(db, title, body="", category=None, created=None):
    created = created or datetime.now()
    text = f"{title}\n{body}"
    if category not in CATEGORIES:
        category = classify(title, body)
    deadline = extract_deadline(text, created.date())
    important = int(is_important(text, category, deadline))
    cur = db.execute(
        "INSERT INTO notices (title, body, category, deadline, important, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            title.strip(),
            body.strip(),
            category,
            deadline.isoformat() if deadline else None,
            important,
            created.isoformat(timespec="seconds"),
        ),
    )
    db.commit()
    return cur.lastrowid, category, deadline, bool(important)


def seed_samples(db):
    """Fill an empty database with a few example notices."""
    today = date.today()
    fmt = lambda n: (today + timedelta(days=n)).strftime("%d %b %Y")
    at = lambda d, h, m: datetime.combine(d, time(h, m))
    yesterday = today - timedelta(days=1)
    samples = [
        ("Library timings changed",
         "The central library will stay open from 8 AM to 8 PM from Monday. "
         "Please carry your ID card.", at(today, 9, 10)),
        ("DBMS Assignment 3 submission",
         f"Submit DBMS Assignment 3 on the college portal by {fmt(2)}. "
         "Late submissions will not be accepted.", at(today, 10, 30)),
        ("Annual Tech Fest - Innovex 2026",
         f"Innovex hackathon and coding competition will be held on {fmt(9)}. "
         "Teams of up to 4 students can take part.", at(today, 11, 45)),
        ("Scholarship application form",
         f"Fill the online scholarship form on the student portal before {fmt(4)}. "
         "Attach your income certificate. Mandatory for all applicants.",
         at(today, 13, 20)),
        ("Mid-semester exam timetable released",
         f"The mid-semester exam timetable is out. Exams start from {fmt(12)}. "
         "Hall tickets will be issued soon.", at(today, 14, 5)),
        ("Holiday this Friday",
         "The college will remain closed this Friday for a local festival.",
         at(yesterday, 16, 40)),
        ("Physics lab record",
         f"Submit your Physics lab record to the lab assistant by {fmt(1)}.",
         at(yesterday, 12, 15)),
    ]
    for title, body, created in samples:
        add_notice(db, title, body, created=created)


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute(
        "INSERT OR IGNORE INTO student (id, name, roll_no, branch, year, division,"
        " email, phone, college) VALUES (1, 'Student Name', '', '', '', '', '', '', '')"
    )
    db.commit()
    if db.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 0:
        seed_samples(db)
    db.close()


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------
def deadline_label(category: str) -> str:
    return {"exam": "Exam date", "events": "Event date"}.get(category, "Deadline")


def enrich(row):
    n = dict(row)
    cat = CATEGORIES[n["category"]]
    n.update(cat_name=cat["name"], icon=cat["icon"], color=cat["color"])
    n["when"] = datetime.fromisoformat(n["created_at"]).strftime("%d %b, %I:%M %p")
    n["deadline_label"] = deadline_label(n["category"])
    if n["deadline"]:
        d = date.fromisoformat(n["deadline"])
        left = (d - date.today()).days
        n["deadline_fmt"] = d.strftime("%a, %d %b %Y")
        if left < 0:
            n["urgency"], n["days_text"] = "overdue", f"passed {-left} day{'s' * (-left != 1)} ago"
        elif left == 0:
            n["urgency"], n["days_text"] = "soon", "today!"
        elif left == 1:
            n["urgency"], n["days_text"] = "soon", "tomorrow"
        elif left <= 3:
            n["urgency"], n["days_text"] = "soon", f"in {left} days"
        elif left <= 7:
            n["urgency"], n["days_text"] = "near", f"in {left} days"
        else:
            n["urgency"], n["days_text"] = "ok", f"in {left} days"
        n["days_left"] = left
    return n


def safe_next(target):
    """Only allow redirects back to a path on this site."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("home")


@app.context_processor
def inject_layout():
    db = get_db()
    unread = {
        r["category"]: r["c"]
        for r in db.execute(
            "SELECT category, COUNT(*) AS c FROM notices WHERE is_read = 0 GROUP BY category"
        )
    }
    drawer = [
        {"slug": s, "name": c["name"], "icon": c["icon"], "unread": unread.get(s, 0)}
        for s, c in CATEGORIES.items()
    ]
    name = (db.execute("SELECT name FROM student WHERE id = 1").fetchone() or {"name": ""})["name"]
    parts = [p for p in (name or "").split() if p]
    initials = "".join(p[0] for p in parts[:2]).upper() or "S"
    return {"drawer": drawer, "initials": initials, "CATEGORIES": CATEGORIES}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    db = get_db()
    today = date.today()
    rows = db.execute(
        "SELECT * FROM notices WHERE date(created_at) = ? ORDER BY created_at DESC",
        (today.isoformat(),),
    ).fetchall()
    items = [enrich(r) for r in rows]

    counts = {s: 0 for s in CATEGORIES}
    for n in items:
        counts[n["category"]] += 1
    chips = [
        {"slug": s, "name": c["name"], "icon": c["icon"], "color": c["color"], "count": counts[s]}
        for s, c in CATEGORIES.items()
    ]

    horizon = (today + timedelta(days=7)).isoformat()
    upcoming = [
        enrich(r)
        for r in db.execute(
            "SELECT * FROM notices WHERE deadline IS NOT NULL AND important = 1"
            " AND deadline BETWEEN ? AND ? ORDER BY deadline ASC LIMIT 8",
            (today.isoformat(), horizon),
        )
    ]
    return render_template(
        "home.html",
        active="home",
        items=items,
        chips=chips,
        upcoming=upcoming,
        today_str=today.strftime("%A, %d %B %Y"),
    )


@app.route("/category/<slug>")
def category(slug):
    if slug not in CATEGORIES:
        return redirect(url_for("home"))
    flt = request.args.get("filter", "all")
    rows = [
        enrich(r)
        for r in get_db().execute(
            "SELECT * FROM notices WHERE category = ? ORDER BY created_at DESC", (slug,)
        )
    ]
    tabs = [
        ("all", "All", len(rows)),
        ("important", "Important", sum(1 for n in rows if n["important"])),
        ("unread", "Unread", sum(1 for n in rows if not n["is_read"])),
        ("deadline", "With dates", sum(1 for n in rows if n["deadline"])),
    ]
    if flt == "important":
        rows = [n for n in rows if n["important"]]
    elif flt == "unread":
        rows = [n for n in rows if not n["is_read"]]
    elif flt == "deadline":
        rows = sorted(
            (n for n in rows if n["deadline"]), key=lambda n: n["deadline"]
        )
    else:
        flt = "all"
        rows.sort(key=lambda n: not n["important"])  # stable: important first
    return render_template(
        "category.html", active=slug, cat=CATEGORIES[slug], slug=slug,
        items=rows, tabs=tabs, flt=flt,
    )


@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        cat = request.form.get("category", "auto")
        if not title:
            return render_template("add.html", error="Please enter a title.", form=request.form)
        _id, cat, _dl, _imp = add_notice(get_db(), title, body, cat)
        return redirect(url_for("category", slug=cat))
    return render_template("add.html", form={}, error=None, active="add")


@app.post("/read/<int:nid>")
def toggle_read(nid):
    db = get_db()
    db.execute("UPDATE notices SET is_read = 1 - is_read WHERE id = ?", (nid,))
    db.commit()
    return redirect(safe_next(request.form.get("next")))


@app.post("/move/<int:nid>")
def move(nid):
    slug = request.form.get("category")
    if slug in CATEGORIES:
        db = get_db()
        row = db.execute("SELECT * FROM notices WHERE id = ?", (nid,)).fetchone()
        if row:
            dl = date.fromisoformat(row["deadline"]) if row["deadline"] else None
            imp = int(is_important(f"{row['title']}\n{row['body']}", slug, dl))
            db.execute(
                "UPDATE notices SET category = ?, important = ? WHERE id = ?", (slug, imp, nid)
            )
            db.commit()
    return redirect(safe_next(request.form.get("next")))


@app.post("/delete/<int:nid>")
def delete(nid):
    db = get_db()
    db.execute("DELETE FROM notices WHERE id = ?", (nid,))
    db.commit()
    return redirect(safe_next(request.form.get("next")))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    db = get_db()
    fields = ["name", "roll_no", "branch", "year", "division", "email", "phone", "college"]
    saved = False
    if request.method == "POST":
        values = [request.form.get(f, "").strip() for f in fields]
        db.execute(
            "UPDATE student SET " + ", ".join(f"{f} = ?" for f in fields) + " WHERE id = 1",
            values,
        )
        db.commit()
        saved = True
    student = dict(db.execute("SELECT * FROM student WHERE id = 1").fetchone())
    total = db.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
    unread = db.execute("SELECT COUNT(*) FROM notices WHERE is_read = 0").fetchone()[0]
    return render_template(
        "profile.html", student=student, saved=saved, total=total, unread=unread,
    )


@app.post("/api/ingest")
def api_ingest():
    """Let another program push notices in.

    curl -X POST http://127.0.0.1:5000/api/ingest \
         -H "Content-Type: application/json" \
         -d '{"title": "Maths quiz", "body": "Quiz on 30 Sep, room 204"}'

    Set the environment variable INGEST_KEY to require an X-API-Key header.
    """
    key = os.environ.get("INGEST_KEY")
    if key and request.headers.get("X-API-Key") != key:
        return jsonify(error="invalid API key"), 401
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify(error="'title' is required"), 400
    created = None
    if data.get("received_at"):
        try:
            created = datetime.fromisoformat(data["received_at"])
        except ValueError:
            return jsonify(error="received_at must be ISO format"), 400
    nid, cat, dl, imp = add_notice(
        get_db(), title, data.get("body", ""), data.get("category"), created
    )
    return jsonify(id=nid, category=cat, deadline=dl.isoformat() if dl else None, important=imp), 201


# ---------------------------------------------------------------------------
# Templates (kept in this file so the whole app is one script)
# ---------------------------------------------------------------------------
TEMPLATES = {
"base.html": r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Campus Notices{% endblock %}</title>
<style>
:root{--ink:#1b2333;--muted:#5d6779;--line:#dde2ec;--bg:#eef1f7;--card:#fff;--brand:#243b6b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Segoe UI",system-ui,-apple-system,Roboto,sans-serif;line-height:1.5}
a{color:inherit;text-decoration:none}
.topbar{position:sticky;top:0;z-index:10;display:flex;align-items:center;justify-content:space-between;height:56px;padding:0 12px;background:var(--brand);color:#fff}
.brand{font-weight:600;font-size:1.05rem;letter-spacing:.2px}
.icon-btn{width:40px;height:40px;border:0;border-radius:50%;background:transparent;color:#fff;font-size:1.6rem;line-height:1;cursor:pointer}
.icon-btn:hover,.avatar:hover{background:rgba(255,255,255,.16)}
.avatar{width:38px;height:38px;border-radius:50%;background:#fff;color:var(--brand);display:grid;place-items:center;font-weight:700;font-size:.9rem}
:focus-visible{outline:3px solid #ffb703;outline-offset:2px}
#overlay{position:fixed;inset:0;background:rgba(15,20,35,.45);opacity:0;pointer-events:none;transition:opacity .22s;z-index:20}
#overlay.show{opacity:1;pointer-events:auto}
#drawer{position:fixed;top:0;left:0;bottom:0;width:284px;max-width:85vw;background:#fff;transform:translateX(-102%);transition:transform .22s ease;z-index:30;overflow-y:auto;box-shadow:6px 0 24px rgba(0,0,0,.18)}
#drawer.open{transform:none}
.drawer-head{padding:18px 20px;background:var(--brand);color:#fff;font-weight:600}
#drawer a{display:flex;align-items:center;gap:12px;padding:14px 20px;border-left:5px solid transparent}
#drawer a:hover{background:#f3f5fa}
#drawer a.active{background:#eaeffa;border-left-color:var(--brand);font-weight:600}
#drawer hr{border:0;border-top:1px solid var(--line);margin:8px 0}
.pill{margin-left:auto;min-width:24px;padding:0 8px;border-radius:99px;background:var(--brand);color:#fff;font-size:.78rem;text-align:center}
main{max-width:760px;margin:0 auto;padding:18px 14px 96px}
h1{margin:6px 0 2px;font-size:1.6rem}
h2{margin:26px 0 10px;font-size:1.05rem}
.sub{margin:0 0 14px;color:var(--muted)}
.chips{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px}
.chip{flex:0 0 auto;display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:12px;background:var(--card);border:1px solid var(--line)}
.chip b{display:inline-grid;place-items:center;min-width:24px;height:24px;border-radius:99px;background:var(--c);color:#fff;font-size:.8rem}
.chip.zero{opacity:.55}
.strip{display:flex;gap:10px;overflow-x:auto;padding-bottom:6px}
.dl{flex:0 0 220px;padding:10px 12px;border-radius:10px;background:var(--card);border:1px solid var(--line);border-top:4px solid #2f9e44;display:flex;flex-direction:column;gap:2px}
.dl span{font-size:.85rem;color:var(--muted)}
.dl.soon{border-top-color:#d64545}.dl.near{border-top-color:#d98a00}
.card{position:relative;margin:0 0 12px;padding:14px 16px 12px;background:var(--card);border-radius:8px 14px 14px 8px;border:1px solid var(--line);border-left:7px solid var(--c)}
.card.unread{box-shadow:0 0 0 2px rgba(36,59,107,.18)}
.card.unread h3::before{content:"";display:inline-block;width:9px;height:9px;margin-right:8px;border-radius:50%;background:#2b6cf0;vertical-align:middle}
.meta{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:.82rem;color:var(--muted)}
.badge{padding:2px 9px;border-radius:99px;background:color-mix(in srgb,var(--c) 14%,#fff);color:var(--ink);font-weight:600}
.imp{color:#b42318;font-weight:700}
.time{margin-left:auto}
.card h3{margin:8px 0 4px;font-size:1.05rem}
.card p{margin:0 0 8px;color:#333c4d;white-space:pre-line}
.deadline{display:inline-block;margin:2px 0 8px;padding:5px 10px;border-radius:8px;background:#e7f6ec;color:#1e6b34;font-size:.9rem}
.deadline.near{background:#fff3d6;color:#8a5a00}
.deadline.soon,.deadline.overdue{background:#fde4e4;color:#a12020}
.actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:4px}
.actions form{margin:0}
.actions button,.actions select{font:inherit;font-size:.85rem;padding:5px 10px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink);cursor:pointer}
.actions button:hover,.actions select:hover{background:#f3f5fa}
.actions .del{margin-left:auto;color:#a12020}
.tabs{display:flex;gap:6px;overflow-x:auto;margin:6px 0 16px}
.tab{flex:0 0 auto;padding:7px 14px;border-radius:99px;border:1px solid var(--line);background:#fff;font-size:.9rem}
.tab.on{background:var(--brand);border-color:var(--brand);color:#fff}
.empty{padding:28px 16px;text-align:center;color:var(--muted);background:var(--card);border:1px dashed #b9c1d1;border-radius:12px}
.fab{position:fixed;right:18px;bottom:20px;width:56px;height:56px;border-radius:50%;background:#e8590c;color:#fff;font-size:2rem;display:grid;place-items:center;box-shadow:0 6px 18px rgba(0,0,0,.28)}
form.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
form.panel label{display:block;margin:12px 0 4px;font-weight:600;font-size:.92rem}
form.panel input,form.panel textarea,form.panel select{width:100%;font:inherit;padding:9px 11px;border:1px solid #c3cad8;border-radius:8px;background:#fff}
form.panel textarea{min-height:130px;resize:vertical}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
@media(max-width:520px){.grid2{grid-template-columns:1fr}}
.btn{margin-top:16px;padding:10px 20px;border:0;border-radius:8px;background:var(--brand);color:#fff;font:inherit;font-weight:600;cursor:pointer}
.ok{padding:10px 14px;border-radius:8px;background:#e7f6ec;color:#1e6b34;margin-bottom:12px}
.err{padding:10px 14px;border-radius:8px;background:#fde4e4;color:#a12020;margin-bottom:12px}
.who{display:flex;align-items:center;gap:14px;margin-bottom:14px}
.who .avatar{width:64px;height:64px;font-size:1.4rem;background:var(--brand);color:#fff}
.stats{display:flex;gap:10px;margin:8px 0 16px}
.stats div{flex:1;padding:10px;background:var(--card);border:1px solid var(--line);border-radius:10px;text-align:center}
.stats b{display:block;font-size:1.4rem}
@media(prefers-reduced-motion:reduce){ #drawer,#overlay{transition:none}}
</style>
</head>
<body>
<header class="topbar">
  <button class="icon-btn" id="menuBtn" aria-label="Open categories menu" aria-controls="drawer">&#8942;</button>
  <a class="brand" href="{{ url_for('home') }}">Campus Notices</a>
  <a class="avatar" href="{{ url_for('profile') }}" aria-label="Student profile">{{ initials }}</a>
</header>

<div id="overlay"></div>
<nav id="drawer" aria-label="Categories">
  <div class="drawer-head">Notice categories</div>
  <a href="{{ url_for('home') }}" class="{{ 'active' if active == 'home' }}">🏠 Today</a>
  {% for c in drawer %}
  <a href="{{ url_for('category', slug=c.slug) }}" class="{{ 'active' if active == c.slug }}">
    <span>{{ c.icon }}</span> {{ c.name }}
    {% if c.unread %}<span class="pill" title="{{ c.unread }} unread">{{ c.unread }}</span>{% endif %}
  </a>
  {% endfor %}
  <hr>
  <a href="{{ url_for('add') }}" class="{{ 'active' if active == 'add' }}">＋ Add a notice</a>
</nav>

<main>{% block content %}{% endblock %}</main>
<a class="fab" href="{{ url_for('add') }}" aria-label="Add a notice">+</a>

<script>
(function(){
  var d=document.getElementById('drawer'),o=document.getElementById('overlay'),b=document.getElementById('menuBtn');
  function set(open){d.classList.toggle('open',open);o.classList.toggle('show',open);b.setAttribute('aria-expanded',open)}
  b.addEventListener('click',function(){set(!d.classList.contains('open'))});
  o.addEventListener('click',function(){set(false)});
  document.addEventListener('keydown',function(e){if(e.key==='Escape')set(false)});
})();
</script>
</body>
</html>
""",

"macros.html": r"""{% macro card(n, next) %}
<article class="card{% if not n.is_read %} unread{% endif %}" style="--c: {{ n.color }}">
  <div class="meta">
    <span class="badge">{{ n.icon }} {{ n.cat_name }}</span>
    {% if n.important %}<span class="imp">★ Important</span>{% endif %}
    <span class="time">{{ n.when }}</span>
  </div>
  <h3>{{ n.title }}</h3>
  {% if n.body %}<p>{{ n.body }}</p>{% endif %}
  {% if n.deadline %}
  <div class="deadline {{ n.urgency }}">⏰ {{ n.deadline_label }}: <b>{{ n.deadline_fmt }}</b> ({{ n.days_text }})</div>
  {% endif %}
  <div class="actions">
    <form method="post" action="{{ url_for('toggle_read', nid=n.id) }}">
      <input type="hidden" name="next" value="{{ next }}">
      <button type="submit">{{ 'Mark as unread' if n.is_read else 'Mark as read' }}</button>
    </form>
    <form method="post" action="{{ url_for('move', nid=n.id) }}">
      <input type="hidden" name="next" value="{{ next }}">
      <select name="category" onchange="this.form.submit()" aria-label="Move to another category">
        {% for slug, c in CATEGORIES.items() %}
        <option value="{{ slug }}" {{ 'selected' if slug == n.category }}>{{ c.name }}</option>
        {% endfor %}
      </select>
    </form>
    <form method="post" action="{{ url_for('delete', nid=n.id) }}" onsubmit="return confirm('Delete this notice?')">
      <input type="hidden" name="next" value="{{ next }}">
      <button class="del" type="submit">Delete</button>
    </form>
  </div>
</article>
{% endmacro %}
""",

"home.html": r"""{% extends "base.html" %}
{% from "macros.html" import card %}
{% block content %}
<h1>Today's notices</h1>
<p class="sub">{{ today_str }} · {{ items|length }} received today</p>

<div class="chips">
  {% for c in chips %}
  <a class="chip{{ ' zero' if not c.count }}" style="--c: {{ c.color }}" href="{{ url_for('category', slug=c.slug) }}">
    {{ c.icon }} {{ c.name }} <b>{{ c.count }}</b>
  </a>
  {% endfor %}
</div>

{% if upcoming %}
<h2>Deadlines in the next 7 days</h2>
<div class="strip">
  {% for n in upcoming %}
  <a class="dl {{ n.urgency }}" href="{{ url_for('category', slug=n.category) }}">
    <b>{{ n.title }}</b>
    <span>{{ n.deadline_label }}: {{ n.deadline_fmt }} ({{ n.days_text }})</span>
  </a>
  {% endfor %}
</div>
{% endif %}

<h2>Everything from today</h2>
{% for n in items %}{{ card(n, url_for('home')) }}
{% else %}
<div class="empty">No notices have arrived today. Use the + button to add one, or open a category to see earlier notices.</div>
{% endfor %}
{% endblock %}
""",

"category.html": r"""{% extends "base.html" %}
{% from "macros.html" import card %}
{% block title %}{{ cat.name }} · Campus Notices{% endblock %}
{% block content %}
<h1>{{ cat.icon }} {{ cat.name }}</h1>
<p class="sub">Only {{ cat.name|lower }} notices appear here.</p>
<div class="tabs">
  {% for key, label, count in tabs %}
  <a class="tab{{ ' on' if key == flt }}" href="{{ url_for('category', slug=slug, filter=key) }}">{{ label }} ({{ count }})</a>
  {% endfor %}
</div>
{% for n in items %}{{ card(n, url_for('category', slug=slug, filter=flt)) }}
{% else %}
<div class="empty">Nothing to show in this view.</div>
{% endfor %}
{% endblock %}
""",

"add.html": r"""{% extends "base.html" %}
{% block title %}Add a notice · Campus Notices{% endblock %}
{% block content %}
<h1>Add a notice</h1>
<p class="sub">Paste the message from your college. The category and any deadline are detected automatically.</p>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form class="panel" method="post" action="{{ url_for('add') }}">
  <label for="title">Title</label>
  <input id="title" name="title" value="{{ form.get('title', '') }}" placeholder="e.g. Maths assignment 2" required>
  <label for="body">Message</label>
  <textarea id="body" name="body" placeholder="Paste the full notice here">{{ form.get('body', '') }}</textarea>
  <label for="category">Category</label>
  <select id="category" name="category">
    <option value="auto">Detect automatically</option>
    {% for slug, c in CATEGORIES.items() %}
    <option value="{{ slug }}" {{ 'selected' if form.get('category') == slug }}>{{ c.name }}</option>
    {% endfor %}
  </select>
  <button class="btn" type="submit">Save notice</button>
</form>
{% endblock %}
""",

"profile.html": r"""{% extends "base.html" %}
{% block title %}Student profile · Campus Notices{% endblock %}
{% block content %}
<div class="who">
  <div class="avatar">{{ initials }}</div>
  <div>
    <h1 style="margin:0">{{ student.name or 'Student' }}</h1>
    <div class="sub" style="margin:0">{{ student.branch }}{% if student.branch and student.year %}, {% endif %}{{ student.year }}</div>
  </div>
</div>
<div class="stats">
  <div><b>{{ total }}</b>notices</div>
  <div><b>{{ unread }}</b>unread</div>
</div>
{% if saved %}<div class="ok">Profile saved.</div>{% endif %}
<form class="panel" method="post" action="{{ url_for('profile') }}">
  <label for="name">Full name</label>
  <input id="name" name="name" value="{{ student.name or '' }}">
  <div class="grid2">
    <div><label for="roll_no">Roll number</label><input id="roll_no" name="roll_no" value="{{ student.roll_no or '' }}"></div>
    <div><label for="division">Division</label><input id="division" name="division" value="{{ student.division or '' }}"></div>
    <div><label for="branch">Branch</label><input id="branch" name="branch" value="{{ student.branch or '' }}"></div>
    <div><label for="year">Year</label><input id="year" name="year" value="{{ student.year or '' }}"></div>
    <div><label for="email">Email</label><input id="email" type="email" name="email" value="{{ student.email or '' }}"></div>
    <div><label for="phone">Phone</label><input id="phone" name="phone" value="{{ student.phone or '' }}"></div>
  </div>
  <label for="college">College</label>
  <input id="college" name="college" value="{{ student.college or '' }}">
  <button class="btn" type="submit">Save changes</button>
</form>
{% endblock %}
""",
}

app.jinja_env.loader = DictLoader(TEMPLATES)

init_db()

if __name__ == "__main__":
    # Use HOST=0.0.0.0 to open the app from your phone on the same Wi-Fi.
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)))
