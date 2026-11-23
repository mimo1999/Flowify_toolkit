"""Contract checks: what a change breaks for code that did not change with it."""
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import GraphPayload  # noqa: E402,F401  (models import must come first)
from app import history, llm_provider, pipeline, pr_impact, storage  # noqa: E402

LIB = textwrap.dedent('''
    from os.path import join


    def pair(a, b):
        return a + b


    def need(a):
        return a


    def wait(a, timeout=30):
        return a, timeout


    def both(a, b):
        return a + b


    def dup():
        return 1


    class Base:
        def run(self):
            return 1


    class Child(Base):
        pass
''')

USE = textwrap.dedent('''
    from lib import pair, need, wait, both, dup, join, Child


    def use(xs):
        pair(1, 2)
        pair(*xs)
        need(1)
        wait(1)
        both(1, 2)
        dup()
        join("a", "b")
        return Child().run()
''')


def git(repo, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def changed(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "c.db")
    monkeypatch.setattr(storage, "_local", threading.local())
    monkeypatch.setattr(llm_provider, "_active_provider", None, raising=False)
    from app import trace
    trace._cache.clear()
    history.incidents.cache_clear()
    r = tmp_path / "r"
    r.mkdir()
    (r / "lib.py").write_text(LIB)
    (r / "use.py").write_text(USE)
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    (r / "lib.py").write_text(LIB
                              .replace("def pair(a, b):\n    return a + b", "def pair(a):\n    return a")
                              .replace("def need(a):", "def need(a, b):")
                              .replace("timeout=30", "timeout=5")
                              .replace("def both(a, b):\n    return a + b", "def both(a, b):\n    return a")
                              .replace("class Child(Base):\n    pass", "class Child(Base):\n    def run(self):\n        return 2")
                              + "\n\ndef dup():\n    return 2\n\n\ndef join(*parts):\n    return '/'.join(parts)\n")
    payload = pipeline.ingest(str(r))
    out = pr_impact.review(payload, "HEAD")
    rows = {}
    for c in out["functions"]:
        rows.setdefault(c["name"], []).append(c)
    return out, rows


def kinds(rows, name):
    return {(f["kind"], f["severity"]) for c in rows.get(name, []) for f in c["findings"]}


def test_a_dropped_parameter_breaks_a_caller_that_still_passes_it(changed):
    _, rows = changed
    (f,) = [f for c in rows["pair"] for f in c["findings"]]
    assert f["severity"] == "breaks" and "2 positional" in f["message"]
    assert f["evidence"][0]["file"] == "use.py" and f["evidence"][0]["code"] == "pair(1, 2)"   # the proof
    assert all(f["evidence"][0]["code"] != "pair(*xs)" for c in rows["pair"] for f in c["findings"])  # cannot tell


def test_a_new_required_parameter_breaks_callers(changed):
    _, rows = changed
    assert kinds(rows, "need") == {("signature", "breaks")}
    assert "`b`" in rows["need"][0]["findings"][0]["message"]


def test_a_changed_default_names_the_callers_relying_on_it(changed):
    _, rows = changed
    (f,) = rows["wait"][0]["findings"]
    assert f["kind"] == "default" and "30" in f["message"] and "5" in f["message"] and "1 caller" in f["message"]


def test_a_parameter_no_longer_used_is_flagged(changed):
    _, rows = changed
    assert kinds(rows, "both") == {("unused_param", "check")}


def test_new_definitions_that_take_over_an_existing_name(changed):
    _, rows = changed
    assert ("redefinition", "breaks") in kinds(rows, "dup")                 # the later def wins every call
    assert ("shadows_import", "breaks") in kinds(rows, "join")               # os.path.join is gone in this file
    assert ("override", "check") in kinds(rows, "Child.run")                 # Base.run is overridden


def test_findings_add_a_named_factor_and_a_hash(changed):
    out, rows = changed
    pair = rows["pair"][0]
    assert any(f["name"] == "contract" and f["points"] == 10 for f in pair["factors"])
    assert len(pair["hash"]) == 16 and out["head"]
    assert all(c["score"] <= 100 for c in out["functions"])
    assert {c["name"] for c in pair["not_updated"]} == {"use"}               # its caller was not changed with it


def test_chunk_hash_changes_with_the_code_and_only_then():
    c = {"file": "a.py", "name": "f", "hunks": [[1, 1]], "removed": [{"at": 1, "lines": ["x = 1"]}]}
    h = pr_impact._chunk_hash(["x = 2"], c)
    assert h == pr_impact._chunk_hash(["x = 2", "unrelated"], c)
    assert h != pr_impact._chunk_hash(["x = 3"], c)


def test_old_side_ranges():
    diff = "--- a/m.py\n+++ b/m.py\n@@ -3,2 +3,0 @@\n-a\n-b\n@@ -10 +9,4 @@\n-c\n+d\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x\n"
    assert pr_impact.old_ranges(diff) == {"m.py": [(3, 4), (10, 10)]}


def test_incident_history_counts_revert_and_hotfix_commits(tmp_path):
    history.incidents.cache_clear()
    r = tmp_path / "h"
    r.mkdir()
    (r / "a.py").write_text("x = 1\n")
    (r / "b.py").write_text("y = 1\n")
    git(r, "init", "-q", "-b", "main"); git(r, "add", "-A"); git(r, "commit", "-qm", "base")
    for i, msg in enumerate(["Hotfix: crash on empty batch", "Revert \"speed up a\"", "refactor a"]):
        (r / "a.py").write_text(f"x = {i + 2}\n")
        git(r, "commit", "-qam", msg)
    got = history.incidents(str(r), "k")
    assert got["a.py"]["count"] == 2 and "b.py" not in got
    assert got["a.py"]["latest"][1].startswith("Revert")
