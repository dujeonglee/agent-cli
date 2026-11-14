"""루프 엔진 패키지 — 서브모듈을 직접 import 한다(`agent_cli.loop.core` 의 `AgentLoop`,
`agent_cli.loop.run` 의 `run_loop` …). 여기서는 아무것도 재수출하지 않는다: 재수출하면
`loop.kind` 만 쓰는 세션 쪽(`runtime`·`subagent`·`skills.executor`)이 패키지 머리를 거쳐
엔진 전체(`loop.core`·`loop.dispatch` …)를 끌고 와 순환이 된다.
"""
