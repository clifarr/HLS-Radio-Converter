"""Metadata consistency: the URLs and claims the project publishes.

These exist because the v2 rewrite was written with a *guessed* repository URL
(``github.com/cliff/...``) while the real remote is ``github.com/clifarr/...``.
That wrong URL shipped into the README, all the ``git clone`` instructions and
``pyproject.toml``'s project URLs, where nobody would notice until a user
tried to follow the instructions.

Nothing here tests behaviour - it tests that what we *tell* people is true.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: The single source of truth. If the project ever moves, change it here and
#: the failure messages below will point at every place that must follow.
CANONICAL_REPO = "https://github.com/clifarr/HLS-Radio-Converter"


def _read(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


# --------------------------------------------------------------------- remote
def test_canonical_url_matches_the_actual_git_remote():
    """Guard against documenting a repository that does not exist.

    The remote is the authoritative answer to "where does this live?", so
    comparing against it catches the whole class of mistake this module is
    about. Skipped when there is no git checkout (e.g. an unpacked sdist).
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        # ty mis-resolves pytest.skip's signature (see tests/conftest.py).
        pytest.skip("git is not available")  # ty: ignore[too-many-positional-arguments]

    if result.returncode != 0:
        pytest.skip("not a git checkout")  # ty: ignore[too-many-positional-arguments]

    remote = result.stdout.strip().removesuffix(".git").removesuffix("/")
    assert remote == CANONICAL_REPO, (
        f"the git remote is {remote!r} but this test says {CANONICAL_REPO!r}. "
        "Update CANONICAL_REPO (and everything it guards) to match."
    )


# ------------------------------------------------------------------ pyproject
def test_project_urls_point_at_the_canonical_repo():
    pyproject = _read("pyproject.toml")
    section = pyproject.split("[project.urls]", 1)
    assert len(section) == 2, "pyproject.toml has no [project.urls] section"
    urls_block = section[1].split("\n[", 1)[0]

    found = re.findall(r'=\s*"(https?://[^"]+)"', urls_block)
    assert found, "no URLs declared in [project.urls]"
    for url in found:
        assert url.startswith(CANONICAL_REPO), (
            f"[project.urls] advertises {url!r}, which is not under {CANONICAL_REPO}"
        )


