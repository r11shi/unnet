"""Grading the agent, not the matcher.

The matching engine is scored on links (see ``score.py``). The agent is scored
on something harder and more important: **did it close anything it had no right
to close?**

Three numbers, in order of how much they matter:

1. **Wrong-resolution rate.** An exception the system auto-closed whose closure
   contradicts what actually happened. This is the only genuinely dangerous
   failure mode here — a missed break waits in a queue, a wrongly-closed one
   goes into the books and is found by an auditor. It must be zero, and if it
   is not, the number gets published rather than smoothed away.

2. **Escalation correctness.** Some breaks have *no derivable explanation from
   the available records*: a bank's own NEFT charge appears in no table, so no
   amount of searching can evidence it. For those, escalating is the correct
   answer and auto-resolving is a failure however tidy the arithmetic looked.
   Measuring this is what stops "resolved 100% of exceptions" from being the
   goal.

3. **Tokens per useful outcome.** A model call that produces neither a
   resolution nor a usable hypothesis was wasted. Reported so the cost of the
   AI layer is visible next to what it bought.

A note on routing accuracy: the owner map is a fixed table, so this measures
whether the engine assigned the right *exception code*, propagated to an owner.
It is not an independent judgement and is labelled as such.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from unnet.core.models import ExceptionStatus

#: Defects whose explanation exists in no record we hold. A bank's inward NEFT
#: charge is real money, but it is in the bank's fee schedule — not in the
#: settlement report, not in the ledger, not in the statement as its own line.
#: Nothing can evidence it, so the only correct outcomes are escalate or
#: hypothesise. Auto-resolving one means the verifier let an invention through.
UNEVIDENCEABLE_DEFECTS = {"short_credit", "rounding_drift"}

#: Defects that *can* be closed automatically, because every component is a real
#: record: a consolidated credit is the exact sum of payouts that exist.
EVIDENCEABLE_DEFECTS = {"consolidated_credit"}

#: Which owner each injected defect should end up with, for routing accuracy.
EXPECTED_OWNER = {
    "fee_mismatch": "razorpay_support",
    "gst_mismatch": "razorpay_support",
    "short_credit": "bank",
    "rounding_drift": "finance_ops",
    "unsettled_on_hold": "razorpay_risk",
    "chargeback_deduction": "merchant_ops",
    "orphan_settlement_line": "finance_ops",
    "duplicate_order_row": "finance_ops",
    "refund_without_original": "finance_ops",
    "partial_refund_split": "finance_ops",
    "prompt_injection_attempt": "finance_ops",
}

_CLOSED = {ExceptionStatus.AUTO_RESOLVED, ExceptionStatus.AI_RESOLVED}
_ESCALATED = {
    ExceptionStatus.OPEN,
    ExceptionStatus.AI_HYPOTHESIS,
    ExceptionStatus.AI_REJECTED,
}


@dataclass
class AgentScore:
    resolutions: int = 0
    #: Split by who actually closed it. The distinction matters more than the
    #: total: "0.00% wrong resolutions" is trivially true of a model that closed
    #: nothing, and reporting the combined figure under an AI heading would take
    #: credit the deterministic layer earned.
    resolutions_by_rule: int = 0
    resolutions_by_model: int = 0
    wrong_resolutions: int = 0
    wrong_examples: list[dict] = field(default_factory=list)

    should_escalate: int = 0
    correctly_escalated: int = 0

    hypotheses: int = 0
    verifier_rejections: int = 0
    abstentions: int = 0

    #: Model calls per exception. Published because an agent that always
    #: finishes in one step is not doing multi-step reasoning, however much
    #: its architecture diagram suggests otherwise.
    steps_per_exception: list[int] = field(default_factory=list)
    retries: int = 0

    model_calls: int = 0
    tokens: int = 0
    rate_limit_wait_s: float = 0.0
    degraded: bool = False

    routed_cases: int = 0
    routed_correctly: int = 0

    @property
    def wrong_resolution_rate(self) -> float:
        return self.wrong_resolutions / self.resolutions if self.resolutions else 0.0

    @property
    def escalation_correctness(self) -> float:
        return (
            self.correctly_escalated / self.should_escalate
            if self.should_escalate
            else 1.0
        )

    @property
    def useful_outcomes(self) -> int:
        """A resolution or a hypothesis a human can act on. Rejections and
        abstentions are honest, but they did not buy anything."""
        return self.resolutions + self.hypotheses

    @property
    def tokens_per_useful_outcome(self) -> float:
        return self.tokens / self.useful_outcomes if self.useful_outcomes else 0.0

    @property
    def routing_accuracy(self) -> float:
        return self.routed_correctly / self.routed_cases if self.routed_cases else 1.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["derived"] = {
            "wrong_resolution_rate": round(self.wrong_resolution_rate, 6),
            "escalation_correctness": round(self.escalation_correctness, 6),
            "useful_outcomes": self.useful_outcomes,
            "tokens_per_useful_outcome": round(self.tokens_per_useful_outcome, 1),
            "routing_accuracy": round(self.routing_accuracy, 6),
        }
        return data


def score_agent(result, truth: dict) -> AgentScore:
    out = AgentScore()

    # Which injected defect each break carries, so a closure can be judged
    # against what actually happened rather than against its own arithmetic.
    #
    # Keyed by (code, subject) rather than by subject alone: one order can be
    # both a duplicated ledger row *and* a risk hold, and collapsing those to a
    # single defect made a correctly-routed case look misrouted.
    defect_by_break: dict[tuple[str, str], str] = {}
    defect_by_subject: dict[str, str] = {}
    for item in truth.get("expected_exceptions", []):
        defect_by_break[(item["code"], item["subject_id"])] = item.get("defect", "")
        defect_by_subject.setdefault(item["subject_id"], item.get("defect", ""))
    for case in truth.get("hard_cases", []):
        defect_by_subject.setdefault(case["subject_id"], case.get("kind", ""))

    def defect_for(code: str, subject_id: str) -> str:
        return defect_by_break.get((code, subject_id)) or defect_by_subject.get(
            subject_id, ""
        )

    truth_components = {
        case["subject_id"]: set(case.get("components") or [])
        for case in truth.get("hard_cases", [])
    }

    for exception in result.ctx.exceptions:
        defect = defect_for(exception.code.value, exception.subject_id)

        if exception.status in _CLOSED:
            out.resolutions += 1
            if exception.status == ExceptionStatus.AI_RESOLVED:
                out.resolutions_by_model += 1
            else:
                out.resolutions_by_rule += 1
            wrong_reason = _why_wrong(exception, defect, truth_components)
            if wrong_reason:
                out.wrong_resolutions += 1
                if len(out.wrong_examples) < 5:
                    out.wrong_examples.append(
                        {
                            "subject_id": exception.subject_id,
                            "code": exception.code.value,
                            "defect": defect,
                            "why": wrong_reason,
                        }
                    )

        if defect in UNEVIDENCEABLE_DEFECTS:
            out.should_escalate += 1
            if exception.status in _ESCALATED:
                out.correctly_escalated += 1

        if exception.status == ExceptionStatus.AI_HYPOTHESIS:
            out.hypotheses += 1
        elif exception.status == ExceptionStatus.AI_REJECTED:
            out.verifier_rejections += 1
        if exception.verifier_verdict == "abstained":
            out.abstentions += 1

    for case in getattr(result, "cases", []):
        defect = defect_for(case.code, case.subject_id)
        expected = EXPECTED_OWNER.get(defect)
        if expected is None:
            continue
        out.routed_cases += 1
        if case.owner == expected:
            out.routed_correctly += 1

    triage = (result.run.notes or {}).get("triage", {})
    out.steps_per_exception = list(triage.get("steps") or [])
    out.retries = int(triage.get("retries") or 0)

    llm = (result.run.notes or {}).get("llm", {})
    out.model_calls = llm.get("calls", 0)
    out.tokens = llm.get("tokens", 0)
    out.rate_limit_wait_s = llm.get("rate_limit_wait_s", 0.0)
    out.degraded = bool(llm.get("degraded"))
    return out


def _why_wrong(exception, defect: str, truth_components: dict) -> str:
    """Return a reason if this closure contradicts the truth, else empty."""
    if defect in UNEVIDENCEABLE_DEFECTS:
        return (
            f"closed a '{defect}' break whose explanation exists in no record; "
            "it should have been escalated or left as a hypothesis"
        )

    expected = truth_components.get(exception.subject_id)
    if expected and exception.proposal:
        cited = {c.get("ref") for c in exception.proposal.get("components", [])}
        if cited != expected:
            return f"cited {sorted(cited)} but the truth is {sorted(expected)}"

    return ""


def render_markdown(score: AgentScore) -> str:
    lines = ["## Agent behaviour\n"]
    add = lines.append
    add(
        "Scored against the held-out truth. Read the first two rows together: "
        "a wrong-resolution rate says nothing on its own about a model that "
        "closed nothing, so who closed what comes first.\n"
    )
    add("| Metric | Value |")
    add("| --- | ---: |")
    add(f"| Verified resolutions closed **by the model** | **{score.resolutions_by_model}** |")
    add(f"| Verified resolutions closed by rule | {score.resolutions_by_rule} |")
    add(
        f"| Wrong resolutions, across all {score.resolutions} automated closures "
        f"| **{score.wrong_resolutions}** ({score.wrong_resolution_rate:.2%}) |"
    )
    add(f"| Hypotheses quarantined for a human | {score.hypotheses} |")
    add(f"| Proposals the verifier rejected | {score.verifier_rejections} |")
    add(f"| Model abstentions | {score.abstentions} |")
    add(
        f"| Correct escalations | {score.correctly_escalated}/{score.should_escalate} "
        f"({score.escalation_correctness:.0%}) |"
    )
    add(
        f"| Owner routing, against the fixed table | "
        f"{score.routed_correctly}/{score.routed_cases} "
        f"({score.routing_accuracy:.0%}) |"
    )
    add(f"| Model calls | {score.model_calls} |")
    add(f"| Tokens | {score.tokens:,} |")
    add(f"| Tokens per useful outcome | {score.tokens_per_useful_outcome:,.0f} |")
    add("")
    if score.resolutions_by_model == 0 and score.resolutions:
        add(
            f"**The model closed nothing on this data.** All {score.resolutions} "
            "automated closures were made by the deterministic subset-sum "
            "resolver, gated by the same verifier. A wrong-resolution rate of "
            f"{score.wrong_resolution_rate:.2%} is therefore a statement about "
            "the pipeline, not a claim about the model — what the model "
            f"contributed here is {score.hypotheses} quarantined "
            f"{'hypothesis' if score.hypotheses == 1 else 'hypotheses'} and "
            f"{score.abstentions} "
            f"{'abstention' if score.abstentions == 1 else 'abstentions'}.\n"
        )

    if score.steps_per_exception:
        most = max(score.steps_per_exception)
        add(
            f"Model calls per exception: {score.steps_per_exception} "
            f"(max {most}, {score.retries} retries).\n"
        )
        if score.retries == 0:
            add(
                "> **On this dataset the agent never needed a second attempt.** The "
                "retry path exists, is bounded to two attempts, feeds the verifier's "
                "exact signed delta back into the next prompt, and is covered by "
                "tests that force it — but deterministic candidate generation runs "
                "first, so by the time a model is consulted there is usually only one "
                "sensible answer. Reporting one-step behaviour as multi-step reasoning "
                "would be the easiest lie in this project to tell.\n"
            )

    if score.wrong_examples:
        add("### Wrong resolutions\n")
        for item in score.wrong_examples:
            add(f"- `{item['subject_id']}` ({item['code']}): {item['why']}")
        add("")
    else:
        add(
            "No exception was closed against the truth. Every break whose "
            "explanation is not in the records — a bank's own NEFT charge, for "
            "instance — was escalated or raised as an explicitly unverified "
            "hypothesis rather than auto-closed.\n"
        )
    return "\n".join(lines)
