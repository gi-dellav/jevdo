"""Opt-in structured JSONL run logging.

One JSON object per line. Two event kinds per executed step:

- ``plan``: the request/state summary, the planned action (node, flags, paths,
  confidence, risk, threshold, per-layer decisions) and whether Jev was asked
  to continue.
- ``result``: the resolved argv plus how it ended (dry-run, declined, refused,
  or executed with returncode/stdout/stderr).

Eval mode emits one ``eval`` event per case instead. Logging is entirely
opt-in (``--log PATH``): nothing is written unless a path is given.
"""

from __future__ import annotations

import json
import time
from typing import Any


def _layer_obj(layer) -> dict:
    return {
        "question_id": layer.question_id,
        "choice": layer.choice,
        "probability": layer.probability,
        "confidence": layer.confidence,
        "probabilities": dict(layer.probabilities or {}),
    }


def _action_fields(action) -> dict:
    if action is None:
        return {
            "command": None, "subcommand": None, "flags": [], "flag_values": {},
            "paths": {}, "risk": None, "threshold": None, "threshold_source": None,
            "layers": [],
        }
    return {
        "command": action.command,
        "subcommand": action.subcommand,
        "flags": list(action.flags),
        "flag_values": dict(action.flag_values),
        "paths": dict(action.paths),
        "risk": action.risk,
        "threshold": action.threshold,
        "threshold_source": action.threshold_source,
        "layers": [_layer_obj(x) for x in action.layers],
    }


def plan_event(step: int, request: str, cwd: str, outcome, context=None, *,
               continue_asked: bool = False, continue_value: float | None = None,
               continue_threshold: float | None = None,
               continue_threshold_source: str | None = None,
               max_steps: int | None = None, max_history: int | None = None,
               history_len: int = 0, repeat_retry: bool = False) -> dict:
    """Build a ``plan`` event from a PlanOutcome."""
    ev = {
        "type": "plan",
        "ts": time.time(),
        "step": step,
        "request": request,
        "cwd": cwd,
        "ok": outcome.ok,
        "reason": outcome.reason,
        "confidence": outcome.confidence,
        "continue_asked": continue_asked,
        "continue": continue_value,
        "continue_threshold": continue_threshold,
        "continue_threshold_source": continue_threshold_source,
        "max_steps": max_steps,
        "max_history": max_history,
        "history_len": history_len,
        "repeat_retry": repeat_retry,
    }
    ev.update(_action_fields(outcome.action))
    if context:
        ev["cwd_files"] = context.get("cwd_files")
        ev["cwd_dirs"] = context.get("cwd_dirs")
    return ev


def result_event(step: int, argv: list[str] | None = None, *,
                 dry_run: bool = False, declined: bool = False,
                 returncode: int | None = None, stdout: str = "",
                 stderr: str = "", error: str | None = None,
                 stop_reason: str | None = None) -> dict:
    """Build a ``result`` event for an attempted/executed step."""
    return {
        "type": "result",
        "ts": time.time(),
        "step": step,
        "argv": list(argv) if argv is not None else None,
        "dry_run": dry_run,
        "declined": declined,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "error": error,
        "stop_reason": stop_reason,
    }


def eval_event(case, result) -> dict:
    """Build an ``eval`` event from an EvalTestResult."""
    from jevdo.eval import _join

    def fmt(steps):
        if steps is None:
            return None
        return " ; ".join(_join(a) for a in steps)

    return {
        "type": "eval",
        "ts": time.time(),
        "name": case.name,
        "input": case.input,
        "expected": fmt(case.expected),
        "actual": fmt(result.actual),
        "passed": result.passed,
        "reason": result.reason,
        "confidence": result.confidence,
        "risk": result.risk,
        "steps": len(result.actual) if result.actual is not None else 0,
    }


class JsonlLogger:
    """Append-only JSONL writer. Use as a context manager or call close()."""

    def __init__(self, path: str):
        self.path = path
        self._f = None

    def log(self, event: dict) -> None:
        if self._f is None:
            self._f = open(self.path, "a", encoding="utf-8")
        self._f.write(json.dumps(event, default=str) + "\n")
        self._f.flush()

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None

    def __enter__(self) -> "JsonlLogger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
