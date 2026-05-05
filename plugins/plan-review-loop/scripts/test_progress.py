#!/usr/bin/env python3
"""Tests for the convergence-detection logic in `_lib.runner`.

Run directly:

    python3 scripts/test_progress.py

The plan-review loop exits via convergence (Jaccard similarity of
finding-IDs across iterations), not a hard iteration cap. These tests
exercise the boundary between "converging", "drift_warning",
"diverging", and the safety cap so the loop can't regress into a
runaway again.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib.runner import (  # noqa: E402
    _CONVERGENCE_MIN_OVERLAP,
    _DRIFT_STREAK_TO_EXIT,
    _assess_progress,
    _finding_fingerprint,
)


def F(severity: str, title: str, fid: str | None = None) -> dict:
    """Helper: build a finding with a stable fingerprint."""
    f = {"severity": severity, "title": title}
    if fid:
        f["id"] = fid
    return f


class AssessProgressTests(unittest.TestCase):
    def test_clean_returns_clean(self) -> None:
        status, overlap, streak = _assess_progress(2, [], [F("P0", "x", "x")], 0)
        self.assertEqual(status, "clean")
        self.assertEqual(overlap, 1.0)
        self.assertEqual(streak, 0)

    def test_first_iteration_skipped(self) -> None:
        # No previous findings to compare against.
        status, _, streak = _assess_progress(1, [F("P0", "a")], [], 0)
        self.assertEqual(status, "first_iteration")
        self.assertEqual(streak, 0)

    def test_full_overlap_converging(self) -> None:
        prev = [F("P0", "issue-a", "a"), F("P1", "issue-b", "b")]
        curr = [F("P0", "issue-a", "a"), F("P1", "issue-b", "b")]
        status, overlap, streak = _assess_progress(3, curr, prev, 0)
        self.assertEqual(status, "converging")
        self.assertEqual(overlap, 1.0)
        self.assertEqual(streak, 0, "converging must reset drift streak")

    def test_partial_overlap_at_threshold_converging(self) -> None:
        # 2 same + 1 new = jaccard 2/3 ≈ 0.67 ≥ 0.5
        prev = [F("P0", "a", "a"), F("P1", "b", "b")]
        curr = [F("P0", "a", "a"), F("P1", "b", "b"), F("P2", "c", "c")]
        status, overlap, streak = _assess_progress(3, curr, prev, 0)
        self.assertEqual(status, "converging")
        self.assertGreaterEqual(overlap, _CONVERGENCE_MIN_OVERLAP)
        self.assertEqual(streak, 0)

    def test_low_overlap_first_round_warns_only(self) -> None:
        # 1 same + 2 new previous + 2 new current = jaccard 1/5 = 0.2
        prev = [F("P0", "a", "a"), F("P1", "b", "b"), F("P1", "c", "c")]
        curr = [F("P0", "a", "a"), F("P2", "d", "d"), F("P2", "e", "e")]
        status, overlap, streak = _assess_progress(3, curr, prev, 0)
        self.assertEqual(status, "drift_warning")
        self.assertLess(overlap, _CONVERGENCE_MIN_OVERLAP)
        self.assertEqual(streak, 1)

    def test_low_overlap_repeated_diverges(self) -> None:
        prev = [F("P0", "a", "a"), F("P1", "b", "b")]
        curr = [F("P2", "x", "x"), F("P2", "y", "y")]
        # incoming drift_streak=1 (already had one drift round). Second
        # consecutive drift round must trip divergence at default
        # _DRIFT_STREAK_TO_EXIT=2.
        status, overlap, streak = _assess_progress(4, curr, prev, 1)
        self.assertEqual(status, "diverging")
        self.assertLess(overlap, _CONVERGENCE_MIN_OVERLAP)
        self.assertEqual(streak, _DRIFT_STREAK_TO_EXIT)

    def test_drift_streak_counts_only_consecutive(self) -> None:
        # A converging round between two drifts must reset the streak.
        prev = [F("P0", "a", "a"), F("P1", "b", "b")]
        same = [F("P0", "a", "a"), F("P1", "b", "b")]
        # Round 1: full overlap, resets streak to 0.
        _, _, s1 = _assess_progress(3, same, prev, 1)
        self.assertEqual(s1, 0)
        # Round 2: drift_warning (streak 1) — NOT diverging yet.
        new_curr = [F("P2", "z", "z")]
        status, _, s2 = _assess_progress(4, new_curr, same, s1)
        self.assertEqual(status, "drift_warning")
        self.assertEqual(s2, 1)

    def test_fingerprint_falls_back_to_synthesis(self) -> None:
        # When the reviewer doesn't supply an id, the fingerprint must
        # still match across iterations on identical (severity, title).
        a = {"severity": "P0", "title": "Same title"}
        b = {"severity": "P0", "title": "Same title"}
        self.assertEqual(_finding_fingerprint(a), _finding_fingerprint(b))

    def test_fingerprint_id_overrides_synthesis(self) -> None:
        # Reviewer-supplied id wins so reword-but-same-id is still convergent.
        a = {"severity": "P0", "title": "Same title", "id": "abc"}
        b = {"severity": "P1", "title": "totally different wording", "id": "abc"}
        self.assertEqual(_finding_fingerprint(a), _finding_fingerprint(b))


if __name__ == "__main__":
    unittest.main(verbosity=2)
