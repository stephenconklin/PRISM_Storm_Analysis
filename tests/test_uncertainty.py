"""Uncertainty components and the bounding range (pure functions)."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import uncertainty as u  # noqa: E402


def test_gauge_component():
    et = {"all": {"n": 88, "pbias_pct": -4.0}, "independent": {"n": 7, "pbias_pct": 0.5}}
    c = u.gauge_component(et)
    assert c["high_pct"] == pytest.approx((1 / 0.96 - 1) * 100, abs=1e-3)   # gauges imply more water
    assert c["low_pct"] == pytest.approx((1 / 1.005 - 1) * 100, abs=1e-3)
    few = u.gauge_component({"all": {"n": 40, "pbias_pct": -4.0}, "independent": {"n": 3, "pbias_pct": 10.0}})
    assert few["low_pct"] == 0 and "only 3 independent" in few["detail"]   # too few independent gauges
    assert not u.gauge_component(None)["assessed"]


def test_boundary_and_revision_components():
    b = u.boundary_component({"centers": 100.0, "weighted": 101.0, "touched": 105.0}, 100.0)
    assert (b["low_pct"], b["high_pct"]) == (0.0, 1.0)  # touched is not used
    rc = {"change_pct": -0.72, "revised": [(dt.date(2026, 10, 26), "1", "2")]}
    r = u.revision_component(rc, [2, 2, 8])
    assert (r["low_pct"], r["high_pct"]) == (-0.72, 0.72) and "2 of 3 days not final (release 2)" in r["detail"]
    none_yet = u.revision_component({"change_pct": 0.0, "revised": []}, [2, 2])
    assert not none_yet["assessed"]
    assert u.revision_component({"change_pct": 0.0, "revised": []}, [8, 8])["assessed"]


def test_bounding_range_adds_components():
    comps = [u.component("a", -0.5, 4.2, ""), u.component("b", -0.7, 0.7, ""), u.product_component()]
    assert u.bounding_range(comps) == pytest.approx((-1.2, 4.9))
    lo, hi = u.volume_range(1000.0, comps)
    assert (lo, hi) == (pytest.approx(988.0), pytest.approx(1049.0))
    lines = u.report(4.685e9, comps)
    assert lines[0].startswith("Uncertainty: 4.685 km³, range 4.629-4.915 km³") and "not assessed" in lines[-1]
