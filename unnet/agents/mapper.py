"""Schema-mapping agent.

This is the clearest case in the system for using a model, and worth stating
plainly because the rest of the pipeline deliberately avoids one.

The alias table in ``unnet.ingest.mapping`` handles headers we have seen. It
cannot handle headers we have not: every merchant names their ledger columns
differently, and a bank in Coimbatore exports "Particulars / Withdrawal Amt. /
Deposit Amt." while the next one exports "Narration / Dr / Cr". Enumerating
those is not a rule you can finish writing — it is the open-ended
natural-language task that models are actually good at.

What keeps it safe is that the model never touches data. It proposes a mapping
from column names to canonical fields; the mapping is then dry-run against real
rows by :func:`~unnet.ingest.mapping.validate_spec`, and a proposal that does
not parse is discarded in favour of the heuristic. The worst case is a wasted
API call.
"""

from __future__ import annotations

import json

from unnet.ingest.mapping import (
    OPTIONAL_FIELDS,
    REQUIRED_FIELDS,
    MappingSpec,
)
from unnet.llm.provider import LLMClient, LLMUnavailable

SCHEMA = {
    "type": "object",
    "properties": {
        "columns": {
            "type": "object",
            "description": (
                "Canonical field name -> the exact column header in this file. "
                "Omit any canonical field the file does not contain."
            ),
        },
        "date_format": {
            "type": "string",
            "description": "Python strptime format for the date columns, e.g. %d/%m/%Y.",
        },
        "reasoning": {"type": "string"},
    },
    "required": ["columns"],
}

PROMPT = """\
You are mapping a CSV export onto a fixed reconciliation schema.

Source kind: {kind}

Canonical fields that MUST be mapped if the data is present:
{required}

Canonical fields that are optional:
{optional}

The file's column headers are:
{headers}

Here are the first rows, so you can tell columns apart by their contents rather
than by their names alone:
{sample}

Return a mapping from canonical field name to the EXACT header string as it
appears above. Rules:
- Use only headers from the list. Do not invent or reword them.
- Omit a canonical field entirely if this file has no column for it.
- Amounts may be strings with commas, currency symbols, or brackets for negatives.
- If dates share one format, give it as a Python strptime string.
- A bank statement's "value date" is when the money moved; prefer it over a
  posting or transaction date when both exist.
"""


class ModelSchemaMapper:
    """Proposes a :class:`MappingSpec`. Never applies one."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client
        self.attempts = 0
        self.proposals = 0

    def propose(
        self, kind: str, headers: list[str], sample_rows: list[dict]
    ) -> MappingSpec | None:
        self.attempts += 1
        prompt = PROMPT.format(
            kind=kind,
            required="\n".join(f"  - {f}" for f in sorted(REQUIRED_FIELDS.get(kind, set()))),
            optional="\n".join(f"  - {f}" for f in sorted(OPTIONAL_FIELDS.get(kind, set()))),
            headers="\n".join(f"  - {h!r}" for h in headers),
            sample=json.dumps(sample_rows[:3], indent=2, default=str),
        )

        try:
            response = self.client.complete("schema_mapping", prompt, SCHEMA)
        except LLMUnavailable:
            # No key, no cassette, or the breaker has tripped. The heuristic
            # mapping stands and the run continues.
            return None

        columns = response.data.get("columns") or {}
        if not isinstance(columns, dict):
            return None

        # Discard anything naming a header the file does not have, before the
        # spec is validated, so a partly-hallucinated mapping degrades to a
        # partly-correct one rather than being thrown out whole.
        header_set = set(headers)
        cleaned = {
            field: header
            for field, header in columns.items()
            if isinstance(header, str) and header in header_set
        }
        if not cleaned:
            return None

        self.proposals += 1
        return MappingSpec(
            source_kind=kind,
            columns=cleaned,
            date_format=response.data.get("date_format") or None,
            produced_by=f"model:{response.decider_ref}",
            confidence=800,
            notes=str(response.data.get("reasoning", ""))[:500],
        )
