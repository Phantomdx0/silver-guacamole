#!/usr/bin/env python3
"""
Decision Impact engine.

app.py (web) imports this file for the rules. Run this file directly for the
terminal version:  python3 decision_impact.py
Needs Python 3.8 or newer.
"""

import copy
import json
import re
import secrets
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

DATA_FILE = Path.home() / ".decision_impact" / "data.json"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
ROOMS = {"roomA": "Room A", "roomB": "Room B"}
CHANGES = {"date": "Day", "time": "Start time", "resource": "Owner", "capacity": "Daily capacity"}
HELP = ("I can answer: what's at risk, suggest a fix, who is overloaded, "
        "the critical path, explain the plan, or any task name.")

STATE = {
    "settings": {"dayStart": 8, "dayEnd": 17, "defaultView": "all"},
    "people": {},    # id -> {name, username, dailyCapacity}
    "tasks": {},     # id -> {name, day, start, duration, owner, room, deps}
    "activity": [],  # newest first
}
last_result = None   # result of the last impact analysis


# ---------- Storage ----------

def load_data():
    try:
        saved = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for key in STATE:
        STATE[key] = saved.get(key, STATE[key])


def save_data():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(STATE, indent=2), encoding="utf-8")


def log_activity(text):
    STATE["activity"].insert(0, {"t": datetime.now().strftime("%Y-%m-%d %H:%M"), "who": "You", "text": text})
    del STATE["activity"][60:]
    save_data()


def clear_analysis():
    global last_result
    last_result = None


def reset_plan():
    STATE["tasks"] = {}
    clear_analysis()
    log_activity("Cleared all tasks")


# ---------- Helpers ----------

def num(x):
    x = float(x)
    return int(x) if x.is_integer() else x


def end(t):
    return t["start"] + t["duration"]


def clock(h):
    hh = int(h)
    mm = round((h - hh) * 60)
    return f"{(hh + 11) % 12 + 1}:{mm:02d} {'PM' if hh >= 12 else 'AM'}"


def slot(t):
    return f"{DAYS[t['day'] - 1]} {clock(t['start'])} to {clock(end(t))}"


def overlaps(a, b):
    return a["day"] == b["day"] and a["start"] < end(b) and b["start"] < end(a)


def deps_ok(task, state):
    for d in task["deps"]:
        p = state[d]
        if not (p["day"] < task["day"] or (p["day"] == task["day"] and end(p) <= task["start"])):
            return False
    return True


def day_load(pid, day, state=None):
    state = STATE["tasks"] if state is None else state
    return sum(t["duration"] for t in state.values() if t["owner"] == pid and t["day"] == day)


def task_count(pid):
    return sum(t["owner"] == pid for t in STATE["tasks"].values())


def find_task(q):
    tasks = STATE["tasks"]
    for tid, t in tasks.items():
        if t["name"].lower() in q:
            return tid
    for tid, t in tasks.items():
        if any(len(w) > 3 and w in q for w in t["name"].lower().split(" ")):
            return tid
    return None


# ---------- Impact engine ----------

