"""PR impact: diff hunks -> changed functions -> upstream dependents."""
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import GraphPayload  # noqa: E402,F401  (models import must come first)
from app import llm_provider, pipeline, pr_impact, storage  # noqa: E402

SRC = textwrap.dedent('''
    def handler():
        return service()

    def service():
        return helper()

    def helper():
        return 1

    def lonely():
        return 2
''')


def git(repo, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(storage, "_local", threading.local())
    monkeypatch.setattr(llm_provider, "_active_provider", None, raising=False)
    r = tmp_path / "r"
    (r / "tests").mkdir(parents=True)
    (r / "app.py").write_text(SRC)
    (r / "tests" / "test_app.py").write_text("from app import handler\n\ndef test_h():\n    handler()\n")
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    git(r, "checkout", "-qb", "pr")
    (r / "app.py").write_text(SRC.replace("return 1", "return 11").replace("return 2", "return 22"))
    git(r, "commit", "-qam", "change")
    return r


def test_changed_functions_and_upstream(repo):
    files, deleted = pr_impact.changed_lines("main", str(repo))
    assert list(files) == ["app.py"] and deleted == []
    payload = pipeline.ingest(str(repo))
    rows = {r["name"]: r for r in pr_impact.analyze(payload, files, payload.graph_id)}
    assert set(rows) == {"helper", "lonely"}
    assert rows["helper"]["upstream"] >= 2          # service, handler (+ test)
    assert rows["helper"]["tested"] and not rows["lonely"]["tested"]
    out = pr_impact.review(payload, "main", committed=True)        # the PR path: base...HEAD, scored
    assert {r["name"] for r in out["functions"]} == {"helper", "lonely"} and out["has_tests"]
    md = pr_impact.render(out)
    assert "`helper`" in md and "**no**" in md                     # lonely: no test in its chain
    assert "ranked by risk" in md and "| # | Risk | Change |" in md and "Why these scores" in md
    assert all(f"**{r['score']} {r['band'].title()}**" in md for r in out["functions"])
    assert pr_impact.render({**out, "functions": [], "module_chunks": [], "deleted": [], "unmapped": []}).endswith("No functions changed.")


def test_review_endpoint_sees_uncommitted_edits(repo):
    from fastapi import HTTPException
    from app import main, trace
    trace._cache.clear()
    payload = pipeline.ingest(str(repo))
    assert main.review_change(payload.graph_id, "HEAD")["functions"] == []      # clean tree
    (repo / "app.py").write_text((repo / "app.py").read_text().replace("return 11", "return 12"))
    out = main.review_change(payload.graph_id, "HEAD")
    (row,) = out["functions"]
    assert row["name"] == "helper" and row["upstream"] >= 2 and row["risk"]
    assert row["hunks"] and row["line"] <= row["hunks"][0][0]                    # changed line is inside the function
    assert row["removed"] == [{"at": row["hunks"][0][0], "lines": ["    return 11"]}]  # the "before" side
    (p,) = out["pointers"]                                                       # service calls helper, was not edited
    assert (p["kind"], p["name"], p["targets"][0]["name"]) == ("not_updated", "service", "helper")
    assert p["line"] == 6 and p["rule"] and p["file_in_diff"] and not p["is_test"]  # checkable: app.py:6
    assert p["severity"] == row["risk"]                                          # a caller inherits its target's risk
    t = {"id": "t", "name": "test_a", "file": "tests/test_a.py", "line": 3, "rule": "r", "confidence": 0.9}
    (tp,) = pr_impact.pointers([{**row, "risk": "critical", "direct": [t], "tested": True, "endpoints": []}], set())
    assert tp["kind"] == "test" and "severity" not in tp                         # a test is not a "critical change"
    ranked = pr_impact.pointers([{**row, "id": "a", "risk": "low", "direct": [], "tested": False, "endpoints": []},
                                 {**row, "id": "b", "risk": "critical", "direct": [], "tested": False, "endpoints": ["e"]},
                                 {**row, "id": "c", "risk": "low", "direct": [], "tested": True, "endpoints": []}], set())
    assert [(q["id"], q["severity"]) for q in ranked] == [("b", "critical"), ("b", "critical"), ("a", "low")]
    full = main.review_change(payload.graph_id, "main")
    assert {r["name"] for r in full["functions"]} == {"helper", "lonely"}
    assert [(q["kind"], q["name"]) for q in full["pointers"]] == [("not_updated", "service"), ("untested", "lonely")]
    with pytest.raises(HTTPException):
        main.review_change(payload.graph_id, "--output=/tmp/x")                  # never passed to git as an option


@pytest.fixture
def svc_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "s.db")
    monkeypatch.setattr(storage, "_local", threading.local())
    monkeypatch.setattr(llm_provider, "_active_provider", None, raising=False)
    r = tmp_path / "r"
    for d in ("api", "core", "tests"):
        (r / d).mkdir(parents=True)
    (r / "core" / "limits.py").write_text("LIMIT = 10\nNAME = 'x'\n\n\ndef compute(n):\n    return n * 2\n")
    (r / "core" / "util.py").write_text("def helper(n):\n    return n + 1\n")
    (r / "api" / "routes.py").write_text(
        "from core.limits import LIMIT, compute\nfrom core.util import helper\n\n\n"
        "def handle(n):\n    return compute(n) + LIMIT\n\n\ndef other(n):\n    return helper(n)\n")
    (r / "tests" / "test_routes.py").write_text("from api.routes import handle\n\n\ndef test_handle():\n    handle(1)\n")
    git(r, "init", "-q", "-b", "main"); git(r, "add", "-A"); git(r, "commit", "-qm", "base")
    return r


