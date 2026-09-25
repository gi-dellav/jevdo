"""Eval toml loading + no-exec run + CLI --eval mode."""

import textwrap

import pytest

from jevdo.dispatcher import PlannedAction, PlanOutcome
from jevdo.eval import EvalCase, EvalError, load_eval, run_eval


def _write(tmp_path, body: str, name: str = "eval.toml") -> str:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return str(p)


BASE = """\
[[test]]
name = "status"
input = "show git status"
expected = "git status"

[[test]]
name = "brief history"
input = "brief git history"
expected = "git log --oneline"
"""


def test_load_ok_with_defaults(tmp_path):
    cfg = load_eval(_write(tmp_path, BASE))
    assert len(cfg) == 2
    assert cfg[0] == EvalCase(name="status", input="show git status",
                              expected=("git", "status"), expected_str="git status",
                              expect_abstain=False, cwd=None, min_confidence=None)
    assert cfg[1].expected == ("git", "log", "--oneline")


def test_load_auto_names_and_meta_defaults(tmp_path):
    body = """\
[meta]
cwd = "proj"
min_confidence = 0.7

[[test]]
input = "run tests"
expected = "pytest -q"
"""
    (cases,) = load_eval(_write(tmp_path, body))
    assert cases.name == "test-1"
    assert cases.cwd == "proj"
    assert cases.min_confidence == 0.7


def test_load_abstain_case(tmp_path):
    body = """\
[[test]]
name = "weird"
input = "do something unknown"
expect_abstain = true
"""
    (case,) = load_eval(_write(tmp_path, body))
    assert case.expect_abstain is True and case.expected is None


def test_load_quoted_expected_parses_like_shell(tmp_path):
    body = """\
[[test]]
input = "search history"
expected = 'git log --grep="foo bar"'
"""
    (case,) = load_eval(_write(tmp_path, body))
    assert case.expected == ("git", "log", "--grep=foo bar")


def test_load_errors(tmp_path):
    with pytest.raises(EvalError, match="not found"):
        load_eval("/nonexistent/eval.toml")
    with pytest.raises(EvalError, match="at least one"):
        load_eval(_write(tmp_path, "[meta]\ncwd = '.'\n"))
    with pytest.raises(EvalError, match="'input' must be"):
        load_eval(_write(tmp_path, '[[test]]\nexpected = "git status"\n'))
    with pytest.raises(EvalError, match="'expected' must be"):
        load_eval(_write(tmp_path, '[[test]]\ninput = "x"\n'))
    with pytest.raises(EvalError, match="must be omitted"):
        load_eval(_write(tmp_path, '[[test]]\ninput = "x"\nexpected = "git status"\nexpect_abstain = true\n'))
    with pytest.raises(EvalError, match="duplicate test name"):
        load_eval(_write(tmp_path, BASE + '[[test]]\nname = "status"\ninput = "x"\nexpected = "y"\n'))
    with pytest.raises(EvalError, match="unknown key"):
        load_eval(_write(tmp_path, '[[test]]\ninput = "x"\nexpected = "y"\nbogus = 1\n'))
    with pytest.raises(EvalError, match="must be a number in"):
        load_eval(_write(tmp_path, '[[test]]\ninput = "x"\nexpected = "y"\nmin_confidence = 2\n'))
    with pytest.raises(EvalError, match="not parseable"):
        load_eval(_write(tmp_path, '[[test]]\ninput = "x"\nexpected = "unclosed \\"quote"\n'))


def _outcome(action=None, ok=True, reason="ok", conf=0.9):
    return PlanOutcome(ok=ok, action=action, reason=reason, confidence=conf)


def test_run_eval_pass_fail_abstain(config, workdir, monkeypatch):
    import jevdo.dispatcher as disp

    def fake_dispatch(cfg, request, cwd=".", **kw):
        if request == "show git status":
            a = PlannedAction(command="git", subcommand="status", confidence=0.9, risk="read")
            return _outcome(a), None, None, None
        if request == "weird request":
            return _outcome(None, ok=False, reason="no known command", conf=0.95), None, None, None
        a = PlannedAction(command="pytest", subcommand=None, confidence=0.9, risk="read")
        return _outcome(a), None, None, None

    monkeypatch.setattr(disp, "dispatch", fake_dispatch)
    cases = [
        EvalCase(name="ok", input="show git status", expected=("git", "status"),
                 expected_str="git status"),
        EvalCase(name="mismatch", input="run tests", expected=("git", "status"),
                 expected_str="git status"),
        EvalCase(name="abstain-ok", input="weird request", expected=None,
                 expected_str=None, expect_abstain=True),
        EvalCase(name="abstain-missed", input="show git status", expected=None,
                 expected_str=None, expect_abstain=True),
    ]
    summary = run_eval(config, cases, workdir)
    assert summary.total == 4 and summary.passed == 2 and not summary.ok
    by_name = {r.case.name: r for r in summary.results}
    assert by_name["ok"].passed and by_name["ok"].actual == ("git", "status")
    assert not by_name["mismatch"].passed and "got 'pytest -q'" in by_name["mismatch"].reason
    assert by_name["abstain-ok"].passed and by_name["abstain-ok"].actual is None
    assert not by_name["abstain-missed"].passed and "expected abstention" in by_name["abstain-missed"].reason


