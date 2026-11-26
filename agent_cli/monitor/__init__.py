"""조건 → 보고 감시 (docs/monitor/DESIGN.md).

`MonitorRegistry` 가 스레드를 소유하고, 보고는 **메일박스**에 쌓여 턴 경계에서
`tool="monitor"` 관찰 레코드로 배달된다 (`AgentRegistry` 와 같은 모양).
"""

from agent_cli.monitor.conditions import build

__all__ = ["build"]
