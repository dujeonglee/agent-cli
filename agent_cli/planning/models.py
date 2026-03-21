"""Plan data model with serialization support."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PlanStep:
    id: int
    description: str
    status: str = "pending"  # pending | in_progress | done | failed | skipped
    result: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PlanStep:
        return cls(
            id=data["id"],
            description=data["description"],
            status=data.get("status", "pending"),
            result=data.get("result"),
        )


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    current_step: int = 0

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "current_step": self.current_step,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Plan:
        return cls(
            goal=data["goal"],
            steps=[PlanStep.from_dict(s) for s in data.get("steps", [])],
            current_step=data.get("current_step", 0),
        )

    def save(self, path: str | Path) -> None:
        """Save plan to JSON file."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path) -> Plan:
        """Load plan from JSON file."""
        try:
            text = Path(path).read_text(encoding="utf-8")
            data = json.loads(text)
            if "goal" not in data:
                raise ValueError("Missing 'goal' field in plan file")
            return cls.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise RuntimeError(f"Invalid plan file {path}: {e}") from e
