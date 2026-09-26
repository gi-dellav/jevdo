from jevdo.questions import build_questions


def test_question_layers(config, workdir):
    questions, ctx = build_questions(config, workdir)
    # L1 + L2 + flags + path layers
    assert "__command__" in questions
    assert "__subcommand__:git" in questions
    assert "flag.git.log.--oneline" in questions
    assert "flag.ls.-la" in questions
    assert "path.git.add.path" in questions
    assert "path.ls.path" in questions
    assert "path_stated.ls.path" in questions  # optional gate only
    assert "path_stated.git.add.path" not in questions  # required => no gate
    # none_of_the_above present at every Choice layer
    for qid in ("__command__", "__subcommand__:git", "path.git.add.path", "path.ls.path"):
        assert "none_of_the_above" in questions[qid].criteria
    # dotfiles excluded from candidates
    assert ".hidden" not in ctx["candidates"][("git.add", "path")]
    assert ctx["candidates"][("ls", "path")] == ["srcdir"]


def test_empty_candidates_omit_path_question(config, tmp_path):
    questions, ctx = build_questions(config, str(tmp_path))
    assert "path.git.add.path" not in questions
    assert ctx["candidates"][("git.add", "path")] == []


def test_custom_instructions(config, tmp_path):
    from jevdo.config import Command, EnvConfig, Subcommand
    cmd = Command(name="git", description="vcs", argv=None,
                  instructions_subcommand="Pick it?",
                  subcommands=(Subcommand(name="status", description="s",
                                          argv=("git", "status")),))
    cfg = EnvConfig(commands=(cmd,), command_question="Do what?")
    q, _ = build_questions(cfg, str(tmp_path))
    assert q["__command__"].instructions == "Do what?"
    assert q["__subcommand__:git"].instructions == "Pick it?"


def test_continue_gate_only_when_allowed(config, workdir):
    from jevdo.questions import CONTINUE_QID
    q, _ = build_questions(config, workdir)
    assert CONTINUE_QID not in q
    q2, _ = build_questions(config, workdir, allow_continue=True)
    assert q2[CONTINUE_QID].type == "noul"


def test_step_one_prompts_unchanged(config, workdir):
    """Step 1 (no history) keeps the exact legacy wording (back-compat)."""
    from jevdo.questions import CONTINUE_INSTRUCTIONS, CONTINUE_QID
    q, _ = build_questions(
        config, workdir, allow_continue=True, step_index=0, history=None,
        max_steps=2)
    assert q["__command__"].instructions == "What shell task is the user asking for?"
    assert q[CONTINUE_QID].instructions == CONTINUE_INSTRUCTIONS


def test_step_two_prompts_name_progress(config, workdir):
    """Step 2+ L1/continue instructions name step, budget, and completed steps."""
    from jevdo.questions import CONTINUE_QID
    hist = [{"step": 1, "node": "git.add", "describe": "git add a",
             "argv": ["git", "add", "a"], "returncode": 0,
             "stdout_tail": "", "stderr_tail": ""}]
    q, _ = build_questions(
        config, workdir, allow_continue=True, step_index=1, history=hist,
        max_steps=3)
    l1 = q["__command__"].instructions
    assert "step 2 of at most 3" in l1
    assert "git.add" in l1 and "git add a" in l1
    assert "NEXT step, not a repeat" in l1
    cont = q[CONTINUE_QID].instructions
    assert "step 2 of at most 3" in cont
    assert "git add a" in cont


def test_repeat_hint_appends_nudge(config, workdir):
    from jevdo.questions import CONTINUE_QID, REPEAT_HINT_INSTRUCTIONS
    hist = [{"step": 1, "node": "git.add", "describe": "git add a",
             "argv": ["git", "add", "a"], "returncode": 0,
             "stdout_tail": "", "stderr_tail": ""}]
    q, _ = build_questions(
        config, workdir, allow_continue=True, step_index=1, history=hist,
        max_steps=2, repeat_hint=True)
    assert REPEAT_HINT_INSTRUCTIONS.strip() in q[CONTINUE_QID].instructions


def test_continue_question_override(config, workdir, tmp_path):
    import dataclasses
    cfg = dataclasses.replace(config, continue_question="Keep going?")
    q, _ = build_questions(cfg, workdir, allow_continue=True)
    from jevdo.questions import CONTINUE_QID
    assert q[CONTINUE_QID].instructions == "Keep going?"
