"""Shared fakes + fixtures for jevdo tests."""

import pytest

from jevdo.config import EnvConfig, Command, Subcommand, Flag


class FakeChoice:
    type = "choice"

    def __init__(self, choice, probabilities, confidence=0.9):
        self.choice = choice
        self.probabilities = probabilities
        self.confidence = confidence


class FakeNoul:
    type = "noul"

    def __init__(self, noul):
        self.noul = noul


def sample_config(**over) -> EnvConfig:
    cmds = (
        Command(
            name="git", description="vcs", argv=None, subcommands=(
                Subcommand(name="status", description="status", argv=("git", "status")),
                Subcommand(
                    name="log", description="history", argv=("git", "log"),
                    flags=(Flag("--oneline", "compact", ("--oneline",)),),
                ),
                Subcommand(
                    name="add", description="stage", argv=("git", "add", "{path}"),
                    takes_path=True, path_kind="files",
                ),
            ),
        ),
        Command(
            name="ls", description="list", argv=("ls", "{path}"),
            takes_path=True, path_kind="dirs", path_optional=True,
            flags=(Flag("-la", "long", ("-la",)),),
        ),
        Command(name="pytest", description="tests", argv=("pytest", "-q")),
    )
    kw = dict(model="jev-latest", min_confidence=0.5, timeout=60.0, commands=cmds)
    kw.update(over)
    return EnvConfig(**kw)


@pytest.fixture
def config():
    return sample_config()


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "README.md").write_text("x")
    (tmp_path / "notes.txt").write_text("y")
    (tmp_path / ".hidden").write_text("z")
    (tmp_path / "srcdir").mkdir()
    return str(tmp_path)


def ctx_for(workdir):
    """Build a context dict compatible with plan_from_answers for workdir."""
    from jevdo.questions import build_questions
    _q, ctx = build_questions(sample_config(), workdir)
    return ctx
