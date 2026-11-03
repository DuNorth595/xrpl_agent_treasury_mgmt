# SPDX-FileCopyrightText: 2026 Justin Douglas
# SPDX-License-Identifier: MIT
"""
The published version and the tag must agree.

A release whose tag says one version and whose package reports another is
a release nobody can install correctly, and the mismatch is invisible
until someone checks the installed metadata against the source.

The tag is checked for EXISTENCE and VERSION AGREEMENT, not for agreement
with HEAD. A release tag points at the release commit; the branch may carry
later commits (a follow-up fix, a docs change) that are not part of the
release. Requiring the tag to equal HEAD made CI fail on every commit pushed
after a release, which is the normal workflow, not a defect.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# tomllib is 3.11+. The CI matrix includes 3.10, so read the values we need
# with a regex rather than adding a dependency to parse a TOML file.
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
INIT = (ROOT / "src" / "xrpl_agent_treasury_mgmt" / "__init__.py").read_text(
    encoding="utf-8"
)


def _pyproject_version() -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT, re.M)
    assert match, "pyproject.toml has no version"
    return match.group(1)


def _module_version() -> str:
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', INIT, re.M)
    assert match, "__init__.py has no __version__"
    return match.group(1)


def test_pyproject_and_module_versions_agree():
    assert _pyproject_version() == _module_version(), (
        f"pyproject says {_pyproject_version()} but the module says "
        f"{_module_version()}"
    )


def test_the_release_tag_is_published():
    """The tag for the declared version must exist on the remote.

    The tag is resolved with `git ls-remote` rather than `git tag --list`,
    because a CI checkout is a shallow clone with no tags fetched. Reading
    the local tag list would therefore pass locally and fail in CI for the
    same commit.

    The check is that the tag EXISTS and that it carries the version the
    package declares. It deliberately does NOT require the tag to equal
    HEAD: a release tag marks the release commit, and the branch may move
    on afterwards. That is the normal workflow, and making CI fail on it
    would mean the repository could never be worked on after a release.
    """
    version = _module_version()
    tag = f"v{version}"

    remote = subprocess.run(
        ["git", "ls-remote", "--tags", "origin", f"{tag}^{{}}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert remote.returncode == 0, f"could not query the remote for {tag}"
    assert remote.stdout.strip(), (
        f"{tag} is not published, but the package declares {version}. "
        "Tag the release, or the version is wrong."
    )


def test_the_changelog_has_an_entry_for_this_version():
    version = _module_version()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"[{version}]" in changelog, f"CHANGELOG has no entry for {version}"


def test_no_dev_suffix_is_published():
    """A published package does not report a development version."""
    assert "-dev" not in _module_version(), (
        f"module reports {_module_version()!r}; a release must not carry a "
        "development suffix"
    )


def test_dependencies_are_pinned_to_a_tag():
    """A git dependency on a moving branch can change under an install."""
    for dep in re.findall(r'"([^"]*git\+[^"]*)"', PYPROJECT):
        assert "@v" in dep, f"git dependency is not pinned to a tag: {dep}"
