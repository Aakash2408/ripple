"""Which PROJECT owns this file, and where should the validator run?

WHY THIS IS THE MONOREPO FEATURE
There is no separate "monorepo support" to build. A monorepo is just a repository
where the answer to "which project owns this file" is not "the root", and getting
that answer right IS the feature. Everything else -- workspace manifests, project
references, blast radius -- is refinement on top of it.

WHY THE ROOT IS THE WRONG ANSWER, ALWAYS
`tsc` run at a monorepo root does one of two useless things:

    the root tsconfig EXCLUDES the package   -> the changed file is never checked, so
                                                a broken fix validates clean
    the root tsconfig INCLUDES everything    -> thousands of unrelated files, minutes
                                                of compile, and errors from code the
                                                fix never touched

Both produce a confident verdict about the wrong thing, which is worse than admitting
we cannot validate. So a file with no owning project returns None and the caller
degrades to REVIEW with the reason stated -- it never falls back to the root.

RESOLUTION IS DRIVEN BY THE FILE'S LANGUAGE, NOT BY THE NEAREST MANIFEST
"Nearest manifest wins" alone is wrong in a polyglot repo: a `.ts` file two
directories under a `go.mod` would resolve to a Go project. So the file's language is
detected first and only THAT language's manifests are considered.

TWO ROOTS, BECAUSE WORKSPACES HOIST
`app/validation.py` requires package.json AND tsconfig.json in the SAME directory.
pnpm and yarn workspaces routinely violate that: the package holds tsconfig.json
while node_modules and the lockfile live at the workspace root. So `root` (where the
compiler config is, where the validator should run) and `deps_root` (where
dependencies resolve from) are reported SEPARATELY, and when they differ the caller
can see it rather than discovering it as a mysterious UNABLE_TO_VALIDATE.

Making the validator work across that split is deliberately not done here. Reporting
the split honestly is; pretending the two coincide is what produces the mystery.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .languages import detect as _detect_language

#: language -> (compiler-config manifests, dependency manifests), each nearest-first.
#: A language absent here has no resolution, so a file in it gets REVIEW rather than
#: a guessed project. Adding a language means adding a row, which is the one edit.
MANIFESTS: dict = {
    "typescript": (("tsconfig.json",), ("package.json",)),
    "javascript": (("jsconfig.json", "tsconfig.json"), ("package.json",)),
    "python": (("pyproject.toml", "setup.cfg", "setup.py"),
               ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile")),
    "go": (("go.mod",), ("go.mod",)),
    "rust": (("Cargo.toml",), ("Cargo.toml",)),
}

RESOLVED_LANGUAGES = tuple(MANIFESTS)


@dataclass(frozen=True)
class Project:
    #: Absolute path the validator should run in -- where the compiler config is.
    root: str
    language: str
    #: Manifest filenames actually present at `root`.
    found: tuple
    #: Nearest directory at or above `root` holding a dependency manifest. Empty when
    #: none exists. DIFFERENT from `root` in a hoisted workspace, which is the case
    #: that silently breaks validation if nobody looks.
    deps_root: str
    #: Human-readable, for the PR body and for blocked reasons.
    reason: str

    @property
    def rel_root(self) -> str:
        """`root` relative to the tree, for display. Empty string means the root."""
        return self._rel

    def as_detail(self) -> dict:
        return {"project_root": self._rel or ".",
                "project_language": self.language,
                "project_manifests": list(self.found),
                "deps_root_differs": bool(self.deps_root)
                and os.path.normpath(self.deps_root) != os.path.normpath(self.root)}


def _walk_up(tree: str, start_dir: str):
    """Directories from `start_dir` up to and including `tree`. Never above it.

    Bounded at the tree root deliberately: an archive path that escaped upward would
    otherwise let resolution read manifests outside the extracted tree.
    """
    tree = os.path.realpath(tree)
    here = os.path.realpath(start_dir)
    if not (here == tree or here.startswith(tree + os.sep)):
        return
    while True:
        yield here
        if here == tree:
            return
        parent = os.path.dirname(here)
        if parent == here:
            return
        here = parent


def _first_dir_with(tree: str, start_dir: str, names) -> tuple:
    """(directory, matching filenames) for the nearest ancestor holding any of them."""
    for directory in _walk_up(tree, start_dir):
        present = tuple(n for n in names
                        if os.path.isfile(os.path.join(directory, n)))
        if present:
            return directory, present
    return "", ()


def resolve(tree: str, rel_path: str) -> Project | None:
    """The project owning `rel_path`, or None when nothing does.

    None means REVIEW with a reason -- never "use the repo root".
    """
    language = _detect_language(rel_path)
    spec = MANIFESTS.get(language)
    if spec is None:
        return None

    config_names, deps_names = spec
    abs_path = os.path.realpath(os.path.join(tree, rel_path))
    real_tree = os.path.realpath(tree)
    if not abs_path.startswith(real_tree + os.sep):
        return None                       # outside the tree; refuse to resolve

    start = os.path.dirname(abs_path)
    root, found = _first_dir_with(tree, start, config_names)
    if not root:
        # No compiler config anywhere above the file. A dependency manifest alone is
        # not a project a compiler can be pointed at.
        return None

    deps_root, deps_found = _first_dir_with(tree, root, deps_names)

    rel = os.path.relpath(root, real_tree)
    rel = "" if rel == "." else rel
    hoisted = bool(deps_root) and os.path.normpath(deps_root) != os.path.normpath(root)
    reason = (f"{language} project at {rel or '<repo root>'} "
              f"(found {', '.join(found)})")
    if hoisted:
        reason += (f"; dependencies resolve from "
                   f"{os.path.relpath(deps_root, real_tree)} -- a hoisted workspace")
    elif not deps_root:
        reason += "; no dependency manifest found, so a toolchain may not install"

    project = Project(root=root, language=language,
                      found=tuple(found) + tuple(deps_found if hoisted else ()),
                      deps_root=deps_root, reason=reason)
    object.__setattr__(project, "_rel", rel)
    return project


def group(tree: str, rel_paths) -> tuple:
    """(projects -> files, unresolved files).

    Grouping matters for correctness, not tidiness: one breaking change can touch
    several packages in a monorepo, and each must be validated in ITS OWN project.
    Validating them together at the root is the failure this module exists to prevent.
    """
    grouped: dict = {}
    unresolved = []
    for rel in rel_paths:
        project = resolve(tree, rel)
        if project is None:
            unresolved.append(rel)
            continue
        grouped.setdefault(project.root, (project, []))[1].append(rel)
    return grouped, unresolved
