"""Schema mapping: the one place a rule is structurally, not just practically, short.

These tests exist to stop the benchmark flattering the model. Two failure modes
have already happened here and both are covered:

* the response schema declared `columns` as a bare object, so Gemini had nothing
  to populate and returned `{}` every time — the capability was inert for as
  long as it existed, and nothing failed loudly because the heuristic fallback
  is correct;
* the benchmark scored "no required field missing" rather than "every field on
  the right column", and passed a mapping that had put the narration in the
  reference column.
"""

from __future__ import annotations

import pytest

from unnet.evaluation.schema_bench import (
    EXPECTED,
    LAYOUTS,
    SAMPLES,
    _wrong_fields,
    run_bench,
)
from unnet.ingest.mapping import EITHER_FIELDS, REQUIRED_FIELDS, heuristic_map


def test_every_layout_has_a_known_good_mapping_over_its_own_headers():
    """A benchmark whose answer key is wrong scores nothing at all."""
    for name, headers in LAYOUTS.items():
        expected = EXPECTED[name]
        assert expected, f"{name} has no expected mapping"
        unknown = sorted(set(expected.values()) - set(headers))
        assert not unknown, f"{name} expects headers it does not have: {unknown}"


def test_sample_rows_are_aligned_with_their_own_headers():
    """The first version of this fixture was shifted, and the model said so."""
    for name, headers in LAYOUTS.items():
        sample = SAMPLES[name]
        assert set(sample) <= set(headers)
        narration_header = EXPECTED[name]["narration"]
        assert "NEFT" in sample[narration_header], f"{name}: narration is not narration"


def test_the_alias_table_handles_most_real_layouts_unaided():
    """The honest headline, asserted so it cannot quietly stop being true.

    If a future change breaks the deterministic path, the model would silently
    start "winning" this benchmark, which would be a regression dressed as a
    result.
    """
    bench = run_bench(None)
    assert bench.heuristic_solved >= 6, (
        "the alias table should carry the common layouts on its own; "
        "a drop here makes the model look better by making the rules worse"
    )


def test_a_mapping_on_the_wrong_column_is_not_a_success():
    swapped = dict(EXPECTED["Terse export"])
    swapped["narration"], swapped["bank_ref"] = swapped["bank_ref"], swapped["narration"]

    wrong = _wrong_fields(swapped, "Terse export")

    assert wrong == ["bank_ref", "narration"]


def test_opaque_headers_defeat_the_alias_table():
    """The boundary the model exists for. If a rule could do it, it should."""
    headers = LAYOUTS["Column-coded dump"]
    spec = heuristic_map(headers, "bank_statement")
    mapped = set(getattr(spec, "columns", spec) or {})

    assert REQUIRED_FIELDS["bank_statement"] - mapped, (
        "headers carrying no words should be unmappable by a vocabulary table"
    )


def test_a_statement_with_one_date_column_is_accepted():
    """Axis and plenty of others ship a single date; the loader always coped.

    Requiring `value_date` by name meant validation rejected a file the parser
    could read, so Unnet refused a real bank format on a technicality about
    which of two synonymous columns was present.
    """
    spec = heuristic_map(LAYOUTS["Axis-style"], "bank_statement")
    mapped = set(getattr(spec, "columns", spec) or {})

    assert "value_date" not in mapped, "this layout genuinely has no value date"
    assert not (REQUIRED_FIELDS["bank_statement"] - mapped)
    assert any(alt & mapped for alt in EITHER_FIELDS["bank_statement"])


@pytest.mark.skipif(
    not __import__("pathlib").Path("data/cassettes/schema_mapping").exists(),
    reason="run `make record` to capture the schema-mapping cassettes",
)
def test_the_model_recovers_what_the_alias_table_cannot():
    """Replayed from committed cassettes, so this is a recorded model result."""
    from unnet.llm.provider import build_client

    bench = run_bench(build_client(provider="offline"))
    consulted = [r for r in bench.results if r.model_consulted]

    assert consulted, "the benchmark should still contain layouts rules cannot do"
    for result in consulted:
        assert result.recovered_by_model, (
            f"{result.name}: model left {result.model_missing} missing "
            f"and {result.model_wrong} on the wrong column"
        )
