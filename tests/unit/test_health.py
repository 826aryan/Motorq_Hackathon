from shared.health import run_checks


def _ok():
    pass


def _fail():
    raise ConnectionError("down")


def test_all_deps_ok_reports_ok():
    body = run_checks(["a", "b"], checks={"a": _ok, "b": _ok})
    assert body["status"] == "ok"
    assert body["deps"] == {"a": "ok", "b": "ok"}


def test_one_failing_dep_reports_degraded_without_raising():
    body = run_checks(["a", "b"], checks={"a": _ok, "b": _fail})
    assert body["status"] == "degraded"
    assert body["deps"]["b"] == "error: ConnectionError"


def test_no_deps_is_ok():
    assert run_checks([], checks={})["status"] == "ok"
