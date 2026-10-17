"""FFmpeg discovery: where the binary may come from, and what we say when it cannot.

The important case is 32-bit ARM. ``imageio-ffmpeg`` publishes no ``armv7l``
build, so on an older Raspberry Pi the pip install provides nothing and the
advice must point at the distro package instead of at a wheel that does not
exist. These tests pin that, because the surrounding code and docs spent a long
time claiming the wheel covered armv7 when it never did.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

from radioconv import ffmpeg as ff


@pytest.fixture(autouse=True)
def _clear_cache():
    """``locate_ffmpeg`` is ``lru_cache``d, so tests must not inherit a result."""
    ff.locate_ffmpeg.cache_clear()
    yield
    ff.locate_ffmpeg.cache_clear()


# ------------------------------------------------------------------ platforms
@pytest.mark.parametrize("machine", ["armv7l", "armv6l"])
def test_no_bundled_binary_on_32_bit_arm(monkeypatch, machine):
    monkeypatch.setattr(ff.platform, "machine", lambda: machine)
    assert ff.bundled_ffmpeg_supported() is False


@pytest.mark.parametrize("machine", ["x86_64", "aarch64", "arm64", "AMD64"])
def test_bundled_binary_on_supported_platforms(monkeypatch, machine):
    monkeypatch.setattr(ff.platform, "machine", lambda: machine)
    assert ff.bundled_ffmpeg_supported() is True


# --------------------------------------------------------------------- advice
def test_hint_on_arm_tells_the_user_to_use_apt(monkeypatch):
    monkeypatch.setattr(ff.platform, "machine", lambda: "armv7l")
    hint = ff.install_hint()
    assert "apt install ffmpeg" in hint
    # Naming the wheel is fine (it explains *why* there is nothing to bundle);
    # telling the user to reinstall it would be advice that cannot work.
    assert "reinstall" not in hint
    assert "imageio-ffmpeg" in hint


def test_hint_elsewhere_mentions_both_options(monkeypatch):
    monkeypatch.setattr(ff.platform, "machine", lambda: "x86_64")
    hint = ff.install_hint()
    assert "apt install ffmpeg" in hint
    assert "imageio-ffmpeg" in hint


# ------------------------------------------------------------------ discovery
def _nothing_available(monkeypatch):
    """Make every discovery route fail, including the bundled wheel."""
    monkeypatch.setattr(ff, "_sidecar_ffmpeg", lambda: None)
    monkeypatch.setattr(ff.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ff, "_runs", lambda _path: False)


def test_missing_on_arm_reports_the_platform_problem(monkeypatch):
    _nothing_available(monkeypatch)
    monkeypatch.setattr(ff.platform, "machine", lambda: "armv7l")

    with pytest.raises(ff.FFmpegNotFound) as excinfo:
        ff.locate_ffmpeg()

    message = str(excinfo.value)
    assert "armv7l" in message
    assert "apt install ffmpeg" in message
    assert "reinstall" not in message


def test_configured_path_wins(monkeypatch):
    monkeypatch.setattr(ff.shutil, "which", lambda name: name)
    monkeypatch.setattr(ff, "_runs", lambda _path: True)
    assert ff.locate_ffmpeg("/opt/ffmpeg") == "/opt/ffmpeg"


def test_configured_path_that_is_not_executable_is_an_error(monkeypatch):
    monkeypatch.setattr(ff.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ff, "_runs", lambda _path: False)
    with pytest.raises(ff.FFmpegNotFound):
        ff.locate_ffmpeg("/nonexistent/ffmpeg")


def test_system_ffmpeg_is_used_when_the_wheel_gives_nothing(monkeypatch):
    """The Pi's actual situation: no bundled binary, but /usr/bin/ffmpeg works."""
    monkeypatch.setattr(ff, "_sidecar_ffmpeg", lambda: None)
    monkeypatch.setattr(ff.platform, "machine", lambda: "armv7l")
    monkeypatch.setattr(
        ff.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
    )
    monkeypatch.setattr(ff, "_runs", lambda path: path == "/usr/bin/ffmpeg")
    assert ff.locate_ffmpeg() == "/usr/bin/ffmpeg"