def analyze(cid, kind, value):
    people = STATE["people"]
    before = STATE["tasks"]
    after = copy.deepcopy(before)
    ch = after[cid]
    caps = {k: p["dailyCapacity"] for k, p in people.items()}
    notes = []

    if kind == "date":
        ch["day"] = int(value)
        notes.append(f"{ch['name']} moves from {DAYS[before[cid]['day'] - 1]} to {DAYS[ch['day'] - 1]}.")
    elif kind == "time":
        ch["start"] = float(value)
        notes.append(f"{ch['name']} starts at {clock(ch['start'])} instead of {clock(before[cid]['start'])}.")
    elif kind == "resource":
        notes.append(f"{ch['name']} is now owned by {people[value]['name']}.")
        ch["owner"] = value
    elif kind == "capacity":
        caps[ch["owner"]] = float(value)
        notes.append(f"{people[ch['owner']]['name']}'s daily capacity changes to {num(value)} hours.")

    levels, queue = {}, deque()

    def add(tid, lvl, reason, parent):
        if tid != cid and (tid not in levels or levels[tid]["level"] > lvl):
            levels[tid] = {"level": lvl, "reason": reason, "parent": parent}
            queue.append(tid)

    # Level 1: direct impacts
    for tid, o in after.items():
        if tid == cid:
            continue
        if ch["owner"] == o["owner"] and overlaps(ch, o):
            add(tid, 1, f"{people[o['owner']]['name']} is double-booked: '{ch['name']}' and '{o['name']}' overlap on {DAYS[o['day'] - 1]}.", cid)
        if ch["room"] and ch["room"] == o["room"] and overlaps(ch, o):
            add(tid, 1, f"{ROOMS[o['room']]} is double-booked by '{ch['name']}' and '{o['name']}'.", cid)
        if cid in o["deps"] and (ch["day"] > o["day"] or (ch["day"] == o["day"] and end(ch) > o["start"])):
            add(tid, 1, f"'{o['name']}' depends on '{ch['name']}', which now finishes after '{o['name']}' starts.", cid)
        if tid in ch["deps"] and (o["day"] > ch["day"] or (o["day"] == ch["day"] and end(o) > ch["start"])):
            add(tid, 1, f"'{ch['name']}' depends on '{o['name']}', which now ends after '{ch['name']}' begins.", cid)

    # Levels 2 and 3: follow the dependency chain
    guard = 0
    while queue and guard < 100:
        guard += 1
        src = queue.popleft()
        if levels[src]["level"] >= 3:
            continue
        for tid, c in after.items():
            if src in c["deps"]:
                add(tid, levels[src]["level"] + 1,
                    f"'{c['name']}' waits on '{after[src]['name']}', which is now delayed. "
                    f"'{c['name']}' slides later as a result.", src)

    # Capacity check with the proposed capacities
    for pid, p in people.items():
        cap = caps[pid]
        for day in range(1, 6):
            owned = [i for i in after if after[i]["owner"] == pid and after[i]["day"] == day]
            hours = sum(after[i]["duration"] for i in owned)
            if hours > cap:
                for i in owned:
                    add(i, 1, f"{p['name']} is over capacity on {DAYS[day - 1]} "
                              f"({num(hours)}h planned, {num(cap)}h available).", cid)
                if cid not in owned and not any("over capacity" in n for n in notes):
                    notes.append(f"{p['name']} is over capacity on {DAYS[day - 1]}.")

    return {"after": after, "changed_id": cid, "kind": kind, "value": value, "caps": caps,
            "notes": notes, "levels": levels, "mitigations": mitigations(cid, after, levels, caps)}


def find_slot(task, state, ignore, caps):
    cap = caps.get(task["owner"], 0)
    for day in range(task["day"], 6):
        start = task["start"] if day == task["day"] else 8
        while start + task["duration"] <= 17:
            cand = {**task, "day": day, "start": start}
            clash = any(
                i != ignore and (o["owner"] == task["owner"] or (task["room"] and o["room"] == task["room"]))
                and overlaps(cand, o)
                for i, o in state.items()
            )
            hours = sum(state[i]["duration"] for i in state
                        if i != ignore and state[i]["owner"] == task["owner"] and state[i]["day"] == day)
            if not clash and deps_ok(cand, state) and hours + task["duration"] <= cap:
                return {"day": day, "start": start}
            start += 0.5
    return None


