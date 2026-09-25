import pytest

from jevdo.discovery import discover, list_candidates, validate_selection
from jevdo.config import PathSlot


def _tree(root):
    (root / "README.md").write_text("x")
    (root / "notes.txt").write_text("y")
    (root / ".hidden").write_text("z")
    (root / "srcdir").mkdir()
    (root / "srcdir" / "app.py").write_text("x")
    (root / "srcdir" / "nested").mkdir()
    (root / "srcdir" / "nested" / "deep.py").write_text("x")
    return str(root)


def test_lists_files_dirs_both_skip_dotfiles(workdir):
    assert list_candidates("files", workdir) == ["README.md", "notes.txt"]
    assert list_candidates("dirs", workdir) == ["srcdir"]
    assert list_candidates("both", workdir) == ["README.md", "notes.txt", "srcdir"]
    assert ".hidden" not in list_candidates("both", workdir)


def test_validate_ok_and_kind_enforcement(workdir):
    assert validate_selection("README.md", "files", workdir) == "README.md"
    assert validate_selection("srcdir", "dirs", workdir) == "srcdir"
    with pytest.raises(ValueError, match="expected a file"):
        validate_selection("srcdir", "files", workdir)
    with pytest.raises(ValueError, match="not a directory"):
        validate_selection("README.md", "dirs", workdir)


@pytest.mark.parametrize("bad", ["", "..", ".", ".hidden", "../x", "a/b", "a\\b", "/abs", "a\0b", "missing.txt"])
def test_validate_rejects_legacy(workdir, bad):
    with pytest.raises(ValueError):
        validate_selection(bad, "both", workdir)


def test_discover_depth_and_glob(tmp_path):
    cwd = _tree(tmp_path)
    assert discover(PathSlot(name="s", kind="files"), cwd) == ["README.md", "notes.txt"]
    assert discover(PathSlot(name="s", kind="files", depth=3), cwd) == [
        "README.md", "notes.txt", "srcdir/app.py", "srcdir/nested/deep.py"]
    assert discover(PathSlot(name="s", kind="files", depth=3, glob="*.py"), cwd) == [
        "srcdir/app.py", "srcdir/nested/deep.py"]
    assert discover(PathSlot(name="s", kind="dirs", depth=2), cwd) == ["srcdir", "srcdir/nested"]
    assert ".hidden" not in discover(PathSlot(name="s", kind="both", depth=3), cwd)


def test_discover_dotfiles_and_base(tmp_path):
    cwd = _tree(tmp_path)
    got = discover(PathSlot(name="s", kind="files", depth=2, include_dotfiles=True), cwd)
    assert ".hidden" in got
    got = discover(PathSlot(name="s", kind="files", base="srcdir", depth=1), cwd)
    assert got == ["srcdir/app.py"]
    with pytest.raises(ValueError, match="escapes"):
        discover(PathSlot(name="s", base="../"), cwd)


def test_discover_cap(tmp_path):
    cwd = _tree(tmp_path)
    with pytest.raises(ValueError, match="max_results"):
        discover(PathSlot(name="s", kind="both", depth=3, max_results=1), cwd)


def test_validate_slot_subpaths(tmp_path):
    cwd = _tree(tmp_path)
    slot = PathSlot(name="s", kind="files", depth=3)
    assert validate_selection("srcdir/app.py", slot, cwd) == "srcdir/app.py"
    with pytest.raises(ValueError, match="expected a file"):
        validate_selection("srcdir", slot, cwd)
    with pytest.raises(ValueError):
        validate_selection("../evil", slot, cwd)
    with pytest.raises(ValueError, match="does not exist|not in current"):
        validate_selection("nope.py", slot, cwd)
