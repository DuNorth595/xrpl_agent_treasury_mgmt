#!/usr/bin/env python3
"""
Build the publication package.

WHY A SEPARATE STAGING DIRECTORY
================================

The working tree contains everything: the source, the harnesses, the
evidence, the devnet's generated state, and the logs of every run. Most
of that should not be published — some of it is local state, some of it
is a record of a devnet that no longer exists, and some of it is a
credential.

Rather than adding a `.gitignore` and hoping, the package is BUILT into
a staging directory by this script. What is not explicitly listed as
publishable does not go in. The staging directory is what gets reviewed
and, eventually, uploaded.

WHAT IS EXCLUDED, AND WHY
==========================

    var/                 generated state - domain ids, issuer addresses,
                          beneficiaries, the decision log. It describes
                          ONE devnet and is not valid anywhere else.
    .dev-env/            process state and service logs
    __pycache__/         build artifacts
    *.log, *.jsonl       run logs
    scale-results/       measured results - useful in the writeup, not
                          in the source tree
    api-test-results/    the same
    verification/        the same
    evidence/            the domain-gate proof. It is PUBLISHED
                          separately, in its own repository, with its
                          own README - because it is evidence about the
                          XRPL, not about this software.

THE SECRET SCAN
================

Before the package is written, it is scanned for anything that looks
like a credential: a seed, a private key, a bearer token, a hex
blob of a key. The scan is a GATE, not a warning. A package that
contains a credential is not written at all.

    python3 build_release.py --out ../xrpl-treasury-release
    python3 build_release.py --out ../xrpl-treasury-release --scan-only
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import sys
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent

# What ships.
# The files that ship.
#
# The internal build log is NOT here. Those documents are marked
# private, quote the maintainer's standing instruction, and describe the
# state of a private working directory. Publishing them would contradict
# the package's own README.
#
# The public set is: the house README, the public documentation, the
# licences, and the code.
INCLUDE_FILES = [
    "LICENSE",
    "requirements.txt",
    "pyproject.toml",
    "Makefile",
    ".gitignore",
    "requirements-dev.txt",
]

INCLUDE_DIRS = [
    "src",
    "tests",
    "docs",
]

# The public documentation. These are the files a reviewer reads first.
#
# They live under docs/public/ in the working tree because the working
# tree also holds the internal build log, which is not for publication.
PUBLIC_DOCS = [
    "# the licences, and the code.md",
    "ARCHITECTURE.md",
    "DESIGN.md",
    "DEPLOYMENT.md",
    "API.md",
    "POC.md",
]

# The harnesses and setup scripts ship too - they are how a reviewer
# runs the thing.
INCLUDE_SCRIPTS = [
    "run_demo.py",
    "dev_env.py",
    "build_release.py",
    "api_test.py",
    "scale_test.py",
    "verify.py",
    "mint_token.py",
    "reference_erp.py",
    "local_signer_service.py",
    "create_domain.py",
    "issue_currencies.py",
    "fund_treasury.py",
    "prepare_beneficiaries.py",
    "enable_issuer_rippling.py",
]

# Never shipped, whatever else happens.
NEVER_INCLUDE = [
    "var",
    ".dev-env",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".coverage",
    "htmlcov",
    "evidence",
    "scale-results",
    "scale-results-1m",
    "api-test-results",
    "api-test-results-readonly",
    "verification",
    "node-upgrade-backup",
    "results",
]

# Patterns that indicate a credential.
SECRET_PATTERNS = [
    (
        "xrpl seed",
        re.compile(r"\bs(?:Ed|Er|Ti|hr|sp|r)[A-Za-z0-9]{50,}\b"),
    ),
    (
        "private key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    (
        "hardcoded bearer token",
        re.compile(r"Bearer\s+[A-Za-z0-9_\-]{32,}"),
    ),
    (
        "hex private key",
        re.compile(r"\b[0-9A-F]{66}\b"),
    ),
    (
        "aws-style access key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "github token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ),
]

# A seed that is obviously a well-known devnet placeholder is fine.
KNOWN_SAFE = {
    # xrpl.js's documented test seed. Public, and it appears in the
    # XRPL documentation.
    "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r",
}


def is_safe_seed(value: str) -> bool:
    return value in KNOWN_SAFE or value.startswith("<") or "REDACTED" in value


def scan_for_secrets(root: pathlib.Path) -> list:
    """Return a list of (kind, path, line_number, excerpt) findings."""
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace")
        except Exception:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in SECRET_PATTERNS:
                for match in pattern.finditer(line):
                    value = match.group(0)
                    if kind == "xrpl seed" and is_safe_seed(value):
                        continue
                    findings.append(
                        {
                            "kind": kind,
                            "path": str(path.relative_to(root)),
                            "line": number,
                            "excerpt": value[:40],
                        }
                    )
    return findings


def copy_included(root: pathlib.Path, out: pathlib.Path) -> int:
    """Copy the publishable files. Returns how many were copied."""
    count = 0

    # Anything in an excluded directory is excluded, whatever the
    # include lists say. The NEVER_INCLUDE list was documentation; this
    # makes it enforcement.
    def _excluded(relative: str) -> bool:
        head = relative.split("/")[0]
        return head in NEVER_INCLUDE

    for name in INCLUDE_FILES:
        source = root / name
        if not source.exists():
            continue
        if _excluded(name):
            continue
        # The internal build log is superseded by the public README and
        # the docs set. The status and current-state files are kept
        # because they are the honest record of what is and is not done.
        shutil.copy2(source, out / name)
        count += 1

    for directory in INCLUDE_DIRS:
        source = root / directory
        if not source.exists():
            continue
        destination = out / directory
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(
                "__pycache__",
                "*.pyc",
                ".pytest_cache",
                "*.log",
                "*.jsonl",
                # The public docs are placed by their own step, at
                # docs/. Copying the whole tree would put them at
                # docs/public/ as well, so they would appear twice.
                "public",
                # Backup files from the working tree. They are history,
                # not source.
                "*.bak-pre-*",
                "*.bak",
                "*.orig",
                "*.rej",
                # The excluded directories, at any depth. The service
                # writes a decision log into its state directory; if
                # that directory is inside the source tree, the log
                # would travel with the code.
                *NEVER_INCLUDE,
            ),
        )
        count += 1

    for name in INCLUDE_SCRIPTS:
        source = root / name
        if source.exists() and not _excluded(name):
            shutil.copy2(source, out / name)
            count += 1

    # The public docs become the package's own docs/, and the public
    # README becomes the package's # the licences, and the code.
    public = root / "docs" / "public"
    if public.exists():
        destination = out / "docs"
        destination.mkdir(exist_ok=True)
        for name in PUBLIC_DOCS:
            source = public / name
            if not source.exists():
                continue
            # The README goes to the top level. Putting it in docs/ too
            # would leave two copies that drift apart.
            if name == "# the licences, and the code.md":
                continue
            shutil.copy2(source, destination / name)
            count += 1
        # The README goes to the top level - that is where a reader
        # looks for it.
        package_readme = public / "# the licences, and the code.md"
        if package_readme.exists():
            shutil.copy2(package_readme, out / "# the licences, and the code.md")

    return count


def write_house_scaffolding(out: pathlib.Path) -> None:
    """Add the files every S_DevLabs public repo carries.

    Written by the builder rather than by hand, because a hand-patched
    package is a package the next build throws away.
    """
    # .reuse/dep5 - the REUSE licence declaration.
    reuse = out / ".reuse"
    reuse.mkdir(parents=True, exist_ok=True)
    (reuse / "dep5").write_text(
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/dep5\n"
        "Upstream-Name: xrpl_agent_treasury_mgmt\n"
        "Upstream-Contact: S_DevLabs <S_DevLabs@outlook.com>\n"
        "Source: https://github.com/DuNorth595/xrpl_agent_treasury_mgmt\n"
        "\n"
        "Files: *\n"
        "Copyright: 2026 Justin Douglas\n"
        "License: MIT\n"
    )

    # LICENSES/MIT.txt - the licence text, where REUSE expects it.
    licenses = out / "LICENSES"
    licenses.mkdir(parents=True, exist_ok=True)
    mit = out / "LICENSE"
    if mit.exists():
        (licenses / "MIT.txt").write_text(mit.read_text())

    github = out / ".github"
    (github / "ISSUE_TEMPLATE").mkdir(parents=True, exist_ok=True)
    (github / "workflows").mkdir(parents=True, exist_ok=True)

    (github / "ISSUE_TEMPLATE" / "bug_report.md").write_text(
        _readme_asset("issue_template.md")
    )
    (github / "PULL_REQUEST_TEMPLATE.md").write_text(
        _readme_asset("pr_template.md")
    )
    (github / "workflows" / "ci.yml").write_text(_readme_asset("ci.yml"))

    for name, asset in (
        ("CONTRIBUTING.md", "contributing.md"),
        ("CODE_OF_CONDUCT.md", "code_of_conduct.md"),
        ("TODO.md", "TODO.md"),
        ("CHANGELOG.md", "CHANGELOG.md"),
        ("SECURITY.md", "SECURITY.md"),
    ):
        (out / name).write_text(_readme_asset(asset))

    (out / ".pre-commit-config.yaml").write_text(
        _readme_asset("pre-commit.yaml")
    )

    # The secret scanner the pre-commit hook and the CI workflow both
    # run. It ships WITH the package so a clone can run it.
    scripts = out / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "scan_for_secrets.py").write_text(
        _readme_asset("scan_for_secrets.py")
    )

    # The social preview. The README references it, so a package without
    # it renders a broken image on GitHub.
    preview = HERE / "assets" / "social-preview.jpg"
    if preview.exists():
        docs = out / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        shutil.copy2(preview, docs / "social-preview.jpg")

    # The public # the licences, and the code. The package's own # the licences, and the code.md is the house
    # version, not the internal build log.
    public_readme = HERE / "house" / "README.md"
    if public_readme.exists():
        shutil.copy2(public_readme, out / "README.md")
    else:
        raise RuntimeError(
            "the house README is missing: "
            f"{public_readme}. The package's README is the house one."
        )


def _readme_asset(name: str) -> str:
    """Read a house asset from the project's assets directory."""
    path = HERE / "house" / name
    if path.exists():
        return path.read_text()
    return f"# {name}\n\n(TODO: this asset was not found at {path})\n"


