"""Resolver tiers added from runtime evidence (see benchmarks/accuracy)."""
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import FunctionNode  # noqa: E402,F401  (models import must come first)
from app.graph_builder import build_function_graph  # noqa: E402


def _calls(tmp_path, source):
    (tmp_path / "m.py").write_text(textwrap.dedent(source))
    _, _, edges = build_function_graph(str(tmp_path))
    return {(e.source_id, e.target_id): e.adapter_metadata for e in edges if e.relationship == "INVOKES"}


def test_constructor_calls_resolve_to_init(tmp_path):
    calls = _calls(tmp_path, """
        class Repo:
            def __init__(self): pass
        def run():
            return Repo()
    """)
    meta = calls[("m.py::run", "m.py::Repo.__init__")]
    assert meta["resolution_reasoning"] == "constructor call of class Repo"


def test_unique_method_resolves_but_builtin_names_and_duplicates_do_not(tmp_path):
    calls = _calls(tmp_path, """
        class Repo:
            def save(self): pass
            def get(self): pass
        class A:
            def go(self): pass
        class B:
            def go(self): pass
        def run(repo, r):
            repo.save()  # only one `save` in the repo, and the name says it is a Repo
            r.save()     # same method, but nothing ties `r` to Repo: not guessed
            repo.get()   # `get` is a dict method name: too likely to be something else
            repo.go()    # two candidates: ambiguous, not guessed
    """)
    targets = {t for (s, t) in calls if s == "m.py::run"}
    assert targets == {"m.py::Repo.save"}


def test_local_variable_from_a_constructor_types_its_calls(tmp_path):
    calls = _calls(tmp_path, """
        class A:
            def go(self): pass
            def forward(self, x): pass
        class B:
            def go(self): pass
        def run():
            a = A()
            a.go()      # two `go` in the repo, but `a` is an A
            a(1)        # instance call runs A.forward
            b = A()
            b = B()     # reassigned to another class: type unknown, not guessed
            b.go()
    """)
    targets = {t for (s, t) in calls if s == "m.py::run"}
    assert {"m.py::A.go", "m.py::A.forward"} <= targets
    assert "m.py::B.go" not in targets


def test_method_call_on_an_object_is_not_the_repos_only_function_of_that_name(tmp_path):
    calls = _calls(tmp_path, """
        def update(): pass
        def run(seen):
            seen.update([1])     # a set method, not the function above
    """)
    assert not [t for (s, t) in calls if s == "m.py::run"]


def test_nested_defs_keep_their_own_calls_and_resolve_by_scope(tmp_path):
    calls = _calls(tmp_path, """
        def target(): pass
        def outer():
            def inner():
                target()
            inner()
        class C:
            def other(self): pass
            def m(self):
                def cb():
                    self.other()     # `self` is still C inside the closure
                cb()
    """)
    assert ("m.py::outer", "m.py::target") not in calls            # only inner calls it
    assert {("m.py::outer.inner", "m.py::target"), ("m.py::outer", "m.py::outer.inner"),
            ("m.py::C.m", "m.py::C.m.cb"), ("m.py::C.m.cb", "m.py::C.other")} <= set(calls)


def test_imports_base_classes_and_module_level_calls(tmp_path):
    (tmp_path / "a.py").write_text("def run(): pass\n")
    (tmp_path / "b.py").write_text("def run(): pass\n")
    calls = _calls(tmp_path, """
        from a import run
        class Base:
            def shared(self): pass
        class Other:
            def shared(self): pass
        class Child(Base):
            def go(self):
                self.shared()    # defined on Base, not on Other
            @property
            def x(self): return 1
            @x.setter
            def x(self, v): run()    # the setter's call, not the getter's
        def main():
            run()                # two files define run; the import says which
        main()                   # module-level call
    """)
    assert {("m.py::Child.go", "m.py::Base.shared"), ("m.py::main", "a.py::run"),
            ("m.py::<file>", "m.py::main")} <= set(calls)
    assert [s for (s, t) in calls if t == "a.py::run" and "Child.x" in s] == ["m.py::Child.x#13"]


def test_unique_method_needs_a_receiver_that_names_the_class(tmp_path):
    calls = _calls(tmp_path, """
        class RequestDeduplicator:
            def execute(self, q): pass
        class CircuitBreaker:
            def can_attempt(self): pass
        class _Visitor:
            def emit(self): pass
        def run(db, circuit_breaker):
            db.execute("select 1")          # a sqlite connection, not a RequestDeduplicator
            circuit_breaker.can_attempt()   # the name says which class
            v = _Visitor()
            v.emit()                        # typed by its own assignment, underscore class included
    """)
    targets = {t for (s, t) in calls if s == "m.py::run"}
    assert "m.py::RequestDeduplicator.execute" not in targets
    assert {"m.py::CircuitBreaker.can_attempt", "m.py::_Visitor.emit"} <= targets


def test_a_redefined_function_routes_callers_to_the_live_last_definition(tmp_path):
    (tmp_path / "m.py").write_text("def cdiv(a, b):\n    return -(a // -b)\n\n\ndef cdiv(a, b):\n    return (a + b) // b\n\n\ndef same_file():\n    return cdiv(4, 2)\n")
    (tmp_path / "n.py").write_text("from m import cdiv\n\n\ndef other():\n    return cdiv(4, 2)\n")
    _, _, edges = build_function_graph(str(tmp_path))
    targets = {(e.source_id, e.target_id) for e in edges if e.relationship == "INVOKES"}
    assert ("m.py::same_file", "m.py::cdiv#5") in targets    # the second def, which is the one that runs
    assert ("n.py::other", "m.py::cdiv#5") in targets
    assert not [t for t in targets if t[1] == "m.py::cdiv"]  # nothing reaches the shadowed first def
