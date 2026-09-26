"""Build Jev (TypeSafe) questions from EnvConfig + live discovery.

Layers:
- L1 `__command__`: Choice over commands + none_of_the_above.
- L2 `__subcommand__:<cmd>`: Choice over subs + none_of_the_above.
- L3 flags: `flag.<node>.<flag>` Noul per flag; valued flags add
  `flagval.<node>.<flag>` Choice over values + none_of_the_above.
- L4 paths: one `path.<node>.<slot>` Choice + optional
  `path_stated.<node>.<slot>` Noul gate, per slot.
- Chaining: optional `__continue__` Noul when `allow_continue` is set; Jev
  decides whether another step should follow. On step 2+ the L1 and
  `__continue__` instructions are step-aware (step number, completed
  steps, remaining budget) so Jev plans the *next* part, not a repeat.

Custom instructions: [meta] command_question, per-command
instructions_subcommand, per-slot question/stated_question, per-flag question.
"""

from __future__ import annotations

from typesafe_sdk import Choice, Noul

from jevdo.config import RESERVED, EnvConfig, node_slots
from jevdo.discovery import discover

NONE_DESC = "none of the above; the request does not match any listed option"
NONE_PATH_DESC = "none of the listed files/directories matches the request"
NONE_VALUE_DESC = "none of the listed values matches the request"

CONTINUE_QID = "__continue__"
CONTINUE_INSTRUCTIONS = (
    "Given the request and the steps already taken, should Jev take another "
    "shell step after this one? Answer no if the request is complete or the "
    "current step finishes it."
)

#: Extra hint appended to the `__continue__` instructions when Jev just
#: re-planned an argv identical to the previous step. Nudges the model to
#: pick a *different* next step instead of repeating itself.
REPEAT_HINT_INSTRUCTIONS = (
    " The previous step already ran this exact command. Do not repeat it;"
    " pick a different next step that advances the request, or answer no"
    " if nothing remains."
)

MAX_CHOICE_OPTIONS = 255  # Jev Choice limit
STATE_PREVIEW_LIMIT = 100


def _node_key(cmd: str, sub: str | None) -> str:
    return cmd if sub is None else f"{cmd}.{sub}"


def _history_summary(history: list | None) -> str:
    """One-line-per-step summary of completed steps for step-aware prompts."""
    lines: list[str] = []
    for h in history or []:
        step = h.get("step", "?")
        desc = h.get("describe") or " ".join(str(a) for a in (h.get("argv") or []))
        node = h.get("node")
        lines.append(f"step {step} ({node}): {desc}" if node else f"step {step}: {desc}")
    return "; ".join(lines)


def build_questions(config: EnvConfig, cwd: str = ".", *,
                    allow_continue: bool = False,
                    step_index: int = 0,
                    history: list | None = None,
                    max_steps: int | None = None,
                    repeat_hint: bool = False):
    """Return (questions, context).

    context = {"candidates": {(node, slot): [names]},
               "cwd_files": [...], "cwd_dirs": [...]}
    Legacy "node" keys are also present for single-slot nodes.
    When allow_continue is true, a `__continue__` Noul is included.

    Step-aware chaining (P1): when `history` is non-empty (i.e. this is step
    2+), the L1 and `__continue__` instructions name the step number, the
    completed steps, and the remaining budget, so Jev can tell "done" from
    "next part". Step 1 (no history) keeps the exact legacy wording.
    `repeat_hint` appends an anti-repeat nudge to the `__continue__`
    instructions (P2 retry path).
    """
    questions: dict = {}
    candidates: dict = {}

    budget = config.max_steps if max_steps is None else max_steps
    step_no = step_index + 1

    if allow_continue:
        instructions = (
            config.continue_question or CONTINUE_INSTRUCTIONS)
        if history:
            done = _history_summary(history)
            instructions = (
                f"{instructions} This is step {step_no} of at most {budget} "
                f"for the request. Steps already completed: {done}. "
                f"Answer yes only if a further step is still needed.")
        if repeat_hint:
            instructions = f"{instructions}{REPEAT_HINT_INSTRUCTIONS}"
        questions[CONTINUE_QID] = Noul(instructions=instructions)

    # L1: command
    cmd_criteria = {c.name: c.description for c in config.commands}
    cmd_criteria[RESERVED] = NONE_DESC
    l1_instructions = (config.command_question
                       or "What shell task is the user asking for?")
    if history:
        done = _history_summary(history)
        l1_instructions = (
            f"{l1_instructions} This is step {step_no} of at most {budget} "
            f"for the request. Steps already completed: {done}. "
            f"Pick the command for the NEXT step, not a repeat of a "
            f"completed step.")
    questions["__command__"] = Choice(
        instructions=l1_instructions,
        criteria=cmd_criteria,
    )

    for cmd in config.commands:
        # L2: subcommand
        if cmd.subcommands:
            sub_criteria = {s.name: s.description for s in cmd.subcommands}
            sub_criteria[RESERVED] = "run the base command as-is without a specific subcommand"
            questions[f"__subcommand__:{cmd.name}"] = Choice(
                instructions=cmd.instructions_subcommand
                or f"Which '{cmd.name}' subcommand does the request need?",
                criteria=sub_criteria,
            )
        _add_flag_questions(questions, cmd.name, None, cmd.flags)
        for slot in node_slots(cmd):
            _add_slot_questions(questions, candidates, _node_key(cmd.name, None),
                                slot, cwd)
        for sub in cmd.subcommands:
            key = _node_key(cmd.name, sub.name)
            _add_flag_questions(questions, cmd.name, sub.name, sub.flags)
            for slot in node_slots(sub):
                _add_slot_questions(questions, candidates, key, slot, cwd)

    from jevdo.discovery import list_candidates
    files, dirs = [], []
    if candidates:
        try:
            files = list_candidates("files", cwd)
        except OSError:
            files = []
        try:
            dirs = list_candidates("dirs", cwd)
        except OSError:
            dirs = []
    context = {"candidates": candidates, "cwd_files": files, "cwd_dirs": dirs}
    return questions, context


