"""Prove the playback-continuity and metrics-accuracy tests actually bite.

Reintroduces each half of each defect and asserts the matching test goes red. A
regression test that passes with the bug present is worse than none - and this
module exists because a test that *hangs* is worse still, which one of these
already turned out to be.

Run one at a time: two concurrent runs mutate the same files.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "radioconv"
CONTINUITY = "tests/test_playback_continuity.py"
WEB = "tests/test_web_ui.py"


class Case(NamedTuple):
    label: str
    target: Path
    find: Optional[str]
    replace: str
    test: str


CASES: List[Case] = [
    Case(
        # The exact defect: items stay whatever the pipe returned, so a 128-item
        # queue is a three-second buffer, not a forty-three-second one.
        "fan-out stops normalising chunk size",
        PKG / "session.py",
        "        while len(self._pending) >= self._piece_size():\n"
        "            piece = self._cut_piece()",
        "        while len(self._pending) >= self._piece_size():\n"
        "            piece = bytes(self._pending[: self._piece_size()])\n"
        "            del self._pending[: self._piece_size()]\n"
        "            if True:\n"
        "                self._enqueue(chunk)\n"
        "                return",
        f"{CONTINUITY}::test_small_pipe_reads_are_accumulated_into_full_chunks",
    ),
    Case(
        "fan-out no longer accumulates small reads",
        PKG / "session.py",
        "        while len(self._pending) >= self._piece_size():",
        "        while len(self._pending) >= self._piece_size() * 1000:",
        f"{CONTINUITY}::test_small_pipe_reads_are_accumulated_into_full_chunks",
    ),
    Case(
        "fan-out no longer splits large blobs",
        PKG / "session.py",
        "        while len(self._pending) >= self._piece_size():",
        "        while False:",
        f"{CONTINUITY}::test_large_blobs_are_split_into_full_chunks",
    ),
    Case(
        # The garbled-title defect: the pieces are cut on 8192-byte boundaries
        # and 8192 is not a multiple of a 576-byte MP3 frame, so every
        # listener starts inside a frame and the radio's decoder and its
        # metadata parser desynchronise.
        #
        # The mutation has to remove the *cutting* as well as the alignment:
        # aligning the incoming audio but still cutting the pieces at a fixed
        # 8192 bytes would leave every piece starting mid-frame, which is the
        # defect this case is about.
        "the pieces are no longer cut on MP3 frame boundaries",
        PKG / "session.py",
        "        # Cut at the first frame boundary at or after the target.\n"
        "        cut = 0\n"
        "        while True:\n"
        "            size = self._frame_size_at(bytes(self._pending), cut, fmt)",
        "        # Cut at the first frame boundary at or after the target.\n"
        "        cut = 0\n"
        "        while True:\n"
        "            size = None\n"
        "            if False:\n"
        "                size = self._frame_size_at(bytes(self._pending), cut, fmt)",
        f"{CONTINUITY}::test_every_piece_a_listener_receives_starts_on_a_frame_boundary",
    ),
    Case(
        # The alignment search must span the bytes held so far, not just the
        # current read: FFmpeg's pipe hands back 578 bytes, and 578 bytes
        # cannot contain a frame *pair*.
        "the alignment search no longer spans the held-back bytes",
        PKG / "session.py",
        "            self._align_pending += chunk\n"
        "            pending = bytes(self._align_pending)\n"
        "            offset = self._align_offset(pending, fmt)",
        "            pending = chunk\n"
        "            self._align_pending += chunk\n"
        "            offset = self._align_offset(pending, fmt)",
        f"{CONTINUITY}::test_the_alignment_survives_ffmpegs_small_pipe_reads",
    ),
    Case(
        # The alignment search must span the bytes held so far, not just the
        # current read: FFmpeg's pipe hands back 578 bytes, and 578 bytes
        # cannot contain a frame *pair*.
        "the alignment search no longer spans the held-back bytes",
        PKG / "session.py",
        "            self._align_pending += chunk\n"
        "            pending = bytes(self._align_pending)\n"
        "            offset = self._align_offset(pending, fmt)",
        "            pending = chunk\n"
        "            self._align_pending += chunk\n"
        "            offset = self._align_offset(pending, fmt)",
        f"{CONTINUITY}::test_the_alignment_survives_ffmpegs_small_pipe_reads",
    ),
    Case(
        # The alignment must follow the format ACTUALLY being sent, not the
        # setting. Keying it off the setting left every ADTS output and
        # every `output_codec = "auto"` station unaligned.
        "the alignment follows the setting instead of the format sent",
        PKG / "session.py",
        '        codec = self.pipeline.stats.detected_codec\n        if codec == "mp3":',
        "        codec = self.pipeline.stats.detected_codec\n"
        '        if self.settings.output_codec != "mp3":\n'
        "            return None\n"
        '        if codec == "mp3":',
        f"{CONTINUITY}::test_an_adts_stream_is_also_aligned_to_its_own_frames",
    ),
    Case(
        # A forwarded MP3 stream (`output_codec = "auto"`) must be aligned
        # too - this is the case the setting-based gate missed. The mutation
        # has to make the *format* undetectable, not the setting wrong:
        # returning None for "auto" would pass for the wrong reason.
        "a forwarded MP3 stream is no longer aligned",
        PKG / "session.py",
        '        if codec == "mp3":\n            return "mp3"',
        '        if codec == "mp3" and self.settings.output_codec != "auto":\n'
        '            return "mp3"',
        f"{CONTINUITY}::test_an_auto_codec_station_sending_mp3_is_aligned",
    ),
    Case(
        "the pacer stops pacing entirely",
        PKG / "api.py",
        "        while True:\n            delay = target - time.monotonic()\n"
        "            if delay <= 0:\n                return",
        "        while True:\n            delay = target - time.monotonic()\n"
        "            if True:\n                return",
        f"{CONTINUITY}::test_the_pacer_releases_at_the_stream_rate",
    ),
    Case(
        # Without the clamp a starvation gap earns credit and is repaid as a
        # burst, which empties the buffer so the next gap is heard too.
        "the pacer repays a starvation gap as a burst",
        PKG / "api.py",
        "        if now - target > self._burst:",
        "        if False:",
        f"{CONTINUITY}::test_the_pacer_does_not_repay_a_starvation_gap_as_a_burst",
    ),
    Case(
        "the playlist reload interval drifts back over half a segment",
        PKG / "hls.py",
        "        return min(4.0, max(0.5, self.target_duration / 2))",
        "        return max(1.0, self.target_duration * 0.75)",
        f"{CONTINUITY}::test_the_playlist_reload_interval_is_under_half_a_segment",
    ),
    Case(
        "the output bitrate is no longer measured",
        PKG / "pipeline.py",
        '        if self.stats.mode != MODE_PASSTHROUGH and self.settings.output_codec == "mp3":\n'
        "            # Re-encoded to a bitrate we asked for.\n"
        "            return float(self.settings.bitrate)",
        "        if False:\n            return float(self.settings.bitrate)",
        f"{CONTINUITY}::test_a_transcode_reports_the_bitrate_we_asked_for",
    ),
    Case(
        "a silent session no longer degrades the status",
        PKG / "metrics.py",
        '    any_silent = any(d["stalled"] for d in details)',
        "    any_silent = False",
        f"{WEB}::test_a_mute_session_makes_the_service_degraded",
    ),
    Case(
        "a stalled session is never flagged",
        PKG / "session.py",
        "        return self.starved_for > self.settings.stall_timeout",
        "        return False",
        f"{WEB}::test_a_mute_session_makes_the_service_degraded",
    ),
    Case(
        "a session is accused of stalling as soon as it connects",
        PKG / "session.py",
        "        if not self._live_subscribers() or self.dead:\n            return False\n"
        "        return self.starved_for > self.settings.stall_timeout",
        "        if not self._live_subscribers() or self.dead:\n            return False\n"
        "        return self.starved_for >= 0",
        f"{WEB}::test_a_session_that_is_merely_starting_is_not_accused",
    ),
    Case(
        # The reported contradiction: a connection that never began reading is
        # still listed, its queue has drained, and because a station is judged by
        # the listener with the least buffered it reports zero buffered seconds
        # and a climbing starvation clock - beside a buffer figure that was summed
        # over every connection and so looked healthy.
        "abandoned connections are counted as listeners again",
        PKG / "session.py",
        "        return [s for s in self.subscribers if s.started]",
        "        return [s for s in self.subscribers]",
        "tests/test_session.py::test_an_abandoned_connection_is_not_counted_as_a_listener",
    ),
    Case(
        "abandoned connections are never reaped",
        PKG / "session.py",
        "            if not subscriber.started and now - subscriber.connected_at > NEVER_STARTED_GRACE:",
        "            if False and now - subscriber.connected_at > NEVER_STARTED_GRACE:",
        "tests/test_session.py::test_a_listener_that_never_reads_is_reaped",
    ),
    Case(
        # A cleanup helper nothing calls cleans up nothing, and every test above
        # it still passes - so the call itself is what this one pins.
        "the reaper stops being called by the periodic loop",
        PKG / "session.py",
        "                self._drop_never_started(session, now)",
        "                pass",
        "tests/test_session.py::test_the_reaper_loop_removes_abandoned_connections",
    ),
    Case(
        # Summing the queues over listeners is what let "Buffered" show a healthy
        # number while "Starved" climbed: one well-fed listener masked a starving
        # one. A station is as buffered as its worst-served listener.
        "the buffer is summed across listeners instead of taking the minimum",
        PKG / "session.py",
        "        smallest = min(s.queue.qsize() for s in live)",
        "        smallest = sum(s.queue.qsize() for s in live)",
        "tests/test_session.py::test_the_buffer_and_the_starvation_clock_are_the_same_measurement",
    ),
    Case(
        # The other half of the same mistake, in the other place: the page
        # rebuilding the figure from a chunk count can disagree with the figure
        # the starvation clock uses, and then the two columns contradict.
        "the dashboard recomputes the buffer from a chunk count again",
        PKG / "static" / "dashboard.js",
        "    const seconds = detail.buffered_seconds;",
        "    const seconds = (detail.queued_chunks || 0) * 0.34;",
        f"{WEB}::test_the_page_does_not_recompute_what_the_server_already_measured",
    ),
    Case(
        # The initial burst is a credit - permission to run ahead so a radio can
        # fill its buffer. Anchored forward it became a delay, holding the very
        # first chunk for the whole burst: 1.5s of the measured 1.86s time to
        # first byte, and the reason a radio reconnects into the same wait.
        "the pacer charges the initial burst to the listener",
        PKG / "api.py",
        "            self._started = now - self._burst",
        "            self._started = now + self._burst",
        f"{CONTINUITY}::test_the_initial_burst_is_a_credit_and_not_a_delay",
    ),
    Case(
        # The reporting bug behind "CPU usage seems to only refresh once every 10
        # seconds": sampling was tied to the watchdog clock, which exists to be
        # slow so a single bad reading never causes a restart.
        "resource sampling is tied back to the watchdog interval",
        PKG / "monitor.py",
        "        sample_interval = max(0.2, self.settings.resource_sample_interval)",
        "        sample_interval = max(5.0, self.settings.memory_check_interval)",
        "tests/test_monitor.py::test_resources_are_sampled_faster_than_the_watchdog_acts",
    ),
    Case(
        # The design decision the window rests on: points are recorded on a
        # timer, not when somebody reads the metrics. A sampler that silently
        # stops still leaves correct totals and present gauges - it just makes
        # the window drift into "everything ever".
        "the throughput sampler never records a point",
        PKG / "session.py",
        "            await asyncio.sleep(SAMPLE_SECONDS)\n            self.throughput.sample()",
        "            await asyncio.sleep(SAMPLE_SECONDS)",
        "tests/test_throughput.py::test_the_sampler_records_points_on_its_own",
    ),
    Case(
        # The original inaccuracy: totals summed over whatever sessions happen to
        # be registered, so they fall as soon as one is reaped.
        "byte totals are summed over live sessions again",
        PKG / "metrics.py",
        '            "bytes_in_total": throughput["bytes_in_total"],\n'
        '            "bytes_out_total": throughput["bytes_out_total"],',
        '            "bytes_in_total": sum(d["pipeline"]["bytes_in"] for d in details),\n'
        '            "bytes_out_total": sum(d["session"]["bytes_out"] for d in details),',
        "tests/test_throughput.py::test_the_totals_do_not_fall_when_a_session_is_reaped",
    ),
    Case(
        # "Sent" meaning "produced" - which counts audio for a warm station that
        # nobody is listening to, and audio dropped for a listener that fell
        # behind. Counting it at the fan-out is exactly that mistake.
        "audio is counted as sent when it is produced, not when it is delivered",
        PKG / "session.py",
        "        self.stats.bytes_out += len(chunk)",
        "        self.stats.bytes_out += len(chunk)\n        self.throughput.add_out(len(chunk))",
        "tests/test_session.py::test_audio_produced_for_nobody_is_not_counted_as_sent",
    ),
]


def run(test: str) -> Tuple[str, int]:
    """Run one test; return pytest's summary line *and* its exit code.

    The exit code is the verdict; the summary is only for the log. It is
    searched for rather than taken as the last line of the combined output,
    because ``uv run`` writes "Installed 1 package in 2ms" to stderr when the
    editable install is stale - and stderr lands after stdout, so the last line
    can be uv's, not pytest's.
    """
    # Resolved rather than bare: a bare "uv" is an S607 lint error.
    uv = shutil.which("uv") or "uv"
    proc = subprocess.run(  # noqa: S603
        [uv, "run", "pytest", "-q", "-x", test],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    summary = next(
        (ln for ln in reversed(lines) if "passed" in ln or "failed" in ln or "error" in ln),
        lines[-1] if lines else f"<no output, exit {proc.returncode}>",
    )
    return summary, proc.returncode


still_green: List[str] = []
for case in CASES:
    original = case.target.read_text(encoding="utf-8")
    if case.find is not None and case.find not in original:
        print(f"ERROR: cannot find the text to mutate in {case.target.name}")
        print(f"  looked for: {case.find[:90]!r}")
        sys.exit(2)
    try:
        mutated = original if case.find is None else original.replace(case.find, case.replace, 1)
        case.target.write_text(mutated, encoding="utf-8")
        try:
            result, code = run(case.test)
            went_red = code != 0
        except subprocess.TimeoutExpired:
            # A test that hangs is a failure too, and worth reporting as caught -
            # but loudly, because a hang would otherwise stall a whole suite.
            result = "TIMED OUT (the test hung instead of failing)"
            went_red = True
    finally:
        case.target.write_text(original, encoding="utf-8")
        if case.target.read_text(encoding="utf-8") != original:
            sys.exit(f"ERROR: failed to restore {case.target}")

    print(f"{'RED  ' if went_red else 'GREEN'} {case.label:56} -> {result}")
    if not went_red:
        still_green.append(case.label)

print()
if still_green:
    print("THESE TESTS DO NOT CATCH THE BUG:")
    for label in still_green:
        print(f"  - {label}")
    sys.exit(1)
print(f"all {len(CASES)} reintroduced defects were caught")
