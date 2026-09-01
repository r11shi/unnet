"""Money arithmetic. Every one of these is a real recon break if it regresses."""

from __future__ import annotations

from decimal import Decimal

import pytest

from unnet.core.money import (
    apply_bps,
    format_inr,
    gst_on,
    paise_to_rupees,
    round_half_up,
    rupees_to_paise,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1234.50", 123450),
        ("1,234.50", 123450),          # thousands separators
        ("12,34,567.89", 123456789),   # Indian grouping
        ("(1234.50)", -123450),        # accountancy negatives
        ("-1234.50", -123450),
        ("Rs. 99", 9900),
        ("₹ 1,000.05", 100005),
        ("500.00 Cr", 50000),          # bank Dr/Cr suffixes
        ("500.00 Dr", 50000),
        ("", 0),
        ("0", 0),
        ("0.01", 1),                   # one paisa survives
    ],
)
def test_parses_the_shapes_real_exports_actually_contain(raw, expected):
    assert rupees_to_paise(raw) == expected


def test_no_float_drift_on_the_classic_case():
    """0.1 + 0.2 != 0.3 is not an acceptable failure mode in a ledger."""
    assert rupees_to_paise("0.10") + rupees_to_paise("0.20") == rupees_to_paise("0.30")


def test_a_thousand_small_amounts_sum_exactly():
    total = sum(rupees_to_paise("0.07") for _ in range(1000))
    assert total == 7000
    assert paise_to_rupees(total) == Decimal("70.00")


@pytest.mark.parametrize(
    "paise,expected",
    [
        (123456789, "₹12,34,567.89"),
        (100000, "₹1,000.00"),
        (99, "₹0.99"),
        (0, "₹0.00"),
        (-4321050, "-₹43,210.50"),
        (10000000000, "₹10,00,00,000.00"),  # ten crore
    ],
)
def test_indian_digit_grouping(paise, expected):
    assert format_inr(paise) == expected


def test_gst_is_eighteen_percent_of_the_fee_not_the_transaction():
    # ₹20 MDR -> ₹3.60 GST
    assert gst_on(2000) == 360


def test_gst_rounds_half_up_to_the_paisa():
    # 1 paisa of MDR gives 0.18 paise of GST, which must round to 0, not to 1.
    assert gst_on(1) == 0
    # 3 paise gives 0.54, which rounds to 1.
    assert gst_on(3) == 1


def test_mdr_from_a_basis_point_rate():
    # 2% of ₹1,000.00
    assert apply_bps(100000, 200) == 2000
    # UPI is zero-rated
    assert apply_bps(100000, 0) == 0


def test_round_half_up_does_not_use_bankers_rounding():
    """Python's round() sends 2.5 to 2. A payment gateway does not."""
    assert round_half_up(5, 2) == 3
    assert round_half_up(7, 2) == 4
    assert round(2.5) == 2  # the behaviour we are deliberately not using


def test_round_half_up_is_symmetric_about_zero():
    assert round_half_up(-5, 2) == -3
    assert round_half_up(5, 2) == 3


def test_round_half_up_rejects_a_zero_denominator():
    with pytest.raises(ValueError):
        round_half_up(1, 0)


def test_the_full_fee_chain_reconciles():
    """gross - (mdr + gst) is what the merchant should actually receive."""
    gross = 100000  # ₹1,000
    mdr = apply_bps(gross, 200)
    gst = gst_on(mdr)
    net = gross - (mdr + gst)
    assert mdr == 2000
    assert gst == 360
    assert net == 97640
    assert format_inr(net) == "₹976.40"
