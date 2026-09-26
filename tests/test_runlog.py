"""Opt-in JSONL run logging: writer + event builders."""

import json

from jevdo.dispatcher import PlanOutcome, PlannedAction
from jevdo.eval import EvalCase, EvalTestResult
from jevdo.runlog import JsonlLogger, eval_event, plan_event, result_event


def test_jsonl_logger_appends_across_instances(tmp_path):
    path = tmp_path / "run.jsonl"
    with JsonlLogger(str(path)) as log:
        log.log({"a": 1})
    with JsonlLogger(str(path)) as log:
        log.log({"b": 2})
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert lines == [{"a": 1}, {"b": 2}]


def test_plan_event_fields():
    action = PlannedAction(command="git", subcommand="status", confidence=0.9,
                           risk="read", threshold=0.5, threshold_source="global")
    out = PlanOutcome(True, action, "ok", 0.9)
    ev = plan_event(1, "req", ".", out,
                    {"cwd_files": ["a"], "cwd_dirs": ["d"]},
                    continue_asked=True, continue_value=0.8,
                    continue_threshold=0.4,
                    continue_threshold_source="meta continue_threshold",
                    max_steps=3, max_history=2, history_len=0)
    assert ev["type"] == "plan"
    assert ev["command"] == "git" and ev["subcommand"] == "status"
    assert ev["continue_asked"] is True and ev["continue"] == 0.8
    assert ev["continue_threshold"] == 0.4
    assert ev["continue_threshold_source"] == "meta continue_threshold"
    assert ev["cwd_files"] == ["a"] and ev["cwd_dirs"] == ["d"]
    assert ev["max_steps"] == 3 and ev["max_history"] == 2
    assert ev["layers"] == []
    assert ev["repeat_retry"] is False


def test_result_event_fields():
    ev = result_event(2, ["ls"], returncode=0, stdout="x", stderr="")
    assert ev["type"] == "result" and ev["step"] == 2
    assert ev["argv"] == ["ls"] and ev["returncode"] == 0
    assert ev["dry_run"] is False and ev["declined"] is False


def test_eval_event_fields():
    case = EvalCase(name="t", input="run", expected=(("git", "status"),),
                    expected_str=("git status",))
    result = EvalTestResult(case, True, (("git", "status"),), "ok",
                            confidence=0.9, risk="read")
    ev = eval_event(case, result)
    assert ev["type"] == "eval" and ev["name"] == "t"
    assert ev["expected"] == "git status" and ev["actual"] == "git status"
    assert ev["passed"] is True and ev["steps"] == 1
