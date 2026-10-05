import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from bson import ObjectId
from bson.errors import InvalidId
from flask import Flask, abort, redirect, render_template, request, session, url_for
from pymongo.errors import DuplicateKeyError
from werkzeug.security import check_password_hash, generate_password_hash

from db import check_connection, records, users, workouts

# CSS lives in public/css/style.css (served directly on Vercel)
app = Flask(__name__, static_folder="public", static_url_path="")

# ------------------------------------------------------------ security setup
ON_VERCEL = bool(os.environ.get("VERCEL"))

secret = os.environ.get("SECRET_KEY")
if not secret:
    if ON_VERCEL:
        # Never fall back to a known key in production: anyone could forge logins.
        raise RuntimeError("SECRET_KEY environment variable is not set.")
    secret = "dev-only-secret-change-me"   # local development only

app.secret_key = secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=ON_VERCEL,       # HTTPS-only cookie when deployed
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

MAX_DAYS = 90
MUSCLES = ["Chest", "Back", "Shoulders", "Biceps", "Triceps", "Legs", "Core"]
NUDGE_AFTER = 4   # warn if a muscle hasn't been trained for this many days
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")


# ------------------------------------------------------------ auth helpers
def uid():
    """Id of the logged-in user (as a string)."""
    return session["user_id"]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            nxt = request.full_path.rstrip("?") if request.method == "GET" else None
            return redirect(url_for("login", next=nxt))
        return view(*args, **kwargs)
    return wrapped


def safe_next(target):
    """Only allow redirects to paths inside this site."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("index")


def start_session(user):
    session.clear()   # drop any old session data
    session["user_id"] = str(user["_id"])
    session["username"] = user["username"]
    session.permanent = True


# ---------------------------------------------------------------- helpers
def valid_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d") == s
    except (ValueError, TypeError):
        return False


def to_object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        abort(404)


def fmt_num(n, decimals=1):
    s = f"{float(n):.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def est_1rm(weight, reps):
    """Epley formula. Returns 0 when it can't be calculated."""
    if weight <= 0 or reps < 1:
        return 0
    return weight if reps == 1 else weight * (1 + reps / 30)


def delta(now, before):
    if now == 0 and before == 0:
        return "same", "–", "no data"
    if before == 0:
        return "up", "▲", "new this week"
    pct = round((now - before) / before * 100)
    if pct > 0:
        return "up", "▲", f"+{pct}% vs last week"
    if pct < 0:
        return "down", "▼", f"{pct}% vs last week"
    return "same", "＝", "same as last week"


@app.template_filter("pretty_date")
def pretty_date(s, fmt="%d %b %Y"):
    return datetime.strptime(s, "%Y-%m-%d").strftime(fmt)


@app.template_filter("num")
def num_filter(n, decimals=1):
    return fmt_num(n, decimals)


@app.template_filter("commas")
def commas(n):
    return f"{n:,.0f}"


@app.context_processor
def inject_globals():
    css = os.path.join(app.static_folder, "css", "style.css")
    try:
        version = int(os.path.getmtime(css))
    except OSError:
        version = 0
    return {"css_version": version, "MAX_DAYS": MAX_DAYS, "MUSCLES": MUSCLES}


# ------------------------------------------------------ 90-day rule logic
def kept_dates(u):
    return sorted(workouts.distinct("workout_date", {"user_id": u}), reverse=True)


def prune_old_days(u):
    """Keep only this user's latest MAX_DAYS distinct dates."""
    dates = kept_dates(u)
    if len(dates) > MAX_DAYS:
        cutoff = dates[MAX_DAYS - 1]
        workouts.delete_many({"user_id": u, "workout_date": {"$lt": cutoff}})


def date_allowed(u, d):
    """Reject dates so old they'd be deleted immediately."""
    dates = kept_dates(u)[:MAX_DAYS]
    if len(dates) < MAX_DAYS or d in dates:
        return True
    return d > dates[-1]


# ------------------------------------------------------------- PR / records
def compute_prs(u):
    """
    Returns (prev_by_id, pr_by_date) for one user.
    prev_by_id[_id] = best weight for that exercise on EARLIER dates (or None).
    pr_by_date[date] = number of PRs on that date.
    """
    rows = list(workouts.find({"user_id": u}).sort([("workout_date", 1), ("_id", 1)]))
    best = {}
    prev_by_id = {}
    pr_by_date = defaultdict(int)

    i = 0
    while i < len(rows):
        d = rows[i]["workout_date"]
        j = i
        while j < len(rows) and rows[j]["workout_date"] == d:
            j += 1
        group = rows[i:j]

        for w in group:
            prev = best.get(w["exercise"].lower())
            prev_by_id[w["_id"]] = prev
            if prev is not None and w["weight"] > prev:
                pr_by_date[d] += 1

        for w in group:   # update history only after the whole day is judged
            if w["weight"] > 0:
                key = w["exercise"].lower()
                best[key] = max(best.get(key, 0), w["weight"])
        i = j

    return prev_by_id, pr_by_date