# --------------------------------------------------------------------- README
def test_readme_has_no_stale_github_urls():
    """Every github.com URL in the README must belong to this project."""
    readme = _read("README.md")
    urls = set(re.findall(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", readme))

    allowed_prefixes = (
        CANONICAL_REPO,
        "https://github.com/actions/",
        "https://github.com/astral-sh/",
    )
    unexpected = {u for u in urls if not u.startswith(allowed_prefixes)}
    assert not unexpected, f"README links to unexpected repositories: {sorted(unexpected)}"


def test_readme_clone_instructions_use_the_canonical_repo():
    readme = _read("README.md")
    clones = re.findall(r"git clone (\S+)", readme)
    assert clones, "expected at least one git clone instruction"
    for target in clones:
        assert target.startswith(CANONICAL_REPO), f"README says to clone {target!r}"


def test_installer_uses_the_canonical_repo():
    installer = _read("packaging/install.sh")
    assert f'PROJECT_URL="{CANONICAL_REPO}"' in installer


def test_default_user_agent_points_at_the_canonical_repo():
    """The UA is sent to every upstream CDN, so a stale URL is user-visible."""
    from radioconv.config import Settings

    assert CANONICAL_REPO in Settings().user_agent


# ------------------------------------------------------------------- shipped
#: Anything that ends up in the package, in the repo, or on a user's machine.
#: ``tests/`` is deliberately excluded: it is not shipped, and this module's own
#: docstring quotes the wrong URL in order to explain the bug it guards against.
SHIPPED_PATHS = ("pyproject.toml", "README.md", "CONTRIBUTING.md", "src", "packaging", "scripts")

#: GitHub owners that shipped files are allowed to link to. Everything here is a
#: deliberate decision, not a default: if a new third-party link is needed, add
#: its owner and explain why.
ALLOWED_GITHUB_OWNERS = {"clifarr"}


def _shipped_files():
    for entry in SHIPPED_PATHS:
        path = PROJECT_ROOT / entry
        if path.is_file():
            yield path
            continue
        for candidate in sorted(path.rglob("*")):
            if candidate.is_file() and "__pycache__" not in candidate.parts:
                yield candidate


def test_no_shipped_file_points_at_a_foreign_repository():
    """Every GitHub owner we ship must be a deliberate choice.

    The v2 rewrite was written against a guessed URL (``github.com/cliff/...``)
    while the real remote is ``clifarr``. The original guards covered a
    hand-written list of files, so two shipped artefacts were missed:

    * ``packaging/radioconv.service`` - shown by ``systemctl status radioconv``;
    * ``packaging/config.toml.example`` - installed as
      ``/etc/radioconv/config.toml``, whose ``user_agent`` is sent to every
      upstream CDN.

    Scanning everything we ship, rather than named files, is the point: the next
    file added should not need someone to remember to add it to a list.
    """
    pattern = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)")
    offenders: list[str] = []
    for path in _shipped_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in pattern.finditer(text):
            owner = match.group(1)
            if owner not in ALLOWED_GITHUB_OWNERS:
                rel = path.relative_to(PROJECT_ROOT)
                offenders.append(f"{rel}: {match.group(0)}")
    assert not offenders, (
        "shipped files link to unexpected GitHub owners:\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n(if a link is intentional, add its owner to ALLOWED_GITHUB_OWNERS)"
    )


def test_readme_documents_the_api_control_parameters():
    """A user must be able to find the URL controls without reading source.

    The README is the first thing people read, so it has to name the three
    parameters a user can put in a radio's URL. The deeper API description -
    that streams never end, that `/probe` is a dry run - is asserted against the
    OpenAPI document in tests/test_api_contract.py, which is where it lives now
    that the README is a user document rather than an API manual.
    """
    readme = _read("README.md")

    # Checked as they are actually written in a radio's URL field, not as bare
    # names: a user needs to see the syntax, not a glossary entry.
    for control in ("name=", "br=", "codec="):
        assert control in readme, f"README does not show the {control.rstrip('=')!r} parameter"

    assert "probe" in readme, "the diagnosis command should be discoverable"
    assert "/docs" in readme, "the generated reference should be discoverable"


def test_readme_does_not_advertise_the_removed_post_endpoint():
    """`POST /` was removed: it needed python-multipart and contradicted its label."""
    readme = _read("README.md")
    assert "POST /" not in readme
    assert 'method="post"' not in readme
    api = _read("src/radioconv/api.py")
    assert "@app.post" not in api, "the POST form handler should be gone, not dormant"


def test_docs_have_no_broken_internal_links():
    """Every ``#anchor`` and relative ``.md`` link must resolve.

    Guards the split between README.md (users) and CONTRIBUTING.md
    (developers): moving a section silently breaks links to it, which is exactly
    what happened to the old ``#about-ffmpeg`` reference.
    """
    import re

    for name in ("README.md", "CONTRIBUTING.md"):
        text = _read(name)

        # Collect the anchors this file actually provides (GitHub's slug rules).
        anchors = {
            re.sub(r"[^a-z0-9 -]", "", heading.strip().lower()).replace(" ", "-")
            for heading in re.findall(r"^#{1,6} (.+)$", text, flags=re.MULTILINE)
        }

        missing = []
        for target in re.findall(r"\]\((#[A-Za-z0-9._-]+)\)", text):
            if target.lstrip("#") not in anchors:
                missing.append(f"{name} -> {target}")
        assert not missing, "broken internal links:\n  " + "\n  ".join(missing)

        # Relative markdown links must point at files that exist. The optional
        # anchor is a non-capturing group so findall returns plain strings.
        for target in re.findall(r"\]\((?!https?:|#)([A-Za-z0-9._/-]+\.md)(?:#[^)]*)?\)", text):
            if not (PROJECT_ROOT / target).exists():
                missing.append(f"{name} -> {target}")
        assert not missing, "links to missing files:\n  " + "\n  ".join(missing)


def test_the_project_actually_ships_its_licence():
    """A declared licence with no text is not a licence.

    ``pyproject.toml`` said ``license = { text = "MIT" }`` while the repository
    contained no LICENSE file at all - so the metadata asserted MIT and a user
    received nothing. The README compounded it by saying "MIT, as the original",
    which implied the licence was inherited from some upstream project. There is
    no upstream: this repository is the original, so the phrase was meaningless.

    The modern fields are used instead (PEP 639), which also makes the build
    ship the file: ``license = "MIT"`` plus ``license-files = ["LICENSE"]``.
    """
    licence = PROJECT_ROOT / "LICENSE"
    assert licence.exists(), "no LICENSE file: the declared licence has no text"
    text = licence.read_text(encoding="utf-8")

    assert text.startswith("MIT License"), "LICENSE is not the MIT text"
    assert "WITHOUT WARRANTY" in text.upper()
    assert "Clifford Farrugia" in text, "the copyright holder must be named"

    pyproject = _read("pyproject.toml")
    assert 'license = "MIT"' in pyproject, "licence should be an SPDX expression"
    assert 'license-files = ["LICENSE"]' in pyproject, "the build must ship LICENSE"
    # The deprecated table form and the redundant classifier should be gone.
    assert "license = { text" not in pyproject
    assert "License :: OSI Approved" not in pyproject

    # And the README must point at the file rather than asserting a licence.
    readme = _read("README.md")
    assert "[LICENSE](LICENSE)" in readme


def test_docs_do_not_refer_to_a_mysterious_original():
    """Both versions are ours, so neither is "the original" - say "version 1".

    A phrase like "MIT, as the original" reads as though the project inherited
    something from elsewhere, and "the original version" leaves a reader
    wondering which project is meant. The word has now been removed from both
    documents entirely, so the guard is simply that it must not come back - a
    list of the three bad phrases used to miss `# 1. Original: the whole …`,
    which is exactly the sort of label somebody would write next time.
    """
    for name in ("README.md", "CONTRIBUTING.md"):
        text = _read(name)
        found = re.findall(r"\b[Oo]riginal\w*", text)
        assert not found, (
            f"{name} calls version 1 'the original' ({', '.join(sorted(set(found)))}); "
            "say 'version 1' - both versions are ours"
        )


def test_shipped_service_unit_and_config_example_name_the_canonical_repo():
    """The two files that previously carried the wrong URL, pinned directly."""
    assert CANONICAL_REPO in _read("packaging/radioconv.service")
    assert CANONICAL_REPO in _read("packaging/config.toml.example")


def test_readme_screenshots_exist_and_stay_small():
    """A README image that 404s is invisible until somebody visits GitHub.

    The screenshots are the first thing a visitor sees, so they are load-bearing
    documentation rather than decoration - and unlike a broken link in prose,
    nothing here would fail: the file simply would not render. The size cap is
    because these are committed to the repository; an unoptimised screenshot is
    a megabyte of history that every clone pays for.
    """
    readme = _read("README.md")
    sources = re.findall(r'<img src="([^"]+)"', readme)
    sources += re.findall(r"!\[[^\]]*\]\(([^)]+)\)", readme)
    assert sources, "the README has no screenshots"

    for src in sources:
        assert not src.startswith(("http://", "https://")), (
            f"{src} is an external image; it should be committed under docs/"
        )
        path = PROJECT_ROOT / src
        assert path.is_file(), f"{src} is referenced by the README but does not exist"
        assert path.stat().st_size < 700 * 1024, (
            f"{src} is {path.stat().st_size / 1024:.0f} KiB; resize it before committing"
        )

    # Alt text is what a screen reader reads out, and what shows if the image
    # fails to load - so it has to describe the picture, not name the file.
    for alt in re.findall(r'alt="([^"]*)"', readme):
        assert len(alt) > 40, f"a screenshot has unhelpful alt text: {alt!r}"


# --------------------------------------------------------------------- linting
#: Tools and topics that belong in CONTRIBUTING.md, not in a user's README. A
#: user wants to get the thing running; they do not care how it is linted.
DEVELOPER_ONLY_TERMS = (
    "ruff",
    "pytest",
    "ty check",
    "noqa",
    "hatchling",
    "sdist",
    "Makefile",
    "pyproject.toml",
)


def test_readme_stays_a_user_document():
    """The README must stay short and task-focused.

    It had grown to 772 lines: lint configuration, per-rule exclusion
    justifications, a module-by-module architecture table, release steps and a
    benchmark methodology. None of that helps somebody who wants to install it,
    point a radio at it and troubleshoot it - which is the README's whole job -
    so it lives in CONTRIBUTING.md and this guards against it creeping back.
    """
    readme = _read("README.md")
    lowered = readme.lower()

    offenders = [term for term in DEVELOPER_ONLY_TERMS if term.lower() in lowered]
    assert not offenders, (
        "developer tooling in the user README: "
        + ", ".join(offenders)
        + "  (move it to CONTRIBUTING.md)"
    )

    lines = readme.splitlines()
    assert len(lines) < 400, (
        f"README is {len(lines)} lines. Keep user docs short: installation, "
        "pointing a radio at it, troubleshooting. Move detail to CONTRIBUTING.md."
    )


def test_contributing_documents_the_development_workflow():
    """The material taken out of the README must actually exist somewhere."""
    contributing = _read("CONTRIBUTING.md")
    for topic in ("uv sync", "ruff", "ty check", "Layout", "Releasing"):
        assert topic in contributing, f"CONTRIBUTING.md does not cover {topic!r}"
    # And the README must point at it, or nobody will find it.
    assert "CONTRIBUTING.md" in _read("README.md")


def test_lint_claims_in_the_contributing_guide_match_the_configuration():
    """The dev guide must not overstate how clean the lint configuration is.

    It used to say "zero findings and no blanket suppressions" while
    ``[tool.ruff.lint.per-file-ignores]`` exempts the whole of ``tests/**`` from
    several rule families. That exemption is reasonable; describing it as
    "no blanket suppressions" was not.
    """
    contributing = _read("CONTRIBUTING.md")
    assert "no blanket suppressions" not in contributing
    assert "zero findings" in contributing
    # It should point readers at where exclusions are justified.
    assert "pyproject.toml" in contributing


def test_every_ruff_exclusion_is_documented_in_pyproject():
    """Each excluded rule must have a reason written near it.

    CONTRIBUTING.md says the reason sits next to each entry, so check that claim
    rather than trusting it: every ignored rule code and every per-file-ignores
    block should be accompanied by a comment.
    """
    # Through the project's own shim: `tomli` is only installed below Python
    # 3.11, so importing it directly makes this test fail on newer interpreters -
    # which is exactly what happened the first time it ran in CI.
    from radioconv import _toml

    text = _read("pyproject.toml")
    config = _toml.loads(text)["tool"]["ruff"]["lint"]

    commented_lines = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("#"):
            commented_lines.add(number)

    lines = text.splitlines()

    def documented(entry_line: int, window: int = 12) -> bool:
        """A reason may sit above the entry or trail it on the same line.

        Both styles are used in `pyproject.toml`: some rules have a paragraph
        above, others a `# ...` comment after the comma.
        """
        if any(n in commented_lines for n in range(entry_line - window, entry_line)):
            return True
        return "#" in lines[entry_line - 1].split(",", 1)[-1]

    undocumented = []
    for rule in config.get("ignore", []):
        quoted = f'"{rule}"'
        for number, line in enumerate(lines, start=1):
            if quoted in line and rule not in ("UP006", "UP035", "UP045"):
                # The three UP rules share one long explanatory block.
                if not documented(number, window=20):
                    undocumented.append(f"ignore {rule} (line {number})")
                break

    assert not undocumented, "undocumented ruff exclusions:\n  " + "\n  ".join(undocumented)

    # The per-file blocks must be introduced by a comment too.
    for marker in ("[tool.ruff.lint.per-file-ignores]",):
        index = lines.index(marker)
        assert any(lines[i].strip().startswith("#") for i in range(max(0, index + 1), index + 8)), (
            f"{marker} has no explanatory comment"
        )


# --------------------------------------------------------------------- ffmpeg
def test_docs_are_honest_about_ffmpeg_on_arm():
    """The bundled FFmpeg wheel has no 32-bit ARM build, and the docs said it did.

    Two sentences claimed FFmpeg "needs no package manager" and "works on x86,
    arm64 and armv7". Both were wrong, and wrong in the worst place: `armv7` is
    what the installer targets. A Pi user following that advice gets a service
    that installs cleanly and then fails on nearly every real station, because
    virtually all of them are AAC and only FFmpeg can re-encode them to MP3.
    """
    readme = _read("README.md")
    assert "apt install ffmpeg" in readme, "ARM users must be told to use apt"
    assert "works on x86, arm64 and armv7" not in readme
    assert "needs no package manager" not in readme
    assert "no apt packages are needed" not in readme


def test_docs_cover_the_things_that_actually_block_a_pi_install():
    """Each of these cost real debugging time on a real Pi; keep them documented.

    * ``cffi`` and ``psutil`` have no 32-bit ARM wheel, so a compiler is needed.
    * ``python3 -m venv`` fails without the separate ``python3-venv`` package.
    * Debian 11 is end-of-life, so ``security.debian.org`` 404s and apt's error
      message never mentions the cause.
    """
    readme = _read("README.md")
    installer = _read("packaging/install.sh")

    for phrase in ("python3-venv", "build-essential", "libffi-dev", "--fix-eol-apt"):
        assert phrase in readme, f"README no longer mentions {phrase!r}"

    assert "eol_apt_needed" in installer, "installer lost its end-of-life detection"
    assert "--fix-eol-apt" in installer, "installer lost the repair flag"


# ------------------------------------------------------- not a published package
def test_readme_never_suggests_a_package_index_install():
    """This is an application, not a library, and it is not published anywhere.

    A `pip install hls-radio-converter` in the README would be a lie that fails
    the moment a user pastes it. Since the project has no intention of ever being
    published, the guard is now the reverse of what it once was: a bare
    package-index install must not appear at all, with or without a caveat.
    """
    readme = _read("README.md")
    for command in (
        "pip install hls-radio-converter",
        "pipx install hls-radio-converter",
        "uv tool install hls-radio-converter",
    ):
        assert command not in readme, (
            f"the README offers `{command}`, but nothing is published to a package index"
        )


def test_pyproject_has_no_package_index_metadata():
    """`classifiers` and `keywords` exist only to fill in a package-index page.

    Nothing reads them for an application installed from a checkout, so they are
    noise that implies a distribution channel this project does not use. The
    fields a real install needs - name, version, description, licence and the
    console script - all stay.
    """
    pyproject = _read("pyproject.toml")
    assert "classifiers = [" not in pyproject, "classifiers only serve a package index"
    assert "keywords = [" not in pyproject, "keywords only serve a package index"
    # But the metadata an actual `pip install .` depends on must still be there.
    for needed in (
        'name = "hls-radio-converter"',
        'version = "2.0.2"',
        'license = "MIT"',
        "[project.scripts]",
    ):
        assert needed in pyproject, f"pyproject.toml lost {needed}"


# ------------------------------------------------------------------ installer
def test_installer_is_valid_bash():
    """`bash -n` is the only linter we have for the install script."""
    install = PROJECT_ROOT / "packaging" / "install.sh"
    result = subprocess.run(
        ["bash", "-n", str(install)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"packaging/install.sh has a syntax error:\n{result.stderr}"


def test_installer_does_not_instruct_a_command_using_a_placeholder_url():
    """Don't print a copy-pasteable command that references example.com.

    The summary used to say `radioconv url 'https://example.com/live/index.m3u8'`,
    which reads as a real instruction but sends the user's own stream nowhere.
    """
    installer = _read("packaging/install.sh")
    assert "url 'https://example.com" not in installer
    assert 'url "https://example.com' not in installer


def test_installer_creates_no_station_repository():
    """A fresh install must not leave a station list on the Pi.

    The project is config-free by design: one percent-encoded URL per radio and
    nothing else to maintain. `stations.toml` was removed, so the installer must
    not reference it or write one - and there must be no example file to tempt
    anyone into creating one by hand.
    """
    installer = _read("packaging/install.sh")
    assert "stations.toml" not in installer
    assert not (PROJECT_ROOT / "packaging" / "stations.toml.example").exists()


def test_readme_does_not_document_a_station_list():
    """The README must not offer a station list as a feature.

    The removal is allowed to be *explained* - there is a "Why there is no
    station list" section that names `stations.toml` on purpose, so a bare
    substring check would be wrong. What must not come back is documentation of
    the feature: a TOML preset block, preset URL paths, or preset API rows.
    """
    readme = _read("README.md")
    assert "[stations." not in readme
    assert "/stations" not in readme
    # The v1-compatible shape is what users are told to use.
    assert "https%3A%2F%2F" in readme
    assert "radioconv url" in readme
