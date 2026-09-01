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
from pathlib import Path

from rich.console import Console
from rich.table import Table

from unnet.core.db import AuditLog, make_engine, session_scope
from unnet.core.models import ExceptionStatus
from unnet.core.money import format_inr
from unnet.engine.pipeline import SourcePaths, reconcile
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


def _run(args, *, ai_enabled: bool, label: str, data: str | None = None):
    client = build_client(provider=args.provider) if ai_enabled else None
    engine = make_engine(args.db)
    data = data or args.data

    with session_scope(engine) as session:
        import uuid

        run_id = uuid.uuid4().hex[:12]
        audit = AuditLog(session, run_id)
        result = reconcile(
            SourcePaths.synthetic(data),
            run_id=run_id,
            audit=audit,
            ai_enabled=ai_enabled,
            label=label,
            llm_client=client,
        )
        _persist(session, result)

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
    result, _ = _run(args, ai_enabled=not args.rules_only, label="eval")
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

    baseline_result, _ = _run(args, ai_enabled=False, label="rules only")
    baseline = score(baseline_result, truth, label="rules only")

    ai_result, client = _run(args, ai_enabled=True, label="rules + model")
    ai_report = score(ai_result, truth, label="rules + model")

    # The robustness profile, if it has been generated. Computed before the
    # table is printed so both numbers land in one view.
    messy_report = None
    messy_dir = Path(args.messy_data)
    if (messy_dir / "ground_truth.json").exists():
        messy_result, _ = _run(
            args, ai_enabled=False, label="messy / rules only", data=str(messy_dir)
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

    markdown = render_markdown(ai_report, ablation=baseline, messy=messy_report)
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


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("unnet.api.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unnet", description=__doc__)
    parser.add_argument("--data", default="data/synthetic", help="fixture directory")
    parser.add_argument("--db", default="data/unnet.db", help="SQLite path")
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

    serve = sub.add_parser("serve", help="start API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
