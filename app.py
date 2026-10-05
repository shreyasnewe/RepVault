import os
from collections import defaultdict
from datetime import date, datetime, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from flask import Flask, abort, redirect, render_template, request, url_for

from db import check_connection, records, workouts

app = Flask(__name__, static_folder="public", static_url_path="")

MAX_DAYS = 90
MUSCLES = ["Chest", "Back", "Shoulders", "Biceps", "Triceps", "Legs", "Core"]
NUDGE_AFTER = 4   # warn if a muscle hasn't been trained for this many days


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
    version = int(os.path.getmtime(css)) if os.path.exists(css) else 0
    return {"css_version": version, "MAX_DAYS": MAX_DAYS, "MUSCLES": MUSCLES}


# ------------------------------------------------------ 90-day rule logic
def kept_dates():
    return sorted(workouts.distinct("workout_date"), reverse=True)


def prune_old_days():
    """Keep only the latest MAX_DAYS distinct dates."""
    dates = kept_dates()
    if len(dates) > MAX_DAYS:
        cutoff = dates[MAX_DAYS - 1]
        workouts.delete_many({"workout_date": {"$lt": cutoff}})


def date_allowed(d):
    """Reject dates so old they'd be deleted immediately."""
    dates = kept_dates()[:MAX_DAYS]
    if len(dates) < MAX_DAYS or d in dates:
        return True
    return d > dates[-1]


# ------------------------------------------------------------- PR / records
def compute_prs():
    """
    Returns (prev_by_id, pr_by_date).
    prev_by_id[_id] = best weight for that exercise on EARLIER dates (or None).
    pr_by_date[date] = number of PRs on that date.
    """
    rows = list(workouts.find().sort([("workout_date", 1), ("_id", 1)]))
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


def update_records():
    """Raise all-time bests. Never lowers a record, so pruning can't erase it."""
    best = {}
    cursor = workouts.find({"weight": {"$gt": 0}}).sort(
        [("weight", -1), ("reps", -1), ("workout_date", 1)]
    )
    for w in cursor:
        best.setdefault(w["exercise"].lower(), w)

    for key, w in best.items():
        cur = records.find_one({"_id": key})
        better = cur is None or w["weight"] > cur["best_weight"] or (
            w["weight"] == cur["best_weight"] and w["reps"] > cur["best_reps"]
        )
        if better:
            records.replace_one(
                {"_id": key},
                {
                    "exercise": w["exercise"],
                    "muscle": w["muscle"],
                    "best_weight": w["weight"],
                    "best_reps": w["reps"],
                    "achieved_on": w["workout_date"],
                },
                upsert=True,
            )


# ---------------------------------------------------------- stats helpers
def weekly_summary():
    today = date.today()
    this_start = today - timedelta(days=today.weekday())   # Monday
    last_start = this_start - timedelta(days=7)
    next_start = this_start + timedelta(days=7)

    def totals(a, b):
        rows = list(workouts.find(
            {"workout_date": {"$gte": a.isoformat(), "$lt": b.isoformat()}}
        ))
        return {
            "days": len({r["workout_date"] for r in rows}),
            "sets": sum(r["sets"] for r in rows),
            "volume": sum(r["sets"] * r["reps"] * r["weight"] for r in rows),
        }

    return totals(this_start, next_start), totals(last_start, this_start)


def exercise_suggestions():
    counts = defaultdict(int)
    names = {}
    for w in workouts.find({}, {"exercise": 1}):
        k = w["exercise"].lower()
        counts[k] += 1
        names.setdefault(k, w["exercise"])
    return [names[k] for k in sorted(counts, key=lambda k: (-counts[k], k))]


def last_performance():
    out = {}
    for w in workouts.find().sort([("workout_date", -1), ("_id", -1)]):
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


# ------------------------------------------------------------------ routes
@app.route("/")
def index():
    update_records()   # also back-fills records from existing workouts

    pipeline = [
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

    _, pr_by_date = compute_prs()
    for d in days:
        d["prs"] = pr_by_date.get(d["_id"], 0)

    # weekly tiles
    this_w, last_w = weekly_summary()
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
    recs = list(records.find().sort([("best_weight", -1), ("exercise", 1)]))
    for r in recs:
        r["e1rm"] = est_1rm(r["best_weight"], r["best_reps"])
        r["is_new"] = r["achieved_on"] >= week_ago

    # muscle balance
    stats = {}
    for w in workouts.find({}, {"muscle": 1, "sets": 1, "workout_date": 1}):
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
def day(d):
    if not valid_date(d):
        return redirect(url_for("index"))

    items = list(workouts.find({"workout_date": d}).sort("_id", 1))
    prev_by_id, _ = compute_prs()

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

    dates = kept_dates()
    pos = dates.index(d) if d in dates else None
    newer = dates[pos - 1] if pos not in (None, 0) else None
    older = dates[pos + 1] if pos is not None and pos + 1 < len(dates) else None

    return render_template(
        "day.html", date=d, items=items, tiles=tiles, newer=newer, older=older
    )


@app.route("/add", methods=["GET", "POST"])
def add():
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
        elif not error and not date_allowed(d):
            error = (f"That date is older than your latest {MAX_DAYS} workout days, "
                     "so it would be deleted straight away.")

        if not error:
            for p in parsed:
                p["workout_date"] = d
            workouts.insert_many(parsed)
            update_records()   # records first, then prune
            prune_old_days()
            return redirect(url_for("day", d=d))

        rows = parsed or [{}]

    return render_template(
        "add.html", date=d, rows=rows, error=error,
        suggestions=exercise_suggestions(), last_perf=last_performance(),
    )


@app.route("/edit/<workout_id>", methods=["GET", "POST"])
def edit(workout_id):
    oid = to_object_id(workout_id)
    workout = workouts.find_one({"_id": oid})
    if not workout:
        abort(404)

    orig_date = workout["workout_date"]
    error = None

    if request.method == "POST":
        d = request.form.get("workout_date", "")
        data, error = parse_exercise(request.form.get)
        if not error and not valid_date(d):
            error = "Please enter a valid date."
        elif not error and not date_allowed(d):
            error = (f"That date is older than your latest {MAX_DAYS} workout days, "
                     "so it would be deleted straight away.")

        if not error:
            data["workout_date"] = d
            workouts.update_one({"_id": oid}, {"$set": data})
            update_records()
            prune_old_days()
            return redirect(url_for("day", d=d))

        workout = {**request.form.to_dict(), "workout_date": d}

    return render_template(
        "edit.html", w=workout, wid=workout_id, orig_date=orig_date,
        error=error, suggestions=exercise_suggestions(),
    )


@app.route("/delete/<workout_id>", methods=["POST"])
def delete(workout_id):
    oid = to_object_id(workout_id)
    w = workouts.find_one({"_id": oid})
    if w:
        workouts.delete_one({"_id": oid})
        if workouts.count_documents({"workout_date": w["workout_date"]}) > 0:
            return redirect(url_for("day", d=w["workout_date"]))
    return redirect(url_for("index"))


@app.route("/delete-day", methods=["POST"])
def delete_day():
    d = request.form.get("date", "")
    if valid_date(d):
        workouts.delete_many({"workout_date": d})
    return redirect(url_for("index"))


@app.route("/delete-record", methods=["POST"])
def delete_record():
    records.delete_one({"_id": request.form.get("key", "")})
    return redirect(url_for("index"))


if __name__ == "__main__":
    ok, message = check_connection()
    print("MongoDB connected." if ok else f"WARNING: cannot reach MongoDB: {message}")
    app.run()