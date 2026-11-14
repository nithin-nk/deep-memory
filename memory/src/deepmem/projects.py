"""Decides which project a captured window, or a calling agent, belongs to.

A project is a git repository on this machine, named by its directory. No
model is involved: the label comes from file paths and repository URLs seen
in the window. Everything without such a signal is `personal`.
"""

import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

PERSONAL = "personal"

_PATH = re.compile(r"(?:~|/Users/[\w.-]+)(?:/[\w.@+-]+)+")
_REPO_URL = re.compile(r"https?://(?:www\.)?(?:github\.com|gitlab\.com|bitbucket\.org)/[\w.-]+/([\w.-]+)")
_TITLE_SEPARATORS = re.compile(r" [—–-] ")
_MAX_PATHS = 300  # per window; enough to see what it is about


@lru_cache(maxsize=None)
def _repo_of_dir(directory: Path) -> str | None:
    """Name of the git repository containing `directory`, looking only below
    the home directory (a home directory under version control is not a project)."""
    home = Path.home()
    if home not in directory.parents:
        return None
    git = directory / ".git"
    if git.is_dir():
        return directory.name
    if git.is_file():  # worktree or submodule: `.git` is a pointer to the main repository
        pointer = re.match(r"gitdir: (.*?)/\.git/", git.read_text(errors="ignore"))
        return Path(pointer.group(1)).name if pointer else directory.name
    return _repo_of_dir(directory.parent)


def of_path(path: str | Path) -> str | None:
    """Project of a file or directory path, or None if it is in no repository."""
    path = Path(path).expanduser()
    return _repo_of_dir(path) or _repo_of_dir(path.parent)


def of_window(title: str, url: str | None, text: str, known: set[str]) -> str:
    """Project of one captured window. `known` holds project names seen so
    far and gains any new one found here."""
    # ponytail: projects are keyed by directory name, so two repositories with
    # the same name share memories. Key by remote URL if that ever happens.
    for match in _PATH.finditer(title):  # terminals show the working directory
        if project := of_path(match.group(0)):
            known.add(project)
            return project
    if url and (repo := _REPO_URL.match(url)):
        name = repo.group(1).removesuffix(".git")
        if name in known:  # only repositories that also exist on this machine
            return name
    for segment in _TITLE_SEPARATORS.split(title):  # editors: "file.py — project"
        if segment.strip() in known:
            return segment.strip()
    counts = Counter()
    for index, match in enumerate(_PATH.finditer(text)):
        if index >= _MAX_PATHS:
            break
        if project := of_path(match.group(0)):
            counts[project] += 1
    if counts:
        project = counts.most_common(1)[0][0]
        known.add(project)
        return project
    return PERSONAL


def visible_to(caller_dir: str | Path | None, config: dict) -> set[str] | None:
    """Projects an agent working in `caller_dir` may read. None means no limit."""
    project = of_path(caller_dir) if caller_dir else None
    if project is None:
        outside = config["outside_project"]
        return None if outside == "all" else {PERSONAL} if outside == "personal" else set()
    return {project, PERSONAL} if config["share_personal"] else {project}


def directory_name(project: str) -> str:
    """Safe directory name for a project's memories."""
    return re.sub(r"[^\w.-]", "_", project) or PERSONAL
