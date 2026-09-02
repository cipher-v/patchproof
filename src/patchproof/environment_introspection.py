"""Enumerate what a prepared revision workspace can actually import.

Why this exists
---------------

`CandidateTestValidator` rejects any candidate importing a root that is not
"grounded". Before this module, grounding was derived from the deterministic context
bundle alone: the first path component of changed files and snippets, plus whatever
import statements appeared inside the retrieved snippets. That set has nothing to do
with what is installed.

The consequence is a systematic false negative. In the sealed unseen holdout the
cattrs initial candidate was rejected because the import root ``attrs`` was absent
from the context, even though ``attrs`` is a hard runtime dependency of the project
and was importable in any correctly prepared environment. The candidate never ran.

Once dependencies are genuinely installed (see ``patchproof.install_strategy``), the
authoritative answer to "may this candidate import X?" is simply "is X importable in
the prepared environment?". This module answers that question by reading the
workspace's virtual environment directly.

No code is executed
-------------------

Import roots are read from the filesystem layout of ``site-packages`` and from
``*.dist-info/top_level.txt`` metadata. No subprocess is spawned and no repository or
dependency code is imported in order to compute this set, so a hostile package cannot
influence grounding by running code at introspection time.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: A conservative bound so a pathological environment cannot produce an unbounded set.
MAX_IMPORT_ROOTS = 4_096

_IMPORT_ROOT_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def virtual_environment_site_packages(workspace: Path) -> tuple[Path, ...]:
    """Return existing ``site-packages`` directories for a workspace's ``.venv``."""
    venv = workspace / ".venv"
    if not venv.is_dir():
        return ()
    if os.name == "nt":
        candidates = [venv / "Lib" / "site-packages"]
    else:
        candidates = sorted((venv / "lib").glob("python3.*/site-packages"))
    return tuple(path for path in candidates if path.is_dir())


def installed_import_roots(workspace: Path) -> frozenset[str]:
    """Return top-level importable names available in a prepared workspace.

    Returns an empty set when the workspace has no virtual environment, which leaves
    the caller's existing context-derived grounding in force rather than silently
    widening or narrowing it.
    """
    roots: set[str] = set()
    for site_packages in virtual_environment_site_packages(workspace):
        _collect_from_site_packages(site_packages, roots)
        if len(roots) >= MAX_IMPORT_ROOTS:
            break
    return frozenset(sorted(roots)[:MAX_IMPORT_ROOTS])


def _collect_from_site_packages(site_packages: Path, roots: set[str]) -> None:
    try:
        entries = list(site_packages.iterdir())
    except OSError:
        return
    for entry in entries:
        if len(roots) >= MAX_IMPORT_ROOTS:
            return
        name = entry.name
        if name.endswith((".dist-info", ".egg-info")):
            _collect_from_metadata(entry, roots)
            continue
        if entry.is_dir():
            if _IMPORT_ROOT_PATTERN.fullmatch(name):
                roots.add(name)
            continue
        stem = name.split(".", maxsplit=1)[0]
        if name.endswith((".py", ".pyd", ".so")) and _IMPORT_ROOT_PATTERN.fullmatch(stem):
            roots.add(stem)


def _collect_from_metadata(distribution: Path, roots: set[str]) -> None:
    """Read declared top-level names without importing the distribution."""
    top_level = distribution / "top_level.txt"
    try:
        raw = top_level.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        name = line.strip()
        if _IMPORT_ROOT_PATTERN.fullmatch(name):
            roots.add(name)


def standard_library_roots() -> frozenset[str]:
    """Return the interpreter's standard-library top-level module names plus pytest."""
    return frozenset(sys.stdlib_module_names) | {"pytest"}
