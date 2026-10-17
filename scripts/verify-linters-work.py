"""Prove ruff and ty actually catch things, so "0 findings" means something.

A linter reporting nothing is only evidence of a clean codebase if it *would*
have reported something. Each case below introduces one deliberate defect into a
real source file, asserts the tool fails, then restores the file byte-for-byte.

This exists because public claims like "zero findings and no blanket
suppressions" are only worth making if they have been tested.
"""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent if (ROOT.parent / "pyproject.toml").exists() else ROOT
TARGET = REPO / "src/radioconv/session.py"
BACKUP = TARGET.with_suffix(".py.mutationtest-bak")

RUFF = str(REPO / ".venv/bin/ruff")
TY = str(REPO / ".venv/bin/ty")


def ruff(*args):
    # The paths are our own venv binaries, not untrusted input.
    return subprocess.run(  # noqa: S603
        [RUFF, *args], cwd=REPO, capture_output=True, text=True, check=False
    )


def ty():
    return subprocess.run(  # noqa: S603
        [TY, "check"], cwd=REPO, capture_output=True, text=True, check=False
    )


def main() -> int:
    original = TARGET.read_text()
    shutil.copy(TARGET, BACKUP)
    results = []

    def case(label, mutate, tool):
        """Inject a defect, assert the tool complains, restore.

        The mutation is verified to have *applied* first. Without that check a
        stale anchor silently produces a no-op, the file stays valid, and the
        tool correctly reports nothing - which this script would then report as
        a linter failure. (That is exactly what happened with the bare-except
        case: ``session.py`` has no ``except Exception:`` line.)
        """
        mutated = mutate(original)
        if mutated == original:
            results.append((label, False, "mutation did not apply"))
            print(f"  {label:<42} N/A - mutation did not apply")
            return
        TARGET.write_text(mutated)
        try:
            result = tool()
            caught = result.returncode != 0
        finally:
            TARGET.write_text(original)
        results.append((label, caught, ""))
        print(f"  {label:<42} {'caught' if caught else 'MISSED'}")

    try:
        # Catches: unused import (F401)
        case(
            "ruff: unused import (F401)",
            lambda t: t.replace(
                "from __future__ import annotations",
                "from __future__ import annotations\nimport os  # unused",
                1,
            ),
            lambda: ruff("check", "--select", "F401", str(TARGET)),
        )

        # Catches: bare except (E722), anchored on a clause that exists.
        case(
            "ruff: bare except (E722)",
            lambda t: t.replace("except asyncio.QueueFull:", "except:", 1),
            lambda: ruff("check", "--select", "E722", str(TARGET)),
        )

        # Catches: undefined name (F821)
        case(
            "ruff: undefined name (F821)",
            lambda t: t.replace(
                "def count_listeners(self) -> int:",
                "def count_listeners(self) -> int:\n        return undefined_thing",
                1,
            ),
            lambda: ruff("check", "--select", "F821", str(TARGET)),
        )

        # Catches: comparison to None (E711) - proves non-pyflakes rules are live.
        case(
            "ruff: `if x == None` (E711)",
            lambda t: t.replace(
                "def count_listeners(self) -> int:",
                "def count_listeners(self) -> int:\n        if self.sessions == None:\n            return 0",
                1,
            ),
            lambda: ruff("check", "--select", "E711", str(TARGET)),
        )

        # ty: wrong assigned type
        case(
            "ty: int variable assigned a str",
            lambda t: t.replace(
                "def count_listeners(self) -> int:",
                'def count_listeners(self) -> int:\n        bad: int = "text"\n        return 0',
                1,
            ),
            ty,
        )

        # ty: unknown attribute
        case(
            "ty: unknown attribute",
            lambda t: t.replace(
                "def count_listeners(self) -> int:",
                "def count_listeners(self) -> int:\n        return self.no_such_attribute",
                1,
            ),
            ty,
        )

        # ty: wrong return type
        case(
            "ty: returns str from an int function",
            lambda t: t.replace(
                "def count_listeners(self) -> int:",
                'def count_listeners(self) -> int:\n        return "not an int"',
                1,
            ),
            ty,
        )
    finally:
        TARGET.write_text(original)
        BACKUP.unlink(missing_ok=True)
        restored = TARGET.read_text() == original
        print(f"\n  target restored byte-for-byte: {restored}")
        if not restored:
            raise SystemExit("RESTORE FAILED - session.py is modified")

    missed = [label for label, ok, _ in results if not ok]
    n_applied = sum(1 for _, _, why in results if not why)
    print()
    if missed:
        print("LINTERS FAILED TO CATCH:", "; ".join(missed))
        return 1
    print(f"all {n_applied} injected defects were caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
