#!/usr/bin/env bash
# Harness for the installer's helper functions.
#
# These are extracted rather than sourced because install.sh runs top-to-bottom
# and cannot be sourced safely. Run with bash, NOT zsh: the IP helper relies on
# unquoted expansion word-splitting, which is a bash behaviour.
set -uo pipefail

SCRIPT="${1:-packaging/install.sh}"

extract() {
    awk "/^$1\(\)/,/^}/" "${SCRIPT}"
}

eval "$(extract is_unusable_ip)"
eval "$(extract detect_lan_ip)"
eval "$(extract detect_config_port)"
eval "$(extract wheel_provides_ffmpeg)"
eval "$(extract eol_apt_needed)"

fail=0
check() {
    local label="$1" expected="$2" actual="$3"
    if [[ "${actual}" == "${expected}" ]]; then
        printf '  ok   %-46s %s\n' "${label}" "${actual}"
    else
        printf '  FAIL %-46s got %q, want %q\n' "${label}" "${actual}" "${expected}"
        fail=1
    fi
}

echo "== detect_lan_ip: a radio must be able to reach the answer =="
# The motivating case: docker0 is listed first and would be useless.
hostname() { echo "172.17.0.1 10.0.0.42 192.168.1.50"; }
check "docker0 first, then LAN" "10.0.0.42" "$(detect_lan_ip)"

hostname() { echo "172.17.0.1 172.20.0.1 192.168.0.24"; }
check "docker0 + user bridge, then LAN" "172.20.0.1" "$(detect_lan_ip)"

hostname() { echo "192.168.1.50 172.17.0.1"; }
check "LAN first" "192.168.1.50" "$(detect_lan_ip)"

# Regression: the fallback pass once returned 172.17.0.1 here, i.e. exactly the
# address the first pass had rejected.
hostname() { echo "::1 fe80::1 127.0.0.1 169.254.1.1 172.17.0.1"; }
detect_lan_ip >/dev/null
check "only unusable addresses -> rc=1" "1" "$?"

hostname() { echo "203.0.113.9"; }
check "public address only -> still usable" "203.0.113.9" "$(detect_lan_ip)"

# Both discovery mechanisms must fail for this to be empty; otherwise macOS's
# `ipconfig getifaddr en0` quietly supplies the real address.
hostname() { echo ""; }
ipconfig() { return 1; }
detect_lan_ip >/dev/null
check "no addresses at all -> rc=1" "1" "$?"
unset -f ipconfig

echo "== detect_config_port =="
tmp="$(mktemp -d)"
printf 'port = 8123\n'     > "${tmp}/plain"
printf 'port = "9000"\n'   > "${tmp}/quoted"
printf '# port = 1\n'      > "${tmp}/commented"
printf 'port=7777\n'       > "${tmp}/tight"
printf 'host = "0.0.0.0"\nport = 4444\nbitrate = 192000\n' > "${tmp}/mixed"
printf 'bitrate = 192000\n' > "${tmp}/absent"

check "port = 8123"    "8123" "$(detect_config_port "${tmp}/plain")"
check "port = \"9000\"" "9000" "$(detect_config_port "${tmp}/quoted")"
check "commented out"  ""     "$(detect_config_port "${tmp}/commented")"
check "port=7777"      "7777" "$(detect_config_port "${tmp}/tight")"
check "among others"   "4444" "$(detect_config_port "${tmp}/mixed")"
check "absent"         ""     "$(detect_config_port "${tmp}/absent")"
check "missing file"   ""     "$(detect_config_port "${tmp}/nope")"

echo "== station-list handling must not come back =="
# This project is deliberately config-free: there is no stations.toml, so the
# installer must not grow code to inspect one again.
if grep -q 'stations\.toml\|has_configured_stations' "${SCRIPT}"; then
    printf '  FAIL %s\n' "install.sh still references a station list"
    fail=1
else
    printf '  ok   %s\n' "install.sh has no station-list handling"
fi

echo "== wheel_provides_ffmpeg: 32-bit ARM has no bundled binary =="
# This is the whole reason the installer has to install ffmpeg itself. On a Pi
# the imageio-ffmpeg wheel supplies nothing, so assuming it does leaves a working
# service that fails on every AAC station.
ffmpeg_rc() {
    if wheel_provides_ffmpeg; then echo "provided"; else echo "not provided"; fi
}

uname() { echo "armv7l"; }
check "armv7l (Pi 2/3 32-bit)" "not provided" "$(ffmpeg_rc)"
uname() { echo "armv6l"; }
check "armv6l (Pi Zero)" "not provided" "$(ffmpeg_rc)"
uname() { echo "aarch64"; }
check "aarch64 (64-bit Pi)" "provided" "$(ffmpeg_rc)"
uname() { echo "x86_64"; }
check "x86_64" "provided" "$(ffmpeg_rc)"
unset -f uname

echo "== eol_apt_needed: bullseye's security repo is gone =="
# Debian 11 went EOL in Aug 2026, so security.debian.org 404s for it and apt
# fails with errors that never mention the real cause. Raspberry Pi OS and OSMC
# still ship bullseye, so this is the common case, not an edge case.
SEC_BULLSEYE="deb http://security.debian.org/ bullseye-security main contrib non-free"
SEC_BOOKWORM="deb http://security.debian.org/ bookworm-security main"
MAIN_ONLY="deb http://ftp.debian.org/debian bullseye main"
ARCHIVE_OK="deb http://archive.debian.org/debian-security bullseye-security main"

eol_rc() {
    if eol_apt_needed "$1" "$2"; then echo "needs-fix"; else echo "ok"; fi
}

check "bullseye + security.debian.org" "needs-fix" "$(eol_rc bullseye "${SEC_BULLSEYE}")"
check "bookworm (still supported)" "ok" "$(eol_rc bookworm "${SEC_BOOKWORM}")"
check "bullseye, security already moved" "ok" "$(eol_rc bullseye "${ARCHIVE_OK}")"
check "bullseye with no security line" "ok" "$(eol_rc bullseye "${MAIN_ONLY}")"
check "no codename" "ok" "$(eol_rc "" "${SEC_BULLSEYE}")"

echo "== ffmpeg must be provisioned, never assumed =="
require() {
    local label="$1" pattern="$2"
    if grep -q "${pattern}" "${SCRIPT}"; then
        printf '  ok   %s\n' "${label}"
    else
        printf '  FAIL %-46s missing: %s\n' "${label}" "${pattern}"
        fail=1
    fi
}
forbid() {
    local label="$1" pattern="$2"
    if grep -q "${pattern}" "${SCRIPT}"; then
        printf '  FAIL %-46s still present: %s\n' "${label}" "${pattern}"
        fail=1
    else
        printf '  ok   %s\n' "${label}"
    fi
}

require "installs system ffmpeg"        'apt-get install -y -qq ffmpeg'
require "detects ffmpeg via radioconv"  'detect_ffmpeg'
require "installs build tools for cffi/psutil" 'build-essential'
require "checks ensurepip, not just venv" 'import ensurepip'
forbid  "does not claim the wheel always provides ffmpeg" 'pulls a bundled ffmpeg'
forbid  "does not claim no apt packages are needed" 'no apt packages are needed'

echo
if [[ ${fail} -eq 0 ]]; then
    echo "all helper checks passed"
else
    echo "SOME HELPER CHECKS FAILED"
fi
exit "${fail}"