def update_records(u):
    """Raise this user's all-time bests. Never lowers a record."""
    best = {}
    cursor = workouts.find({"user_id": u, "weight": {"$gt": 0}}).sort(
        [("weight", -1), ("reps", -1), ("workout_date", 1)]
    )
    for w in cursor:
        best.setdefault(w["exercise"].lower(), w)

    for key, w in best.items():
        rid = f"{u}:{key}"   # records are per user
        cur = records.find_one({"_id": rid})
        better = cur is None or w["weight"] > cur["best_weight"] or (
            w["weight"] == cur["best_weight"] and w["reps"] > cur["best_reps"]
        )
        if better:
            records.replace_one(
                {"_id": rid},
                {
                    "user_id": u,
                    "exercise": w["exercise"],
                    "muscle": w["muscle"],
                    "best_weight": w["weight"],
                    "best_reps": w["reps"],
                    "achieved_on": w["workout_date"],
                },
                upsert=True,
            )


# ---------------------------------------------------------- stats helpers
def weekly_summary(u):
    today = date.today()
    this_start = today - timedelta(days=today.weekday())   # Monday
    last_start = this_start - timedelta(days=7)
    next_start = this_start + timedelta(days=7)

    def totals(a, b):
        rows = list(workouts.find({
            "user_id": u,
            "workout_date": {"$gte": a.isoformat(), "$lt": b.isoformat()},
        }))
        return {
            "days": len({r["workout_date"] for r in rows}),
            "sets": sum(r["sets"] for r in rows),
            "volume": sum(r["sets"] * r["reps"] * r["weight"] for r in rows),
        }

    return totals(this_start, next_start), totals(last_start, this_start)


def exercise_suggestions(u):
    counts = defaultdict(int)
    names = {}
    for w in workouts.find({"user_id": u}, {"exercise": 1}):
        k = w["exercise"].lower()
        counts[k] += 1
        names.setdefault(k, w["exercise"])
    return [names[k] for k in sorted(counts, key=lambda k: (-counts[k], k))]


def last_performance(u):
    out = {}
    for w in workouts.find({"user_id": u}).sort([("workout_date", -1), ("_id", -1)]):
        k = w["exercise"].lower()
        if k not in out:
            out[k] = {
                "muscle": w["muscle"],
                "label": pretty_date(w["workout_date"], "%d %b"),
                "sets": w["sets"],
                "reps": w["reps"],
                "weight": w["weight"],
                "best": w["weight"],
            }
        else:
            out[k]["best"] = max(out[k]["best"], w["weight"])
    return out


def parse_exercise(get, index=None):
    """Read one exercise from the form. Returns (data, error)."""
    def pick(name):
        return get(name)[index] if index is not None else get(name)

    try:
        data = {
            "exercise": pick("exercise").strip(),
            "muscle": pick("muscle"),
            "sets": int(pick("sets") or 0),
            "reps": int(pick("reps") or 0),
            "weight": float(pick("weight") or 0),
        }
    except (ValueError, IndexError):
        return None, "Sets, reps and weight must be numbers."

    name = data["exercise"] or "exercise"
    if not data["exercise"]:
        return None, "Exercise name is required."
    if data["muscle"] not in MUSCLES:
        return None, f'Choose a muscle group for "{name}".'
    if data["sets"] < 1 or data["reps"] < 1:
        return None, f'Check "{name}": sets and reps must be at least 1.'
    if data["weight"] < 0:
        return None, f'Check "{name}": weight can\'t be negative.'
    return data, None


