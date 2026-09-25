"""Risk, thresholds, valued flags, multi-slot planning, confirm, sequence."""

import pytest

from jevdo.config import (
    Command, EnvConfig, Flag, PathSlot, Subcommand,
    effective_threshold, node_risk,
)
from jevdo.dispatcher import (
    PlannedAction, _clip_history, continue_requested, dispatch,
    dispatch_sequence, plan_from_answers,
)
from conftest import FakeChoice, FakeNoul, sample_config


def _cmd(choice="git", conf=0.9):
    return {"__command__": FakeChoice(choice, {choice: 0.9, "none_of_the_above": 0.1}, conf)}


def _rich_config(**over):
    cmds = (
        Command(name="git", description="vcs", argv=None, risk="read", subcommands=(
            Subcommand(name="add", description="stage",
                       argv=("git", "add", "{path:src}"),
                       paths=(PathSlot(name="src", kind="files"),),
                       risk="write"),
            Subcommand(name="reset", description="nuke",
                       argv=("git", "reset", "--hard"), risk="destructive"),
        )),
        Command(name="ls", description="list", argv=("ls", "--fmt", "{value}", "{path:out}"),
                risk="read",
                flags=(Flag("--fmt", "fmt", ("--fmt={value}",),
                            values=("json", "plain"),
                            value_descriptions={"json": "machine"}),),
                paths=(PathSlot(name="out", kind="dirs", optional=True),)),
    )
    kw = dict(model="jev-latest", min_confidence=0.5, timeout=60.0, commands=cmds,
              risk_thresholds={"read": 0.5, "write": 0.7, "destructive": 0.9})
    kw.update(over)
    return EnvConfig(**kw)


@pytest.fixture
def rich():
    return _rich_config()


def _rich_ctx():
    return {"candidates": {(("git.add", "src")): ["a.txt"],
                           "git.add": ["a.txt"],
                           (("ls", "out")): ["d"],
                           "ls": ["d"]},
            "cwd_files": ["a.txt"], "cwd_dirs": ["d"]}


def test_risk_inheritance_and_threshold_resolution():
    cfg = sample_config()
    git = cfg.get_command("git")
    assert node_risk(cfg, git, None) == "read"  # meta default
    cfg2 = sample_config(default_risk="write")
    assert node_risk(cfg2, git, None) == "write"
    bar, src = effective_threshold(cfg2, git, None, "write")
    assert bar == 0.5 and src == "global"
    cfg3 = _rich_config()
    g3 = cfg3.get_command("git")
    add = [s for s in g3.subcommands if s.name == "add"][0]
    bar, src = effective_threshold(cfg3, g3, add, "write")
    assert bar == 0.7 and src == "risk write"
    cfg4 = _rich_config()
    cmds = list(cfg4.commands)
    assert effective_threshold(cfg4, cmds[0], add, "write", cli_override=0.1) == (0.1, "cli --min-confidence")


def test_write_below_risk_threshold_abstains(rich):
    ans = (_cmd("git", 0.6)
           | {"__subcommand__:git": FakeChoice("add", {"add": 0.9}, 0.6),
              "path.git.add.src": FakeChoice("a.txt", {"a.txt": 0.9}, 0.6)})
    out = plan_from_answers(rich, ans, _rich_ctx())
    assert not out.ok and "0.70 (risk write)" in out.reason


def test_multislot_and_valued_flag_planning(rich):
    ans = ({"__command__": FakeChoice("ls", {"ls": 0.95}, 0.95)}
           | {"flag.ls.--fmt": FakeNoul(0.9),
              "flagval.ls.--fmt": FakeChoice("json", {"json": 0.9}, 0.92),
              "path_stated.ls.out": FakeNoul(0.9),
              "path.ls.out": FakeChoice("d", {"d": 0.9}, 0.93)})
    out = plan_from_answers(rich, ans, _rich_ctx())
    assert out.ok, out.reason
    assert out.action.flag_values == {"--fmt": "json"}
    assert out.action.paths == {"out": "d"}
    assert out.action.risk == "read"


