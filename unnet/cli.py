"""Unnet command line.

    unnet gen        regenerate the synthetic fixtures and their ground truth
    unnet recon      run one reconciliation and print the result
    unnet eval       score a run against the ground truth, write docs/METRICS.md
    unnet ablation   rules-only vs rules+model, same seed, one flag apart
    unnet serve      start the API and dashboard
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console
from rich.table import Table

from unnet.core.db import (
    DEFAULT_DB_PATH,
    AuditLog,
    make_engine,
    prune_runs,
    session_scope,
)
from unnet.engine import casefile
from unnet.core.models import ExceptionStatus
from unnet.core.money import format_inr
from unnet.engine.pipeline import SourcePaths, reconcile
from unnet.evaluation.agent_score import render_markdown as render_agent
from unnet.evaluation.agent_score import score_agent
from unnet.evaluation.score import load_truth, render_markdown, score
from unnet.llm.provider import build_client

console = Console()


def cmd_gen(args) -> int:
    from unnet.evaluation.generator import GeneratorConfig, generate

    if args.profile == "messy":
        config = GeneratorConfig.messy(
            seed=args.seed, n_payments=args.payments, n_days=args.days
        )
        if args.out:
            config.out_dir = Path(args.out)
    else:
        config = GeneratorConfig(
            seed=args.seed,
            n_payments=args.payments,
            n_days=args.days,
            out_dir=Path(args.out or "data/synthetic"),
        )
    truth = generate(config)

    table = Table(title="Synthetic fixtures", header_style="bold")
    table.add_column("What")
    table.add_column("Count", justify="right")
    for key, value in truth.stats.items():
        display = format_inr(value) if key.endswith("_paise") else f"{value:,}"
        table.add_row(key.replace("_", " "), display)
    console.print(table)
    console.print(f"[dim]Written to {args.out}/[/dim]")
    return 0


@contextmanager
def _scratch_db():
    """A throwaway database for a measurement.

    `eval` and `ablation` answer "how good is this?", which is a question about
    the code, not an operation on the merchant's books. Running them against
    `data/unnet.db` wrote four extra runs into the operational store every time
    — including the robustness profile, whose 1,191 exceptions became permanent
    cases with subject ids no standard run ever revisits, so they could never be
    cleared. `make demo && make ablation` left the dashboard showing 1,208 open
    cases and an audit trail whose latest run had consulted no model at all.

    A measurement gets its own database and throws it away.
    """
    with tempfile.TemporaryDirectory(prefix="unnet-eval-") as directory:
        yield str(Path(directory) / "scratch.db")


def _run(args, *, ai_enabled: bool, label: str, data: str | None = None,
         db: str | None = None):
    client = build_client(provider=args.provider) if ai_enabled else None
    engine = make_engine(db or args.db)
    data = data or args.data

    with session_scope(engine) as session:
        import uuid

        run_id = uuid.uuid4().hex[:12]
        audit = AuditLog(session, run_id)
        # What the previous runs already know. This is what lets a case that a
        # human settled stay settled instead of being raised again.
        previous = casefile.load_previous(session)
        result = reconcile(
            SourcePaths.synthetic(data),
            run_id=run_id,
            audit=audit,
            ai_enabled=ai_enabled,
            label=label,
            llm_client=client,
            previous_cases=previous,
        )
        _persist(session, result)
        casefile.persist(session, result.cases, run_id, previous)

    # Retention runs after the commit, so a failed reconciliation never costs
    # history it did not replace.
    keep = getattr(args, "keep_runs", 0)
    if keep:
        removed = prune_runs(engine, keep=keep)
        if removed:
            total = sum(removed.values())
            console.print(
                f"[dim]Pruned {total:,} rows from runs older than the last "
                f"{keep}.[/dim]"
            )

    return result, client


def _persist(session, result) -> None:
    """Write the run to SQLite so the dashboard and the audit trail survive it."""
    ctx = result.ctx
    for row in (
        ctx.orders
        + ctx.lines
        + ctx.batches
        + ctx.bank_txns
        + ctx.matches
        + ctx.exceptions
        + [result.run]
    ):
        session.add(row)


def cmd_recon(args) -> int:
    result, client = _run(args, ai_enabled=not args.rules_only, label=args.label or "recon")
    run, ctx = result.run, result.ctx

    table = Table(title=f"Reconciliation {run.run_id}", header_style="bold")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Orders", f"{run.orders_count:,}")
    table.add_row("Settlement lines", f"{run.settlement_lines_count:,}")
    table.add_row("Bank rows", f"{run.bank_txns_count:,}")
    table.add_row("Links made", f"{run.matched_count:,}")
    table.add_row("Exceptions open", f"{run.exceptions_open:,}")
    table.add_row("Value reconciled", format_inr(run.value_reconciled_paise))
    table.add_row("Value in exceptions", format_inr(run.value_in_exceptions_paise))
    table.add_row("Duration", f"{run.duration_ms:,} ms")
    console.print(table)

    breakdown = Table(title="Exceptions", header_style="bold")
    breakdown.add_column("Code")
    breakdown.add_column("Open", justify="right")
    breakdown.add_column("Resolved", justify="right")
    counts: dict[str, list[int]] = {}
    for exception in ctx.exceptions:
        row = counts.setdefault(exception.code.value, [0, 0])
        if exception.status in {ExceptionStatus.OPEN, ExceptionStatus.AI_REJECTED}:
            row[0] += 1
        else:
            row[1] += 1
    for code in sorted(counts):
        breakdown.add_row(code, f"{counts[code][0]}", f"{counts[code][1]}")
    console.print(breakdown)

    if client and client.stats()["degraded"]:
        console.print(f"[yellow]Model layer degraded:[/yellow] {client.stats()['degraded_reason']}")
    return 0


def cmd_eval(args) -> int:
    truth = load_truth(Path(args.data) / "ground_truth.json")
    with _scratch_db() as scratch:
        result, _ = _run(
            args, ai_enabled=not args.rules_only, label="eval", db=scratch
        )
    report = score(result, truth, label="rules + model" if not args.rules_only else "rules only")

    console.print(f"Auto-match rate      [bold]{report.auto_match_rate:.2%}[/bold]")
    console.print(f"False-match rate     [bold]{report.false_match_rate:.2%}[/bold]")
    console.print(f"Value reconciled     [bold]{format_inr(report.value_reconciled_paise)}[/bold]")
    console.print(f"Throughput           [bold]{report.throughput_records_per_sec:,.0f}[/bold] rec/s")

    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(report.to_dict(), indent=2, default=str))
    console.print(f"[dim]Wrote {args.json_out}[/dim]")
    return 0


def cmd_ablation(args) -> int:
    """The comparison that says whether the model layer is worth having."""
    truth = load_truth(Path(args.data) / "ground_truth.json")

    # Each regime gets a fresh scratch database, so no comparison inherits the
    # cases the previous one left behind, and none of them touch the real store.
    with _scratch_db() as scratch:
        baseline_result, _ = _run(args, ai_enabled=False, label="rules only", db=scratch)
    baseline = score(baseline_result, truth, label="rules only")

    with _scratch_db() as scratch:
        ai_result, client = _run(args, ai_enabled=True, label="rules + model", db=scratch)
    ai_report = score(ai_result, truth, label="rules + model")

    # The robustness profile, if it has been generated. Computed before the
    # table is printed so both numbers land in one view.
    messy_report = None
    messy_dir = Path(args.messy_data)
    if (messy_dir / "ground_truth.json").exists():
        with _scratch_db() as scratch:
            messy_result, _ = _run(
                args, ai_enabled=False, label="messy / rules only",
                data=str(messy_dir), db=scratch,
            )
        messy_report = score(
            messy_result,
            load_truth(messy_dir / "ground_truth.json"),
            label="identifiers missing",
        )

    table = Table(title="Ablation — same fixtures, one flag apart", header_style="bold")
    table.add_column("Metric")
    table.add_column("Rules only", justify="right")
    table.add_column("Rules + model", justify="right")
    table.add_row("Auto-match rate", f"{baseline.auto_match_rate:.2%}", f"{ai_report.auto_match_rate:.2%}")
    table.add_row("False-match rate", f"{baseline.false_match_rate:.2%}", f"{ai_report.false_match_rate:.2%}")
    table.add_row("Exceptions open", f"{baseline.exceptions_open}", f"{ai_report.exceptions_open}")
    table.add_row("Closed by model", f"{baseline.exceptions_ai_resolved}", f"{ai_report.exceptions_ai_resolved}")
    table.add_row("Verifier rejections", f"{baseline.exceptions_ai_rejected}", f"{ai_report.exceptions_ai_rejected}")
    table.add_row("Duration (ms)", f"{baseline.duration_ms:,}", f"{ai_report.duration_ms:,}")
    console.print(table)

    if messy_report is not None:
        robustness = Table(
            title="Robustness — 35% of gateway identifiers removed", header_style="bold"
        )
        robustness.add_column("Metric")
        robustness.add_column("Standard", justify="right")
        robustness.add_column("Identifiers missing", justify="right")
        robustness.add_row(
            "Auto-match rate",
            f"{ai_report.auto_match_rate:.2%}",
            f"{messy_report.auto_match_rate:.2%}",
        )
        robustness.add_row(
            "False-match rate",
            f"{ai_report.false_match_rate:.2%}",
            f"{messy_report.false_match_rate:.2%}",
        )
        robustness.add_row(
            "Exceptions open",
            f"{ai_report.exceptions_open:,}",
            f"{messy_report.exceptions_open:,}",
        )
        console.print(robustness)
        console.print(
            "[dim]Recall falls, precision holds — the engine refuses ambiguous "
            "matches rather than guessing.[/dim]"
        )

    agent = score_agent(ai_result, truth)
    agent_table = Table(title="Agent behaviour", header_style="bold")
    agent_table.add_column("Metric")
    agent_table.add_column("Value", justify="right")
    agent_table.add_row("Wrong-resolution rate", f"{agent.wrong_resolution_rate:.2%}")
    agent_table.add_row(
        "Escalation correctness",
        f"{agent.escalation_correctness:.0%} ({agent.correctly_escalated}/{agent.should_escalate})",
    )
    agent_table.add_row("Hypotheses for a human", f"{agent.hypotheses}")
    agent_table.add_row("Verifier rejections", f"{agent.verifier_rejections}")
    agent_table.add_row("Model abstained", f"{agent.abstentions}")
    agent_table.add_row(
        "Routing accuracy",
        f"{agent.routing_accuracy:.0%} ({agent.routed_correctly}/{agent.routed_cases})",
    )
    agent_table.add_row("Tokens / useful outcome", f"{agent.tokens_per_useful_outcome:,.0f}")
    console.print(agent_table)

    # Schema mapping is measured separately because it is the only place the
    # deterministic answer is structurally incomplete rather than merely
    # fiddly — and because on most layouts it shows the model is not needed.
    from unnet.evaluation.schema_bench import render_markdown as render_schema
    from unnet.evaluation.schema_bench import run_bench

    bench = run_bench(client)
    schema_table = Table(
        title="Schema mapping — realistic bank layouts", header_style="bold"
    )
    schema_table.add_column("Metric")
    schema_table.add_column("Value", justify="right")
    schema_table.add_row("Layouts tested", f"{len(bench.results)}")
    schema_table.add_row("Solved by the alias table alone", f"{bench.heuristic_solved}")
    schema_table.add_row("Needed the model", f"{len(bench.results) - bench.heuristic_solved}")
    schema_table.add_row("Recovered correctly by the model", f"{bench.model_recovered}")
    schema_table.add_row("Still unsolved", f"{bench.unsolved}")
    console.print(schema_table)

    markdown = render_markdown(ai_report, ablation=baseline, messy=messy_report)
    markdown += "\n" + render_agent(agent)
    markdown += "\n" + render_schema(bench)
    markdown += "\n" + _render_cases(ai_result)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(markdown)
    console.print(f"[dim]Wrote {args.out}[/dim]")

    if client and client.stats()["degraded"]:
        console.print(f"[yellow]Model degraded:[/yellow] {client.stats()['degraded_reason']}")
    elif client and client.stats()["calls"] and not client.stats()["live_calls"]:
        if not client.stats()["cassette_hits"]:
            console.print(
                "[yellow]No model was reached[/yellow] — no cassettes and no provider "
                "configured, so the model column above is the rules result. "
                "See README: 'Running the agents'."
            )
    return 0


def cmd_agent(args) -> int:
    """What the agent actually does, measured — and the loop, forced.

    Two sections, kept apart on purpose. The first is the production fixtures:
    how many model calls each exception really cost and how often the retry
    path fired. On this data the answer is "one call, no retries", and that is
    published rather than dressed up. The second forces a rejection with a
    scripted model so the adaptive path can be watched end to end, and is
    labelled as the demonstration it is.

    A loop that never runs is how every fake agent is built, so both halves
    have to be visible: the mechanism is real, and this data does not need it.
    """
    from unnet.agents.triage import MAX_ATTEMPTS, TriageAgent
    from unnet.core.models import ExceptionCode, ExceptionStatus
    from unnet.llm.scripted import ScriptedClient

    with _scratch_db() as scratch:
        result, client = _run(args, ai_enabled=True, label="agent", db=scratch)
    agent = score_agent(result, load_truth(Path(args.data) / "ground_truth.json"))

    console.print()
    console.print("[bold]Measured — the production fixtures[/bold]")
    measured = Table(header_style="bold", show_header=True)
    measured.add_column("Metric")
    measured.add_column("Value", justify="right")
    calls = agent.steps_per_exception
    measured.add_row("Exceptions the model was asked about", f"{len(calls)}")
    measured.add_row("Model calls per exception", str(calls) if calls else "—")
    measured.add_row("Retries the verifier forced", f"{agent.retries}")
    measured.add_row(
        "Verified resolutions, closed by rule", f"{agent.resolutions_by_rule}"
    )
    measured.add_row(
        "Verified resolutions, closed by model", f"{agent.resolutions_by_model}"
    )
    measured.add_row("Hypotheses quarantined for a human", f"{agent.hypotheses}")
    measured.add_row("Verifier rejections", f"{agent.verifier_rejections}")
    measured.add_row("Abstentions", f"{agent.abstentions}")
    measured.add_row("Tokens", f"{agent.tokens:,}")
    console.print(measured)

    if agent.resolutions_by_model == 0:
        console.print(
            f"[dim]The model closed nothing. The {agent.resolutions_by_rule} verified "
            "resolutions above were made by the deterministic subset-sum resolver, "
            "which the verifier gates on the same terms. A wrong-resolution rate of "
            "zero is therefore a statement about the pipeline, not a claim about "
            "the model.[/dim]"
        )

    if agent.retries == 0:
        console.print(
            "[dim]No retry was needed on this data: deterministic candidate "
            "generation runs first, so the model's single attempt was either "
            "right or an honest abstention. The loop below is the same code "
            "path, driven by a scripted model.[/dim]"
        )

    # ---------------------------------------------------------------- #
    # The forced demonstration.
    # ---------------------------------------------------------------- #
    target = None
    for exception in result.ctx.exceptions:
        if exception.code == ExceptionCode.SHORT_CREDIT:
            target = exception
            break
    if target is None:
        console.print("[yellow]No short credit in the fixtures to demonstrate on.[/yellow]")
        return 0

    amount = abs(target.residual_paise)
    target.status = ExceptionStatus.OPEN
    target.proposal = None
    target.verifier_verdict = None
    target.evidence = {
        k: v for k, v in (target.evidence or {}).items() if k != "agent_trace"
    }

    scripted = ScriptedClient([
        # Attempt 1: 50 paise short. The verifier can only reject this.
        {"components": [{"kind": "bank_charge", "ref": "neft_fee",
                         "amount_paise": amount - 50}],
         "reasoning": "Looks like the bank's inward NEFT charge."},
        # Attempt 2: corrected using the delta the verifier handed back.
        {"components": [{"kind": "bank_charge", "ref": "neft_fee_plus_gst",
                         "amount_paise": amount}],
         "reasoning": "Adding the 18% GST on that charge closes the gap exactly."},
    ])
    loop = TriageAgent(scripted)
    loop._triage_one(result.ctx, target)

    console.print()
    console.print(
        f"[bold]Forced — the same loop, with a model scripted to be wrong first[/bold]"
    )
    console.print(
        f"[dim]Subject {target.subject_id}, residual {format_inr(amount)}. "
        f"Attempt limit is {MAX_ATTEMPTS}.[/dim]"
    )

    trace = (target.evidence or {}).get("agent_trace", [])
    steps = Table(header_style="bold")
    steps.add_column("#", justify="right")
    steps.add_column("Action")
    steps.add_column("Proposed")
    steps.add_column("Verifier")
    steps.add_column("Out by", justify="right")
    for step in trace:
        components = "  +  ".join(step.get("components") or []) or "—"
        delta = step.get("delta_paise")
        steps.add_row(
            str(step.get("step", "")),
            str(step.get("action", "")),
            components,
            str(step.get("verdict") or "—"),
            format_inr(delta) if delta else "—",
        )
    console.print(steps)

    console.print(
        f"Model calls [bold]{scripted.calls}[/bold] · "
        f"retries [bold]{loop.retries}[/bold] · "
        f"terminal verdict [bold]{target.verifier_verdict}[/bold]"
    )

    # The thing that makes it a loop rather than a re-roll: the second prompt
    # carries the verifier's arithmetic, not just "that was wrong, try again".
    if len(scripted.prompts) > 1:
        fed_back = [
            line.strip()
            for line in scripted.prompts[1].splitlines()
            if "out by" in line.lower() or "REJECTED" in line
        ]
        console.print()
        console.print("[bold]What the second attempt was told[/bold]")
        for line in fed_back or ["(the retry prompt carried no verifier finding)"]:
            console.print(f"  [dim]{line}[/dim]")

    console.print()
    console.print(
        "[dim]The revised proposal sums exactly and is still quarantined as a "
        "hypothesis, because a bank charge appears in no table we hold. "
        "Arithmetic is not provenance.[/dim]"
    )

    # ---------------------------------------------------------------- #
    # What is guaranteed, and where the guarantee is checked.
    # ---------------------------------------------------------------- #
    console.print()
    console.print("[bold]Safety properties, and the tests that hold them[/bold]")
    guarantees = Table(header_style="bold")
    guarantees.add_column("Property")
    guarantees.add_column("Held by")
    for prop, where in (
        ("Two explanations that both sum exactly → neither is posted",
         "tests/test_ambiguity.py"),
        ("A component in no ledger row → hypothesis, never a closure",
         "tests/test_verifier.py"),
        ("A malformed or unreadable model reply → abstention, run continues",
         "tests/test_malformed_model_output.py"),
        ("A provider outage → not_attempted, never a resolution",
         "tests/test_malformed_model_output.py"),
        ("Payer-controlled narration → fenced, and quoted in drafts",
         "tests/test_injection.py"),
        ("A model's SQL → single read-only SELECT, allow-listed tables",
         "tests/test_sql_guard.py"),
    ):
        guarantees.add_row(prop, where)
    console.print(guarantees)
    console.print(
        "[dim]The ambiguity case is constructed against the real engine rather "
        "than drawn from the fixtures: on 21 payouts only one credit and one "
        "payout survive to the resolver, so no rival explanation exists to "
        "find. An untriggered guard and an absent guard look identical in a "
        "metrics table, which is why it is asserted instead of counted.[/dim]"
    )
    return 0


def cmd_cases(args) -> int:
    """What is outstanding, who owns it, and how the money is at stake."""
    engine = make_engine(args.db)
    with session_scope(engine) as session:
        cases = list(casefile.load_previous(session).values())

    if not cases:
        console.print("No cases yet. Run `unnet recon` first.")
        return 0

    summary = casefile.summarise(cases)
    impact = Table(title="Identified — not recovered", header_style="bold")
    impact.add_column("How the money is at stake")
    impact.add_column("Cases", justify="right")
    impact.add_column("Amount", justify="right")
    labels = {
        "claimable": "Claimable — a counterparty owes it",
        "at_risk": "At risk — whereabouts unresolved",
        "bookkeeping": "Bookkeeping — no money moves",
        "contestable_loss": "Lost unless contested",
    }
    for key, value in sorted(summary["by_impact"].items(), key=lambda kv: -kv[1]["paise"]):
        impact.add_row(labels.get(key, key), f"{value['count']}", format_inr(value["paise"]))
    console.print(impact)

    owners = Table(title="Routed to", header_style="bold")
    owners.add_column("Owner")
    owners.add_column("Cases", justify="right")
    owners.add_column("Amount", justify="right")
    for key, value in sorted(summary["by_owner"].items(), key=lambda kv: -kv[1]["paise"]):
        owners.add_row(key, f"{value['count']}", format_inr(value["paise"]))
    console.print(owners)

    console.print(
        f"[dim]{summary['open_cases']} open, {summary['resolved_cases']} resolved.[/dim]"
    )

    if args.owner:
        detail = Table(title=f"Open cases for {args.owner}", header_style="bold")
        detail.add_column("Key")
        detail.add_column("Code")
        detail.add_column("Amount", justify="right")
        detail.add_column("Ask")
        for case in sorted(cases, key=lambda c: -c.amount_paise):
            if case.owner != args.owner or case.status == "resolved":
                continue
            detail.add_row(
                case.case_key[:10], case.code, case.amount_display, case.message[:90]
            )
        console.print(detail)
    return 0


def cmd_resolve(args) -> int:
    """Record that a case was settled, so the next run stops raising it."""
    import uuid

    engine = make_engine(args.db)
    with session_scope(engine) as session:
        updated = casefile.resolve(
            session, args.case_key, run_id=f"manual-{uuid.uuid4().hex[:6]}", note=args.note
        )
    if updated:
        console.print(f"[green]Resolved[/green] {args.case_key}")
    else:
        console.print(f"[red]No case[/red] {args.case_key}")
    return 0 if updated else 1


def _render_cases(result) -> str:
    """The loop-closure section: what is outstanding and who owns it."""
    summary = casefile.summarise(result.cases)
    labels = {
        "claimable": "Claimable — a counterparty owes it back",
        "at_risk": "At risk — real money, whereabouts unresolved",
        "bookkeeping": "Bookkeeping — no money moves, the books are wrong",
        "contestable_loss": "Lost unless contested",
    }
    lines = ["## The loop: what is outstanding, and who owns it\n"]
    lines.append(
        "Every unresolved exception is routed to the party who can actually fix "
        "it, and tracked by a key derived from *what the problem is* rather than "
        "a row id — so a case settled once does not come back on the next run.\n"
    )
    lines.append(
        "These figures are **identified**, never recovered. Recovery happens when "
        "a bank credits the money back, which is not an event this system can "
        "observe. They are also never summed: a chargeback already lost and a fee "
        "a supplier owes back are not the same rupee.\n"
    )
    lines.append("| How the money is at stake | Cases | Amount |")
    lines.append("| --- | ---: | ---: |")
    for key, value in sorted(summary["by_impact"].items(), key=lambda kv: -kv[1]["paise"]):
        lines.append(
            f"| {labels.get(key, key)} | {value['count']} | {format_inr(value['paise'])} |"
        )
    lines.append("")
    lines.append("| Routed to | Cases | Amount |")
    lines.append("| --- | ---: | ---: |")
    for key, value in sorted(summary["by_owner"].items(), key=lambda kv: -kv[1]["paise"]):
        lines.append(f"| `{key}` | {value['count']} | {format_inr(value['paise'])} |")
    lines.append("")
    return "\n".join(lines)


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("unnet.api.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The CLI's argument parser, built separately so it can be inspected."""
    parser = argparse.ArgumentParser(prog="unnet", description=__doc__)
    parser.add_argument("--data", default="data/synthetic", help="fixture directory")
    # Defaulting to the literal path meant `UNNET_DB=/data/x unnet recon` wrote
    # to data/unnet.db while `unnet serve` — which builds its engine from
    # DEFAULT_DB_PATH — read /data/x. Two commands, two databases, no error.
    parser.add_argument(
        "--db", default=str(DEFAULT_DB_PATH),
        help="SQLite path (defaults to $UNNET_DB, else data/unnet.db)",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="offline | local | gemini | groq | auto (default: $UNNET_LLM_PROVIDER)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("gen", help="regenerate synthetic fixtures")
    gen.add_argument("--seed", type=int, default=20260905)
    gen.add_argument("--payments", type=int, default=1500)
    gen.add_argument("--days", type=int, default=21)
    gen.add_argument("--out", default="")
    gen.add_argument(
        "--profile",
        choices=["standard", "messy"],
        default="standard",
        help="messy removes 35%% of gateway identifiers, for the robustness numbers",
    )
    gen.set_defaults(func=cmd_gen)

    recon = sub.add_parser("recon", help="run one reconciliation")
    recon.add_argument(
        "--keep-runs", type=int, default=10,
        help="delete rows belonging to runs older than the newest N (0 = keep all)",
    )
    recon.add_argument("--rules-only", action="store_true")
    recon.add_argument("--label", default="")
    recon.set_defaults(func=cmd_recon)

    ev = sub.add_parser("eval", help="score against ground truth")
    ev.add_argument("--rules-only", action="store_true")
    ev.add_argument("--json-out", default="docs/metrics.json")
    ev.set_defaults(func=cmd_eval)

    ab = sub.add_parser("ablation", help="rules-only vs rules+model")
    ab.add_argument("--out", default="docs/METRICS.md")
    ab.add_argument("--messy-data", default="data/synthetic_messy")
    ab.set_defaults(func=cmd_ablation)

    ag = sub.add_parser(
        "agent", help="what the agent measurably did, and the retry loop forced"
    )
    ag.add_argument("--out", default="docs/METRICS.md", help="unused; kept for symmetry")
    ag.set_defaults(func=cmd_agent, rules_only=False, label="agent")

    cases = sub.add_parser("cases", help="outstanding work, by owner and impact")
    cases.add_argument("--owner", default="", help="list open cases for one owner")
    cases.set_defaults(func=cmd_cases)

    res = sub.add_parser("resolve", help="mark a case settled")
    res.add_argument("case_key")
    res.add_argument("--note", default="")
    res.set_defaults(func=cmd_resolve)

    serve = sub.add_parser("serve", help="start API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
