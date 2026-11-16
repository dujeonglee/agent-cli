"""구현(`PostOffice`)과 참조 모델(`reference.Model`)을 같은 연산열로 나란히 모는 도우미.

연산은 ``(이름, 인자 dict)`` — 둘 다에 같은 뜻으로 보낸다. 매 걸음 구현의 `check_consistency()` 를 부르고,
거절 여부와 공개 관찰값(주소별 queued·taken id 순서, 줄 (rid, state, how), arrived·backlog·has_waking)을
비교한다.
"""

from __future__ import annotations

from agent_cli.post.office import PostOffice
from agent_cli.post.postbox import BoxError
from agent_cli.post.request_book import RowError
from tests.post.reference import Model, Refused


def as_dict(env) -> dict:
    return {
        "id": env.id,
        "kind": env.kind,
        "src": env.src,
        "dst": env.dst,
        "re": env.re,
        "closes_by": env.closes_by,
        "wakes": env.wakes,
        "persists": env.persists,
    }


def observe(office: PostOffice, addrs) -> dict:
    return {
        "boxes": {
            a: (
                [e.id for e in office.queued(a)],
                [e.id for e in office.taken(a)],
                office.arrived(a),
                office.backlog(a),
                office.has_waking(a),
            )
            for a in addrs
        },
        "rows": [(r.rid, r.state, r.how) for r in office.rows()],
    }


def _model_args(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if hasattr(v, "closes_by"):
            out[k] = as_dict(v)
        elif isinstance(v, dict):
            out[k] = {rk: as_dict(rv) for rk, rv in v.items()}
        else:
            out[k] = v
    return out


class Twin:
    """구현과 모델 한 쌍."""

    def __init__(self, addrs):
        self.office = PostOffice()
        self.model = Model()
        self.addrs = tuple(addrs)

    def apply(self, op: str, args: dict) -> tuple[bool, bool]:
        """연산 하나 — ``(구현이 거절했나, 모델이 거절했나)``."""
        impl_refused = model_refused = False
        self.last = None
        try:
            self.last = self._impl(op, args)
        except (BoxError, RowError):
            impl_refused = True
        try:
            getattr(self.model, op)(**_model_args(args))
        except Refused:
            model_refused = True
        return impl_refused, model_refused

    def _impl(self, op: str, args: dict) -> None:
        o = self.office
        if op == "close_box":
            return o.close_box(args["addr"])
        if op == "deliver":
            o.deliver(args["e"])
        elif op == "start_run":
            o.start_run(args["addr"])
        elif op == "absorb":
            o.absorb(args["addr"], human_first=args.get("human_first", False))
        elif op == "pay":
            o.pay(args["debtor"], args["rid"], args["reply"], run_id=args["run_id"])
        elif op == "pay_by_message":
            o.pay_by_message(
                args["debtor"], args["to"], args["env"], run_id=args["run_id"]
            )
        elif op == "complete":
            o.complete(args["addr"], args["ids"], run_id=args["run_id"])
        elif op == "end_run":
            o.end_run(
                args["addr"],
                run_id=args["run_id"],
                outcome=args["outcome"],
                replies=args["replies"],
            )
        elif op == "remove_queued":
            o.remove_queued(args["addr"], args["replies"])
        elif op == "drop":
            o.drop(args["rid"])
        elif op == "take_reply":
            return o.take_reply(args["addr"], args["rid"])
        elif op == "ask":
            o.ask(args["qid"], args["asker"], args["target"], "?")
        elif op == "answer":
            o.answer(args["qid"], args["env"])
        elif op == "forget_questions_of":
            o.forget_questions_of(args["asker"])
        else:  # pragma: no cover — 연산 목록과 맞춘다
            raise AssertionError(op)

    def observations(self) -> tuple[dict, dict]:
        return observe(self.office, self.addrs), self.model.observe(self.addrs)

    def step(self, op: str, args: dict) -> str:
        """연산 하나를 적용하고 검사한다 — 어긋나면 그 사정을 글로, 아니면 ""."""
        impl_refused, model_refused = self.apply(op, args)
        if impl_refused != model_refused:
            return f"{op}: implementation refused={impl_refused}, model refused={model_refused}"
        self.office.check_consistency()
        got, want = self.observations()
        if got != want:
            return f"{op}: observations differ\n  impl:  {got}\n  model: {want}"
        return ""
