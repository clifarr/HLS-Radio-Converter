"""The retransmission watchdog: reading the OS counter, and acting on a burst.

Two things are under test, and only the first is arithmetic. The watchdog
decides *when a radio's connection should be closed so it reconnects*, so the
tests pin both halves:

* the parsers, against the real output of both supported platforms - including
  the shapes that would make a careless parser return the wrong number, which
  is worse than returning none;
* the decision, because a watchdog that fires on healthy traffic would turn a
  working stream into a reconnect loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from radioconv.config import Settings
from radioconv.retrans import (
    WINDOWS_RETRANSMIT_OFFSET,
    WINDOWS_TCPSTATS_FIELDS,
    WINDOWS_TCPSTATS_SIZE,
    TcpRetransmitCounter,
    parse_mib_tcpstats,
    parse_netstat_tcp,
    parse_proc_net_snmp,
    tcp_retransmit_counter,
)
from radioconv.session import SessionManager

#: The real thing, captured from the Pi (a `Tcp:` header line followed by a
#: `Tcp:` data line, plus a `Udp:` pair that must be ignored).
PROC_NET_SNMP = """Tcp: RtoAlgorithm RtoMin RtoMax MaxConn ActiveOpens PassiveOpens AttemptFails EstabResets CurrEstab InSegs OutSegs RetransSegs InErrs OutRsts InCsumErrors
Tcp: 1 200 120000 -1 8330 2834 19 101 3 2386725 1778862 4494 0 65265 0
Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors InCsumErrors IgnoredMulti MemErrors
Udp: 62341 12 0 51044 0 0 0 0 0
"""

#: The real thing, captured from macOS.
NETSTAT_MACOS = """tcp:
 0 packet sent
    0 data packet (0 byte)
    0 data packet (0 byte) retransmitted
    0 resend initiated by MTU discovery
    0 ack-only packet (0 delayed)
 0 retransmit timeout