# ------------------------------------------------------------- auth routes
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if "user_id" in session:
        return redirect(url_for("index"))

    error = None
    username = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not USERNAME_RE.match(username):
            error = "Username must be 3 to 30 characters: letters, numbers or underscores."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif len(password) > 128:
            error = "Password is too long (max 128 characters)."
        elif password != confirm:
            error = "The two passwords don't match."
        else:
            doc = {
                "username": username,
                "username_lower": username.lower(),
                "password_hash": generate_password_hash(password),
                "created_at": datetime.now(timezone.utc),
            }
            try:
                result = users.insert_one(doc)
            except DuplicateKeyError:
                error = "That username is already taken."
            else:
                doc["_id"] = result.inserted_id
                start_session(doc)
                return redirect(url_for("index"))

    return render_template("signup.html", error=error, username=username)


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    error = None
    username = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = users.find_one({"username_lower": username.lower()})
        if user and check_password_hash(user["password_hash"], password):
            start_session(user)
            return redirect(safe_next(request.args.get("next")))
        # Same message for "no such user" and "wrong password"
        error = "Incorrect username or password."

    return render_template("login.html", error=error, username=username)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ routes
@app.route("/")
@login_required
def index():
    u = uid()
    update_records(u)   # also back-fills records from existing workouts

    pipeline = [
        {"$match": {"user_id": u}},
        {"$group": {
            "_id": "$workout_date",
            "exercises": {"$sum": 1},
            "total_sets": {"$sum": "$sets"},
            "muscles": {"$addToSet": "$muscle"},
        }},
        {"$sort": {"_id": -1}},
    ]
    days = list(workouts.aggregate(pipeline))
    for d in days:
        d["muscles"] = ", ".join(sorted(d["muscles"]))

    _, pr_by_date = compute_prs(u)
    for d in days:
        d["prs"] = pr_by_date.get(d["_id"], 0)

    # weekly tiles
    this_w, last_w = weekly_summary(u)
    week_metrics = []
    for icon, label, key, unit, color in [
        ("📅", "Workout days", "days", "", "#3b82f6"),
        ("🔁", "Total sets", "sets", "", "#a855f7"),
        ("🏋️", "Volume", "volume", "kg", "#f59e0b"),
    ]:
        cls, arrow, text = delta(this_w[key], last_w[key])
        week_metrics.append({
            "icon": icon, "label": label, "unit": unit, "color": color,
            "now": this_w[key], "before": last_w[key],
            "cls": cls, "arrow": arrow, "text": text,
        })

    # all-time bests
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    recs = list(records.find({"user_id": u}).sort([("best_weight", -1), ("exercise", 1)]))
    for r in recs:
        r["e1rm"] = est_1rm(r["best_weight"], r["best_reps"])
        r["is_new"] = r["achieved_on"] >= week_ago

    # muscle balance
    stats = {}
    for w in workouts.find({"user_id": u}, {"muscle": 1, "sets": 1, "workout_date": 1}):
        s = stats.setdefault(w["muscle"], {"sets": 0, "last": ""})
        s["sets"] += w["sets"]
        s["last"] = max(s["last"], w["workout_date"])

    chart_labels, chart_data, nudges = [], [], []
    today = date.today()
    for m in MUSCLES:
        if m in stats:
            chart_labels.append(m)
            chart_data.append(stats[m]["sets"])
            last = datetime.strptime(stats[m]["last"], "%Y-%m-%d").date()
            ago = max((today - last).days, 0)
            if ago >= NUDGE_AFTER:
                nudges.append(f"You haven't trained {m} in {ago} days.")
        else:
            nudges.append(f"No {m} training in your last {MAX_DAYS} logged days.")

    return render_template(
        "index.html",
        days=days,
        week_metrics=week_metrics,
        total_exercises=sum(d["exercises"] for d in days),
        total_prs=sum(d["prs"] for d in days),
        records=recs,
        chart_labels=chart_labels,
        chart_data=chart_data,
        nudges=nudges,
    )


@app.route("/day/<d>")
@login_required
def day(d):
    if not valid_date(d):
        return redirect(url_for("index"))

    u = uid()
    items = list(workouts.find({"user_id": u, "workout_date": d}).sort("_id", 1))
    prev_by_id, _ = compute_prs(u)

    total_sets = volume = pr_total = 0
    top_e1rm = 0
    muscles = []
    for w in items:
        total_sets += w["sets"]
        volume += w["sets"] * w["reps"] * w["weight"]
        top_e1rm = max(top_e1rm, est_1rm(w["weight"], w["reps"]))
        if w["muscle"] not in muscles:
            muscles.append(w["muscle"])

        prev = prev_by_id.get(w["_id"])
        w["prev"] = prev
        w["is_pr"] = prev is not None and w["weight"] > prev
        w["e1rm"] = est_1rm(w["weight"], w["reps"])
        pr_total += w["is_pr"]

    tiles = [
        ("💪", "Exercises", str(len(items)), "#3b82f6", False),
        ("🔁", "Total sets", str(total_sets), "#a855f7", False),
        ("🏋️", "Volume", f"{volume:,.0f} kg", "#f59e0b", False),
        ("🏆", "New PRs", str(pr_total), "#eab308", False),
        ("⚡", "Top est. 1RM", f"{fmt_num(top_e1rm)} kg" if top_e1rm else "–", "#22c55e", False),
        ("🎯", "Muscles", ", ".join(muscles) or "–", "#06b6d4", True),
    ]

    dates = kept_dates(u)
    pos = dates.index(d) if d in dates else None
    newer = dates[pos - 1] if pos not in (None, 0) else None
    older = dates[pos + 1] if pos is not None and pos + 1 < len(dates) else None

    return render_template(
        "day.html", date=d, items=items, tiles=tiles, newer=newer, older=older
    )