def test_valued_flag_none_means_off(rich):
    ans = ({"__command__": FakeChoice("ls", {"ls": 0.95}, 0.95)}
           | {"flag.ls.--fmt": FakeNoul(0.9),
              "flagval.ls.--fmt": FakeChoice("none_of_the_above", {"none_of_the_above": 0.9}, 0.9),
              "path_stated.ls.out": FakeNoul(0.05)})
    out = plan_from_answers(rich, ans, _rich_ctx())
    assert out.ok and out.action.flags == () and out.action.describe() == "ls"


def test_confirm_accept_decline(monkeypatch):
    from jevdo import cli
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    a = PlannedAction(command="git", subcommand="add", flags=(), paths={"src": "a"},
                      confidence=0.8, risk="write")
    assert cli.confirm(a, ["git", "add", "a"]) is True
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    assert cli.confirm(a, ["git", "add", "a"]) is False

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.confirm(a, ["git", "add", "a"]) is False  # fail-closed


def test_sequence_stops_on_error():
    cfg = sample_config()
    calls = {"n": 0}

    class FakeResp:
        answers = {"__command__": FakeChoice("pytest", {"pytest": 1.0}, 0.99)}

    class FakeClient:
        def system_one(self, **kw): return FakeResp()

    def boom(action, cwd):
        calls["n"] += 1
        return (["pytest", "-q"], 1, "", "fail")

    steps, reason = dispatch_sequence(cfg, "run tests", ".", client=FakeClient(),
                                      execute_fn=boom, max_steps=3)
    assert len(steps) == 1 and "exited 1" in reason and calls["n"] == 1


def test_sequence_max_steps():
    cfg = sample_config()
    class FakeResp:
        answers = {"__command__": FakeChoice("pytest", {"pytest": 1.0}, 0.99),
                   "__continue__": FakeNoul(0.9)}

    class FakeClient:
        def system_one(self, **kw): return FakeResp()

    def ok_exec(action, cwd):
        return (["pytest", "-q"], 0, "ok", "")

    steps, reason = dispatch_sequence(cfg, "run tests", ".", client=FakeClient(),
                                      execute_fn=ok_exec, max_steps=2)
    assert len(steps) == 2 and reason == "max_steps reached"


def test_sequence_stops_when_jev_declines_to_continue():
    cfg = sample_config()
    calls = {"n": 0}

    class FakeResp:
        answers = {"__command__": FakeChoice("pytest", {"pytest": 1.0}, 0.99),
                   "__continue__": FakeNoul(0.1)}

    class FakeClient:
        def system_one(self, **kw): return FakeResp()

    def ok_exec(action, cwd):
        calls["n"] += 1
        return (["pytest", "-q"], 0, "ok", "")

    steps, reason = dispatch_sequence(cfg, "run tests", ".", client=FakeClient(),
                                      execute_fn=ok_exec, max_steps=5)
    assert len(steps) == 1 and "Jev ended" in reason and calls["n"] == 1


def test_sequence_missing_continue_gate_stops():
    cfg = sample_config()

    class FakeResp:
        answers = {"__command__": FakeChoice("pytest", {"pytest": 1.0}, 0.99)}

    class FakeClient:
        def system_one(self, **kw): return FakeResp()

    def ok_exec(action, cwd):
        return (["pytest", "-q"], 0, "ok", "")

    steps, reason = dispatch_sequence(cfg, "run tests", ".", client=FakeClient(),
                                      execute_fn=ok_exec, max_steps=4)
    assert len(steps) == 1 and "Jev ended" in reason  # fail-closed


def test_sequence_max_steps_one_never_asks_continue():
    cfg = sample_config()
    seen = {}

    class FakeResp:
        answers = {"__command__": FakeChoice("pytest", {"pytest": 1.0}, 0.99)}

    class FakeClient:
        def system_one(self, **kw):
            seen["questions"] = kw.get("questions", {})
            return FakeResp()

    def ok_exec(action, cwd):
        return (["pytest", "-q"], 0, "ok", "")

    steps, reason = dispatch_sequence(cfg, "run tests", ".", client=FakeClient(),
                                      execute_fn=ok_exec, max_steps=1)
    assert len(steps) == 1 and reason == "max_steps reached"
    assert "__continue__" not in seen["questions"]


