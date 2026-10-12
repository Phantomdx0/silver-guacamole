#!/usr/bin/env python3
"""
Decision Impact web app (back end).

Serves the page in templates/index.html and a small JSON API.
It uses the rules in decision_impact.py.

Run:  python3 app.py   then open the port 5000 link.
"""

import re
import secrets

from flask import Flask, jsonify, render_template, request

import decision_impact as di

app = Flask(__name__)
di.load_data()


def fail(message, code=400):
    return jsonify({"error": message}), code


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def body():
    return request.get_json(silent=True) or {}


def state_json():
    r = di.last_result
    people = di.STATE["people"]
    over = sum(1 for p in people for d in range(1, 6)
               if di.day_load(p, d) > people[p]["dailyCapacity"])
    return {
        "days": di.DAYS,
        "rooms": di.ROOMS,
        "people": [{"id": pid, **p, "tasks": di.task_count(pid)} for pid, p in people.items()],
        "tasks": [{"id": tid, **t} for tid, t in di.STATE["tasks"].items()],
        "activity": di.STATE["activity"][:15],
        "ripples": len(r["levels"]) if r else 0,
        "over": over,
    }


def analysis_json(r):
    after = r["after"]
    levels = []
    for tid, info in r["levels"].items():
        parent = info["parent"]
        levels.append({
            "id": tid,
            "name": after[tid]["name"],
            "level": info["level"],
            "reason": info["reason"],
            "via": after[parent]["name"] if parent in after else after[r["changed_id"]]["name"],
        })
    return {
        "summary": di.summary_text(r),
        "notes": r["notes"],
        "levels": levels,
        "mitigations": [m["text"] for m in r["mitigations"]],
        "canApply": bool(r["mitigations"]),
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def get_state():
    return jsonify(state_json())


@app.post("/api/person")
def add_person():
    d = body()
    name = str(d.get("name", "")).strip()[:30]
    cap = to_float(d.get("dailyCapacity"))
    user = re.sub(r"^@", "", str(d.get("username", "")).strip().lower()) \
        or re.sub(r"[^a-z0-9]", "", name.lower())

    if not name:
        return fail("Enter a name.")
    if cap is None or not 1 <= cap <= 12:
        return fail("Hours must be 1 to 12.")
    if not re.fullmatch(r"[a-z0-9_.]{3,20}", user):
        return fail("Username: 3 to 20 letters, numbers, _ or .")
    if any(p["username"] == user for p in di.STATE["people"].values()):
        return fail(f"@{user} is already taken.")

    di.STATE["people"]["p" + secrets.token_hex(4)] = {"name": name, "username": user, "dailyCapacity": cap}
    di.log_activity(f"Added team member: {name}")
    return jsonify(state_json())


@app.delete("/api/person/<pid>")
def remove_person(pid):
    people = di.STATE["people"]
    if pid not in people:
        return fail("Team member not found.", 404)
    if di.task_count(pid):
        return fail(f"{people[pid]['name']} still has tasks. Delete or move them first.")
    name = people.pop(pid)["name"]
    di.log_activity(f"Removed team member: {name}")
    return jsonify(state_json())


@app.post("/api/task")
def add_task():
    d = body()
    name = str(d.get("name", "")).strip()[:40]
    owner = d.get("owner")
    day = to_float(d.get("day"))
    start = to_float(d.get("start"))
    duration = to_float(d.get("duration"))
    room = d.get("room") or None
    dep = d.get("dep") or None

    if not name:
        return fail("Give the task a name.")
    if owner not in di.STATE["people"]:
        return fail("Add a team member first.")
    if day is None or not 1 <= day <= 5:
        return fail("Pick a day.")
    if start is None or not 8 <= start <= 16:
        return fail("Start must be 8 to 16.")
    if duration is None or not 0.5 <= duration <= 8:
        return fail("Length must be 0.5 to 8 hours.")
    if start + duration > 17:
        return fail("The task must end by 5 PM.")
    if room not in (None, "roomA", "roomB"):
        return fail("Unknown room.")

    tid = "t" + secrets.token_hex(4)
    di.STATE["tasks"][tid] = {
        "name": name, "day": int(day), "start": start, "duration": duration,
        "owner": owner, "room": room,
        "deps": [dep] if dep in di.STATE["tasks"] else [],
    }
    di.clear_analysis()
    di.log_activity(f"Added task: {name}")
    return jsonify(state_json())


@app.delete("/api/task/<tid>")
def delete_task(tid):
    tasks = di.STATE["tasks"]
    if tid not in tasks:
        return fail("Task not found.", 404)
    name = tasks.pop(tid)["name"]
    for t in tasks.values():
        t["deps"] = [d for d in t["deps"] if d != tid]
    di.clear_analysis()
    di.log_activity(f"Deleted task: {name}")
    return jsonify(state_json())


@app.post("/api/reset")
def reset():
    di.reset_plan()
    return jsonify(state_json())


@app.post("/api/analyze")
def analyze_change():
    d = body()
    tid = d.get("taskId")
    kind = d.get("kind")
    raw = d.get("value")

    if tid not in di.STATE["tasks"]:
        return fail("Pick a task first.")

    if kind == "date":
        value = to_float(raw)
        if value is None or not 1 <= value <= 5:
            return fail("Pick a day.")
        value = int(value)
    elif kind == "time":
        value = to_float(raw)
        if value is None or not 8 <= value <= 16:
            return fail("Start hour must be between 8 and 16.")
    elif kind == "resource":
        if raw not in di.STATE["people"]:
            return fail("Pick an owner.")
        value = raw
    elif kind == "capacity":
        value = to_float(raw)
        if value is None or not 1 <= value <= 12:
            return fail("Capacity must be between 1 and 12 hours.")
    else:
        return fail("Unknown change type.")

    di.last_result = di.analyze(tid, kind, value)
    return jsonify(analysis_json(di.last_result))


@app.post("/api/apply")
def apply_change():
    r = di.last_result
    if not r:
        return fail("Run an analysis first.")
    if not r["mitigations"]:
        return fail("There is no fix to apply.")
    name = r["after"][r["changed_id"]]["name"]
    di.apply_result(r, di.best_fix(r))
    di.log_activity(f"Applied change to {name}")
    return jsonify(state_json())


@app.post("/api/ask")
def ask_plan():
    question = str(body().get("question", ""))
    return jsonify({"answer": di.mochi_answer(question)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)