"""JSON-file storage for TaskBook tasks.

Pair-2 P2 target: the ad-hoc print() logging below is asked to become
structured logging (level + timestamp).
"""

import json
import os

DB_FILE = "tasks.json"


def load_tasks():
    if not os.path.exists(DB_FILE):
        print("DEBUG: no db file yet, starting empty")
        return []
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            tasks = json.load(f)
        print("DEBUG: loaded " + str(len(tasks)) + " tasks")
        return tasks
    except json.JSONDecodeError:
        print("error: cannot read tasks.json, starting empty")
        return []


def save_tasks(tasks):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)
    print("DEBUG: saved " + str(len(tasks)) + " tasks")


def next_id(tasks):
    return max((t["id"] for t in tasks), default=0) + 1
