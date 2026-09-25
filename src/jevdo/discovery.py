"""File discovery: CWD listing + recursive per-slot discovery.

- list_candidates(kind, cwd): legacy top-level listing, dotfiles skipped.
- discover(slot, cwd): recursive walk honoring base/depth/glob/dotfiles/cap.
  Returns sorted relative posix subpaths.
- validate_selection(name, kind_or_slot, cwd): fail-closed revalidation.
"""

from __future__ import annotations

import fnmatch
import os


def list_candidates(kind: str, cwd: str = ".") -> list[str]:
    """List candidate names in cwd for kind in {files, dirs, both}.

    Non-recursive. Skips dotfiles. Returns sorted bare names (no paths).
    """
    if kind not in ("files", "dirs", "both"):
        raise ValueError(f"unknown path kind: {kind!r}")
    files: list[str] = []
    dirs: list[str] = []
    with os.scandir(cwd) as it:
        for entry in it:
            name = entry.name
            if name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(name)
                elif entry.is_file(follow_symlinks=False):
                    files.append(name)
            except OSError:
                continue
    if kind == "files":
        return sorted(files)
    if kind == "dirs":
        return sorted(dirs)
    return sorted(files + dirs)


def _has_dot_segment(rel: str) -> bool:
    return any(seg.startswith(".") for seg in rel.split("/"))


def discover(slot, cwd: str = ".") -> list[str]:
    """Walk slot.base up to slot.depth, filter by kind/glob. Returns relpaths.

    depth=1 => top level only (v1 behavior). Symlinked dirs are not followed.
    Raises ValueError when base escapes cwd or results exceed max_results.
    """
    base = getattr(slot, "base", ".") or "."
    depth = getattr(slot, "depth", 1) or 1
    kind = getattr(slot, "kind", "files")
    glob = getattr(slot, "glob", None)
    include_dotfiles = getattr(slot, "include_dotfiles", False)
    max_results = getattr(slot, "max_results", 200) or 200

    root = os.path.realpath(os.path.join(cwd, base))
    cwd_real = os.path.realpath(cwd)
    if root != cwd_real and not root.startswith(cwd_real + os.sep):
        raise ValueError(f"slot base {base!r} escapes working directory")
    if not os.path.isdir(root):
        return []

    out: list[str] = []
    # stack of (abs_dir, rel_prefix, level) where level=1 lists root's children
    stack = [(root, "" if base in (".", "") else base.replace(os.sep, "/"), 1)]
    while stack:
        abs_dir, rel_prefix, level = stack.pop()
        try:
            entries = sorted(os.scandir(abs_dir), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            rel = f"{rel_prefix}/{name}" if rel_prefix else name
            if not include_dotfiles and (name.startswith(".") or _has_dot_segment(rel)):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if kind in ("dirs", "both") and _match_glob(glob, name, rel):
                    out.append(rel)
                if level < depth:
                    stack.append((os.path.join(abs_dir, name), rel, level + 1))
            elif is_file:
                if kind in ("files", "both") and _match_glob(glob, name, rel):
                    out.append(rel)
            if len(out) > max_results:
                raise ValueError(
                    f"too many candidates ({len(out)} > max_results={max_results}); "
                    "narrow 'glob', lower 'depth', or raise 'max_results'"
                )
    return sorted(out)


def _match_glob(glob: str | None, basename: str, relpath: str) -> bool:
    if not glob:
        return True
    # match basename, or full relpath when pattern contains a slash
    if "/" in glob:
        return fnmatch.fnmatch(relpath, glob)
    return fnmatch.fnmatch(basename, glob)


def validate_selection(name: str, kind_or_slot, cwd: str = ".") -> str:
    """Re-check a planned path against the live filesystem. Raises ValueError.

    kind_or_slot: "files"|"dirs"|"both" (legacy, top-level names only) or a
    PathSlot (recursive relpaths allowed). Guards: empty, absolute, NUL,
    dotfiles (unless slot allows), ".." escape, wrong kind, not in fresh listing.
    """
    if not name or not isinstance(name, str):
        raise ValueError("empty path selection")
    if "\0" in name or "\n" in name:
        raise ValueError(f"invalid path selection: {name!r}")
    if os.path.isabs(name):
        raise ValueError(f"absolute paths not allowed: {name!r}")
    norm = name.replace("\\", "/")
    segs = [s for s in norm.split("/") if s not in ("", ".")]
    if not segs or any(s == ".." for s in segs):
        raise ValueError(f"invalid path selection: {name!r}")

    if isinstance(kind_or_slot, str):
        kind = kind_or_slot
        include_dotfiles = False
        if len(segs) != 1:
            raise ValueError(f"invalid path selection: {name!r}")
        if segs[0].startswith("."):
            raise ValueError(f"dotfile not allowed: {name!r}")
        full = os.path.join(cwd, segs[0])
        is_dir = os.path.isdir(full)
        if kind == "files" and is_dir:
            raise ValueError(f"{name!r} is a directory, expected a file")
        if kind == "dirs" and not is_dir:
            raise ValueError(f"{name!r} is not a directory")
        fresh = list_candidates(kind, cwd)
        if segs[0] not in fresh:
            raise ValueError(f"path {name!r} not in current {kind} listing")
        return segs[0]

    slot = kind_or_slot
    if not slot.include_dotfiles and any(s.startswith(".") for s in segs):
        raise ValueError(f"dotfile not allowed: {name!r}")
    rel = "/".join(segs)
    # containment: realpath must stay inside cwd
    full = os.path.realpath(os.path.join(cwd, rel))
    cwd_real = os.path.realpath(cwd)
    if full != cwd_real and not full.startswith(cwd_real + os.sep):
        raise ValueError(f"path escapes working directory: {name!r}")
    if not os.path.lexists(full):
        raise ValueError(f"path {name!r} does not exist")
    is_dir = os.path.isdir(full)
    if slot.kind == "files" and is_dir:
        raise ValueError(f"{name!r} is a directory, expected a file")
    if slot.kind == "dirs" and not is_dir:
        raise ValueError(f"{name!r} is not a directory")
    fresh = discover(slot, cwd)
    if rel not in fresh:
        raise ValueError(f"path {name!r} not in current candidates")
    return rel
