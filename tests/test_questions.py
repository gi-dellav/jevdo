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
