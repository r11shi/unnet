"""Where the alias table stops, and whether a model picks it up.

Schema mapping is the one place in Unnet where the deterministic answer is
*structurally* incomplete rather than merely fiddly: an alias table can only
recognise vocabulary somebody already wrote down, and every bank names its
columns differently. That makes it the fairest available test of whether the
model layer earns its place — and a test that can embarrass it, since the
heuristic turns out to handle most real layouts perfectly well.

The layouts below are modelled on how Indian bank exports actually differ, not
picked to make the model look good. Four of the seven are already solved by the
alias table; those are kept in the benchmark precisely so the score cannot be
read as "the model does schema mapping" when most of the time it is not needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from unnet.ingest.mapping import EITHER_FIELDS, REQUIRED_FIELDS, heuristic_map

#: Header rows in the shape real Indian bank statement exports use. The first
#: two are the project's own fixtures; the rest are layouts the fixtures never
#: contain, so the alias table has had no chance to be tuned to them.
LAYOUTS: dict[str, list[str]] = {
    "ICICI-style": [
        "Txn Date", "Value Date", "Description", "Chq/Ref No",
        "Debit", "Credit", "Balance",
    ],
    "HDFC-style": [
        "Sl No.", "Tran Date", "Value Dt", "Remarks", "Instrument ID",
        "Withdrawal Amt.", "Deposit Amt.", "Closing Bal",
    ],
    "Kotak-style": [
        "Transaction Date", "Value Date", "Narration", "Reference Number",
        "Withdrawal", "Deposit", "Running Balance",
    ],
    "SBI-style": [
        "Txn Date", "Value Date", "Description", "Ref No./Cheque No.",
        "Debit", "Credit", "Balance",
    ],
    "Axis-style": [
        "Tran Date", "Chq No", "Particulars", "Debit Amount",
        "Credit Amount", "Balance Amount", "Init. Br",
    ],
    "Terse export": ["DT", "PARTICULARS", "REF", "DR", "CR", "BAL"],
    "Bilingual export": [
        "दिनांक / Date", "विवरण / Particulars", "संदर्भ / Ref",
        "नामे / Debit", "जमा / Credit", "शेष / Balance",
    ],
    # The honest boundary. A vocabulary table matches words; these headers have
    # none. Legacy core-banking dumps really do arrive like this, and the only
    # way through is to read the values and work out which column is which —
    # which is a different kind of task, not a longer alias list.
    "Column-coded dump": ["C1", "C2", "C3", "C4", "C5", "C6"],
}

#: The correct mapping for each layout, so the benchmark scores whether the
#: answer is *right* and not merely non-empty. An earlier version checked only
#: that no required field was missing, and passed a mapping that had put the
#: narration in the reference column.
EXPECTED: dict[str, dict[str, str]] = {
    "ICICI-style": {
        "txn_date": "Txn Date", "value_date": "Value Date",
        "narration": "Description", "bank_ref": "Chq/Ref No",
        "debit": "Debit", "credit": "Credit", "balance": "Balance",
    },
    "HDFC-style": {
        "txn_date": "Tran Date", "value_date": "Value Dt",
        "narration": "Remarks", "bank_ref": "Instrument ID",
        "debit": "Withdrawal Amt.", "credit": "Deposit Amt.",
        "balance": "Closing Bal",
    },
    "Kotak-style": {
        "txn_date": "Transaction Date", "value_date": "Value Date",
        "narration": "Narration", "bank_ref": "Reference Number",
        "debit": "Withdrawal", "credit": "Deposit",
        "balance": "Running Balance",
    },
    "SBI-style": {
        "txn_date": "Txn Date", "value_date": "Value Date",
        "narration": "Description", "bank_ref": "Ref No./Cheque No.",
        "debit": "Debit", "credit": "Credit", "balance": "Balance",
    },
    "Axis-style": {
        "txn_date": "Tran Date", "narration": "Particulars",
        "bank_ref": "Chq No", "debit": "Debit Amount",
        "credit": "Credit Amount", "balance": "Balance Amount",
    },
    "Terse export": {
        "txn_date": "DT", "narration": "PARTICULARS", "bank_ref": "REF",
        "debit": "DR", "credit": "CR", "balance": "BAL",
    },
    "Bilingual export": {
        "txn_date": "दिनांक / Date", "narration": "विवरण / Particulars",
        "bank_ref": "संदर्भ / Ref", "debit": "नामे / Debit",
        "credit": "जमा / Credit", "balance": "शेष / Balance",
    },
    "Column-coded dump": {
        "txn_date": "C1", "narration": "C2", "bank_ref": "C3",
        "debit": "C4", "credit": "C5", "balance": "C6",
    },
}

#: One correctly aligned sample row per layout, built from its own expected
#: mapping. Building it any other way is how the first version of this file
#: shipped a shifted fixture that the model then correctly complained about.
_VALUE_FOR = {
    "txn_date": "05/08/26",
    "value_date": "05/08/26",
    "narration": "NEFT CR-KKBKH0000414-RAZORPAY SOFTWARE PVT LTD",
    "bank_ref": "N4945598",
    "debit": "",
    "credit": "6,08,030.10",
    "balance": "12,41,882.55",
}
SAMPLES: dict[str, dict] = {
    name: {
        header: _VALUE_FOR[field]
        for field, header in EXPECTED[name].items()
    }
    for name in LAYOUTS
}


def _wrong_fields(proposed: dict[str, str], name: str) -> list[str]:
    """Fields the proposal placed on the wrong column.

    Only fields the layout actually has are judged; a mapper is not penalised
    for omitting a value date from a file with one date column.
    """
    expected = EXPECTED[name]
    return sorted(
        field
        for field, header in proposed.items()
        if field in expected and header != expected[field]
    )


@dataclass
class LayoutResult:
    name: str
    headers: list[str]
    heuristic_missing: list[str] = field(default_factory=list)
    heuristic_wrong: list[str] = field(default_factory=list)
    model_missing: list[str] | None = None
    model_wrong: list[str] = field(default_factory=list)
    model_consulted: bool = False

    @property
    def heuristic_ok(self) -> bool:
        return not self.heuristic_missing and not self.heuristic_wrong

    @property
    def model_ok(self) -> bool | None:
        if self.model_missing is None:
            return None
        return not self.model_missing and not self.model_wrong

    @property
    def recovered_by_model(self) -> bool:
        """The only cell that is an argument for the model layer existing."""
        return not self.heuristic_ok and self.model_ok is True


@dataclass
class SchemaBench:
    results: list[LayoutResult] = field(default_factory=list)

    @property
    def heuristic_solved(self) -> int:
        return sum(1 for r in self.results if r.heuristic_ok)

    @property
    def model_recovered(self) -> int:
        return sum(1 for r in self.results if r.recovered_by_model)

    @property
    def unsolved(self) -> int:
        return sum(
            1 for r in self.results if not r.heuristic_ok and not r.recovered_by_model
        )

    def to_dict(self) -> dict:
        return {
            "layouts": len(self.results),
            "heuristic_solved": self.heuristic_solved,
            "model_recovered": self.model_recovered,
            "unsolved": self.unsolved,
            "detail": [
                {
                    "layout": r.name,
                    "heuristic_missing": r.heuristic_missing,
                    "model_missing": r.model_missing,
                    "model_consulted": r.model_consulted,
                }
                for r in self.results
            ],
        }


def _missing(spec, kind: str = "bank_statement") -> list[str]:
    """What the parser still lacks, honouring the either/or requirements.

    Checking `REQUIRED_FIELDS` alone would report a statement with only a
    transaction date as unmappable, which is the bug this benchmark found in
    the first place.
    """
    columns = set(getattr(spec, "columns", spec) or {})
    missing = sorted(REQUIRED_FIELDS[kind] - columns)
    for alternatives in EITHER_FIELDS.get(kind, []):
        if not (alternatives & columns):
            missing.append(" or ".join(sorted(alternatives)))
    return sorted(missing)


def run_bench(client=None, kind: str = "bank_statement") -> SchemaBench:
    """Heuristic first, model only where the heuristic came up short.

    That ordering is the product's, not the benchmark's: the model is a
    fallback, so measuring it on layouts the alias table already handles would
    be measuring something the code never does.
    """
    from unnet.agents.mapper import ModelSchemaMapper

    bench = SchemaBench()
    mapper = ModelSchemaMapper(client) if client is not None else None

    for name, headers in LAYOUTS.items():
        result = LayoutResult(name=name, headers=list(headers))
        spec = heuristic_map(headers, kind)
        result.heuristic_missing = _missing(spec, kind)
        result.heuristic_wrong = _wrong_fields(getattr(spec, "columns", spec) or {}, name)

        if not result.heuristic_ok and mapper is not None:
            result.model_consulted = True
            proposal = mapper.propose(kind, list(headers), [SAMPLES[name]])
            if proposal is None:
                result.model_missing = ["<no proposal>"]
            else:
                result.model_missing = _missing(proposal, kind)
                result.model_wrong = _wrong_fields(proposal.columns, name)

        bench.results.append(result)
    return bench


def render_markdown(bench: SchemaBench) -> str:
    lines = ["## Schema mapping — where the alias table stops\n"]
    add = lines.append
    add(
        "Every bank names its columns differently, and an alias table can only "
        "recognise vocabulary somebody already wrote down. This is the one place "
        "in Unnet where the deterministic answer is structurally incomplete "
        "rather than merely fiddly, so it is the fairest available test of "
        "whether the model layer earns its place — and the one most able to "
        "embarrass it.\n"
    )
    add(
        "A layout counts as solved only when every field is on the *right* "
        "column, checked against a known-good mapping. Completeness alone is "
        "not enough: a mapping that fills every slot with the wrong header "
        "produces a ledger, and the ledger is wrong.\n"
    )
    add("| Bank layout | Alias table | Model fallback |")
    add("| --- | --- | --- |")
    for r in bench.results:
        if r.heuristic_ok:
            add(f"| {r.name} | mapped | not consulted |")
            continue
        short = ", ".join(f"`{m}`" for m in (r.heuristic_missing + r.heuristic_wrong))
        if r.recovered_by_model:
            add(f"| {r.name} | cannot resolve {short} | **mapped correctly** |")
        else:
            failed = ", ".join(
                f"`{m}`" for m in ((r.model_missing or []) + r.model_wrong)
            ) or "not consulted"
            add(f"| {r.name} | cannot resolve {short} | still {failed} |")
    add("")

    needed = len(bench.results) - bench.heuristic_solved
    add(
        f"**{bench.heuristic_solved} of {len(bench.results)} layouts need no "
        f"model at all.** That is the honest headline and it is why the model is "
        f"a fallback rather than the ingest path. It is consulted only for the "
        f"{needed} the alias table cannot resolve"
        + (
            f", and mapped {bench.model_recovered} of those correctly.\n"
            if needed
            else ".\n"
        )
    )
    if bench.model_recovered:
        add(
            "The column-coded dump is the case worth looking at: its headers are "
            "`C1`..`C6` and carry no information whatsoever, so no alias table of "
            "any length can resolve it. The mapping has to be inferred from the "
            "values, which is a different kind of task rather than a longer "
            "list — and is the one thing here a model does that a rule cannot.\n"
        )
    add(
        "The mapper proposes; it never applies. Every proposed spec is dry-run "
        "against real rows by `validate_spec`, and one that fails to parse is "
        "discarded in favour of the heuristic. The worst case is a wasted call.\n"
    )
    return "\n".join(lines)