def _add_flag_questions(questions, cmd: str, sub: str | None, flags) -> None:
    prefix = f"flag.{cmd}.{sub}" if sub is not None else f"flag.{cmd}"
    node = f"'{cmd} {sub}'" if sub is not None else f"'{cmd}'"
    for fl in flags:
        questions[f"{prefix}.{fl.name}"] = Noul(
            instructions=fl.question
            or f"Does the request ask for '{fl.name}' ({fl.description}) when running {node}?"
        )
        if fl.values:
            criteria = {v: fl.value_descriptions.get(v) for v in fl.values}
            criteria[RESERVED] = NONE_VALUE_DESC
            vprefix = f"flagval.{cmd}.{sub}" if sub is not None else f"flagval.{cmd}"
            questions[f"{vprefix}.{fl.name}"] = Choice(
                instructions=f"Which value for '{fl.name}' does the request need when running {node}?",
                criteria=criteria,
            )


def _add_slot_questions(questions, candidates, node_key, slot, cwd) -> None:
    try:
        cands = discover(slot, cwd)
    except ValueError:
        raise
    key = (node_key, slot.name)
    candidates[key] = cands
    # legacy alias: single-slot nodes readable via bare node key
    if node_key not in candidates:
        candidates[node_key] = cands
    else:
        # multi-slot node: keep bare key out to avoid ambiguity; planner uses tuples
        if isinstance(candidates[node_key], list) and slot.name != "path":
            del candidates[node_key]
    if not cands:
        return  # dispatcher abstains (required) or omits (optional)
    if len(cands) + 1 > MAX_CHOICE_OPTIONS:
        raise ValueError(
            f"too many path candidates for '{node_key}.{slot.name}' "
            f"({len(cands)} + none_of_the_above > {MAX_CHOICE_OPTIONS}); "
            "narrow 'glob', lower 'depth', or raise 'max_results'"
        )
    label = slot.description or f"for '{node_key}'"
    criteria = {name: None for name in cands}
    criteria[RESERVED] = NONE_PATH_DESC
    questions[f"path.{node_key}.{slot.name}"] = Choice(
        instructions=slot.question or f"Which file or directory {label} does the request refer to?",
        criteria=criteria,
    )
    if slot.optional:
        questions[f"path_stated.{node_key}.{slot.name}"] = Noul(
            instructions=slot.stated_question
            or f"Does the request mention a specific file or directory {label}?"
        )


def state_preview(items: list[str], limit: int = STATE_PREVIEW_LIMIT) -> list[str]:
    if len(items) <= limit:
        return items
    return items[:limit] + [f"... ({len(items) - limit} more)"]