def test_clip_history():
    hist = [{"step": i} for i in range(1, 6)]
    assert _clip_history(hist, None) == hist
    assert _clip_history(hist, 2) == hist[-2:]
    assert _clip_history(hist, 0) == []
    assert _clip_history([], 3) == []


def test_continue_requested_fail_closed():
    class Resp:
        def __init__(self, answers): self.answers = answers

    assert continue_requested(Resp({"__continue__": FakeNoul(0.9)})) is True
    assert continue_requested(Resp({"__continue__": FakeNoul(0.49)})) is False
    assert continue_requested(Resp({})) is False
    assert continue_requested(Resp(None)) is False


def test_dispatch_clips_history_and_asks_continue(config, tmp_path):
    seen = {}

    class Resp:
        answers = {"__command__": FakeChoice("pytest", {"pytest": 1.0}, 0.99)}

    class FakeClient:
        def system_one(self, **kw):
            seen["state"] = kw["state"]
            seen["questions"] = kw["questions"]
            return Resp()

    hist = [{"step": i} for i in range(1, 6)]
    dispatch(config, "run tests", str(tmp_path), client=FakeClient(),
             history=hist, max_steps=4, max_history=2, allow_continue=True)
    assert seen["state"]["history"] == hist[-2:]
    assert "__continue__" in seen["questions"]

    dispatch(config, "run tests", str(tmp_path), client=FakeClient(),
             max_steps=1)
    assert "history" not in seen["state"]
    assert "__continue__" not in seen["questions"]


def _cli_args(*argv):
    from jevdo import cli
    return cli.build_parser().parse_args(list(argv))


def test_cli_run_sequence_stops_when_jev_declines(monkeypatch, config):
    from jevdo import cli
    from jevdo.dispatcher import PlanOutcome
    from jevdo.executor import ExecutionResult

    calls = {"n": 0}
    answers = iter([True, False])

    def fake_dispatch(cfg, request, cwd=".", **kw):
        a = PlannedAction(command="pytest", subcommand=None, confidence=0.9,
                          risk="read")
        return PlanOutcome(True, a, "ok", 0.9), {}, {}, object()

    monkeypatch.setattr(cli, "dispatch", fake_dispatch)
    monkeypatch.setattr(cli, "continue_requested", lambda resp: next(answers))

    def fake_exec(cfg, action, cwd):
        calls["n"] += 1
        return ExecutionResult(argv=["pytest", "-q"], returncode=0,
                               stdout="", stderr="")

    monkeypatch.setattr(cli, "execute", fake_exec)
    args = _cli_args("run tests", "--max-steps", "5")
    rc = cli.run_sequence(config, args, 5)
    assert rc == cli.EXIT_OK and calls["n"] == 2


def test_cli_max_steps_one_never_asks_continue(monkeypatch, config):
    from jevdo import cli
    from jevdo.dispatcher import PlanOutcome
    from jevdo.executor import ExecutionResult

    def fake_dispatch(cfg, request, cwd=".", **kw):
        a = PlannedAction(command="pytest", subcommand=None, confidence=0.9,
                          risk="read")
        return PlanOutcome(True, a, "ok", 0.9), {}, {}, object()

    def boom(resp):
        raise AssertionError("must not ask continue at max-steps=1")

    monkeypatch.setattr(cli, "dispatch", fake_dispatch)
    monkeypatch.setattr(cli, "continue_requested", boom)
    monkeypatch.setattr(cli, "execute",
                        lambda cfg, a, cwd: ExecutionResult(
                            argv=["pytest", "-q"], returncode=0,
                            stdout="", stderr=""))
    args = _cli_args("run tests", "--max-steps", "1")
    assert cli.run_sequence(config, args, 1) == cli.EXIT_OK


def test_dispatch_passes_temperature(config, tmp_path):
    seen = {}

    class Resp:
        answers = {"__command__": FakeChoice("pytest", {"pytest": 1.0}, 0.99)}

    class FakeClient:
        def system_one(self, **kw):
            seen.clear()
            seen.update(kw)
            return Resp()

    dispatch(config, "run tests", str(tmp_path), client=FakeClient(),
             temperature=0.3)
    assert seen["extra_body"] == {"temperature": 0.3}

    dispatch(config, "run tests", str(tmp_path), client=FakeClient())
    assert seen["extra_body"] is None

    import dataclasses
    cfg = dataclasses.replace(config, temperature=0.15)
    dispatch(cfg, "run tests", str(tmp_path), client=FakeClient())
    assert seen["extra_body"] == {"temperature": 0.15}