def mitigations(cid, after, levels, caps):
    out = []
    for tid, info in levels.items():
        r = info["reason"]
        if info["level"] == 1 and ("over capacity" in r or "double-booked" in r):
            t = after[tid]
            s = find_slot(t, after, tid, caps)
            if s:
                out.append({"taskId": tid, "slot": s, "text":
                            f"Move '{t['name']}' from {slot(t)} to {DAYS[s['day'] - 1]} {clock(s['start'])} "
                            f"to {clock(s['start'] + t['duration'])}. This clears the conflict and keeps "
                            f"its owner within capacity."})
    for tid, info in levels.items():
        r = info["reason"]
        if ("depends on" in r or "waits on" in r) and not any(m["taskId"] == tid for m in out):
            t = after[tid]
            s = find_slot(t, after, tid, caps)
            if s:
                out.append({"taskId": tid, "slot": s, "text":
                            f"Re-slot '{t['name']}' to {DAYS[s['day'] - 1]} {clock(s['start'])} so it starts "
                            f"after its prerequisites finish, with no new conflicts."})
    if not out and levels:
        out.append({"taskId": cid, "slot": None, "text":
                    f"No clean reschedule was found. Consider keeping '{after[cid]['name']}' in its "
                    f"original slot, or splitting it across two days."})
    return out


def best_fix(r):
    m = next((m for m in r["mitigations"] if m["slot"]), None)
    if not m:
        return None
    return {"taskId": m["taskId"], "day": m["slot"]["day"], "start": m["slot"]["start"]}


def apply_result(r, fix):
    STATE["tasks"] = copy.deepcopy(r["after"])
    for k, p in STATE["people"].items():
        p["dailyCapacity"] = r["caps"].get(k, p["dailyCapacity"])
    if fix:
        t = STATE["tasks"][fix["taskId"]]
        t["day"], t["start"] = fix["day"], fix["start"]
    clear_analysis()
    save_data()


# ---------- Text answers ----------

def summary_text(r):
    n = len(r["levels"])
    if n == 0:
        return f"Clean result. Moving {r['after'][r['changed_id']]['name']} causes no conflicts."
    counts = {1: 0, 2: 0, 3: 0}
    for info in r["levels"].values():
        counts[info["level"]] += 1
    first = next((i["reason"] for i in r["levels"].values() if i["level"] == 1), "")
    fix = r["mitigations"][0]["text"] if r["mitigations"] else "I could not find a clean fix."
    plural = "" if n == 1 else "s"
    return (f"{n} ripple{plural}: {counts[1]} direct, {counts[2]} second-order, {counts[3]} third-order.\n"
            f"Biggest concern: {first}\nSuggested fix: {fix}")


def impact_text(r):
    if not r["levels"]:
        return "The last analysis found no ripples."
    lines = [f"L{i['level']} {r['after'][tid]['name']}: {i['reason']}" for tid, i in r["levels"].items()]
    return f"{len(lines)} ripples from the last analysis:\n" + "\n".join(lines)


def fix_text():
    if last_result and last_result["mitigations"]:
        return "\n".join(m["text"] for m in last_result["mitigations"]) + \
               "\nPropose the change again to apply the fix."
    return "Run an analysis first. I suggest fixes for the ripples it finds."


def load_text():
    lines = []
    for pid, p in STATE["people"].items():
        for d in range(1, 6):
            load = day_load(pid, d)
            if load:
                flag = "  (OVER)" if load > p["dailyCapacity"] else ""
                lines.append(f"{p['name']} on {DAYS[d - 1]}: {num(load)}h of {num(p['dailyCapacity'])}h{flag}")
    header = "Some people are overloaded:\n" if "(OVER)" in "".join(lines) else "Nobody is over capacity:\n"
    return header + "\n".join(lines)


def critical_text():
    tasks = STATE["tasks"]
    if not tasks:
        return "No tasks yet."

    def key(i):
        return tasks[i]["day"] * 100 + end(tasks[i])

    latest = max(tasks, key=key)
    chain, cur = [latest], latest
    while tasks[cur]["deps"]:
        cur = max(tasks[cur]["deps"], key=key)
        chain.insert(0, cur)
    names = " → ".join(tasks[i]["name"] for i in chain)
    return f"Longest chain: {names}.\nIt finishes {slot(tasks[latest])}."


