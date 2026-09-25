import pytest

from jevdo.dispatcher import PlannedAction
from jevdo.executor import ExecutionError, execute, resolve_argv


def _action(**kw):
    base = dict(command="git", subcommand="status", flags=(), path=None, confidence=0.9)
    base.update(kw)
    return PlannedAction(**base)


def test_resolve_leaf_and_subcommand(config, workdir):
    assert resolve_argv(config, _action(), workdir) == ["git", "status"]
    assert resolve_argv(config, _action(command="pytest", subcommand=None), workdir) == ["pytest", "-q"]


def test_resolve_path_and_flags(config, workdir):
    a = _action(subcommand="log", flags=("--oneline",))
    assert resolve_argv(config, a, workdir) == ["git", "log", "--oneline"]
    a = _action(subcommand="add", path="README.md", paths={"path": "README.md"})
    assert resolve_argv(config, a, workdir) == ["git", "add", "README.md"]
    # optional path omitted -> template slot dropped
    a = PlannedAction(command="ls", subcommand=None, flags=(), path=None, confidence=0.9)
    assert resolve_argv(config, a, workdir) == ["ls"]


def test_resolve_rejects(config, workdir):
    with pytest.raises(ExecutionError, match="unknown flag"):
        resolve_argv(config, _action(subcommand="log", flags=("--evil",)), workdir)
    with pytest.raises(ExecutionError, match="not allowed|invalid path|not in current|escapes|does not exist"):
        resolve_argv(config, _action(subcommand="add", path="../evil",
                                     paths={"path": "../evil"}), workdir)
    with pytest.raises(ExecutionError, match="unknown path slot|takes no path"):
        resolve_argv(config, _action(path="README.md"), workdir)
    with pytest.raises(ExecutionError, match="missing required path"):
        resolve_argv(config, _action(subcommand="add", path=None), workdir)


def test_execute_dry_run_and_real(config, workdir, tmp_path):
    a = PlannedAction(command="ls", subcommand=None, flags=(), path=None, confidence=0.9)
    dry = execute(config, a, workdir, dry_run=True)
    assert dry.dry_run and dry.argv == ["ls"] and dry.returncode == 0
    real = execute(config, a, workdir)
    assert real.returncode == 0 and "README.md" in real.stdout


def test_multipath_and_valued_flag(config, tmp_path):
    from jevdo.config import Command, Subcommand, Flag, PathSlot
    cfg_cmd = Command(
        name="cp", description="copy", argv=("cp", "{path:src}", "{path:dst}"),
        paths=(PathSlot(name="src", kind="files"),
               PathSlot(name="dst", kind="dirs", optional=True)),
        subcommands=(),
    )
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "d").mkdir()
    from jevdo.config import EnvConfig
    cfg = EnvConfig(commands=(cfg_cmd,))
    a = PlannedAction(command="cp", subcommand=None, paths={"src": "a.txt", "dst": "d"})
    assert resolve_argv(cfg, a, str(tmp_path)) == ["cp", "a.txt", "d"]
    a = PlannedAction(command="cp", subcommand=None, paths={"src": "a.txt"})
    assert resolve_argv(cfg, a, str(tmp_path)) == ["cp", "a.txt"]

    fcfg = EnvConfig(commands=(Command(
        name="ls", description="l", argv=("ls",),
        flags=(Flag("--format", "fmt", ("--format={value}",),
                    values=("json", "plain"), value_descriptions={"json": "j"}),),
    ),))
    a = PlannedAction(command="ls", subcommand=None, flags=("--format",),
                      flag_values={"--format": "json"})
    assert resolve_argv(fcfg, a, str(tmp_path)) == ["ls", "--format=json"]
    a = PlannedAction(command="ls", subcommand=None, flags=("--format",),
                      flag_values={"--format": "yaml"})
    with pytest.raises(ExecutionError, match="not allowlisted"):
        resolve_argv(fcfg, a, str(tmp_path))
