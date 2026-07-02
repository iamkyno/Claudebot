"""Sniper scalp engine — pure exit-logic tests (no threads, no I/O)."""

import pytest

from bot.scalp_engine import ScalpEngine


class TestTrailDistance:
    def test_trail_is_60pct_of_edge(self):
        # Mirrors the scalper's own bracket geometry: SL = 0.6x target edge.
        assert ScalpEngine.trail_distance(100.0, 101.0) == pytest.approx(0.6)

    def test_symmetric_for_shorts(self):
        assert ScalpEngine.trail_distance(100.0, 99.0) == pytest.approx(0.6)


class TestBracketExit:
    def test_long_exits_at_stop_or_tp(self):
        assert ScalpEngine.bracket_exit("buy", 99.0, stop=99.5, tp=101.0)
        assert ScalpEngine.bracket_exit("buy", 101.2, stop=99.5, tp=101.0)
        assert not ScalpEngine.bracket_exit("buy", 100.2, stop=99.5, tp=101.0)

    def test_short_exits_inverted(self):
        assert ScalpEngine.bracket_exit("sell", 100.6, stop=100.5, tp=99.0)
        assert ScalpEngine.bracket_exit("sell", 98.9, stop=100.5, tp=99.0)
        assert not ScalpEngine.bracket_exit("sell", 99.8, stop=100.5, tp=99.0)

    def test_missing_bracket_never_exits(self):
        assert not ScalpEngine.bracket_exit("buy", 50.0, stop=None, tp=None)