def test_run_eval_never_executes(config, workdir, monkeypatch):
    """Even for a planned action, run_eval must not touch subprocess."""
    import subprocess

    import jevdo.dispatcher as disp

    def boom(*a, **k):
        raise AssertionError("must not execute")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr("jevdo.executor.execute", boom)

    def fake_dispatch(cfg, request, cwd=".", **kw):
        a = PlannedAction(command="pytest", subcommand=None, confidence=0.99, risk="read")
        return _outcome(a), None, None, None

    monkeypatch.setattr(disp, "dispatch", fake_dispatch)
    cases = [EvalCase(name="t", input="run tests", expected=("pytest", "-q"),
                      expected_str="pytest -q")]
    summary = run_eval(config, cases, workdir)
    assert summary.ok  # resolved via resolve_argv only, no subprocess


def test_run_eval_refusal_is_failure(config, workdir, monkeypatch):
    import jevdo.dispatcher as disp

    def fake_dispatch(cfg, request, cwd=".", **kw):
        # hallucinated flag => resolve_argv refuses
        a = PlannedAction(command="git", subcommand="status", flags=("--evil",),
                          confidence=0.9, risk="read")
        return _outcome(a), None, None, None

    monkeypatch.setattr(disp, "dispatch", fake_dispatch)
    cases = [EvalCase(name="t", input="x", expected=("git", "status"),
                      expected_str="git status")]
    summary = run_eval(config, cases, workdir)
    assert not summary.ok and "refused" in summary.results[0].reason


def test_run_eval_per_case_cwd_and_threshold(config, tmp_path, monkeypatch):
    import jevdo.dispatcher as disp

    seen = {}

    def fake_dispatch(cfg, request, cwd=".", **kw):
        seen["cwd"] = cwd
        seen["min_confidence"] = kw.get("min_confidence")
        a = PlannedAction(command="pytest", subcommand=None, confidence=0.9, risk="read")
        return _outcome(a), None, None, None

    monkeypatch.setattr(disp, "dispatch", fake_dispatch)
    (tmp_path / "sub").mkdir()
    cases = [EvalCase(name="t", input="run tests", expected=("pytest", "-q"),
                      expected_str="pytest -q", cwd="sub", min_confidence=0.77)]
    summary = run_eval(config, cases, str(tmp_path))
    assert summary.ok
    assert seen["cwd"].endswith("sub") and seen["min_confidence"] == 0.77


def test_cli_eval_mode_exits(monkeypatch, tmp_path, config, capsys):
    from jevdo import cli

    env = _write(tmp_path, '[meta]\nmodel = "jev-latest"\n' + """\
[[command]]
name = "pytest"
description = "tests"
argv = ["pytest", "-q"]
""", name="environment.toml")
    ev = _write(tmp_path, BASE.replace("git status", "pytest -q").replace(
        "git log --oneline", "pytest -q"))
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")

    import jevdo.dispatcher as disp

    def fake_dispatch(cfg, request, cwd=".", **kw):
        a = PlannedAction(command="pytest", subcommand=None, confidence=0.99, risk="read")
        return _outcome(a), None, None, None

    monkeypatch.setattr(disp, "dispatch", fake_dispatch)
    rc = cli.main(["--env", env, "--cwd", str(tmp_path), "--eval", ev])
    assert rc == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "2/2 passed" in out

    # one mismatch => EXIT_EVAL_FAIL, and eval needs no positional request
    ev2 = _write(tmp_path, '[[test]]\ninput = "run tests"\nexpected = "git status"\n',
                 name="eval2.toml")
    rc = cli.main(["--env", env, "--cwd", str(tmp_path), "--eval", ev2])
    assert rc == cli.EXIT_EVAL_FAIL


def test_cli_eval_never_executes_or_prompts(monkeypatch, tmp_path, capsys):
    from jevdo import cli

    env = _write(tmp_path, '[meta]\nmodel = "jev-latest"\n' + """\
[[command]]
name = "pytest"
description = "tests"
argv = ["pytest", "-q"]
""", name="environment.toml")
    ev = _write(tmp_path, '[[test]]\ninput = "run tests"\nexpected = "pytest -q"\n')
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")

    import jevdo.dispatcher as disp

    def fake_dispatch(cfg, request, cwd=".", **kw):
        a = PlannedAction(command="pytest", subcommand=None, confidence=0.99,
                          risk="destructive")  # would prompt/execute if not eval
        return _outcome(a), None, None, None

    monkeypatch.setattr(disp, "dispatch", fake_dispatch)
    monkeypatch.setattr("jevdo.cli.execute",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no exec")))
    monkeypatch.setattr("jevdo.cli.confirm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt")))
    rc = cli.main(["--env", env, "--cwd", str(tmp_path), "--eval", ev])
    assert rc == cli.EXIT_OK


def test_cli_eval_bad_file_is_config_error(monkeypatch, tmp_path, capsys):
    from jevdo import cli

    env = _write(tmp_path, '[meta]\nmodel = "jev-latest"\n' + """\
[[command]]
name = "pytest"
description = "tests"
argv = ["pytest", "-q"]
""", name="environment.toml")
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    rc = cli.main(["--env", env, "--eval", str(tmp_path / "missing.toml")])
    assert rc == cli.EXIT_CONFIG
