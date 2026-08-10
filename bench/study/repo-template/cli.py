"""TaskBook — a tiny task list CLI.

Usage:
    python cli.py add "write report" --priority 2
    python cli.py list
    python cli.py done 3

Pair-2 P1 target: add a --json output option to ``list`` (keep the text
output as the default).
"""

import argparse
import sys

import config
import storage
from validate import ValidationError, validate_priority, validate_title


def cmd_add(args):
    tasks = storage.load_tasks()
    try:
        title = validate_title(args.title)
        priority = validate_priority(args.priority)
    except ValidationError as e:
        print("[TaskBook] " + str(e))
        return 1
    task = {
        "id": storage.next_id(tasks),
        "title": title,
        "priority": priority,
        "done": False,
    }
    tasks.append(task)
    storage.save_tasks(tasks)
    print("added #%d: %s (p%d)" % (task["id"], task["title"], task["priority"]))
    return 0


def cmd_list(args):
    tasks = storage.load_tasks()
    if not tasks:
        print("nothing to do!")
        return 0
    for t in sorted(tasks, key=lambda t: (t["done"], t["priority"])):
        mark = "x" if t["done"] else " "
        print("[%s] #%d p%d %s" % (mark, t["id"], t["priority"], t["title"]))
    return 0


def cmd_done(args):
    tasks = storage.load_tasks()
    for t in tasks:
        if t["id"] == args.task_id:
            t["done"] = True
            storage.save_tasks(tasks)
            print("done #%d" % args.task_id)
            return 0
    print("Error: no such task!!!")
    return 1


def main(argv=None):
    settings = config.load_config()
    _ = settings  # reserved for future options
    parser = argparse.ArgumentParser(prog="taskbook")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="add a task")
    p_add.add_argument("title")
    p_add.add_argument("--priority", default=3)
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="list tasks")
    p_list.set_defaults(func=cmd_list)

    p_done = sub.add_parser("done", help="mark a task done")
    p_done.add_argument("task_id", type=int)
    p_done.set_defaults(func=cmd_done)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
