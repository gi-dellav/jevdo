"""Dispatcher: call Jev, read the winning branch, return a PlannedAction.

Confidence = min over Choice layers actually used (cookbook: least-certain
judgement, not product). Abstains when:
- L1 picks none_of_the_above
- L2 picks none_of_the_above and base argv is None
- required path/valued slot missing or none
- confidence < effective threshold (CLI > node > risk tier > global)

Chaining is Jev-decided: each step optionally carries a `__continue__` Noul
(absent when max_steps == 1); `dispatch_sequence` stops early when it is not
affirmed. History sent back is capped by max_history. Three mechanisms make
chains work better than a bare repeat-the-request loop:

- P1 step-aware prompts: step 2+ L1/`__continue__` instructions name the step
  number, the completed steps (node + argv), and the remaining budget, and
  the history entries carried in `state["history"]` include `node` and a
  human-readable `describe` string next to `argv`.
- P2 anti-repeat retry: if step 2+ resolves to the exact argv of the
  previous step, the harness re-plans once with an anti-repeat nudge
  (`repeat_hint` + `state["hint"]`). If the retry still repeats (or fails),
  the run stops honestly instead of executing a duplicate.
- P3 tunable `__continue__` gate: the Noul bar defaults to 0.5 but resolves
  CLI `--continue-threshold` > `[meta] continue_threshold` > default via
  `effective_continue_threshold`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jevdo.config import (
    RESERVED, EnvConfig, effective_continue_threshold, effective_threshold,
    node_for_selection, node_risk, node_slots,
)
from jevdo.questions import CONTINUE_QID

PATH_YES = 0.5  # Noul threshold for stated-gates and boolean flags
CONTINUE_YES = 0.5  # default Noul bar for the `__continue__` gate (see P3)
REPEAT_HINT_STATE = (
    "The previous step already ran this exact command. "
    "Choose a DIFFERENT next step that advances the request, "
    "or decline to continue if nothing remains."
)


@dataclass(frozen=True)
class LayerDetail:
    question_id: str
    choice: str | None
    probability: float
    confidence: float | None  # None for Noul layers
    probabilities: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedAction:
    command: str
    subcommand: str | None
    flags: tuple[str, ...] = ()
    flag_values: dict = field(default_factory=dict)
    paths: dict = field(default_factory=dict)
    path: str | None = None  # legacy alias: single-slot value
    confidence: float = 0.0
    risk: str = "read"
    threshold: float = 0.5
    threshold_source: str = "global"
    layers: tuple[LayerDetail, ...] = ()

    @property
    def node_key(self) -> str:
        return self.command if self.subcommand is None else f"{self.command}.{self.subcommand}"

    def describe(self) -> str:
        parts = [self.command]
        if self.subcommand:
            parts.append(self.subcommand)
        for fl in self.flags:
            if fl in self.flag_values:
                parts.append(f"{fl}={self.flag_values[fl]}")
            else:
                parts.append(fl)
        for _slot, val in self.paths.items():
            parts.append(val)
        return " ".join(parts)


@dataclass(frozen=True)
class PlanOutcome:
    ok: bool
    action: PlannedAction | None
    reason: str
    confidence: float = 0.0


@dataclass(frozen=True)
class StepOutcome:
    action: PlannedAction
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    dry_run: bool = False


def _choice_info(answer) -> tuple[str, float, float | None, dict]:
    probs = dict(answer.probabilities or {})
    choice = answer.choice
    p = float(probs.get(choice, 0.0))
    conf = answer.confidence
    conf = float(conf) if conf is not None else None
    return choice, p, conf, probs


def _candidates_for(context: dict, node_key: str, slot_name: str) -> list:
    cands = context.get("candidates") or {}
    v = cands.get((node_key, slot_name))
    if v is None:
        v = cands.get(node_key, [])
    return list(v)


def plan_from_answers(
    config: EnvConfig,
    answers: dict,
    context: dict,
    *,
    min_confidence: float | None = None,
) -> PlanOutcome:
    """Pure planning from a Jev answers dict. No I/O. Fully unit-testable."""
    layers: list[LayerDetail] = []
    confidences: list[float] = []

    def track(qid, choice, prob, conf, probs):
        layers.append(LayerDetail(qid, choice, prob, conf, dict(probs)))
        if conf is not None:
            confidences.append(conf)

    # ---- L1: command ----
    ans = answers.get("__command__")
    if ans is None or getattr(ans, "type", None) != "choice":
        return PlanOutcome(False, None, "missing __command__ answer")
    cmd_name, cmd_p, cmd_conf, cmd_probs = _choice_info(ans)
    track("__command__", cmd_name, cmd_p, cmd_conf, cmd_probs)
    if cmd_name == RESERVED:
        return PlanOutcome(False, None, "request matches no known command", _min_or_zero(confidences))
    cmd = config.get_command(cmd_name)
    if cmd is None:
        return PlanOutcome(False, None, f"unknown command '{cmd_name}'", _min_or_zero(confidences))

    # ---- L2: subcommand ----
    sub = None
    if cmd.subcommands:
        qid = f"__subcommand__:{cmd.name}"
        sans = answers.get(qid)
        if sans is None or getattr(sans, "type", None) != "choice":
            return PlanOutcome(False, None, f"missing {qid} answer", _min_or_zero(confidences))
        sub_name, sub_p, sub_conf, sub_probs = _choice_info(sans)
        track(qid, sub_name, sub_p, sub_conf, sub_probs)
        if sub_name == RESERVED:
            sub = None
            if cmd.argv is None:
                return PlanOutcome(False, None,
                    f"'{cmd.name}' needs a subcommand and none matches",
                    _min_or_zero(confidences))
        else:
            _, found = node_for_selection(config, cmd.name, sub_name)
            sub = found
    key = cmd.name if sub is None else f"{cmd.name}.{sub.name}"
    node = sub if sub is not None else cmd
    slots = list(node_slots(node))
    flags_spec = list(node.flags)
    risk = node_risk(config, cmd, sub)
    bar, bar_source = effective_threshold(config, cmd, sub, risk, cli_override=min_confidence)

    # ---- L3: flags (Noul each; valued flags add a Choice when on) ----
    chosen_flags: list[str] = []
    flag_values: dict[str, str] = {}
    fprefix = f"flag.{cmd.name}.{sub.name}" if sub is not None else f"flag.{cmd.name}"
    vprefix = f"flagval.{cmd.name}.{sub.name}" if sub is not None else f"flagval.{cmd.name}"
    for fl in flags_spec:
        qid = f"{fprefix}.{fl.name}"
        fans = answers.get(qid)
        if fans is None or getattr(fans, "type", None) != "noul":
            continue  # missing flag answer => off (fail-closed)
        v = float(fans.noul)
        layers.append(LayerDetail(qid, "yes" if v >= PATH_YES else "no", v, None, {}))
        if v < PATH_YES:
            continue
        if not fl.values:
            chosen_flags.append(fl.name)
            continue
        # valued flag: read Choice; none/missing/hallucinated => treat as off
        vqid = f"{vprefix}.{fl.name}"
        vans = answers.get(vqid)
        if vans is None or getattr(vans, "type", None) != "choice":
            continue
        val, vp, vc, vprobs = _choice_info(vans)
        layers.append(LayerDetail(vqid, val, vp, vc, dict(vprobs)))
        if vc is not None:
            confidences.append(vc)
        if val == RESERVED or val not in fl.values:
            continue  # fail-closed to off
        chosen_flags.append(fl.name)
        flag_values[fl.name] = val

    # ---- L4: path slots ----
    paths: dict[str, str] = {}
    for slot in slots:
        cands = _candidates_for(context, key, slot.name)
        if not cands:
            if slot.optional:
                continue
            return PlanOutcome(False, None,
                f"no {key}.{slot.name} candidates to choose from",
                _min_or_zero(confidences))
        picked: str | None = None
        if slot.optional:
            gate = answers.get(f"path_stated.{key}.{slot.name}")
            if gate is not None and getattr(gate, "type", None) == "noul":
                gv = float(gate.noul)
                layers.append(LayerDetail(f"path_stated.{key}.{slot.name}",
                                          "yes" if gv >= PATH_YES else "no", gv, None, {}))
                if gv < PATH_YES:
                    continue
            # missing gate => fall through to choice (fail-open to model, still strict)
        picked = _read_slot_choice(answers, context, key, slot.name, layers, confidences)
        if picked == "__ABSTAIN__":
            if slot.optional:
                continue  # none_of_the_above on optional slot => omit
            return PlanOutcome(False, None,
                f"no {key}.{slot.name} file/dir matches the request",
                _min_or_zero(confidences))
        if picked is None:
            if slot.optional:
                continue
            return PlanOutcome(False, None, f"missing path for '{key}.{slot.name}'",
                               _min_or_zero(confidences))
        paths[slot.name] = picked

    conf = _min_or_zero(confidences)
    legacy_path = None
    if len(paths) == 1:
        legacy_path = next(iter(paths.values()))
    action = PlannedAction(
        command=cmd.name,
        subcommand=(sub.name if sub is not None else None),
        flags=tuple(chosen_flags),
        flag_values=dict(flag_values),
        paths=dict(paths),
        path=legacy_path,
        confidence=conf,
        risk=risk,
        threshold=bar,
        threshold_source=bar_source,
        layers=tuple(layers),
    )
    if conf < bar:
        return PlanOutcome(False, action,
            f"confidence {conf:.2f} below minimum {bar:.2f} ({bar_source})", conf)
    return PlanOutcome(True, action, "ok", conf)


def _read_slot_choice(answers, context, node_key, slot_name, layers, confidences):
    qid = f"path.{node_key}.{slot_name}"
    pans = answers.get(qid)
    if pans is None or getattr(pans, "type", None) != "choice":
        # legacy fallback: single-slot nodes may answer under bare node key
        legacy = answers.get(f"path.{node_key}")
        if legacy is not None and getattr(legacy, "type", None) == "choice":
            pans = legacy
            qid = f"path.{node_key}"
        else:
            return None
    name, p, c, probs = _choice_info(pans)
    layers.append(LayerDetail(qid, name, p, c, dict(probs)))
    if c is not None:
        confidences.append(c)
    if name == RESERVED:
        return "__ABSTAIN__"
    if name not in _candidates_for(context, node_key, slot_name):
        return "__ABSTAIN__"  # hallucinated filename => abstain
    return name


def _min_or_zero(xs: list[float]) -> float:
    return min(xs) if xs else 0.0


def _clip_history(history: list, max_history: int | None) -> list:
    """Keep at most max_history most-recent entries (None => unlimited)."""
    if not history:
        return []
    if max_history is None:
        return list(history)
    if max_history <= 0:
        return []
    return list(history[-max_history:])


def continue_info(response) -> float | None:
    """Raw `__continue__` Noul probability, or None if absent/malformed."""
    answers = getattr(response, "answers", None)
    if not isinstance(answers, dict):
        return None
    ans = answers.get(CONTINUE_QID)
    if ans is None or getattr(ans, "type", None) != "noul":
        return None
    try:
        return float(ans.noul)
    except (TypeError, ValueError):
        return None


def continue_requested(response, threshold: float | None = None) -> bool:
    """True iff Jev answered the `__continue__` Noul at/above the bar.

    `threshold` defaults to CONTINUE_YES (0.5); pass the resolved
    `effective_continue_threshold` bar to honor config/CLI overrides.
    Missing/abstaining/malformed answer => False (fail-closed).
    """
    v = continue_info(response)
    bar = CONTINUE_YES if threshold is None else threshold
    return v is not None and v >= bar


def describe_action(action: PlannedAction) -> str:
    """Short human-readable summary of a planned step for history prompts."""
    try:
        return action.describe()
    except Exception:
        return action.node_key


def history_entry(step: int, action: PlannedAction, argv: list[str],
                  returncode: int, stdout: str, stderr: str) -> dict:
    """Build the history dict appended after each chained step (P1).

    Carries `node` (e.g. ``ruff.check``) and `describe` (human-readable
    summary) alongside `argv` so later steps see semantic progress, not
    just argv tokens.
    """
    return {
        "step": step,
        "node": action.node_key,
        "describe": describe_action(action),
        "argv": list(argv),
        "returncode": returncode,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
    }


def dispatch(config: EnvConfig, request: str, cwd: str = ".", *, client=None,
             min_confidence: float | None = None, history: list | None = None,
             max_steps: int | None = None, max_history: int | None = None,
             allow_continue: bool | None = None,
             temperature: float | None = None,
             step_index: int = 0,
             repeat_hint: bool = False,
             hint: str | None = None):
    """Live dispatch: build state+questions, call Jev once, plan. Returns
    (PlanOutcome, questions, context, response).

    allow_continue adds the `__continue__` gate; defaults to (budget > 1).
    max_history caps history entries sent to Jev; defaults to config.max_history.
    temperature defaults to config.temperature and rides in extra_body.
    step_index/repeat_hint drive P1 step-aware prompts: step 2+ names the
    step number, completed steps, and remaining budget. `hint` (P2 retry)
    is surfaced to Jev as `state["hint"]`.
    """
    from jevdo.client import create_client

    from jevdo.questions import build_questions, state_preview

    budget = config.max_steps if max_steps is None else max_steps
    if allow_continue is None:
        allow_continue = budget > 1
    cap = config.max_history if max_history is None else max_history
    temp = config.temperature if temperature is None else temperature
    hist = _clip_history(history or [], cap)
    questions, context = build_questions(
        config, cwd, allow_continue=allow_continue,
        step_index=step_index, history=hist or None,
        max_steps=budget, repeat_hint=repeat_hint)
    state = {
        "request": request,
        "cwd_files": state_preview(context["cwd_files"]),
        "cwd_dirs": state_preview(context["cwd_dirs"]),
    }
    if hist:
        state["history"] = hist
    if step_index:
        state["step"] = step_index + 1
        state["max_steps"] = budget
    if hint:
        state["hint"] = hint
    extra_body = {"temperature": temp} if temp is not None else None
    own = False
    if client is None:
        client = create_client(config)
        own = True
    try:
        response = client.system_one(state=state, questions=questions,
                                     model=config.model, extra_body=extra_body)
    finally:
        if own:
            try:
                client.close()
            except Exception:
                pass
    outcome = plan_from_answers(config, response.answers, context,
                                min_confidence=min_confidence)
    return outcome, questions, context, response


def dispatch_sequence(config: EnvConfig, request: str, cwd: str = ".", *,
                      client=None, min_confidence: float | None = None,
                      max_steps: int | None = None, max_history: int | None = None,
                      temperature: float | None = None, execute_fn=None,
                      workflow: list[str] | None = None, logger=None,
                      continue_threshold: float | None = None,
                      repeat_guard: bool = True):
    """Chained dispatch: plan -> execute -> refresh -> repeat.

    Each step includes a `__continue__` Noul so Jev decides whether to chain
    further; the loop stops early when Jev says no (or the gate is absent).
    `max_steps` is the upper bound (1 => never ask, single shot).
    `continue_threshold` (P3) overrides the `__continue__` Noul bar for this
    run; None resolves CLI-override > meta > 0.5 default (note: when called
    via run_eval/eval harness pass the per-case bar here; bare None falls
    back to config).

    P2 anti-repeat: when step 2+ resolves to the previous step's argv, the
    harness re-plans once with an anti-repeat nudge. A second repeat (or a
    failed retry) stops the run honestly instead of executing a duplicate.
    `repeat_guard` enables this (default True); pass False in unit probes
    that pin identical fake plans across steps.

    Returns (steps, stop_reason) where steps is a list[StepOutcome].
    execute_fn(action, cwd) -> (argv, returncode, stdout, stderr); defaults to
    jevdo.executor.execute (real run). stop_on_error defaults True.
    logger, when given, is a runlog.JsonlLogger receiving plan/result events.
    """
    from jevdo import executor as _executor

    budget = max_steps or config.max_steps
    budget = max(1, min(10, budget))
    bar, bar_source = effective_continue_threshold(
        config, cli_override=continue_threshold)
    steps: list[StepOutcome] = []
    history: list[dict] = []
    own = False
    if client is None and execute_fn is None:
        from jevdo.client import create_client
        client = create_client(config)
        own = True
    try:
        for i in range(budget):
            ask_continue = budget > 1 and i + 1 < budget
            outcome, _q, context, response = dispatch(
                config, request, cwd, client=client,
                min_confidence=min_confidence, history=history or None,
                max_steps=budget, max_history=max_history,
                allow_continue=ask_continue, temperature=temperature,
                step_index=i)
            if logger is not None:
                from jevdo import runlog
                logger.log(runlog.plan_event(
                    i + 1, request, cwd, outcome, context,
                    continue_asked=ask_continue,
                    continue_value=continue_info(response),
                    continue_threshold=bar,
                    continue_threshold_source=bar_source,
                    max_steps=budget, max_history=max_history,
                    history_len=len(history)))
            if workflow is not None and i < len(workflow):
                # pinned workflow: force node, keep model's slots/flags/values
                node = workflow[i]
                if outcome.action is not None and outcome.action.node_key != node:
                    parts = node.split(".", 1)
                    forced = _force_node(outcome, parts[0],
                                         parts[1] if len(parts) > 1 else None)
                    if forced is None:
                        return steps, f"workflow step {i+1} node '{node}' invalid"
                    import dataclasses
                    outcome = _outcome_with_action(config, outcome, forced,
                                                   min_confidence)
                if not outcome.ok:
                    return steps, f"workflow step {i+1} abstained: {outcome.reason}"
            if not outcome.ok or outcome.action is None:
                reason = (f"abstained: {outcome.reason}" if i == 0
                          else f"stop: {outcome.reason}")
                if logger is not None:
                    logger.log(runlog.result_event(i + 1, error=outcome.reason,
                                                   stop_reason=reason))
                return steps, reason
            action = outcome.action
            if execute_fn is not None:
                argv, rc, out, err = execute_fn(action, cwd)
            else:
                res = _executor.execute(config, action, cwd)
                argv, rc, out, err = res.argv, res.returncode, res.stdout, res.stderr
            if history and list(argv) == list(history[-1].get("argv") or []):
                # P2: step repeats the previous argv verbatim. Re-plan once
                # with an anti-repeat nudge instead of executing a duplicate.
                # Guarded by `repeat_guard`: unit fakes that pin identical
                # plans across steps (FakeClient + canned execute_fn) keep
                # the legacy behavior; live runs (and tests) opt in by
                # passing repeat_guard=True.
                retry = None
                if repeat_guard:
                    retry = _retry_after_repeat(
                        config, request, cwd, client=client,
                        min_confidence=min_confidence, history=history,
                        max_steps=budget, max_history=max_history,
                        temperature=temperature, step_index=i,
                        ask_continue=ask_continue, prev_argv=list(argv),
                        logger=logger, execute_fn=execute_fn)
                if retry is None and repeat_guard:
                    steps.append(StepOutcome(action, argv, rc, out, err))
                    history.append(history_entry(
                        i + 1, action, argv, rc, out, err))
                    if logger is not None:
                        logger.log(runlog.result_event(i + 1, argv, returncode=rc,
                                                       stdout=out, stderr=err))
                    return steps, (
                        f"stop: step {i+1} repeats step {i} "
                        f"({ ' '.join(argv)}); not executing a duplicate")
                elif retry is not None:
                    outcome, context, response, argv, rc, out, err, action = retry
            steps.append(StepOutcome(action, argv, rc, out, err))
            history.append(history_entry(i + 1, action, argv, rc, out, err))
            if logger is not None:
                logger.log(runlog.result_event(i + 1, argv, returncode=rc,
                                               stdout=out, stderr=err))
            if rc != 0:
                return steps, f"stop: step {i+1} exited {rc}"
            if workflow is not None and i + 1 >= len(workflow):
                return steps, "workflow complete"
            if ask_continue and not continue_requested(response, bar):
                return steps, f"stop: Jev ended after step {i+1}"
    finally:
        if own:
            try:
                client.close()
            except Exception:
                pass
    return steps, "max_steps reached"


def _retry_after_repeat(config, request, cwd, *, client,
                        min_confidence, history, max_steps, max_history,
                        temperature, step_index, ask_continue, prev_argv,
                        logger=None, execute_fn=None):
    """P2 anti-repeat: re-plan step `step_index` once with an anti-repeat nudge.

    Returns (outcome, context, response, argv, rc, out, err, action) for the
    retry when it plans a *different* argv, else None (caller stops honestly
    instead of executing a duplicate). The retry is executed through the
    same path as a normal step (execute_fn or real executor).
    """
    from jevdo import executor as _executor

    outcome, _q, context, response = dispatch(
        config, request, cwd, client=client,
        min_confidence=min_confidence, history=history or None,
        max_steps=max_steps, max_history=max_history,
        allow_continue=ask_continue, temperature=temperature,
        step_index=step_index, repeat_hint=True, hint=REPEAT_HINT_STATE)
    if logger is not None:
        from jevdo import runlog
        logger.log(runlog.plan_event(
            step_index + 1, request, cwd, outcome, context,
            continue_asked=ask_continue,
            continue_value=continue_info(response),
            max_steps=max_steps, max_history=max_history,
            history_len=len(history), repeat_retry=True))
    if not outcome.ok or outcome.action is None:
        return None
    action = outcome.action
    try:
        argv = list(_executor.resolve_argv(config, action, cwd))
    except Exception:
        return None
    if argv == list(prev_argv):
        return None  # still repeats: stop honestly
    if execute_fn is not None:
        rc_out = execute_fn(action, cwd)
        argv, rc, out, err = list(rc_out[0]), rc_out[1], rc_out[2], rc_out[3]
    else:
        res = _executor.execute(config, action, cwd)
        argv, rc, out, err = list(res.argv), res.returncode, res.stdout, res.stderr
    return outcome, context, response, argv, rc, out, err, action


def _force_node(outcome: PlanOutcome, command: str, subcommand: str | None):
    """Rebuild action on a pinned node keeping flags/values/paths that fit."""
    if outcome.action is None:
        return None
    import dataclasses
    a = outcome.action
    return dataclasses.replace(a, command=command, subcommand=subcommand)


def _outcome_with_action(config, outcome: PlanOutcome, action, min_confidence):
    from jevdo.config import node_for_selection, node_risk, effective_threshold
    try:
        cmd, sub = node_for_selection(config, action.command, action.subcommand)
    except Exception as e:
        return PlanOutcome(False, None, str(e), outcome.confidence)
    risk = node_risk(config, cmd, sub)
    bar, src = effective_threshold(config, cmd, sub, risk, cli_override=min_confidence)
    import dataclasses
    action = dataclasses.replace(action, risk=risk, threshold=bar, threshold_source=src)
    if action.confidence < bar:
        return PlanOutcome(False, action,
            f"confidence {action.confidence:.2f} below minimum {bar:.2f} ({src})",
            action.confidence)
    return PlanOutcome(True, action, "ok", action.confidence)