@app.route("/add", methods=["GET", "POST"])
@login_required
def add():
    u = uid()
    error = None
    rows = [{}]
    d = request.args.get("date", "")
    if not valid_date(d):
        d = date.today().isoformat()

    if request.method == "POST":
        d = request.form.get("workout_date", "")
        names = request.form.getlist("exercise")
        parsed = []

        for i, name in enumerate(names):
            if not name.strip():
                continue   # skip blank rows
            data, err = parse_exercise(request.form.getlist, i)
            if err and not error:
                error = err
            parsed.append(data or {
                "exercise": name,
                "muscle": request.form.getlist("muscle")[i],
            })

        if not valid_date(d):
            error = "Please enter a valid date."
        elif not parsed:
            error = "Add at least one exercise."
        elif not error and not date_allowed(u, d):
            error = (f"That date is older than your latest {MAX_DAYS} workout days, "
                     "so it would be deleted straight away.")

        if not error:
            for p in parsed:
                p["workout_date"] = d
                p["user_id"] = u
            workouts.insert_many(parsed)
            update_records(u)   # records first, then prune
            prune_old_days(u)
            return redirect(url_for("day", d=d))

        rows = parsed or [{}]

    return render_template(
        "add.html", date=d, rows=rows, error=error,
        suggestions=exercise_suggestions(u), last_perf=last_performance(u),
    )


@app.route("/edit/<workout_id>", methods=["GET", "POST"])
@login_required
def edit(workout_id):
    u = uid()
    oid = to_object_id(workout_id)
    workout = workouts.find_one({"_id": oid, "user_id": u})   # only your own
    if not workout:
        abort(404)

    orig_date = workout["workout_date"]
    error = None

    if request.method == "POST":
        d = request.form.get("workout_date", "")
        data, error = parse_exercise(request.form.get)
        if not error and not valid_date(d):
            error = "Please enter a valid date."
        elif not error and not date_allowed(u, d):
            error = (f"That date is older than your latest {MAX_DAYS} workout days, "
                     "so it would be deleted straight away.")

        if not error:
            data["workout_date"] = d
            workouts.update_one({"_id": oid, "user_id": u}, {"$set": data})
            update_records(u)
            prune_old_days(u)
            return redirect(url_for("day", d=d))

        workout = {**request.form.to_dict(), "workout_date": d}

    return render_template(
        "edit.html", w=workout, wid=workout_id, orig_date=orig_date,
        error=error, suggestions=exercise_suggestions(u),
    )


@app.route("/delete/<workout_id>", methods=["POST"])
@login_required
def delete(workout_id):
    u = uid()
    oid = to_object_id(workout_id)
    w = workouts.find_one({"_id": oid, "user_id": u})
    if w:
        workouts.delete_one({"_id": oid, "user_id": u})
        if workouts.count_documents({"user_id": u, "workout_date": w["workout_date"]}) > 0:
            return redirect(url_for("day", d=w["workout_date"]))
    return redirect(url_for("index"))


@app.route("/delete-day", methods=["POST"])
@login_required
def delete_day():
    d = request.form.get("date", "")
    if valid_date(d):
        workouts.delete_many({"user_id": uid(), "workout_date": d})
    return redirect(url_for("index"))


@app.route("/delete-record", methods=["POST"])
@login_required
def delete_record():
    records.delete_one({"_id": request.form.get("key", ""), "user_id": uid()})
    return redirect(url_for("index"))


if __name__ == "__main__":
    ok, message = check_connection()
    print("MongoDB connected." if ok else f"WARNING: cannot reach MongoDB: {message}")
    app.run()