def test_sequence_logs_plan_and_result(tmp_path):
    import json

    from jevdo.runlog import JsonlLogger

    cfg = sample_config()

    class Resp:
        answers = {"__command__": FakeChoice("pytest", {"pytest": 1.0}, 0.99),
                   "__continue__": FakeNoul(0.9)}

    class FakeClient:
        def system_one(self, **kw): return Resp()

    def ok_exec(action, cwd):
        return (["pytest", "-q"], 0, "out", "")

    path = tmp_path / "run.jsonl"
    with JsonlLogger(str(path)) as log:
        steps, reason = dispatch_sequence(
            cfg, "run tests", ".", client=FakeClient(), execute_fn=ok_exec,
            max_steps=2, logger=log)
    assert len(steps) == 2
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [e["type"] for e in events] == ["plan", "result", "plan", "result"]
    assert events[0]["continue_asked"] is True
    assert events[0]["continue"] == 0.9
    assert events[2]["continue_asked"] is False
    assert events[1]["argv"] == ["pytest", "-q"]
    assert events[1]["returncode"] == 0 and events[1]["stdout"] == "out"


def test_cli_single_run_writes_jsonl_log(monkeypatch, tmp_path):
    import json

    from jevdo import cli
    from jevdo.dispatcher import PlanOutcome
    from jevdo.executor import ExecutionResult

    env = tmp_path / "environment.toml"
    env.write_text('[meta]\nmodel = "jev-latest"\n' + """\
[[command]]
name = "pytest"
description = "tests"
argv = ["pytest", "-q"]
""")
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")

    def fake_dispatch(cfg, request, cwd=".", **kw):
        a = PlannedAction(command="pytest", subcommand=None, confidence=0.9,
                          risk="read")
        return PlanOutcome(True, a, "ok", 0.9), {}, {"cwd_files": [], "cwd_dirs": []}, object()

    monkeypatch.setattr(cli, "dispatch", fake_dispatch)
    monkeypatch.setattr(cli, "execute",
                        lambda cfg, a, cwd: ExecutionResult(
                            argv=["pytest", "-q"], returncode=0,
                            stdout="ok", stderr=""))
    logp = tmp_path / "run.jsonl"
    rc = cli.main(["--env", str(env), "--cwd", str(tmp_path),
                   "--log", str(logp), "run tests"])
    assert rc == cli.EXIT_OK
    events = [json.loads(line) for line in logp.read_text().splitlines()]
    assert [e["type"] for e in events] == ["plan", "result"]
    assert events[0]["command"] == "pytest" and events[0]["ok"] is True
    assert events[1]["argv"] == ["pytest", "-q"] and events[1]["returncode"] == 0


def test_cli_temperature_flag_passed(monkeypatch, tmp_path):
    from jevdo import cli
    from jevdo.dispatcher import PlanOutcome
    from jevdo.executor import ExecutionResult

    env = tmp_path / "environment.toml"
    env.write_text('[meta]\nmodel = "jev-latest"\n' + """\
[[command]]
name = "pytest"
description = "tests"
argv = ["pytest", "-q"]
""")
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    seen = {}

    def fake_dispatch(cfg, request, cwd=".", **kw):
        seen["temperature"] = kw.get("temperature")
        a = PlannedAction(command="pytest", subcommand=None, confidence=0.9,
                          risk="read")
        return PlanOutcome(True, a, "ok", 0.9), {}, {}, object()

    monkeypatch.setattr(cli, "dispatch", fake_dispatch)
    monkeypatch.setattr(cli, "execute",
                        lambda cfg, a, cwd: ExecutionResult(
                            argv=["pytest", "-q"], returncode=0,
                            stdout="", stderr=""))
    rc = cli.main(["--env", str(env), "--cwd", str(tmp_path),
                   "--temperature", "0.4", "run tests"])
    assert rc == cli.EXIT_OK and seen["temperature"] == 0.4