"""


def _settings(**overrides) -> Settings:
    """Short timeouts keep these tests quick, as the session tests do."""
    return Settings(
        linger_seconds=overrides.get("linger_seconds", 0.3),
        queue_chunks=overrides.get("queue_chunks", 64),
        chunk_size=overrides.get("chunk_size", 1024),
        max_sessions=overrides.get("max_sessions", 2),
        stall_reconnect_cooldown_seconds=overrides.get("stall_reconnect_cooldown_seconds", 5.0),
        retransmit_reconnect_segments=overrides.get("retransmit_reconnect_segments", 3),
    )


# ------------------------------------------------------------ the parsers
def test_the_linux_counter_is_read_by_field_name():
    assert parse_proc_net_snmp(PROC_NET_SNMP) == 4494


def test_the_linux_counter_survives_a_column_being_inserted():
    """A kernel that adds a counter must not shift every number after it.

    Parsing by position would return the neighbouring field - a plausible
    number that is not retransmissions - and a watchdog fed that would either
    never fire or fire constantly, with nothing in the logs to explain it.
    """
    header = PROC_NET_SNMP.splitlines()[0].replace("ActiveOpens", "ActiveOpens BrandNewCounter")
    data = PROC_NET_SNMP.splitlines()[1].replace(" 8330 ", " 8330 777 ")
    text = "\n".join([header, data]) + "\n"
    assert parse_proc_net_snmp(text) == 4494, "the field was located by position"


def test_the_linux_counter_reports_nothing_when_it_cannot_be_found():
    """Absent or ragged output must be ``None``, never zero.

    Zero would read as "no retransmissions", which is a measurement the parser
    did not make. ``None`` switches the watchdog off for that tick instead.
    """
    assert parse_proc_net_snmp("") is None
    assert parse_proc_net_snmp("Udp: InDatagrams\nUdp: 1\n") is None
    # Header without its data line.
    assert parse_proc_net_snmp(PROC_NET_SNMP.splitlines()[0] + "\n") is None
    # Data line shorter than the header.
    assert parse_proc_net_snmp("Tcp: A B RetransSegs\nTcp: 1 2\n") is None
    # Non-numeric where the figure should be.
    assert parse_proc_net_snmp("Tcp: A RetransSegs\nTcp: 1 nope\n") is None


def test_the_macos_counter_is_read_from_the_plural_line():
    assert parse_netstat_tcp(NETSTAT_MACOS) == 0


def test_the_macos_counter_handles_one_retransmission():
    """macOS switches to singular nouns for a single packet.

    A parser written against the plural misses exactly the quiet case - which is
    the case the counter has to get right, because one retransmission is a
    measurement and zero is not.
    """
    assert parse_netstat_tcp("    1 data packet (1 byte) retransmitted\n") == 1
    assert parse_netstat_tcp("    7 data packets (9 bytes) retransmitted\n") == 7


def test_the_macos_counter_reports_nothing_when_it_cannot_be_found():
    assert parse_netstat_tcp("") is None
    assert parse_netstat_tcp("tcp:\n 0 retransmit timeout\n") is None
    # The substring appears, but not as the counter line.
    assert parse_netstat_tcp(" 5 packets retransmitted via RACK\n") is None


# -------------------------------------------------- choosing a supplier
def test_the_counter_is_chosen_by_platform(monkeypatch):
    """Linux needs ``/proc``; the choice is made by platform, not by probing.

    Probing would mean a Linux container without ``/proc`` mounted appeared to
    work and returned nothing for ever, which is indistinguishable from a
    healthy network. The check is therefore explicit, and the test drives it in
    both directions rather than depending on what this machine happens to have.
    """
    monkeypatch.setattr(os.path, "exists", lambda path: True)
    assert tcp_retransmit_counter("linux").name == "linux"
    assert tcp_retransmit_counter("linux2").name == "linux"
    assert tcp_retransmit_counter("darwin").name == "darwin"

    # A Linux host with no /proc: the watchdog must switch itself off.
    monkeypatch.setattr(os.path, "exists", lambda path: False)
    unsupported = tcp_retransmit_counter("linux")
    assert unsupported.available is False, "a missing /proc was reported as working"
    assert unsupported.read() is None


def test_windows_uses_the_structured_api_not_netstat():
    """Windows is supported, through a struct rather than through text.

    An earlier version of this module declared Windows unsupported, on the
    grounds that ``netstat -s`` is localised. That was true of netstat and wrong
    about Windows: the IP Helper API returns ``MIB_TCPSTATS``, whose
    ``dwRetransSegs`` field is a number at a fixed offset, in every language.
    """
    counter = tcp_retransmit_counter("win32")
    assert counter.name == "windows"
    assert counter.available is True


def test_the_windows_struct_offset_is_where_the_sdk_puts_it():
    """``dwRetransSegs`` is the 12th DWORD of ``MIB_TCPSTATS``.

    Reading the wrong slot is the one failure this path can have that looks
    entirely healthy: the watchdog would watch, say, ``dwInErrs`` instead, fire
    on the wrong traffic, and nothing in the logs would explain it. The SDK
    declares 15 DWORDs with dwRetransSegs twelfth, so the offsets are pinned
    here explicitly rather than left as an index nobody re-derives.
    """
    assert len(WINDOWS_TCPSTATS_FIELDS) == 15, (
        "MIB_TCPSTATS gained or lost a field; re-check the SDK header before "
        "trusting the offset below"
    )
    assert WINDOWS_TCPSTATS_FIELDS.index("dwRetransSegs") == 11
    assert WINDOWS_RETRANSMIT_OFFSET == 44
    assert WINDOWS_TCPSTATS_SIZE == 60


def test_the_windows_struct_is_decoded_from_the_right_slot():
    """Build a struct by hand and read it back, with a distinct value in every
    slot, so a wrong offset cannot accidentally agree."""
    values = list(range(1, len(WINDOWS_TCPSTATS_FIELDS) + 1))
    raw = b"".join(v.to_bytes(4, "little") for v in values)
    assert len(raw) == WINDOWS_TCPSTATS_SIZE
    # The 12th field (index 11) was given the value 12.
    assert parse_mib_tcpstats(raw) == 12


def test_the_windows_struct_decoder_refuses_a_short_buffer():
    assert parse_mib_tcpstats(b"") is None
    assert parse_mib_tcpstats(b"\x00" * WINDOWS_RETRANSMIT_OFFSET) is None


def test_a_windows_call_that_cannot_resolve_stays_off(monkeypatch):
    """An old iphlpapi, or a sandbox without the DLL, must not break the stream.

    ``ctypes.WinDLL`` does not exist off Windows, so this is as close as a
    non-Windows test machine can get to the real failure: the reader raises, and
    the counter must report "no measurement" rather than propagate.
    """
    import ctypes

    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no dll")),
        raising=False,
    )
    counter = tcp_retransmit_counter("win32")
    assert counter.read() is None, "a missing iphlpapi raised instead of staying off"


def test_unsupported_platforms_report_unavailable_and_read_nothing():
    """Platforms with no known counter must say so, not guess.

    Windows used to be in this list and should not be: it has a structured API.
    What remains here is everything this project has never been run on and has
    no verified way to read, and for those the watchdog stays off rather than
    watching a number it invented.
    """
    for platform in ("cygwin", "freebsd", "openbsd", "sunos5", "aix"):
        counter = tcp_retransmit_counter(platform)
        assert counter.available is False, f"{platform} claimed to be supported"
        assert counter.name == "unavailable"
        assert counter.read() is None


def test_a_failing_read_returns_none_instead_of_raising():
    """A disappearing counter must not take the service down with it."""

    def boom() -> int:
        raise OSError("no such file")

    counter = TcpRetransmitCounter("broken", boom)
    assert counter.available is True
    assert counter.read() is None


def test_the_unavailable_counter_has_no_reader():
    counter = TcpRetransmitCounter("unavailable")
    assert counter.available is False
    assert counter.read() is None


# ------------------------------------------------------ the watchdog decision
class FakeCounter:
    """A retransmission counter fed from a script, one value per read."""

    name = "fake"
    available = True

    def __init__(self, values: list) -> None:
        self._values = list(values)
        self._last = values[-1] if values else 0

    def read(self):
        if self._values:
            self._last = self._values.pop(0)
        return self._last


async def _manager_with_a_listener(settings: Settings):
    manager = SessionManager(settings)
    await manager.start()
    # ``start()`` launches the retransmit poll loop, because the real counter
    # is available on this platform. Tests that drive the loop themselves must
    # not have a second loop reading the same scripted counter: two readers
    # take alternate values, so each computes its deltas against the other's
    # readings and every total is wrong. (This bit three tests before it was
    # noticed - the numbers they asserted on were not the numbers the code
    # produced.)
    if manager._retransmit_task is not None:
        manager._retransmit_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await manager._retransmit_task
        manager._retransmit_task = None
    session, subscriber = await manager.subscribe(
        "http://x/one.m3u8",
        ["http://x/one.m3u8"],
        client="192.168.1.65",
        user_agent="NSPlayer/8.0.0.3801",
    )
    return manager, session, subscriber


async def test_a_retransmission_burst_closes_the_connection():
    """The remedy is a new connection, because the radio cannot repair itself.

    Its decoder keeps no way to resynchronise once it has decoded damaged
    audio, and it will not ask for a new connection while bytes keep arriving -
    measured: while garbled it acknowledged every byte at full rate for 776 s.
    So the server has to end the connection for it.
    """
    settings = _settings()
    manager, session, subscriber = await _manager_with_a_listener(settings)
    try:
        assert manager.retransmit_counter.available is not None
        closed = manager.reanchor_stalled_sessions()

        assert closed == 1, "the listener was not ended"
        assert session.stall_reconnects == 1
        assert manager.retransmit_reconnects == 1
        # The sentinel the response generator reads as "the stream has ended".
        assert subscriber.queue.get_nowait() is None, (
            "no end-of-stream sentinel was queued, so the response generator "
            "would keep draining audio into a decoder that cannot use it"
        )
        # The session is deliberately still warm: its pipeline and reserve keep
        # running so the reconnecting radio is served instantly.
        assert session.listener_count == 1, "the session was torn down too"
    finally:
        await manager.aclose()


async def test_the_watchdog_waits_out_its_cooldown():
    """A station is not re-anchored twice in a row, however bad the network is.

    Without this an interference burst - which lasts seconds, with damage in
    every one of them - would close the connection on every poll.
    """
    settings = _settings(stall_reconnect_cooldown_seconds=30.0)
    manager, session, _ = await _manager_with_a_listener(settings)
    try:
        assert manager.reanchor_stalled_sessions(now=1000.0) == 1
        assert manager.reanchor_stalled_sessions(now=1001.0) == 0, (
            "the watchdog fired again a second later, inside its cooldown"
        )
        assert manager.reanchor_stalled_sessions(now=1029.0) == 0
        assert manager.reanchor_stalled_sessions(now=1030.0) == 1, (
            "the watchdog never fired again after the cooldown expired"
        )
        assert manager.retransmit_reconnects == 2
        assert session.stall_reconnects == 2
    finally:
        await manager.aclose()


async def test_a_station_with_no_listeners_is_left_alone():
    settings = _settings()
    manager = SessionManager(settings)
    await manager.start()
    try:
        session, subscriber = await manager.subscribe(
            "http://x/one.m3u8", ["http://x/one.m3u8"], client="192.168.1.65"
        )
        await manager.unsubscribe(session, subscriber)
        assert manager.reanchor_stalled_sessions() == 0
        assert manager.retransmit_reconnects == 0
    finally:
        await manager.aclose()


async def test_the_reanchor_does_not_wait_for_the_backlog_to_drain():
    """The radio must be let go at once, not after the queued audio is played.

    This is the fix for a delay that was invisible to every other test.

    The end-of-stream sentinel used to be *appended* to the listener's queue.
    The response generator reads the queue in order and the pacer hands those
    chunks out at the stream's realtime rate, so a listener sitting on the
    27-chunk (9.2 s) reserve did not see the sentinel for 9.2 s. Measured on
    the live Pi: a 7-11 s gap between "closing 1 listener connection" and the
    connection actually closing, in 26 of 33 watchdog fires.

    That delay defeated the purpose. The radio's decoder is already broken
    when the watchdog fires; the re-anchor exists to cut it loose so it can
    resynchronise, and every extra second of backlog is more damaged audio
    handed to a decoder that cannot recover from it. It also swallowed the
    cooldown - the watchdog fired again while the first repair was still in
    flight - so the radio was hit twice and the repair arrived later than the
    "within 10 s" the cooldown was supposed to bound.

    The sentinel must therefore be the *next* thing the generator sees, with
    the backlog dropped: that audio was going to be discarded by the
    disconnect anyway.
    """
    settings = _settings(chunk_size=8192)
    manager, _session, subscriber = await _manager_with_a_listener(settings)
    try:
        # Fill the listener's queue with the backlog a radio accumulates while
        # the pipeline delivers a segment faster than realtime.
        backlog = 27
        for i in range(backlog):
            subscriber.queue.put_nowait((i * 8192, b"x" * 8192))
        assert subscriber.queue.qsize() == backlog

        assert manager.reanchor_stalled_sessions() == 1

        # The very next item the generator reads must be the sentinel, not the
        # backlog that was queued before it.
        assert subscriber.queue.get_nowait() is None, (
            "the end-of-stream sentinel was queued behind the backlog, so the "
            "radio was left playing damaged audio for the whole backlog's "
            "duration - 9.2 s of it at the default settings - instead of being "
            "re-anchored immediately"
        )
        assert subscriber.queue.empty(), (
            "the backlog was not discarded; it is audio that was going to be "
            "thrown away by the disconnect anyway"
        )
    finally:
        await manager.aclose()


async def test_the_reanchor_does_not_count_the_discarded_audio_as_dropped():
    """The dropped counter means "the listener could not keep up".

    It is a capacity signal, and the dashboard shows it next to the queue
    depth. A deliberate disconnect is the opposite of a capacity problem: the
    audio was discarded because the connection is being ended, not because the
    radio was too slow. Counting it as dropped would make a healthy stream
    look like a starved one, and would hide real drops behind repair noise.

    The audio is still not *claimed* to have been delivered: ``bytes_sent``
    only counts what the transport accepted, and the discarded backlog never
    reached it.
    """
    settings = _settings(chunk_size=8192)
    manager, _session, subscriber = await _manager_with_a_listener(settings)
    try:
        for i in range(5):
            subscriber.queue.put_nowait((i * 8192, b"x" * 8192))
        before = subscriber.dropped

        manager.reanchor_stalled_sessions()

        assert subscriber.dropped == before, (
            "the deliberately discarded backlog was counted as dropped chunks, "
            "which the dashboard reports as a listener that could not keep up"
        )
    finally:
        await manager.aclose()


async def test_the_loop_fires_on_a_burst_and_ignores_the_baseline():
    """End to end through the poll loop, with a scripted counter.

    The script is the shape of the real measurement: a healthy baseline of zero,
    then an interference burst of 21 segments in one poll.

    The first value is consumed before the loop starts, so it is the "previous"
    reading and can never itself be a burst - which is what stops a restart from
    reading the machine's entire lifetime of retransmissions as one event.
    """
    settings = _settings()
    settings.retransmit_poll_seconds = 0.01
    settings.retransmit_reconnect_segments = 3
    manager, session, _ = await _manager_with_a_listener(settings)
    try:
        manager.retransmit_counter = FakeCounter([1000, 1000, 1000, 1021])
        manager._retransmit_task = asyncio.create_task(manager._retransmit_loop())

        for _ in range(200):
            if manager.retransmit_reconnects:
                break
            await asyncio.sleep(0.01)

        assert manager.retransmit_reconnects == 1, (
            "a 21-segment retransmission burst did not close the connection"
        )
        assert manager.retransmit_peak_delta == 21
        assert session.stall_reconnects == 1
    finally:
        await manager.aclose()


async def test_the_loop_ignores_a_quiet_network():
    settings = _settings()
    settings.retransmit_poll_seconds = 0.01
    settings.retransmit_reconnect_segments = 3
    manager, _, _ = await _manager_with_a_listener(settings)
    try:
        # A couple of retransmissions is normal traffic and must be tolerated.
        manager.retransmit_counter = FakeCounter([1000, 1001, 1002, 1002, 1003])
        manager._retransmit_task = asyncio.create_task(manager._retransmit_loop())
        await asyncio.sleep(0.15)

        assert manager.retransmit_reconnects == 0, (
            "ordinary retransmissions closed a working connection"
        )
        assert manager.retransmit_peak_delta <= 2
    finally:
        await manager.aclose()


def test_the_default_thresholds_sit_between_the_measured_distributions():
    """The thresholds have to separate healthy traffic from interference.

    Measured on the Pi over 420 consecutive one-second polls on a quiet
    network: 415 polls at zero retransmissions, two at 1, and **three at 6**.
    So a healthy Wi-Fi link produces bursts of 6 in a single poll.

    Measured during interference: per-poll bursts of 3, 4, 5, 5, 5, 7 and 9,
    with **21 segments inside one ten-second window**.

    The per-poll distributions **overlap** - healthy reaches 6, interference
    starts at 3 - so a single-poll threshold cannot separate them. The totals
    over ten seconds do: healthy peaked at **7**, interference reached **21**.

    Hence the two defaults: the per-poll threshold stays high (10, above every
    healthy poll ever measured) because it is only the "one enormous poll"
    case, and the *window* threshold is 14 - twice the healthy maximum,
    comfortably below the interference figure. The window is the working
    trigger.

    This test exists because the design was wrong in both directions in
    released versions. A per-poll threshold of 3 fired on healthy traffic
    every couple of minutes; a per-poll threshold of 10 was unreachable (the
    interference never produced a poll above 9) and silently disabled the
    whole watchdog. The failure mode in both cases is quiet: a watchdog that
    never fires, and one that fires on a clean network, look identical until
    somebody counts the reconnects.
    """
    settings = Settings()
    assert settings.retransmit_reconnect_segments == 10, (
        "the per-poll threshold must stay above every healthy poll ever "
        "measured (6), so it never fires on a clean network"
    )
    assert settings.retransmit_window_seconds == 10.0, (
        "the window must be the ten seconds the measurements were taken in: "
        "healthy peaked at 7 over that window and interference reached 21"
    )
    assert settings.retransmit_window_segments == 14, (
        "the window threshold must sit between the healthy maximum (7) and "
        "the interference figure (21)"
    )


async def test_the_loop_tolerates_healthy_traffic_but_catches_interference():
    """The whole argument for the two thresholds, as behaviour.

    A healthy network produces bursts of 6 in a single poll and totals of 7
    over ten seconds. Interference produces polls of up to 9 and totals of 21.
    The healthy case must not close a connection; the interference case must.
    """
    settings = _settings()
    settings.retransmit_poll_seconds = 0.01
    settings.retransmit_window_seconds = 0.1
    settings.retransmit_window_segments = 14
    settings.retransmit_reconnect_segments = 10
    manager, _session, _ = await _manager_with_a_listener(settings)
    try:
        # A healthy network: the measured shape, including the bursts of 6.
        manager.retransmit_counter = FakeCounter(
            [1000, 1000, 1000, 1006, 1006, 1007, 1007, 1007, 1008]
        )
        manager._retransmit_task = asyncio.create_task(manager._retransmit_loop())
        await asyncio.sleep(0.2)

        assert manager.retransmit_reconnects == 0, (
            "a healthy network's retransmission bursts closed a working "
            "connection; the measured healthy burst is 6 segments"
        )
        assert manager.retransmit_peak_delta == 6, (
            f"the peak should record the healthy burst, got {manager.retransmit_peak_delta}"
        )
    finally:
        await manager.aclose()

    # Now the interference burst: 21 segments spread over several polls, which
    # is what the short microwave test actually produced.
    manager, _session, _ = await _manager_with_a_listener(settings)
    try:
        manager.retransmit_counter = FakeCounter([1000, 1003, 1007, 1011, 1021])
        manager._retransmit_task = asyncio.create_task(manager._retransmit_loop())

        for _ in range(200):
            if manager.retransmit_reconnects:
                break
            await asyncio.sleep(0.01)

        assert manager.retransmit_reconnects == 1, (
            "sustained interference - 21 segments spread over several polls - "
            "did not close the connection; this is the case the short "
            "microwave test produced and the per-poll threshold missed"
        )
    finally:
        await manager.aclose()


async def test_the_window_is_what_catches_sustained_interference():
    """A burst spread over polls is below the per-poll threshold every time.

    The short microwave test produced per-poll bursts of 3, 4, 5 - none of
    which reaches a per-poll threshold of 10, which is why that release's
    watchdog never fired during the test. The ten-second total of 21 is what
    identifies it.

    This test pins that mechanism directly: every individual poll is below the
    per-poll threshold, and only the sum crosses it.
    """
    settings = _settings()
    settings.retransmit_poll_seconds = 0.01
    settings.retransmit_window_seconds = 1.0
    settings.retransmit_window_segments = 14
    settings.retransmit_reconnect_segments = 10
    manager, _session, _ = await _manager_with_a_listener(settings)
    try:
        # Five polls of 3 and 4: no single poll reaches 10, but the sum is 18.
        manager.retransmit_counter = FakeCounter([1000, 1003, 1006, 1010, 1014, 1018])
        manager._retransmit_task = asyncio.create_task(manager._retransmit_loop())

        for _ in range(200):
            if manager.retransmit_reconnects:
                break
            await asyncio.sleep(0.01)

        assert manager.retransmit_reconnects == 1, (
            "sustained damage below the per-poll threshold was not caught; "
            "the windowed sum is the only trigger that can see it"
        )
        assert manager.retransmit_peak_window >= 14, (
            f"the windowed total should have reached the threshold, got "
            f"{manager.retransmit_peak_window}"
        )
    finally:
        await manager.aclose()


async def test_the_window_forgets_old_polls():
    """The window is a trailing window, not a running total.

    Without the eviction the sum would grow for as long as the service ran,
    and every healthy poll would eventually push it over the threshold - the
    watchdog would fire on a clean network after a few hours of uptime, which
    is the worst possible failure mode for a monitor: it looks fine until it
    suddenly does not.

    The window here holds two polls, so the third one's value must not be
    counted.
    """
    settings = _settings()
    settings.retransmit_poll_seconds = 0.05
    settings.retransmit_window_seconds = 0.1
    settings.retransmit_window_segments = 14
    settings.retransmit_reconnect_segments = 10
    manager, _session, _ = await _manager_with_a_listener(settings)
    try:
        # Each poll adds 5, and the window holds two polls: 5 + 5 = 10, which
        # is below the threshold of 14. If the window never evicted, the sum
        # would climb past 14 on the fourth poll and close the connection.
        manager.retransmit_counter = FakeCounter([1000, 1005, 1010, 1015, 1020, 1025, 1030])
        manager._retransmit_task = asyncio.create_task(manager._retransmit_loop())
        await asyncio.sleep(0.4)

        assert manager.retransmit_reconnects == 0, (
            "the window grew without bound, so ordinary traffic eventually "
            "crossed the threshold - a monitor that fires on a clean network "
            "after a few hours of uptime"
        )
        assert manager.retransmit_window_delta <= 10, (
            f"the window should hold at most two polls, but it summed to "
            f"{manager.retransmit_window_delta}"
        )
    finally:
        await manager.aclose()


async def test_one_enormous_poll_fires_on_its_own():
    """The per-poll trigger is the emergency case, and it must still work.

    A single poll of 15 exceeds the per-poll threshold of 10 even though the
    windowed total never reaches 14 - a catastrophic loss in one second. The
    window would dilute it, so the per-poll path has to exist alongside it.
    """
    settings = _settings()
    settings.retransmit_poll_seconds = 0.01
    settings.retransmit_window_seconds = 10.0
    settings.retransmit_window_segments = 10000  # unreachable
    settings.retransmit_reconnect_segments = 10
    manager, _session, _ = await _manager_with_a_listener(settings)
    try:
        manager.retransmit_counter = FakeCounter([1000, 1015])
        manager._retransmit_task = asyncio.create_task(manager._retransmit_loop())

        for _ in range(200):
            if manager.retransmit_reconnects:
                break
            await asyncio.sleep(0.01)

        assert manager.retransmit_reconnects == 1, (
            "a single enormous poll did not close the connection; the "
            "per-poll trigger is the emergency path and must still work"
        )
    finally:
        await manager.aclose()


async def test_a_finished_burst_fires_exactly_once():
    """One interference event must re-anchor once, not once per cooldown.

    Measured on the Pi, 2.3.1: a single event fired **twice**, 5 s apart.
    The per-poll trigger fired on 16 segments in one second, and the window
    still held that same damage (24, 24, 24, 23, 23) - so the moment the
    cooldown expired the *window* trigger fired again on evidence that had
    already been acted upon:

        16:34:09  window=24  reconnects=1  FIRE #1 (per-poll 16)
        16:34:14  window=23  reconnects=2  FIRE #2 (window 23 >= 14)
        16:34:14  listener done after 1s     <- killed as it connected

    The radio had reconnected in that very second and was disconnected within
    it, then reconnected again, and stayed glitching. A radio needs a few
    seconds of unbroken stream to rebuild its decoder state, so a second
    re-anchor taken on the same evidence guarantees it never gets them.

    The cooldown (5 s) is shorter than the window (10 s) by construction, so
    without emptying the window *any* window-triggered re-anchor could always
    be repeated once. This pins that it cannot.
    """
    settings = _settings()
    settings.retransmit_poll_seconds = 0.01
    settings.retransmit_window_seconds = 0.1  # two polls
    settings.retransmit_window_segments = 14
    settings.retransmit_reconnect_segments = 10
    settings.stall_reconnect_cooldown_seconds = 0.0  # cooldown cannot save us
    manager, _session, subscriber = await _manager_with_a_listener(settings)
    try:
        # The measured shape: a big poll, then the same damage sitting in the
        # window for several polls. The counter is FLAT afterwards, which is
        # what the flat segments 24,24,24,23,23 were.
        manager.retransmit_counter = FakeCounter(
            [1000, 1016, 1016, 1016, 1016, 1016, 1016, 1016, 1016, 1016]
        )
        manager._retransmit_task = asyncio.create_task(manager._retransmit_loop())
        await asyncio.sleep(0.4)

        assert manager.retransmit_reconnects == 1, (
            f"one interference event closed the connection "
            f"{manager.retransmit_reconnects} times; the second re-anchor lands "
            "on a radio that has only just reconnected and is still rebuilding "
            "its decoder state"
        )
        assert subscriber.queue.get_nowait() is None
    finally:
        await manager.aclose()


async def test_new_damage_after_a_fire_still_re_arms_the_window():
    """Emptying the window must not defuse the watchdog for good.

    Sustained interference - the microwave staying on - has to keep being
    caught. Only the *already-acted-on* damage is discarded; anything new
    accumulates normally.
    """
    settings = _settings()
    settings.retransmit_poll_seconds = 0.01
    settings.retransmit_window_seconds = 0.1
    settings.retransmit_window_segments = 14
    settings.retransmit_reconnect_segments = 100  # only the window can fire
    settings.stall_reconnect_cooldown_seconds = 0.0
    manager, _session, _ = await _manager_with_a_listener(settings)
    try:
        # A first burst that fires, then a second, genuine burst afterwards.
        manager.retransmit_counter = FakeCounter(
            [1000, 1015, 1015, 1015, 1015, 1035, 1035, 1035, 1035]
        )
        manager._retransmit_task = asyncio.create_task(manager._retransmit_loop())
        await asyncio.sleep(0.4)

        assert manager.retransmit_reconnects >= 2, (
            f"new damage after a re-anchor did not fire again "
            f"({manager.retransmit_reconnects} fires) - emptying the window "
            "must not disable the watchdog for the rest of the outage"
        )
    finally:
        await manager.aclose()


async def test_damage_arriving_with_no_listeners_does_not_kill_the_next_radio():
    """A radio that attaches after the interference is a clean connection.

    The station stays warm for `linger_seconds` after its last radio leaves, so
    an interference burst can land while there is nobody to re-anchor. If that
    damage is kept in the window, the next radio to attach is disconnected on
    its very first poll - for damage it never received, on a connection whose
    stream is clean.

    Measured shape of the risk: the window reached 29 during the test, and the
    radio reconnects on its own every few minutes, so the gap between one
    radio leaving and the next attaching is where this happens.
    """
    settings = _settings()
    settings.retransmit_poll_seconds = 0.01
    # A window long enough that the damage cannot simply expire while the test
    # switches from "no listeners" to "a listener has attached". With a short
    # window the stale damage slides out on its own and the bug hides.
    settings.retransmit_window_seconds = 10.0
    settings.retransmit_window_segments = 14
    settings.retransmit_reconnect_segments = 100  # only the window can fire
    settings.stall_reconnect_cooldown_seconds = 0.0
    manager = SessionManager(settings)
    await manager.start()
    if manager._retransmit_task is not None:
        manager._retransmit_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await manager._retransmit_task
        manager._retransmit_task = None
    try:
        # Damage arrives while nobody is listening.
        manager.retransmit_counter = FakeCounter([1000, 1015, 1015, 1015, 1015])
        manager._retransmit_task = asyncio.create_task(manager._retransmit_loop())
        await asyncio.sleep(0.2)

        assert manager.retransmit_reconnects == 0, (
            "a burst was counted as a re-anchor even though no listener was closed"
        )

        # The radio now attaches, to a stream that is by then clean.
        _session, _subscriber = await manager.subscribe(
            "http://x/one.m3u8",
            ["http://x/one.m3u8"],
            client="192.168.1.65",
            user_agent="NSPlayer/8.0.0.3801",
        )
        await asyncio.sleep(0.1)

        assert manager.retransmit_reconnects == 0, (
            "the freshly attached radio was re-anchored for damage that "
            "arrived before it connected; it is a clean connection and its "
            "decoder has nothing to recover from"
        )
    finally:
        await manager.aclose()


# ------------------------------------------------- the periodic safety net
async def test_a_wedged_radio_is_repaired_without_any_evidence_of_damage():
    """The failure that motivated this: no trigger fired, so nothing repaired it.

    Measured on the song-titles channel: the radio went into the permanent
    garble with **neither** evidence-based trigger firing - the journal for the
    whole episode has no "retransmitted" and no "silence" line - while the
    stream being delivered was verified perfect (555 frames, 0 bad syncs, clean
    `ffmpeg -err_detect explode` decode).

    There was nothing to detect. Once the interference stops, a wedged radio
    acknowledges every byte at full rate, which is exactly what a healthy radio
    does, so *no* evidence-based threshold can bound how long it stays broken.
    Only a timer can, and this is its test: a completely silent network must
    still produce a re-anchor.
    """
    settings = _settings()
    settings.reanchor_interval_seconds = 120.0
    manager, session, subscriber = await _manager_with_a_listener(settings)
    try:
        # A perfectly healthy network: no retransmissions at all.
        assert manager.reanchor_overdue_sessions(now=1000.0) == 1, (
            "a radio was left broken on a quiet network because no trigger "
            "produced evidence; this is the reported permanent glitch"
        )
        assert manager.scheduled_reconnects == 1
        assert session.scheduled_reconnects == 1
        assert subscriber.queue.get_nowait() is None, "no end-of-stream sentinel"
        # The two evidence-based counters must stay at zero: this is the timer,
        # and conflating the two would hide which mechanism is doing the work.
        assert manager.retransmit_reconnects == 0
        assert session.stall_reconnects == 0
    finally:
        await manager.aclose()


async def test_the_safety_net_waits_its_interval():
    """It must not re-anchor on every tick, or it becomes a reconnect storm."""
    settings = _settings()
    settings.reanchor_interval_seconds = 120.0
    manager, _session, _ = await _manager_with_a_listener(settings)
    try:
        assert manager.reanchor_overdue_sessions(now=1000.0) == 1
        assert manager.reanchor_overdue_sessions(now=1119.0) == 0, (
            "the timer fired inside its interval"
        )
        assert manager.reanchor_overdue_sessions(now=1120.0) == 1, (
            "the timer never fired again after its interval expired"
        )
        assert manager.scheduled_reconnects == 2
    finally:
        await manager.aclose()


async def test_the_safety_net_can_be_disabled():
    settings = _settings()
    settings.reanchor_interval_seconds = 0.0
    manager, _session, _ = await _manager_with_a_listener(settings)
    try:
        assert manager.reanchor_overdue_sessions(now=1000.0) == 0
        assert manager.reanchor_overdue_sessions(now=99999.0) == 0
    finally:
        await manager.aclose()


async def test_the_safety_net_leaves_a_station_with_no_listeners_alone():
    settings = _settings()
    settings.reanchor_interval_seconds = 120.0
    manager = SessionManager(settings)
    await manager.start()
    try:
        session, subscriber = await manager.subscribe(
            "http://x/one.m3u8", ["http://x/one.m3u8"], client="192.168.1.65"
        )
        await manager.unsubscribe(session, subscriber)
        assert manager.reanchor_overdue_sessions(now=1000.0) == 0
        assert manager.scheduled_reconnects == 0
    finally:
        await manager.aclose()


def test_the_default_safety_net_is_on_and_sane():
    """The reported failure was "glitches until the service is restarted".

    A default of 0 would mean the release cannot fix that on its own for anyone
    who does not read the config, so the default has to be on - and the interval
    has to be short enough to matter while staying in the same order as the
    radio's own reconnect cadence (20-60 s, measured over hundreds of
    connections in the journal).
    """
    settings = Settings()
    assert settings.reanchor_interval_seconds > 0, (
        "the safety net is off by default, so a radio that stops decoding with "
        "no evidence of damage would stay broken until the service restarts"
    )
    assert 60.0 <= settings.reanchor_interval_seconds <= 300.0, (
        f"an interval of {settings.reanchor_interval_seconds}s is out of proportion "
        "to the radio's own reconnect cadence (20-60 s)"
    )


async def test_the_loop_ignores_a_counter_that_goes_backwards():
    """The OS can reset its statistics, and that is not an interference burst.

    Two things have to hold. It must not close a connection - a negative delta
    is already below any threshold, so that part is free. And it must not
    *publish* the negative figure: `retransmit_last_delta` is what an operator
    reads to answer "was the Wi-Fi bad?", and -4990 segments in one second is
    not a measurement, it is a reset.

    The published value is therefore sampled throughout, not checked at the end.
    A first version asserted only the final value, and passed even with the
    guard removed: the negative is visible for exactly one poll and is
    overwritten by the next, healthy reading, so the last value seen is always
    fine.
    """
    settings = _settings()
    settings.retransmit_poll_seconds = 0.01
    settings.retransmit_reconnect_segments = 3
    manager, _, _ = await _manager_with_a_listener(settings)
    try:
        manager.retransmit_counter = FakeCounter([5000, 10, 10, 10, 10, 10])
        manager._retransmit_task = asyncio.create_task(manager._retransmit_loop())

        worst = 0
        for _ in range(100):
            worst = min(worst, manager.retransmit_last_delta)
            await asyncio.sleep(0.002)

        assert manager.retransmit_reconnects == 0, (
            "a counter reset was read as a burst of retransmissions"
        )
        assert worst >= 0, (
            f"a counter reset was published as {worst} retransmissions in one "
            "poll; the figure is read by an operator to judge the network"
        )
        assert manager.retransmit_peak_delta >= 0
    finally:
        await manager.aclose()


async def test_the_watchdog_is_not_started_when_it_cannot_work():
    """Off by configuration, or unsupported by the platform, must mean off."""
    # Threshold 0 = explicitly off.
    manager = SessionManager(_settings(retransmit_reconnect_segments=0))
    await manager.start()
    try:
        assert manager._retransmit_task is None, (
            "the poll loop runs even though the watchdog is disabled"
        )
    finally:
        await manager.aclose()

    # A platform that cannot report the figure: enabled by configuration, but
    # there is nothing to read, so the loop must not start either.
    manager = SessionManager(_settings())
    manager.retransmit_counter = TcpRetransmitCounter("unavailable")
    await manager.start()
    try:
        assert manager._retransmit_task is None, (
            "the poll loop runs on a platform that cannot report retransmissions"
        )
    finally:
        await manager.aclose()


def test_the_counter_source_is_reported():
    """An operator has to be able to tell "armed" from "silently disabled"."""
    manager = SessionManager(_settings())
    described = manager.as_dict()
    assert described["retransmit_source"] in ("linux", "darwin", "unavailable")
    assert "retransmit_reconnects" in described
