"""Prove the container-detection tests fail if the bug is reintroduced.

Restores each part of the original defect in turn and asserts the matching test
goes red. A regression test that passes with the bug present is worse than none.

Run one at a time: two concurrent runs of this script mutate the same files and
can leave one of them corrupted.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Tuple

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "radioconv"
MOD = "tests/test_container_detection.py"

CASES = [
    (
        "ID3 tag not skipped (the original detection bug)",
        PKG / "tsdemux.py",
        "    offset = skip_id3(data)\n    if offset:",
        "    offset = 0\n    if offset:",
        f"{MOD}::test_probe_describes_an_id3_adts_stream_correctly",
    ),
    (
        # Targets the unit test, not the end-to-end one, and that is deliberate.
        # Dropping the mapping does NOT break a stream any more: build_command
        # then omits `-f` entirely and ffmpeg probes the bytes instead. The two
        # mechanisms are redundant on purpose (the mapping is the fast, reliable
        # path; probing is the safety net), so an end-to-end "does audio come
        # out" test legitimately passes with either one removed. What the mapping
        # guarantees is *without* relying on probing - and that is what
        # test_build_command_uses_the_mapped_demuxer asserts.
        "adts missing from the ffmpeg format table",
        PKG / "ffmpeg.py",
        'INPUT_FORMATS = {"ts": "mpegts", "mp4": "mp4", "adts": "aac", "mp3": "mp3"}',
        'INPUT_FORMATS = {"ts": "mpegts", "mp4": "mp4", "mp3": "mp3"}',
        f"{MOD}::test_build_command_uses_the_mapped_demuxer",
    ),
    (
        # The safety net itself: a guessed `-f` for an unrecognised container is
        # the original bug, and it must stay impossible.
        "build_command guesses -f for an unknown container",
        PKG / "ffmpeg.py",
        '    if fmt:\n        cmd += ["-f", fmt]\n',
        '    cmd += ["-f", fmt or UNKNOWN_INPUT]\n',
        f"{MOD}::test_build_command_omits_the_demuxer_for_an_unknown_container",
    ),
    (
        "prime() claims ts again (the false dichotomy, at the call site)",
        PKG / "hls.py",
        'container="mp4" if has_init or _url_looks_mp4(self.media_url) else "unknown",',
        'container="mp4" if has_init or _url_looks_mp4(self.media_url) else "ts",',
        f"{MOD}::test_priming_a_non_mp4_playlist_does_not_claim_ts",
    ),
    (
        "PlaylistInfo defaults to ts again",
        PKG / "hls.py",
        '    container: str = "unknown"',
        '    container: str = "ts"',
        f"{MOD}::test_the_default_container_is_honest_when_the_playlist_does_not_say",
    ),
    (
        "pipeline ignores detection and uses the guess",
        PKG / "pipeline.py",
        "        input_format = detected or info.container",
        '        input_format = "ts"',
        f"{MOD}::test_the_audio_from_an_id3_adts_stream_is_valid_mp3",
    ),
    (
        "ADTS check accepts an MPEG audio frame (the FF FB ambiguity)",
        PKG / "tsdemux.py",
        "    if data[1] & 0x06:  # layer (bits 2-1) must be 00 in ADTS\n        return False\n",
        "",
        "tests/test_tsdemux.py::test_raw_mp3_is_recognised",
    ),
]


def run(test: str) -> Tuple[str, int]:
    """Run one test; return pytest's summary line *and* its exit code.

    The exit code is the verdict. The summary is only for the log, and it is
    searched for rather than taken as the last line of the combined output:
    ``uv run`` writes "Installed 1 package in 2ms" to stderr when the editable
    install is stale, and stderr lands after stdout, so the last line was not
    pytest's. That reported "the test passed with the bug in place" for a test
    that had in fact failed - a false alarm about a guard that was working.
    """
    # Resolved rather than bare, because a bare "uv" is an S607 lint error. The
    # argv is built only from constants in this file - no shell, no user input -
    # which is the same justification as verify-web-guards.py.
    uv = shutil.which("uv") or "uv"
    proc = subprocess.run(  # noqa: S603
        [uv, "run", "pytest", "-q", "-x", test],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    summary = next(
        (ln for ln in reversed(lines) if "passed" in ln or "failed" in ln or "error" in ln),
        lines[-1] if lines else f"<no output, exit {proc.returncode}>",
    )
    return summary, proc.returncode


still_green = []
for label, path, find, replace, test in CASES:
    original = path.read_text(encoding="utf-8")
    if find not in original:
        print(f"ERROR: cannot find the text to mutate in {path.name}: {find[:60]!r}")
        sys.exit(2)
    try:
        path.write_text(original.replace(find, replace, 1), encoding="utf-8")
        result, code = run(test)
        went_red = code != 0
    finally:
        # Restore, then verify the restore, so a failure here is loud.
        path.write_text(original, encoding="utf-8")
        if path.read_text(encoding="utf-8") != original:
            sys.exit(f"ERROR: failed to restore {path}")

    print(f"{'RED  ' if went_red else 'GREEN'} {label:52} -> {result}")
    if not went_red:
        still_green.append(label)

print()
if still_green:
    print("THESE TESTS DO NOT CATCH THE BUG:")
    for label in still_green:
        print(f"  - {label}")
    sys.exit(1)
print(f"all {len(CASES)} reintroduced defects were caught")