def task_info(tid):
    tasks = STATE["tasks"]
    t = tasks[tid]
    owner = STATE["people"][t["owner"]]
    s = f"{t['name']}: {slot(t)}, owned by {owner['name']}."
    s += f"\n{owner['name']} has {num(day_load(t['owner'], t['day']))}h that day, out of {num(owner['dailyCapacity'])}h."
    if t["deps"]:
        s += "\nIt waits on: " + ", ".join(tasks[d]["name"] for d in t["deps"]) + "."
    dependents = [o["name"] for o in tasks.values() if tid in o["deps"]]
    s += ("\nIt unlocks: " + ", ".join(dependents) + ".") if dependents else "\nNothing depends on it."
    return s


def overview_text():
    return (f"The plan has {len(STATE['tasks'])} tasks, {len(STATE['people'])} people "
            f"and {len(ROOMS)} rooms.\n" + load_text())


def mochi_answer(raw):
    q = raw.lower().strip()
    if not q:
        return "Ask me about the plan. Type help to see what I can answer."
    if q.startswith("help"):
        return HELP
    hit = find_task(q)
    if hit and not re.search(r"risk|fix|suggest", q):
        return task_info(hit)
    if re.search(r"risk|ripple|impact|affect", q):
        return impact_text(last_result) if last_result else "Propose a change first."
    if re.search(r"fix|suggest|solve|improve", q):
        return fix_text()
    if re.search(r"busy|overload|capacity|workload|who", q):
        return load_text()
    if re.search(r"critical|longest|bottleneck|deadline", q):
        return critical_text()
    if re.search(r"overview|explain|summary|plan|status", q):
        return overview_text()
    return "I'm not sure about that yet. Type help to see what I can answer."


# ---------- Terminal app ----------

