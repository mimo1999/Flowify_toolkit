"""Risk score and impacted-services catalogue for a change chunk."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import FunctionNode  # noqa: E402,F401  (models import must come first)
from app import change_risk as cr  # noqa: E402


def _names(r):
    return {f["name"] for f in r["factors"]}


def test_score_is_the_sum_of_named_factors_and_banded():
    quiet = cr.score_chunk(upstream=0, has_tests=True, tested=True, new_code=True, lines_changed=3)
    assert quiet["band"] == "low" and quiet["score"] == sum(f["points"] for f in quiet["factors"])

    big = cr.score_chunk(upstream=500, endpoints=4, has_tests=True, tested=False, header_changed=True,
                         lines_changed=60, importance=0.99, services_crossed=3)
    assert big["band"] == "critical" and big["score"] <= 100
    assert {"reach", "exposure", "coverage", "kind", "size", "centrality", "crossings"} <= _names(big)
    assert all(f["why"] for f in big["factors"])               # every point can be explained to a reviewer


def test_more_reach_and_exposure_never_lower_the_score():
    base = dict(has_tests=True, tested=True, lines_changed=5)
    scores = [cr.score_chunk(upstream=n, **base)["score"] for n in (0, 1, 10, 100, 1000)]
    assert scores == sorted(scores) and scores[-1] > scores[0]
    assert cr.score_chunk(upstream=10, endpoints=1, **base)["score"] > cr.score_chunk(upstream=10, **base)["score"]
    assert cr.score_chunk(upstream=10, endpoints=4, **base)["score"] > cr.score_chunk(upstream=10, endpoints=1, **base)["score"]
    # a new function whose `def` line is "changed" is new code, not a signature change
    new = cr.score_chunk(upstream=3, header_changed=True, new_code=True)
    assert [f["why"] for f in new["factors"] if f["name"] == "kind"] == ["new code, nothing depended on it before"]


def test_no_tests_in_the_repo_is_not_penalised_as_untested():
    assert "coverage" not in _names(cr.score_chunk(upstream=5, has_tests=False, tested=False))
    assert "coverage" in _names(cr.score_chunk(upstream=5, has_tests=True, tested=False))


def test_thin_evidence_raises_a_floor_instead_of_rating_low():
    r = cr.score_chunk(upstream=0, name_shared=400, has_tests=True, tested=True, new_code=True)
    assert r["confidence"]["level"] == "low" and r["score"] == cr.FLOOR and r["band"] == "medium"
    assert "uncertainty floor" in _names(r) and r["confidence"]["notes"]
    # confidence never reduces a score that is already high
    hi = cr.score_chunk(upstream=500, endpoints=2, name_shared=400, has_tests=True, tested=False)
    assert hi["score"] > cr.FLOOR and hi["confidence"]["level"] == "low"
    assert cr.score_chunk(upstream=3, possible=2)["confidence"]["level"] == "medium"
    assert cr.score_chunk(upstream=3)["confidence"]["level"] == "high"


def test_module_level_chunks_are_always_low_confidence():
    r = cr.score_chunk(upstream=9, module_level=True, has_tests=True, tested=True, lines_changed=1)
    assert r["confidence"]["level"] == "low" and any("file-level" in n for n in r["confidence"]["notes"])
    assert any("files import it" in f["why"] for f in r["factors"])


def test_service_config_globs_first_then_a_fallback_that_splits_the_code(tmp_path):
    is_test = lambda p: p.startswith("tests/")  # noqa: E731
    paths = ["pkg/core/a.py", "pkg/net/b.py", "pkg/net/deep/c.py", "tests/test_a.py", "setup.py"]
    # no config: the single top-level package is split one level down; tests do not count
    fallback = cr.service_resolver(paths, [], is_test)
    assert fallback("pkg/core/a.py") == ("pkg/core", None) and fallback("pkg/net/deep/c.py") == ("pkg/net", None)
    assert fallback("setup.py") == (".", None)

    (tmp_path / cr.CONFIG_NAME).write_text(json.dumps({"services": [
        {"name": "network", "owner": "@net", "paths": ["pkg/net/**"]},
        {"name": "everything-else", "paths": ["pkg/**"]}]}))
    config, err = cr.load_services(str(tmp_path))
    assert err is None
    resolve = cr.service_resolver(paths, config, is_test)
    assert resolve("pkg/net/deep/c.py") == ("network", "@net")       # `**` crosses directories; first match wins
    assert resolve("pkg/core/a.py") == ("everything-else", None)

    (tmp_path / cr.CONFIG_NAME).write_text("{not json")
    assert cr.load_services(str(tmp_path))[0] == [] and "ignored" in cr.load_services(str(tmp_path))[1]
    (tmp_path / cr.CONFIG_NAME).write_text(json.dumps({"services": [{"name": "x", "paths": []}]}))
    assert "ignored" in cr.load_services(str(tmp_path))[1]
    assert cr.load_services(str(tmp_path / "nowhere")) == ([], None)


def _node(i, path, kind=None, name=None):
    return SimpleNamespace(id=i, file_path=path, name=name or i.split("::")[-1], qualified_name=None, kind="function",
                           adapter_metadata={"semantic_kind": kind} if kind else {})


def test_catalogue_groups_dependents_by_service_and_lists_what_they_reach():
    nodes = {n.id: n for n in [
        _node("pkg/api/x.py::handler", "pkg/api/x.py", "EXPOSES_API", "handler"),
        _node("pkg/api/x.py::other", "pkg/api/x.py"),
        _node("pkg/db/y.py::save", "pkg/db/y.py", "USES_DB"),
        _node("tests/test_x.py::test_h", "tests/test_x.py"),
        _node("pkg/core/z.py::target", "pkg/core/z.py"),
    ]}
    resolve = cr.service_resolver([n.file_path for n in nodes.values()], [], lambda p: p.startswith("tests/"))
    cat = cr.catalogue({"pkg/api/x.py::handler", "pkg/api/x.py::other", "pkg/db/y.py::save", "tests/test_x.py::test_h"},
                       nodes, resolve, lambda p: p.startswith("tests/"), "pkg/core/z.py")
    assert cat["own_service"] == "pkg/core" and cat["crosses"] == 2
    assert [(s["name"], s["dependents"]) for s in cat["services"]] == [("pkg/api", 2), ("pkg/db", 1)]
    assert cat["endpoints"] == {"count": 1, "examples": [{"name": "handler", "file": "pkg/api/x.py"}]}
    assert cat["db"]["count"] == 1 and cat["tests"] == {"count": 1, "examples": ["test_h"], "run": ["tests/test_x.py::test_h"]}   # tests are not "services"; ids to run


def test_contract_and_history_factors_and_the_clamp():
    brk = [{"severity": "breaks"}, {"severity": "check"}]
    s = cr.score_chunk(upstream=500, endpoints=9, has_tests=True, tested=False, header_changed=True,
                       lines_changed=80, importance=0.99, services_crossed=5, findings=brk,
                       incidents={"count": 3, "latest": ["abc123", "Revert x"]})
    f = {x["name"]: x for x in s["factors"]}
    assert f["contract"]["points"] == 10 and "1 static check" in f["contract"]["why"]
    assert f["history"]["points"] == 8 and "abc123" in f["history"]["why"]
    assert s["score"] == 100                                                     # maxima sum past 100: clamped
    only_check = cr.score_chunk(upstream=1, findings=[{"severity": "check"}], incidents={"count": 1, "latest": ["a", "b"]})
    g = {x["name"]: x["points"] for x in only_check["factors"]}
    assert g["contract"] == 4 and g["history"] == 4
    assert "contract" not in {x["name"] for x in cr.score_chunk(upstream=1, findings=[])["factors"]}


def test_a_changes_top_three_are_always_must_check():
    from app import pr_impact
    rows = [{"name": n, "score": s, "band": cr.band(s), "findings": []} for n, s in
            [("a", 30), ("b", 31), ("c", 29), ("d", 30), ("e", 45)]]
    pr_impact.mark_top(rows)
    assert {r["name"] for r in rows if pr_impact.bucket(r) == "must"} == {"e", "b", "a"}   # ties broken by name
    assert cr.band(40) == "high" and cr.band(60) == "critical" and cr.band(39) == "medium"