def _stub_imageio(monkeypatch, exe: str) -> None:
    """Install a stub ``imageio_ffmpeg`` whose ``get_ffmpeg_exe`` returns *exe*.

    A ModuleType *subclass* rather than assigning an attribute onto a bare
    ModuleType: the type checker then knows the method exists, and the linter
    does not flag a dynamic attribute assignment.
    """

    class _Stub(types.ModuleType):
        def get_ffmpeg_exe(self) -> str:
            return exe

    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", _Stub("imageio_ffmpeg"))


def test_bare_command_name_from_imageio_is_resolved_to_a_path(monkeypatch):
    """imageio-ffmpeg returns the bare name "ffmpeg" as its last resort.

    That is its step 4 ("try system ffmpeg command"), which is what runs on
    32-bit ARM. Reported verbatim it makes `radioconv check` print
    `ffmpeg - version`, and `/health` say `"path": "ffmpeg"`, which hides
    whether the binary is bundled or the system one - precisely the question a
    Pi user is trying to answer.
    """
    _stub_imageio(monkeypatch, "ffmpeg")
    monkeypatch.setattr(ff, "_sidecar_ffmpeg", lambda: None)
    monkeypatch.setattr(ff, "_runs", lambda _path: True)
    monkeypatch.setattr(
        ff.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
    )

    resolved = ff.locate_ffmpeg()
    assert resolved == "/usr/bin/ffmpeg"
    assert os.path.isabs(resolved), "a bare command name must never be reported"


def test_a_system_ffmpeg_wins_over_the_bundled_wheel(monkeypatch):
    """An installed ffmpeg is the administrator's choice; the wheel is a fallback.

    This used to be the other way round, and the consequence was severe: imageio's
    x86_64 release build segfaults on any MPEG-TS input, so on a machine that had
    both, every transcoded station played silence - while `/health` reported the
    bundled path as if that were normal.
    """
    _stub_imageio(monkeypatch, "/opt/bundled/ffmpeg-linux-x86_64-v7.0.2")
    monkeypatch.setattr(ff, "_sidecar_ffmpeg", lambda: None)
    monkeypatch.setattr(ff, "_runs", lambda _path: True)
    monkeypatch.setattr(ff.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    assert ff.locate_ffmpeg() == "/usr/bin/ffmpeg"


def test_the_bundled_wheel_is_used_when_there_is_no_system_ffmpeg(monkeypatch):
    """Which is the case the wheel exists for: `pip install` and nothing else."""
    _stub_imageio(monkeypatch, "/opt/bundled/ffmpeg-linux-aarch64-v7.0.2")
    monkeypatch.setattr(ff, "_sidecar_ffmpeg", lambda: None)
    monkeypatch.setattr(ff, "_runs", lambda _path: True)
    monkeypatch.setattr(ff.shutil, "which", lambda _name: None)

    assert ff.locate_ffmpeg() == "/opt/bundled/ffmpeg-linux-aarch64-v7.0.2"


def test_a_system_ffmpeg_that_does_not_run_is_skipped(monkeypatch):
    """A broken binary on PATH must not shadow a working bundled one."""
    _stub_imageio(monkeypatch, "/opt/bundled/ffmpeg-linux-aarch64-v7.0.2")
    monkeypatch.setattr(ff, "_sidecar_ffmpeg", lambda: None)
    monkeypatch.setattr(ff, "_runs", lambda path: path.startswith("/opt/bundled"))
    monkeypatch.setattr(ff.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    assert ff.locate_ffmpeg() == "/opt/bundled/ffmpeg-linux-aarch64-v7.0.2"