def ask(prompt, default=""):
    hint = f" [{default}]" if default != "" else ""
    try:
        text = input(f"{prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return text or str(default)


def pick(prompt, options):
    for i, label in enumerate(options, 1):
        print(f"  {i}) {label}")
    raw = ask(prompt)
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return int(raw) - 1
    print("Not one of the options.")
    return None


def parse_num(text):
    try:
        return float(text)
    except ValueError:
        return None


def show_week():
    for d, day in enumerate(DAYS, 1):
        print(f"\n{day}")
        items = sorted((t for t in STATE["tasks"].values() if t["day"] == d), key=lambda t: t["start"])
        if not items:
            print("  (nothing scheduled)")
        for t in items:
            who = STATE["people"].get(t["owner"], {}).get("name", "")
            room = ROOMS.get(t["room"], "")
            extra = f" · {room}" if room else ""
            print(f"  {clock(t['start'])} to {clock(end(t))}  {t['name']}  ({who}{extra})")


def show_result(r):
    print("\n" + summary_text(r))
    for n in r["notes"]:
        print(" ", n)
    for tid, info in r["levels"].items():
        print(f"  L{info['level']} {r['after'][tid]['name']}: {info['reason']}")
    for m in r["mitigations"]:
        print("Fix:", m["text"])


def propose():
    global last_result
    ids = list(STATE["tasks"])
    if not ids:
        print("Add a task first.")
        return
    k = pick("Which task", [STATE["tasks"][i]["name"] for i in ids])
    if k is None:
        return
    tid = ids[k]

    kinds = list(CHANGES)
    k = pick("What to change", [CHANGES[x] for x in kinds])
    if k is None:
        return
    kind = kinds[k]

    value = None
    if kind == "date":
        k = pick("New day", DAYS)
        value = None if k is None else k + 1
    elif kind == "resource":
        pids = list(STATE["people"])
        if not pids:
            print("Add a team member first.")
            return
        k = pick("New owner", [STATE["people"][p]["name"] for p in pids])
        value = None if k is None else pids[k]
    else:
        lo, hi = (8, 16) if kind == "time" else (1, 12)
        value = parse_num(ask(f"New {CHANGES[kind].lower()} ({lo} to {hi})"))
        if value is None or not lo <= value <= hi:
            print(f"Enter a number from {lo} to {hi}.")
            return
    if value is None:
        return

    last_result = analyze(tid, kind, value)
    show_result(last_result)
    if last_result["mitigations"] and ask("Apply the change and suggested fix? (y/n)", "n").lower() == "y":
        name = last_result["after"][tid]["name"]
        apply_result(last_result, best_fix(last_result))
        log_activity(f"Applied change to {name}")
        print("Applied.")


def add_task():
    people = STATE["people"]
    if not people:
        print("Add a team member first.")
        return
    name = ask("Task name")[:40]
    pids = list(people)
    k = pick("Owner", [people[p]["name"] for p in pids])
    d = pick("Day", DAYS)
    if not name or k is None or d is None:
        print("Cancelled.")
        return
    start = parse_num(ask("Start hour (8 to 16)", "9"))
    dur = parse_num(ask("Length in hours (0.5 to 8)", "1"))
    if start is None or dur is None or not (8 <= start <= 16 and 0.5 <= dur <= 8 and start + dur <= 17):
        print("Start must be 8 to 16, length 0.5 to 8, and the task must end by 5 PM.")
        return
    room = pick("Room", ["No room", "Room A", "Room B"])
    rooms = [None, "roomA", "roomB"]
    ids = list(STATE["tasks"])
    w = pick("Waits on", ["Nothing"] + [STATE["tasks"][i]["name"] for i in ids])
    tid = "t" + secrets.token_hex(4)
    STATE["tasks"][tid] = {
        "name": name, "day": d + 1, "start": start, "duration": dur,
        "owner": pids[k], "room": rooms[room] if room else None,
        "deps": [ids[w - 1]] if w else [],
    }
    clear_analysis()
    log_activity(f"Added task: {name}")
    print(f"Task added: {slot(STATE['tasks'][tid])}")


def add_person():
    name = ask("Name")[:30]
    cap = parse_num(ask("Hours a day (1 to 12)", "8"))
    if not name or cap is None or not 1 <= cap <= 12:
        print("Enter a name, and hours from 1 to 12.")
        return
    user = re.sub(r"^@", "", ask("Username (optional)").lower()) or re.sub(r"[^a-z0-9]", "", name.lower())
    if not re.fullmatch(r"[a-z0-9_.]{3,20}", user) or any(p["username"] == user for p in STATE["people"].values()):
        print("Username must be 3 to 20 letters, numbers, _ or ., and not already used.")
        return
    STATE["people"]["p" + secrets.token_hex(4)] = {"name": name, "username": user, "dailyCapacity": cap}
    log_activity(f"Added team member: {name}")


def remove_person():
    ids = list(STATE["people"])
    if not ids:
        print("No team members yet.")
        return
    k = pick("Remove who", [STATE["people"][p]["name"] for p in ids])
    if k is None:
        return
    pid = ids[k]
    if task_count(pid):
        print("They still have tasks. Delete or move them first.")
        return
    name = STATE["people"].pop(pid)["name"]
    log_activity(f"Removed team member: {name}")


def team():
    while True:
        print()
        if not STATE["people"]:
            print("No team members yet.")
        for pid, p in STATE["people"].items():
            print(f"- {p['name']} (@{p['username']}) · {num(p['dailyCapacity'])}h a day · {task_count(pid)} task(s)")
        k = pick("Choose", ["Add team member", "Remove team member", "Back"])
        if k == 0:
            add_person()
        elif k == 1:
            remove_person()
        elif k == 2:
            return


def assistant():
    print("Ask about the plan. Press Enter on an empty line to go back.")
    while (q := ask("You")):
        print(f"Mochi: {mochi_answer(q)}\n")


def main():
    load_data()
    print(f"Decision Impact · data file: {DATA_FILE}")
    actions = {
        "Week view": show_week,
        "Propose a change": propose,
        "Add a task": add_task,
        "Ask the assistant": assistant,
        "Team": team,
    }
    while True:
        ripples = len(last_result["levels"]) if last_result else 0
        print(f"\nTasks {len(STATE['tasks'])} · Team {len(STATE['people'])} · Ripples {ripples}")
        k = pick("Go to", list(actions) + ["Quit"])
        if k is None:
            continue
        if k == len(actions):
            break
        list(actions.values())[k]()
        save_data()
    print("Saved. Goodbye.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nGoodbye.")