def test_review_scores_chunks_and_catalogues_what_they_reach(svc_repo):
    from app import main, trace
    trace._cache.clear()
    payload = pipeline.ingest(str(svc_repo))
    (svc_repo / "flowify.services.json").write_text(
        '{"services": [{"name": "edge", "owner": "@web", "paths": ["api/**"]}, {"name": "domain", "paths": ["core/**"]}]}')
    (svc_repo / "core" / "limits.py").write_text(
        "LIMIT = 99\nNAME = 'x'\n\n\ndef compute(n, scale=1):\n    return n * 2 * scale\n")        # constant + signature
    (svc_repo / "core" / "util.py").write_text("def helper(n):\n    return n + 2\n")             # body only
    out = main.review_change(payload.graph_id, "HEAD")

    assert out["services_config"] == {"found": True, "error": None}
    scores = {r["name"]: r for r in out["functions"]}
    assert set(scores) == {"compute", "helper"}
    comp, helper = scores["compute"], scores["helper"]
    assert [r["score"] for r in out["functions"]] == sorted((r["score"] for r in out["functions"]), reverse=True)
    names = lambda r: {f["name"] for f in r["factors"]}                      # noqa: E731
    assert "coverage" in names(helper) and "coverage" not in names(comp)     # no test reaches helper, one reaches compute
    assert comp["risk"] == comp["band"]                                       # pointers use the same vocabulary
    assert any(f["name"] == "kind" and "signature" in f["why"] for f in comp["factors"])
    assert all(f["why"] for r in out["functions"] for f in r["factors"])

    cat = comp["impact"]
    assert cat["own_service"] == "domain" and cat["crosses"] == 1
    assert cat["services"][0]["name"] == "edge" and cat["services"][0]["owner"] == "@web"
    assert cat["tests"]["count"] == 1                                         # test_handle reaches it via handle

    (mod,) = out["module_chunks"]                                             # LIMIT changed outside any function
    assert mod["names"] == ["LIMIT"] and mod["file"] == "core/limits.py"
    assert mod["importers"] == ["api/routes.py"]                              # imports LIMIT by name
    assert mod["confidence"]["level"] == "low" and mod["score"] >= 30         # file-level evidence: floor, never "low"
    assert mod["impact"]["services"][0]["name"] == "edge"
    assert mod["hunks"] == [[1, 1]] and mod["removed"][0]["lines"] == ["LIMIT = 10"]
    assert mod["excerpt"]["start_line"] == 1 and mod["excerpt"]["code"].startswith("LIMIT = 99")   # what the UI shows
    (const,) = mod["findings"]
    assert const["kind"] == "constant" and "`10` to `99`" in const["message"] and "1 file import" in const["message"]
    assert comp["findings"] == []                         # `scale=1` added with a default: handle's compute(n) still binds
    assert all(f["max"] >= f["points"] for r in out["functions"] + [mod] for f in r["factors"])


def test_a_bad_services_file_is_reported_not_fatal(svc_repo):
    from app import main, trace
    trace._cache.clear()
    payload = pipeline.ingest(str(svc_repo))
    (svc_repo / "flowify.services.json").write_text("{oops")
    (svc_repo / "core" / "util.py").write_text("def helper(n):\n    return n + 3\n")
    out = main.review_change(payload.graph_id, "HEAD")
    assert out["services_config"]["found"] is False and "ignored" in out["services_config"]["error"]
    assert out["functions"][0]["impact"]["own_service"] == "core"             # fell back to directories
