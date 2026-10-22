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
