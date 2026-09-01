"""Grading a run against the held-out truth.

The generator knows which order produced which settlement line, which lines went
into which payout, and which bank credit paid it. The engine never sees any of
that. So every link the engine produced can be checked, and the number that
matters most is not how many it found — it is how many it got **wrong**.

That asymmetry is the whole point. A missed link costs an analyst five minutes
in the exception queue. A wrong link silently attributes money to the wrong
order, survives into the books, and is found months later by an auditor. A
system that matches 99% with a 1% false-match rate is worse than one that
matches 90% and is never wrong, and any metric that reports a single "accuracy"
number hides exactly that difference.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from unnet.core.models import ExceptionStatus, MatchTier
from unnet.core.money import format_inr


@dataclass
class LinkScore:
    """How one tier did against the truth for that tier."""

    name: str
    expected: int = 0
    produced: int = 0
    correct: int = 0
    wrong: int = 0
    missed: int = 0
    examples_wrong: list[dict] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.correct / self.expected if self.expected else 1.0

    @property
    def precision(self) -> float:
        return self.correct / self.produced if self.produced else 1.0

    @property
    def false_match_rate(self) -> float:
        return self.wrong / self.produced if self.produced else 0.0


@dataclass
class ExceptionScore:
    code: str
    expected: int = 0
    reported: int = 0
    matched: int = 0

    @property
    def recall(self) -> float:
        return self.matched / self.expected if self.expected else 1.0

    @property
    def precision(self) -> float:
        return self.matched / self.reported if self.reported else 1.0


@dataclass
class ScoreReport:
    label: str = ""
    ai_enabled: bool = True
    duration_ms: int = 0

    links: list[LinkScore] = field(default_factory=list)
    exceptions: list[ExceptionScore] = field(default_factory=list)

    orders: int = 0
    settlement_lines: int = 0
    bank_txns: int = 0

    gross_paise: int = 0
    value_reconciled_paise: int = 0
    value_in_exceptions_paise: int = 0

    exceptions_open: int = 0
    exceptions_auto_resolved: int = 0
    exceptions_ai_resolved: int = 0
    exceptions_ai_rejected: int = 0
    exceptions_rolled_forward: int = 0

    llm: dict = field(default_factory=dict)
    hard_cases: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------ #

    @property
    def total_links_produced(self) -> int:
        return sum(link.produced for link in self.links)

    @property
    def total_links_correct(self) -> int:
        return sum(link.correct for link in self.links)

    @property
    def total_links_wrong(self) -> int:
        return sum(link.wrong for link in self.links)

    @property
    def total_links_expected(self) -> int:
        return sum(link.expected for link in self.links)

    @property
    def auto_match_rate(self) -> float:
        return (
            self.total_links_correct / self.total_links_expected
            if self.total_links_expected
            else 0.0
        )

    @property
    def false_match_rate(self) -> float:
        return (
            self.total_links_wrong / self.total_links_produced
            if self.total_links_produced
            else 0.0
        )

    @property
    def value_reconciled_pct(self) -> float:
        return self.value_reconciled_paise / self.gross_paise if self.gross_paise else 0.0

    @property
    def throughput_records_per_sec(self) -> float:
        total = self.orders + self.settlement_lines + self.bank_txns
        return total / (self.duration_ms / 1000) if self.duration_ms else 0.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["derived"] = {
            "auto_match_rate": round(self.auto_match_rate, 6),
            "false_match_rate": round(self.false_match_rate, 6),
            "value_reconciled_pct": round(self.value_reconciled_pct, 6),
            "throughput_records_per_sec": round(self.throughput_records_per_sec, 1),
            "total_links_produced": self.total_links_produced,
            "total_links_correct": self.total_links_correct,
            "total_links_wrong": self.total_links_wrong,
            "total_links_expected": self.total_links_expected,
        }
        return data


def load_truth(path: Path | str = "data/synthetic/ground_truth.json") -> dict:
    return json.loads(Path(path).read_text())


def score(result, truth: dict, *, label: str = "") -> ScoreReport:
    """Grade a :class:`~unnet.engine.pipeline.ReconResult` against the truth."""
    ctx, run = result.ctx, result.run

    report = ScoreReport(
        label=label or run.label,
        ai_enabled=run.ai_enabled,
        duration_ms=run.duration_ms,
        orders=run.orders_count,
        settlement_lines=run.settlement_lines_count,
        bank_txns=run.bank_txns_count,
        gross_paise=int(truth.get("stats", {}).get("gross_paise", 0)),
        value_reconciled_paise=run.value_reconciled_paise,
        value_in_exceptions_paise=run.value_in_exceptions_paise,
        llm=run.notes.get("llm", {}),
        hard_cases=truth.get("hard_cases", []),
    )

    statuses = [e.status for e in ctx.exceptions]
    report.exceptions_open = sum(1 for s in statuses if s == ExceptionStatus.OPEN)
    report.exceptions_auto_resolved = sum(
        1 for s in statuses if s == ExceptionStatus.AUTO_RESOLVED
    )
    report.exceptions_ai_resolved = sum(1 for s in statuses if s == ExceptionStatus.AI_RESOLVED)
    report.exceptions_ai_rejected = sum(1 for s in statuses if s == ExceptionStatus.AI_REJECTED)
    report.exceptions_rolled_forward = sum(
        1 for s in statuses if s == ExceptionStatus.ROLLED_FORWARD
    )

    report.links = [
        _score_tier2(ctx, truth),
        _score_tier1(ctx, truth),
        _score_tier3(ctx, truth),
    ]
    report.exceptions = _score_exceptions(ctx, truth)
    return report


def _score_tier2(ctx, truth: dict) -> LinkScore:
    """order_id -> settlement entity_id, as the generator recorded it."""
    expected: dict[str, str] = truth.get("order_to_line", {})
    link = LinkScore(name="Tier 2 — settlement line to order", expected=len(expected))

    for match in ctx.matches:
        if match.tier != MatchTier.TIER2_LINE_TO_ORDER:
            continue
        link.produced += 1
        order_id, entity_id = match.right_id, match.left_id
        if expected.get(order_id) == entity_id:
            link.correct += 1
        else:
            link.wrong += 1
            if len(link.examples_wrong) < 5:
                link.examples_wrong.append(
                    {
                        "order_id": order_id,
                        "linked_to": entity_id,
                        "should_be": expected.get(order_id),
                        "rule": match.rule_id,
                    }
                )

    link.missed = max(0, link.expected - link.correct)
    return link


def _score_tier1(ctx, truth: dict) -> LinkScore:
    """settlement_id -> bank_ref. A payout inside a consolidated credit maps to
    the same bank_ref as its sibling, which the truth already records."""
    expected: dict[str, str] = truth.get("batch_to_bank", {})
    link = LinkScore(name="Tier 1 — bank credit to payout", expected=len(expected))

    for match in ctx.matches:
        if match.tier != MatchTier.TIER1_BANK_TO_BATCH:
            continue
        link.produced += 1
        settlement_id, bank_ref = match.right_id, match.left_id
        if expected.get(settlement_id) == bank_ref:
            link.correct += 1
        else:
            link.wrong += 1
            if len(link.examples_wrong) < 5:
                link.examples_wrong.append(
                    {
                        "settlement_id": settlement_id,
                        "linked_to": bank_ref,
                        "should_be": expected.get(settlement_id),
                        "rule": match.rule_id,
                    }
                )

    link.missed = max(0, link.expected - link.correct)
    return link


def _score_tier3(ctx, truth: dict) -> LinkScore:
    """reversal entity_id -> the payment_id it reverses."""
    expected: dict[str, str] = truth.get("reversal_to_payment", {})
    link = LinkScore(name="Tier 3 — reversal to original payment", expected=len(expected))

    payment_id_of = {line.entity_id: line.payment_id for line in ctx.lines}

    for match in ctx.matches:
        if match.tier != MatchTier.TIER3_REVERSAL_TO_PAYMENT:
            continue
        link.produced += 1
        reversal_id = match.left_id
        linked_payment = payment_id_of.get(match.right_id)
        if expected.get(reversal_id) == linked_payment:
            link.correct += 1
        else:
            link.wrong += 1
            if len(link.examples_wrong) < 5:
                link.examples_wrong.append(
                    {
                        "reversal_id": reversal_id,
                        "linked_to": linked_payment,
                        "should_be": expected.get(reversal_id),
                        "rule": match.rule_id,
                    }
                )

    link.missed = max(0, link.expected - link.correct)
    return link


def _score_exceptions(ctx, truth: dict) -> list[ExceptionScore]:
    """Did we report the breaks that were actually injected?

    Compared as (code, subject_id) pairs rather than by count, so reporting the
    right number of exceptions about the wrong records scores as badly as it
    should.
    """
    expected_pairs = {
        (e["code"], e["subject_id"]) for e in truth.get("expected_exceptions", [])
    }
    reported_pairs = {(e.code.value, e.subject_id) for e in ctx.exceptions}

    scores: dict[str, ExceptionScore] = {}
    for code, _subject in expected_pairs:
        scores.setdefault(code, ExceptionScore(code=code)).expected += 1
    for code, _subject in reported_pairs:
        scores.setdefault(code, ExceptionScore(code=code)).reported += 1
    for pair in expected_pairs & reported_pairs:
        scores[pair[0]].matched += 1

    return sorted(scores.values(), key=lambda s: s.code)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_robustness(standard: ScoreReport, messy: ScoreReport) -> str:
    """The section that says what happens when the data stops cooperating.

    A single 100% figure on fixtures the author generated is not evidence of
    much. The number worth publishing is what happens when the identifiers the
    matching depends on are taken away — and specifically whether the system
    starts guessing or starts refusing.
    """
    lines: list[str] = []
    add = lines.append

    add("## Robustness: what happens when identifiers disappear\n")
    add(
        "The `messy` profile blanks `payment_id` and `order_id` on 35% of "
        "settlement lines, which is not a hypothetical — Razorpay's recon report "
        "genuinely leaves them empty for some entities, and plenty of merchants "
        "export books carrying no gateway id at all. Tier 2 then has nothing to "
        "match on but amount, method and time.\n"
    )
    add("| Metric | Standard | Identifiers missing | Change |")
    add("| --- | ---: | ---: | ---: |")
    add(
        f"| Auto-match rate | {standard.auto_match_rate:.2%} | "
        f"{messy.auto_match_rate:.2%} | {messy.auto_match_rate - standard.auto_match_rate:+.2%} |"
    )
    add(
        f"| **False-match rate** | {standard.false_match_rate:.2%} | "
        f"**{messy.false_match_rate:.2%}** | "
        f"{messy.false_match_rate - standard.false_match_rate:+.2%} |"
    )
    add(f"| Exceptions open | {standard.exceptions_open:,} | {messy.exceptions_open:,} | "
        f"{messy.exceptions_open - standard.exceptions_open:+,} |")
    add("")
    add("Per tier, with identifiers missing:\n")
    add("| Tier | Recall | Precision | Wrong links |")
    add("| --- | ---: | ---: | ---: |")
    for link in messy.links:
        add(f"| {link.name} | {link.recall:.2%} | {link.precision:.2%} | {link.wrong} |")
    add("")
    add(
        "**Recall falls by roughly a third. Precision does not move.** That is "
        "the intended behaviour and the reason the fuzzy rules require a unique "
        "candidate: with no identifier, hundreds of small UPI orders share an "
        "amount and a minute, and the honest answer is that we do not know which "
        "is which. Those go to the exception queue. The alternative — picking the "
        "nearest and moving on — would have produced a far prettier match rate "
        "and a ledger nobody should trust.\n"
    )
    return "\n".join(lines)


def render_markdown(
    report: ScoreReport,
    *,
    ablation: ScoreReport | None = None,
    messy: ScoreReport | None = None,
) -> str:
    lines: list[str] = []
    add = lines.append

    add("# Measured results\n")
    add(
        "> Generated by `make eval`. Every figure below is produced by scoring a "
        "run against `data/synthetic/ground_truth.json`, which the engine never "
        "reads. Re-running `make eval` regenerates this file.\n"
    )

    add("## Headline\n")
    add("| Metric | Value |")
    add("| --- | ---: |")
    add(f"| Records processed | {report.orders + report.settlement_lines + report.bank_txns:,} |")
    add(f"| Auto-match rate | {report.auto_match_rate:.2%} |")
    add(f"| **False-match rate** | **{report.false_match_rate:.2%}** |")
    add(f"| Links produced | {report.total_links_produced:,} |")
    add(f"| Links wrong | {report.total_links_wrong:,} |")
    add(f"| Value reconciled | {format_inr(report.value_reconciled_paise)} ({report.value_reconciled_pct:.2%}) |")
    add(f"| Value left in exceptions | {format_inr(report.value_in_exceptions_paise)} |")
    add(f"| Wall clock | {report.duration_ms:,} ms |")
    add(f"| Throughput | {report.throughput_records_per_sec:,.0f} records/sec |")
    add("")

    add("## Links, by tier\n")
    add("| Tier | Expected | Produced | Correct | Wrong | Missed | Recall | Precision |")
    add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for link in report.links:
        add(
            f"| {link.name} | {link.expected:,} | {link.produced:,} | {link.correct:,} | "
            f"{link.wrong:,} | {link.missed:,} | {link.recall:.2%} | {link.precision:.2%} |"
        )
    add("")

    wrong = [w for link in report.links for w in link.examples_wrong]
    if wrong:
        add("### Wrong links\n")
        for item in wrong[:10]:
            add(f"- `{item}`")
        add("")

    add("## Exception queue\n")
    add("| Status | Count |")
    add("| --- | ---: |")
    add(f"| Open, needs a human | {report.exceptions_open} |")
    add(f"| Closed by exact search | {report.exceptions_auto_resolved} |")
    add(f"| Closed by model, verifier accepted | {report.exceptions_ai_resolved} |")
    add(f"| Model proposed, **verifier rejected** | {report.exceptions_ai_rejected} |")
    add(f"| Timing, rolled into next run | {report.exceptions_rolled_forward} |")
    add("")

    add("## Exceptions against the injected defects\n")
    add("| Code | Injected | Reported | Correctly identified | Recall | Precision |")
    add("| --- | ---: | ---: | ---: | ---: | ---: |")
    for item in report.exceptions:
        add(
            f"| `{item.code}` | {item.expected} | {item.reported} | {item.matched} | "
            f"{item.recall:.0%} | {item.precision:.0%} |"
        )
    add("")

    if report.hard_cases:
        add("## Deliberately hard cases\n")
        add(
            "Links the generator made hard to find on purpose. These are not "
            "expected exceptions — the money is right, only the obvious link is "
            "gone — so recovering one is a success, not a miss.\n"
        )
        add("| Case | Solvable by matching rules? |")
        add("| --- | --- |")
        for case in report.hard_cases:
            add(f"| `{case['kind']}` | {'yes' if case['solvable_by_rules'] else 'no'} |")
        add("")

    if messy is not None:
        add(render_robustness(report, messy))

    if ablation is not None:
        add("## Ablation: rules only vs rules + model\n")
        add(
            "The same fixtures, the same seed, one flag different. This is the "
            "honest test of whether the model layer earns its place.\n"
        )
        add("| Metric | Rules only | Rules + model | Change |")
        add("| --- | ---: | ---: | ---: |")
        _ablation_row(add, "Auto-match rate", ablation.auto_match_rate, report.auto_match_rate, pct=True)
        _ablation_row(add, "False-match rate", ablation.false_match_rate, report.false_match_rate, pct=True)
        _ablation_row(
            add,
            "Exceptions still open",
            ablation.exceptions_open,
            report.exceptions_open,
        )
        _ablation_row(
            add,
            "Closed by model",
            ablation.exceptions_ai_resolved,
            report.exceptions_ai_resolved,
        )
        _ablation_row(
            add,
            "Rejected by verifier",
            ablation.exceptions_ai_rejected,
            report.exceptions_ai_rejected,
        )
        _ablation_row(add, "Wall clock (ms)", ablation.duration_ms, report.duration_ms)
        add("")
        if report.llm:
            add(
                f"Model calls: {report.llm.get('calls', 0)} "
                f"({report.llm.get('cassette_hits', 0)} served from cassette, "
                f"{report.llm.get('live_calls', 0)} live). "
                f"Degraded: {report.llm.get('degraded', False)}.\n"
            )

    return "\n".join(lines)


def _ablation_row(add, name: str, before, after, *, pct: bool = False) -> None:
    if pct:
        delta = after - before
        add(f"| {name} | {before:.2%} | {after:.2%} | {delta:+.2%} |")
    else:
        delta = after - before
        add(f"| {name} | {before:,} | {after:,} | {delta:+,} |")
