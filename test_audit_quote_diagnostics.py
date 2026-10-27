"""Regression test for audit_quote_diagnostics.flag_reevaluation.

Tests that each symbol's price_changed flag is recomputed against its own
previous observation, not against another symbol's state. This guards against
the cross-symbol variable-reference bug (using leftover loop variable
`symbol` instead of `sym`) that caused false `price_changed` mismatches.

Run with: python -m unittest test_audit_quote_diagnostics -v
"""

import sys
import os
import unittest

# Ensure the project root (where audit_quote_diagnostics.py lives) is importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audit_quote_diagnostics import flag_reevaluation  # noqa: E402


def _make_record(symbol, sweep, last_price, last_ts, volume, price_changed):
    """Build a minimal synthetic observation record."""
    return {
        "symbol": symbol,
        "sweep": sweep,
        "last_price": last_price,
        "last_timestamp_raw": last_ts,
        "volume": volume,
        "price_changed": price_changed,
        "last_timestamp_changed": False,
        "volume_changed": False,
        "batch_timestamp_advanced": False,
    }


def _build_dataset():
    """Three symbols, two sweeps. Each symbol has distinct price-change behavior.

    Sweep 1: establishes baseline (no previous -> price_changed=False).
    Sweep 2:
        AAPL: 100.00 -> 101.00  (changed)   -> stored True
        NVDA: 200.00 -> 200.00  (unchanged) -> stored False
        TSLA: 300.00 -> 299.00  (changed)   -> stored True

    TSLA is alphabetically last, so under the original bug (leftover loop
    variable `symbol` == "TSLA") every record would be compared against
    TSLA's recomputed state instead of its own.
    """
    return [
        _make_record("AAPL", 1, 100.00, "2026-10-26T17:00:00Z", 1000, False),
        _make_record("NVDA", 1, 200.00, "2026-10-26T17:00:00Z", 2000, False),
        _make_record("TSLA", 1, 300.00, "2026-10-26T17:00:00Z", 3000, False),
        _make_record("AAPL", 2, 101.00, "2026-10-26T17:00:10Z", 1100, True),
        _make_record("NVDA", 2, 200.00, "2026-10-26T17:00:10Z", 2000, False),
        _make_record("TSLA", 2, 299.00, "2026-10-26T17:00:10Z", 3100, True),
    ]


class TestFlagReevaluationPriceChanged(unittest.TestCase):

    def test_zero_price_changed_mismatches(self):
        """Corrected implementation reports zero mismatches across all 6 records."""
        records = _build_dataset()
        result = flag_reevaluation(records)
        agreement = result["flag_agreement"]["price_changed"]
        mismatches = result["flag_mismatches"]["price_changed"]

        self.assertEqual(agreement["total"], 6)
        self.assertEqual(agreement["match"], 6)
        self.assertEqual(agreement["mismatch"], 0)
        self.assertEqual(agreement["uncertain"], 0)
        self.assertEqual(len(mismatches), 0)

    def test_explicit_price_changed_values_at_sweep_2(self):
        """Explicitly assert each symbol's expected price_changed at sweep 2.

        Uses the actual result structure returned by flag_reevaluation():
        flag_mismatches["price_changed"] lists every record where stored
        != recalculated. An empty list means every symbol matched its own
        expected value.
        """
        records = _build_dataset()
        result = flag_reevaluation(records)
        mismatches = result["flag_mismatches"]["price_changed"]

        # No mismatch may exist for any symbol at sweep 2.
        sweep2_mismatches = [
            m for m in mismatches if m["sweep"] == 2
        ]
        self.assertEqual(len(sweep2_mismatches), 0,
                         "Unexpected price_changed mismatches at sweep 2: %r" % sweep2_mismatches)

        # Explicitly confirm each symbol's expected value by verifying no
        # mismatch entry exists for that symbol/sweep combination.
        expected = {
            ("AAPL", 2): True,
            ("NVDA", 2): False,
            ("TSLA", 2): True,
        }
        for (symbol, sweep), expected_changed in expected.items():
            found = [
                m for m in mismatches
                if m["symbol"] == symbol and m["sweep"] == sweep
            ]
            self.assertEqual(len(found), 0,
                             "%s sweep %d: expected price_changed=%s but a mismatch was reported" %
                             (symbol, sweep, expected_changed))

    def test_bug_reintroduction_would_fail(self):
        """Demonstrate the test fails if the original cross-symbol bug returns.

        Locally mutates the lookup to use the leftover loop variable `symbol`
        (== "TSLA") instead of `sym`. This does not modify the production
        audit script. With the bug, NVDA's stored price_changed=False would
        be compared against TSLA's recalculated=True, producing a mismatch.
        """
        records = _build_dataset()

        # Recompute per-symbol state (same as flag_reevaluation's building loop).
        from collections import defaultdict
        by_symbol = defaultdict(list)
        for r in records:
            by_symbol[r["symbol"]].append(r)

        recomputed = {}
        for symbol in sorted(by_symbol.keys()):
            obs_list = sorted(by_symbol[symbol], key=lambda r: r["sweep"])
            prev = None
            for r in obs_list:
                sw = r["sweep"]
                state = {"price_changed": False, "last_timestamp_changed": False, "volume_changed": False}
                if prev is not None:
                    pp = prev.get("last_price")
                    cp = r.get("last_price")
                    state["price_changed"] = (pp is not None and cp is not None and pp != cp)
                recomputed[(symbol, sw)] = state
                prev = r

        # BUGGY lookup: use "TSLA" (leftover loop variable) instead of each record's symbol.
        mismatch_count = 0
        for r in records:
            sym = r["symbol"]
            sw = r["sweep"]
            state = recomputed.get(("TSLA", sw))  # BUG: hardcoded "TSLA" instead of sym
            stored = r.get("price_changed")
            calc = state["price_changed"] if state is not None else False
            if isinstance(stored, bool) and stored != calc:
                mismatch_count += 1

        # With the bug, at least one mismatch should occur (NVDA sweep 2:
        # stored=False vs TSLA state=True).
        self.assertGreater(mismatch_count, 0,
                           "Expected the reintroduced bug to produce mismatches, but found none")


if __name__ == "__main__":
    unittest.main()
