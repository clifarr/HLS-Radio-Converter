"""How many TCP segments the operating system has retransmitted, ever.

Why this exists
---------------
A radio's decoder cannot recover from damaged audio, and the damage arrives as
Wi-Fi interference: packets are lost, TCP retransmits them, and what the radio
receives is late and bursty rather than absent.  That is invisible to this
process - the kernel's socket buffers absorbed a measured **2.8 s** stall
without the application ever seeing a blocked write - so no amount of timing
inside the server can detect it.

The kernel does know, though: it counts every segment it had to send twice.
Measured on the Pi, on the same connection:

* normal running: **47 retransmissions in 776 s** (0.06 per second, plus a
  handful per hour from unrelated traffic);
* during microwave interference: **21 in a ten-second window**, and 27 (36 KB)
  inside a single stall.

That is a signal with three orders of magnitude of headroom, it needs no
privileges, and it is available on Linux and macOS.

Portability
-----------
``None`` means "this platform cannot tell us", and the watchdog that uses it is
then switched off rather than guessed at:

===================  ==========================================================
Linux                ``/proc/net/snmp``, the ``Tcp:`` line's ``RetransSegs``
macOS                ``netstat -s -p tcp``, "data packet ... retransmitted"
Windows              ``GetTcpStatisticsEx`` from ``iphlpapi.dll``, via ctypes
other                unavailable
===================  ==========================================================

On Windows the figure is *not* read from ``netstat``. Its output is localised, so
a parser would key on a label that changes with the system language - on a Dutch
or Japanese Windows the number is there and unreadable. The IP Helper API returns
a **struct** instead, and a struct field has no language: ``MIB_TCPSTATS``
carries ``dwRetransSegs`` at a fixed offset. That is why this module does not
need to declare Windows unsupported, which an earlier version wrongly did.

The Windows path is the one part of this module that cannot be exercised by the
test suite: the tests run on Linux and macOS, and ``ctypes.WinDLL`` does not
exist anywhere else. What *is* tested is the part that could silently be wrong -
which 4-byte slot of the struct holds the count - by decoding raw bytes in
``parse_mib_tcpstats``. The call itself is written defensively so that a failure
on a real Windows box leaves the figure unreadable, and unreadable means the
watchdog stays off, not that the service misbehaves.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Callable, Optional, Protocol

#: Where Linux exposes its TCP counters.
PROC_NET_SNMP = "/proc/net/snmp"

#: The field of the `Tcp:` line that counts retransmitted segments.
LINUX_FIELD = "RetransSegs"

#: The fields of Windows' ``MIB_TCPSTATS``, in declaration order.
#:
#: Hand-transcribed from the SDK header rather than generated, because ctypes has
#: no way to read a header. The order is the contract: ``dwRetransSegs`` is the
#: twelfth field, at byte offset 44. ``parse_mib_tcpstats`` isolates that offset
#: so the tests can pin it without Windows.
WINDOWS_TCPSTATS_FIELDS = (
    "dwRtoAlgorithm",
    "dwRtoMin",
    "dwRtoMax",
    "dwMaxConn",
    "dwActiveOpens",
    "dwPassiveOpens",
    "dwAttemptFails",
    "dwEstabResets",
    "dwCurrEstab",
    "dwInSegs",
    "dwOutSegs",
    "dwRetransSegs",
    "dwInErrs",
    "dwOutRsts",
    "dwNumConns",
)

#: Byte offset of ``dwRetransSegs`` inside a ``MIB_TCPSTATS``. Every field is a
#: DWORD, so the offset is purely the index times four - derived, not copied, so
#: the two cannot drift apart.
WINDOWS_RETRANSMIT_OFFSET = 4 * WINDOWS_TCPSTATS_FIELDS.index("dwRetransSegs")

#: ``sizeof(MIB_TCPSTATS)``. Asserted so a field being added to the tuple above
#: without thought is caught rather than silently shifting the offset.
WINDOWS_TCPSTATS_SIZE = 4 * len(WINDOWS_TCPSTATS_FIELDS)

#: ``ADDRESS_FAMILY`` values for ``GetTcpStatisticsEx``.
_AF_INET = 2
_AF_INET6 = 23

#: macOS spells the counter out. Note the singular/plural nouns: the line reads
#: "1 data packet (1 byte) retransmitted" or "7 data packets (9 bytes)
#: retransmitted", so a parser that demands the plural misses the quiet case.
_MACOS_RETRANSMITTED = re.compile(
    r"^\s*(\d+)\s+data\s+packets?\s+\(\s*\d+\s+bytes?\s*\)\s+retransmitted\b",
    re.MULTILINE,
)


def parse_proc_net_snmp(text: str) -> Optional[int]:
    """The ``RetransSegs`` total from the contents of ``/proc/net/snmp``.

    The file is a sequence of header/data pairs, both prefixed with the same
    protocol name, so the field is located by name in the header rather than by
    a fixed column - a kernel that adds a counter would otherwise silently shift
    every number after it. ``None`` if the pair is absent or ragged.
    """
    header: Optional[list] = None
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name.strip() != "Tcp":
            continue
        fields = rest.split()
        if header is None:
            header = fields
            continue
        # The second `Tcp:` line is the data, in the header's order.
        if LINUX_FIELD not in header:
            return None
        index = header.index(LINUX_FIELD)
        if index >= len(fields):
            return None
        try:
            return int(fields[index])
        except ValueError:
            return None
    return None


def parse_netstat_tcp(text: str) -> Optional[int]:
    """The retransmitted-packet total from ``netstat -s -p tcp`` on macOS."""
    match = _MACOS_RETRANSMITTED.search(text)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - the regex guarantees digits
        return None


class RetransmitCounter(Protocol):
    """What the watchdog needs from a counter, and all it is allowed to assume.

    The manager accepts *any* object with these two attributes and a ``read``,
    which is how a test drives the watchdog with a scripted series of readings
    instead of the machine's real traffic. Declaring the attribute as this
    protocol rather than as `TcpRetransmitCounter` says so in the type system
    instead of only in the docstrings.
    """

    #: Short name for logs and metrics: ``linux``, ``darwin``, ``windows`` or
    #: ``unavailable``.
    name: str
    #: False when this platform cannot report the figure at all.
    available: bool

    def read(self) -> Optional[int]:
        """The cumulative count, or ``None`` when it cannot be read now."""
        ...


class TcpRetransmitCounter:
    """A cumulative retransmission count, or a stand-in that cannot read one.

    ``read`` never raises: a counter that disappears (a kernel module unloaded,
    a platform that cannot be read) must switch the watchdog off, not take the
    stream down with it.
    """

    #: Short name for logs and metrics: ``linux``, ``darwin`` or ``unavailable``.
    name: str
    #: False when this platform cannot report the figure at all.
    available: bool

    def __init__(
        self,
        name: str,
        reader: Optional[Callable[[], Optional[int]]] = None,
    ) -> None:
        self.name = name
        self.available = reader is not None
        self._reader = reader

    def read(self) -> Optional[int]:
        if self._reader is None:
            return None
        try:
            return self._reader()
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            # A vanished /proc entry, a netstat that would not run, or a
            # ctypes call that could not resolve its symbol all mean "no
            # measurement right now". AttributeError is in the list for the
            # Windows path: an older iphlpapi without GetTcpStatisticsEx raises
            # it, and the correct response is to switch the watchdog off rather
            # than take the stream down.
            return None


def _read_proc_net_snmp() -> Optional[int]:
    with open(PROC_NET_SNMP, encoding="ascii", errors="replace") as handle:
        return parse_proc_net_snmp(handle.read())


def _read_netstat_tcp() -> Optional[int]:
    # Resolved rather than run by bare name: a system without netstat must
    # report "no measurement", not raise a FileNotFoundError into the watchdog.
    executable = shutil.which("netstat")
    if executable is None:
        return None
    result = subprocess.run(  # noqa: S603 - a fixed argument list, no shell, no input
        [executable, "-s", "-p", "tcp"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        return None
    return parse_netstat_tcp(result.stdout.decode("utf-8", "replace"))


def parse_mib_tcpstats(raw: bytes) -> Optional[int]:
    """The ``dwRetransSegs`` field of a raw ``MIB_TCPSTATS`` struct.

    Split out from the ``ctypes`` call on purpose. On Windows this reads memory
    that ctypes has already laid out; in the tests it reads a hand-built buffer.
    The only thing that can silently be wrong in either case is *which* 4-byte
    slot holds the count - read the wrong one and the watchdog watches the wrong
    number while looking perfectly healthy - so that decision is isolated here
    where it can be tested on a machine that is not Windows.
    """
    if len(raw) < WINDOWS_RETRANSMIT_OFFSET + 4:
        return None
    end = WINDOWS_RETRANSMIT_OFFSET + 4
    # MIB_TCPSTATS is a C struct of DWORDs: unsigned, native (little-endian on
    # every Windows architecture this project can run on).
    return int.from_bytes(raw[WINDOWS_RETRANSMIT_OFFSET:end], "little")


def _read_windows_tcp_stats() -> Optional[int]:
    """Retransmitted segments, from ``GetTcpStatisticsEx``.

    The IP Helper API rather than ``netstat``: netstat's output is localised, so
    the label a parser would search for is different in every language. A struct
    field is not.

    Both address families are summed, because a stream can be carried over
    either and the two counters are kept separately. A failure on one family is
    ignored rather than failing the whole reading - a machine with IPv6 disabled
    still reports IPv4 damage.
    """
    import ctypes
    from ctypes import wintypes

    class MIB_TCPSTATS(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in WINDOWS_TCPSTATS_FIELDS]

    iphlpapi = ctypes.WinDLL("iphlpapi.dll")  # ty: ignore[unresolved-attribute]
    function = iphlpapi.GetTcpStatisticsEx
    function.argtypes = [ctypes.POINTER(MIB_TCPSTATS), wintypes.ULONG]
    function.restype = wintypes.ULONG
    no_error = 0

    total = 0
    seen = False
    for family in (_AF_INET, _AF_INET6):
        stats = MIB_TCPSTATS()
        if function(ctypes.byref(stats), family) != no_error:
            continue
        raw = ctypes.string_at(ctypes.byref(stats), ctypes.sizeof(stats))
        value = parse_mib_tcpstats(raw)
        if value is None:  # pragma: no cover - only if the struct shrank
            continue
        total += value
        seen = True
    return total if seen else None


def tcp_retransmit_counter(platform: Optional[str] = None) -> TcpRetransmitCounter:
    """The counter for this platform, or an unavailable stand-in.

    *platform* is only for the tests; production passes nothing and the real
    ``sys.platform`` decides. It is checked rather than the file's presence, so
    a Linux container without ``/proc`` mounted reports unavailable rather than
    appearing to work and returning nothing for ever.
    """
    name = platform if platform is not None else sys.platform
    if name.startswith("linux"):
        if not os.path.exists(PROC_NET_SNMP):
            return TcpRetransmitCounter("unavailable")
        return TcpRetransmitCounter("linux", _read_proc_net_snmp)
    if name == "darwin":
        return TcpRetransmitCounter("darwin", _read_netstat_tcp)
    if name == "win32":
        return TcpRetransmitCounter("windows", _read_windows_tcp_stats)
    return TcpRetransmitCounter("unavailable")