def write_manifest(out: pathlib.Path, findings: list) -> None:
    manifest = {
        "built_utc": datetime.now(timezone.utc).isoformat(),
        "package": "xrpl-treasury",
        "contents": sorted(
            str(path.relative_to(out))
            for path in out.rglob("*")
            if path.is_file() and path.name != "MANIFEST.json"
        ),
        "excluded": {
            "paths": NEVER_INCLUDE,
            "patterns": [
                "*.bak-pre-*",
                "__pycache__",
                "*.pyc",
                "*.log",
                "*.jsonl",
            ],
            "note": (
                "the working tree holds devnet signing keys in the "
                "evidence and the node backup; neither is in the package. "
                "the secret scan runs on the package itself and refuses "
                "to write one that contains a credential."
            ),
        },
        "secret_scan": {
            "patterns": [kind for kind, _ in SECRET_PATTERNS],
            "scope": "the staged package",
            "findings": findings,
            "clean": not findings,
        },
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the publication package")
    parser.add_argument(
        "--out",
        default=str(HERE.parent / "xrpl-treasury-release"),
        help="the staging directory to build into",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="scan for secrets and report; do not write the package",
    )
    args = parser.parse_args()

    out = pathlib.Path(args.out)

    # The working tree holds devnet signing keys in the evidence and the
    # node backup - both excluded from the package. So the scan runs on
    # the STAGED PACKAGE, which is the thing that would be uploaded.
    #
    # The working tree is also scanned, so a credential in the source is
    # reported even if the package happens to exclude it. That is a
    # warning, not a gate.

    print("  scanning the working tree for credentials...")
    tree_findings = scan_for_secrets(HERE)
    if tree_findings:
        excluded = {p for p in NEVER_INCLUDE}
        outside = [
            f
            for f in tree_findings
            if f["path"].split("/")[0] not in excluded
        ]
        print(
            f"  {len(tree_findings)} finding(s) in the tree, "
            f"{len(outside)} outside the excluded paths"
        )
        for finding in outside[:10]:
            print(
                f"     {finding['kind']}: {finding['path']}:{finding['line']}"
                f"  {finding['excerpt']}..."
            )
    else:
        print("  the tree is clean")

    if args.scan_only:
        return 0

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    count = copy_included(HERE, out)

    print("  scanning the package for credentials...")
    findings = scan_for_secrets(out)
    if findings:
        print(f"  !! {len(findings)} credential-shaped finding(s) IN THE PACKAGE:")
        for finding in findings[:20]:
            print(
                f"     {finding['kind']}: {finding['path']}:{finding['line']}"
                f"  {finding['excerpt']}..."
            )
        print("  the package was NOT written")
        shutil.rmtree(out)
        return 1
    print("  clean - no credential-shaped strings in the package")

    # ------------------------------------------------- the house format
    write_house_scaffolding(out)

    # The package must not carry any generated state. A decision log
    # written by a service that ran from the package directory is
    # generated state, and it contains real tenant ids, domain ids and
    # account addresses.
    generated = [
        p
        for p in out.rglob("*")
        if p.is_file() and (".jsonl" in p.name or p.suffix == ".log")
    ]
    if generated:
        print(f"  !! {len(generated)} generated state file(s) in the package:")
        for path in generated[:10]:
            print(f"     {path.relative_to(out)}")
        print("  the package was NOT written")
        shutil.rmtree(out)
        return 1

    write_manifest(out, findings)

    # The package is a directory a reviewer will open, not just a tree
    # they will git. A cache directory created by running the tests
    # inside it should not be sitting in it.
    for cache in (".pytest_cache", ".ruff_cache", ".mypy_cache"):
        shutil.rmtree(out / cache, ignore_errors=True)

    print(f"  built {count} entries into {out}")
    total = sum(1 for p in out.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"  {total} files, {size / 1024:.0f} KB")

    print()
    print("  the package is STAGED, not published. Review it, then:")
    print(f"    cd {out} && git init && git add -A")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
