"""Planner semantics: none_of_the_above per layer, flags, optional paths, confidence."""

from jevdo.dispatcher import plan_from_answers
from conftest import FakeChoice, FakeNoul, ctx_for

CTX = {
    "candidates": {("git.add", "path"): ["README.md"], ("ls", "path"): ["srcdir"],
                   "git.add": ["README.md"], "ls": ["srcdir"]},
    "cwd_files": ["README.md"],
    "cwd_dirs": ["srcdir"],
}


def _cmd(choice="git", conf=0.9):
    return {"__command__": FakeChoice(choice, {choice: 0.9, "none_of_the_above": 0.1}, conf)}


def test_l1_none_abstains(config):
    out = plan_from_answers(config, _cmd("none_of_the_above", 0.95), CTX)
    assert not out.ok and "no known command" in out.reason


def test_l2_none_without_base_abstains(config):
    # git has argv=None -> picking none at L2 abstains
    ans = _cmd() | {"__subcommand__:git": FakeChoice(
        "none_of_the_above", {"none_of_the_above": 0.8, "status": 0.2}, 0.8)}
    out = plan_from_answers(config, ans, CTX)
    assert not out.ok and "needs a subcommand" in out.reason


def test_l2_none_with_base_runs_bare(config, workdir):
    ans = {"__command__": FakeChoice("pytest", {"pytest": 1.0}, 0.99)}
    out = plan_from_answers(config, ans, CTX)
    assert out.ok and out.action.describe() == "pytest"


def test_happy_path_status(config):
    ans = _cmd() | {"__subcommand__:git": FakeChoice("status", {"status": 0.95}, 0.95)}
    out = plan_from_answers(config, ans, CTX)
    assert out.ok and out.action.describe() == "git status"
    assert out.action.risk == "read"


def test_flag_included_when_noul_yes(config):
    ans = (_cmd() | {"__subcommand__:git": FakeChoice("log", {"log": 0.9}, 0.9),
                     "flag.git.log.--oneline": FakeNoul(0.95)})
    out = plan_from_answers(config, ans, CTX)
    assert out.ok and out.action.describe() == "git log --oneline"


def test_flag_excluded_when_noul_no(config):
    ans = (_cmd() | {"__subcommand__:git": FakeChoice("log", {"log": 0.9}, 0.9),
                     "flag.git.log.--oneline": FakeNoul(0.1)})
    out = plan_from_answers(config, ans, CTX)
    assert out.ok and out.action.describe() == "git log"


def test_required_path_none_abstains(config):
    ans = (_cmd() | {"__subcommand__:git": FakeChoice("add", {"add": 0.9}, 0.9),
                     "path.git.add.path": FakeChoice(
                         "none_of_the_above", {"none_of_the_above": 0.9}, 0.9)})
    out = plan_from_answers(config, ans, CTX)
    assert not out.ok and "no git.add.path file/dir matches" in out.reason


def test_optional_path_omitted_when_not_stated(config):
    ans = ({"__command__": FakeChoice("ls", {"ls": 0.9}, 0.9)}
           | {"path_stated.ls.path": FakeNoul(0.1), "flag.ls.-la": FakeNoul(0.9)})
    out = plan_from_answers(config, ans, CTX)
    assert out.ok and out.action.describe() == "ls -la"


def test_optional_path_used_when_stated(config):
    ans = ({"__command__": FakeChoice("ls", {"ls": 0.9}, 0.9)}
           | {"path_stated.ls.path": FakeNoul(0.9),
              "path.ls.path": FakeChoice("srcdir", {"srcdir": 0.85}, 0.85)})
    out = plan_from_answers(config, ans, CTX)
    assert out.ok and out.action.path == "srcdir"
    assert out.action.paths == {"path": "srcdir"}


def test_low_confidence_abstains_but_keeps_action(config):
    ans = (_cmd("git", 0.2) | {"__subcommand__:git": FakeChoice("status", {"status": 0.9}, 0.2)})
    out = plan_from_answers(config, ans, CTX)
    assert not out.ok and "below minimum" in out.reason
    assert out.action is not None and out.action.describe() == "git status"


def test_context_from_live_workdir(config, workdir):
    live = ctx_for(workdir)
    ans = (_cmd() | {"__subcommand__:git": FakeChoice("add", {"add": 0.9}, 0.9),
                     "path.git.add.path": FakeChoice(
                         "README.md", {"README.md": 0.9}, 0.9)})
    out = plan_from_answers(config, ans, live)
    assert out.ok and out.action.paths == {"path": "README.md"}
