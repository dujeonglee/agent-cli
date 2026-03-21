"""Plan review and editing: Phase 2 of Planning Mode."""

from __future__ import annotations

from agent_cli.planning.models import Plan, PlanStep
from agent_cli.render import console, C, render_plan


def review_plan(plan: Plan, auto_approve: bool = False) -> str:
    """Interactive plan review.

    Returns: "approve" | "regenerate" | "cancel"
    """
    render_plan(plan)

    if auto_approve:
        console.print(f"[{C['accent']}]Auto-approved.[/]")
        return "approve"

    while True:
        try:
            choice = (
                console.input(
                    "  Approve? [bold][Y][/]es / [bold][E][/]dit / "
                    "[bold][R][/]egenerate / [bold][N][/]o: "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            return "cancel"

        if choice in ("y", "yes", ""):
            return "approve"
        elif choice in ("r", "regenerate"):
            return "regenerate"
        elif choice in ("n", "no"):
            return "cancel"
        elif choice in ("e", "edit"):
            edit_plan(plan)
            render_plan(plan)
            # After editing, ask again
        else:
            console.print(f"[{C['muted']}]Invalid choice. Try Y/E/R/N.[/]")


def edit_plan(plan: Plan) -> None:
    """Text-based plan editing.

    Commands:
      <number> <new text>  — replace step description
      d <number>           — delete step
      a <text>             — add step at end
      done                 — finish editing
    """
    console.print(f"\n[{C['accent']}]Edit mode:[/]")
    console.print(f"[{C['muted']}]  <num> <text>  — replace step[/]")
    console.print(f"[{C['muted']}]  d <num>       — delete step[/]")
    console.print(f"[{C['muted']}]  a <text>      — add step[/]")
    console.print(f"[{C['muted']}]  done          — finish editing[/]")
    console.print()

    while True:
        try:
            cmd = console.input(f"[{C['accent']}]edit>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not cmd or cmd.lower() == "done":
            break

        if cmd.lower().startswith("d "):
            # Delete step
            try:
                num = int(cmd[2:].strip())
                plan.steps = [s for s in plan.steps if s.id != num]
                _renumber(plan)
                console.print(f"[{C['muted']}]Deleted step {num}.[/]")
            except ValueError:
                console.print(f"[{C['error']}]Invalid step number.[/]")

        elif cmd.lower().startswith("a "):
            # Add step
            text = cmd[2:].strip()
            if text:
                new_id = len(plan.steps) + 1
                plan.steps.append(PlanStep(id=new_id, description=text))
                console.print(f"[{C['muted']}]Added step {new_id}.[/]")

        else:
            # Replace step: "<num> <new text>"
            parts = cmd.split(maxsplit=1)
            if len(parts) == 2:
                try:
                    num = int(parts[0])
                    text = parts[1]
                    for step in plan.steps:
                        if step.id == num:
                            step.description = text
                            console.print(f"[{C['muted']}]Updated step {num}.[/]")
                            break
                    else:
                        console.print(f"[{C['error']}]Step {num} not found.[/]")
                except ValueError:
                    console.print(f"[{C['error']}]Format: <num> <text>[/]")
            else:
                console.print(f"[{C['error']}]Unknown command.[/]")


def _renumber(plan: Plan) -> None:
    """Re-number steps sequentially after deletion."""
    for i, step in enumerate(plan.steps, 1):
        step.id = i
