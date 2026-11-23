"""Review queue, worktrees and author checks. Local git repos only; nothing may be left behind."""
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import GraphPayload  # noqa: E402,F401  (models import must come first)
from app import history, llm_provider, pipeline, review_queue, storage  # noqa: E402

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
TEST = "from app import handler, lonely\n\n\ndef test_h():\n    assert handler() == 1\n\n\ndef test_l():\n    assert lonely() == 3\n"


def git(repo, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "q.db")
    monkeypatch.setattr(storage, "_local", threading.local())
    monkeypatch.setattr(storage, "_STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(llm_provider, "_active_provider", None, raising=False)
    from app import trace
    trace._cache.clear()
    history.incidents.cache_clear()
    r = tmp_path / "r"
    (r / "tests").mkdir(parents=True)
    (r / "app.py").write_text(SRC)
    (r / "tests" / "__init__.py").write_text("")
    (r / "tests" / "test_app.py").write_text(TEST)
    (r / "conftest.py").write_text("")
    git(r, "init", "-q", "-b", "main"); git(r, "add", "-A"); git(r, "commit", "-qm", "base")
    git(r, "checkout", "-qb", "feat/helper")
    (r / "app.py").write_text(SRC.replace("return 1", "return 11"))
    git(r, "commit", "-qam", "change helper")
    git(r, "checkout", "-qb", "chore/lonely", "main")
    (r / "app.py").write_text(SRC.replace("return 2", "return 22") + "\n\ndef extra():\n    return 3\n")
    git(r, "commit", "-qam", "change lonely")
    git(r, "checkout", "-q", "main")
    return r


def test_queue_lists_branches_and_scores_them_from_the_base_graph(repo):
    payload = pipeline.ingest(str(repo))
    refs = {b["ref"]: b for b in review_queue.branches(str(repo), "main")}
    assert set(refs) == {"feat/helper", "chore/lonely"} and refs["feat/helper"]["ahead"] == 1
    t = review_queue.triage(payload, "main", "feat/helper")
    assert t["worst"]["name"] == "helper" and t["estimate"] and t["added"] == 1 and t["removed"] == 1
    lone = review_queue.triage(payload, "main", "chore/lonely")
    assert lone["worst"]["name"] == "lonely" and lone["new_functions"] == 1    # `extra` is counted, not scored
    assert t["worst"]["score"] > lone["worst"]["score"]                         # helper has callers, lonely none


def test_worktrees_open_update_evict_and_leave_nothing_behind(repo, monkeypatch):
    monkeypatch.setattr(review_queue, "MAX_WORKTREES", 1)
    a = review_queue.open_worktree(str(repo), "feat/helper")
    assert (a / "app.py").read_text().count("return 11") == 1
    g = pipeline.ingest(str(a))
    assert storage.find_graph_by_repo_path(str(a)) == g.graph_id
    again = review_queue.open_worktree(str(repo), "feat/helper")               # reused, not re-created
    assert again == a
    b = review_queue.open_worktree(str(repo), "chore/lonely")                  # evicts the older one
    assert not a.exists() and b.exists() and storage.find_graph_by_repo_path(str(a)) is None
    review_queue.remove_worktree(str(repo), "chore/lonely")
    assert review_queue.worktrees(str(repo)) == [] and not b.exists()
    listed = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
    assert len(listed.strip().splitlines()) == 1                               # only the main checkout


def test_run_tests_reports_each_reaching_test(repo):
    got = review_queue.run_tests(str(repo), ["tests/test_app.py::test_h", "tests/test_app.py::test_l",
                                             "tests/test_app.py::test_missing", "--evil", "x.py; rm -rf /"])
    assert got["outcomes"] == {"tests/test_app.py::test_h": "passed", "tests/test_app.py::test_l": "failed",
                               "tests/test_app.py::test_missing": "not run"}    # bad ids never reach pytest
    assert not list(Path(repo).glob("**/*.xml"))                               # the junit file was a temp file
    assert review_queue.run_tests(str(repo), [])["outcomes"] == {}


def test_notes_round_trip_and_reject_bad_input(repo):
    data = review_queue.write_notes(str(repo), "abc", {"app.py::helper": {"hash": "h1", "note": "checked by hand",
                                                                          "tests": [{"id": "t", "outcome": "passed"}]}})
    assert review_queue.read_notes(str(repo)) == data and data["chunks"]["app.py::helper"]["head"] == "abc"
    with pytest.raises(ValueError):
        review_queue.write_notes(str(repo), "abc", {"no-separator": {"hash": "h"}})


def test_parts_groups_connected_changes():
    row = lambda i, callers: {"id": i, "name": i, "file": f"{i}.py", "hunks": [[1, 10]], "direct": [{"id": c} for c in callers]}  # noqa: E731
    got = review_queue.parts([row("a", ["x"]), row("b", ["x"]), row("c", ["a"]), row("d", [])])
    assert sorted(sorted(g["chunks"]) for g in got) == [["a", "b", "c"], ["d"]]   # shared caller x, c calls into a


def test_endpoints_run_reaching_tests_list_the_queue_and_open_a_branch(repo, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)   # never call the real API from a test
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    from app import ai_review
    monkeypatch.setattr(ai_review, "ENV_FILE", repo / "no.env")      # nor from the developer's .env
    from fastapi import HTTPException
    from app import main
    from app.models import QueueOpenRequest, ReviewAIRequest, ReviewTestsRequest
    payload = pipeline.ingest(str(repo))
    (repo / "app.py").write_text(SRC.replace("return 1", "return 1  # same"))
    out = main.review_change(payload.graph_id, "HEAD")
    (helper,) = out["functions"]
    assert "tests/test_app.py::test_h" in helper["impact"]["tests"]["run"] and out["parts"] and "ai_available" in out
    ran = main.review_tests(ReviewTestsRequest(graph_id=payload.graph_id, chunk_id=helper["id"]))
    assert ran["outcomes"] == {"tests/test_app.py::test_h": "passed"} and ran["hash"] == helper["hash"]
    q = main.review_queue_list(payload.graph_id, "main")
    assert [b["ref"] for b in q["branches"]][0] == "feat/helper"                 # worst first
    opened = main.review_queue_open(QueueOpenRequest(graph_id=payload.graph_id, ref="feat/helper"))
    full = main.review_change(opened["graph_id"], "main", committed=True)
    assert [r["name"] for r in full["functions"]] == ["helper"]
    main.review_queue_remove(payload.graph_id, "feat/helper")
    assert review_queue.worktrees(str(repo)) == []
    with pytest.raises(HTTPException):
        main.review_ai(ReviewAIRequest(graph_id=payload.graph_id, chunk_ids=[helper["id"]]))   # no key: refused


def test_a_test_run_that_cannot_start_says_why(repo, monkeypatch):
    monkeypatch.setenv("FLOWIFY_TEST_PYTHON", sys.executable)
    (repo / "tests" / "test_app.py").write_text("import not_a_module_xyz\n\ndef test_h():\n    pass\n")
    got = review_queue.run_tests(str(repo), ["tests/test_app.py::test_h"])
    assert got["error"].startswith("pytest could not run") and "not_a_module_xyz" in got["output"]
