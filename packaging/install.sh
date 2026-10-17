#!/usr/bin/env bash
#
# Install HLS Radio Converter as a systemd service.
#
# Designed for a Raspberry Pi running Raspberry Pi OS / Debian, but works on any
# systemd Linux. It creates a virtualenv, installs the package, makes sure an
# ffmpeg binary is available, writes a config file, installs a hardened systemd
# unit and starts it.
#
# FFmpeg is not optional in practice. Almost every station is AAC, which has to
# be decoded and re-encoded to MP3, and only FFmpeg does that. The
# ``imageio-ffmpeg`` wheel bundles a binary for macOS, Windows and Linux
# x86_64/aarch64, but publishes no 32-bit ARM build - and 32-bit ARM is the
# default Raspberry Pi OS install on older Pis. So on the platform this script
# mostly targets, ffmpeg has to come from the system package manager, and this
# script installs it when the wheel cannot supply one.
#
#   sudo ./packaging/install.sh                       # from a git checkout
#   sudo ./packaging/install.sh --source /tmp/src     # from a directory
#   sudo ./packaging/install.sh --port 9000 --user pi # options
#   sudo ./packaging/install.sh --uninstall           # remove it again
#
set -euo pipefail

USER_NAME="${SUDO_USER:-$(id -un)}"
PREFIX="/opt/radioconv"
CONFIG_DIR="/etc/radioconv"
STATE_DIR="/var/lib/radioconv"
PORT=""
SOURCE=""
INSTALL_SERVICE=1
UNINSTALL=0
PURGE=0
CHECK=1
FIX_EOL_APT=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SOURCE="$(cd "${SCRIPT_DIR}/.." && pwd)"
#: The real upstream. Kept in one place; tests/test_metadata.py checks that the
#: README and pyproject.toml agree with it.
PROJECT_URL="https://github.com/clifarr/HLS-Radio-Converter"

usage() {
    sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --user NAME       system user the service runs as   (default: the caller)
  --prefix DIR      where the venv lives             (default: /opt/radioconv)
  --config-dir DIR  where config.toml is written     (default: /etc/radioconv)
  --state-dir DIR   writable state directory         (default: /var/lib/radioconv)
  --port PORT       port to listen on                (default: 8123)
  --source PATH     directory or URL to install from (default: this checkout)
  --no-service      install only; do not touch systemd
  --no-check        skip the post-install self-test
  --fix-eol-apt     repair apt on end-of-life Debian (e.g. bullseye) first
  --uninstall       stop and remove the service, venv and unit file
  --purge           with --uninstall, also delete config and state
EOF
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Is this address unusable *by a radio*, whatever else we might find?
#
# Shared by both passes in detect_lan_ip. It used to be inlined in the first
# pass only, which meant the fallback could hand back the very address the
# first pass had just rejected (docker0).
is_unusable_ip() {
    case "$1" in
        *:*)       return 0 ;;   # IPv6 - radios want an IPv4 address
        127.*)     return 0 ;;   # loopback
        169.254.*) return 0 ;;   # link-local
        172.17.*)  return 0 ;;   # docker0 default bridge
    esac
    return 1
}

# Pick an address a radio on the LAN can actually reach.
#
# `hostname -I` lists every address, and the first one is frequently not the
# useful one: with Docker installed it is often 172.17.0.1 (the docker0 bridge),
# which no radio can reach. Prefer a private LAN address, and never return
# something a radio cannot route to - the caller falls back to a clear
# placeholder instead, which is more honest than a URL that cannot work.
detect_lan_ip() {
    local candidates ip
    candidates="$(hostname -I 2>/dev/null)" || candidates=""
    if [[ -z "${candidates}" ]]; then
        # `hostname -I` is a Linux extension; this is for running the script
        # on a Mac to try it out.
        candidates="$(ipconfig getifaddr en0 2>/dev/null || true)"
    fi

    # First pass: a private LAN address, which is what a radio needs.
    for ip in ${candidates}; do
        is_unusable_ip "${ip}" && continue
        case "${ip}" in
            192.168.*|10.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*)
                echo "${ip}"; return 0 ;;
        esac
    done

    # Second pass: accept anything else still usable, e.g. a public address.
    for ip in ${candidates}; do
        is_unusable_ip "${ip}" && continue
        echo "${ip}"; return 0
    done
    return 1
}

# Read the listen port out of the generated config file.
#
# Deliberately POSIX `sed`, not `grep -oP`: -P is a GNU extension, and where it
# is unavailable the failure is silent and the summary prints the wrong port.
detect_config_port() {
    local file="$1"
    [[ -f "${file}" ]] || return 1
    sed -n 's/^[[:space:]]*port[[:space:]]*=[[:space:]]*"\{0,1\}\([0-9][0-9]*\).*/\1/p' \
        "${file}" | head -n 1
}

# Does the `imageio-ffmpeg` wheel bundle a binary for this architecture?
#
# It covers macOS, Windows and Linux x86_64/aarch64 only. There is no 32-bit ARM
# build, and 32-bit ARM is what older Pis run by default - so on the platform
# this script exists for, the wheel supplies nothing and ffmpeg must come from
# the distro. Kept as a function (rather than an inline `case`) so the helper
# harness in scripts/test-installer-helpers.sh can pin the behaviour.
wheel_provides_ffmpeg() {
    case "$(uname -m)" in
        armv6l | armv7l)
            return 1
            ;;
        *)
            return 0
            ;;
    esac
}

# Is there a package manager here that we know how to drive non-interactively?
have_apt() {
    command -v apt-get >/dev/null 2>&1
}

# Ask radioconv where it found ffmpeg. Empty output means "nowhere".
detect_ffmpeg() {
    local venv="$1"
    "${venv}/bin/python" -c \
        'from radioconv.ffmpeg import locate_ffmpeg; print(locate_ffmpeg())' 2>/dev/null || true
}

# Is this a Debian release that has reached end-of-life?
#
# When a release goes EOL its files are pulled from security.debian.org, and apt
# then fails on individual packages with bare "404 Not Found" lines that say
# nothing about the cause. Debian 11 (bullseye) went EOL in August 2026, and it
# is what Raspberry Pi OS and OSMC still ship, so this is the *common* case for
# the platforms this script targets rather than an edge case.
#
# Split in two so the decision itself is testable: this half takes the codename
# and the configured sources as arguments, and the wrapper below reads them from
# the machine.
eol_apt_needed() {
    local codename="$1" sources_text="$2"
    [[ "${codename}" == "bullseye" ]] || return 1
    grep -q 'security\.debian\.org' <<<"${sources_text}" || return 1
    return 0
}

eol_apt_repo_detected() {
    local codename
    codename="$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release 2>/dev/null | tr -d '"')"
    eol_apt_needed "${codename}" \
        "$(cat /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null)"
}

# Apply the minimal end-of-life repair, keeping a backup of sources.list.
#
# Deliberately opt-in via --fix-eol-apt: rewriting a machine's apt sources is a
# system-wide change that should never happen behind an operator's back. The
# archive carries the *final* bullseye security content, so this restores access
# to the last security updates rather than merely disabling the errors.
apply_eol_apt_fix() {
    local sources="/etc/apt/sources.list"
    log "pointing bullseye-security at archive.debian.org (backup kept)"
    cp -n "${sources}" "${sources}.bak-radioconv"
    sed -i 's|http://security\.debian\.org/ bullseye-security|http://archive.debian.org/debian-security bullseye-security|' \
        "${sources}"
    # Archive Release files are expired by definition, so apt must stop refusing
    # to use them.
    printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99radioconv-eol
    apt-get update -qq || true
}

# Print the exact repair for the operator to run (or re-run with the flag).
report_eol_apt() {
    warn "this $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-Debian}") is end-of-life:"
    warn "security.debian.org no longer serves bullseye, so apt install fails with 404s."
    warn "Either re-run this script with --fix-eol-apt, or run:"
    warn "  sudo cp -n /etc/apt/sources.list{,.bak}"
    warn "  sudo sed -i 's|security.debian.org/ bullseye-security|archive.debian.org/debian-security bullseye-security|' /etc/apt/sources.list"
    warn "  echo 'Acquire::Check-Valid-Until \"false\";' | sudo tee /etc/apt/apt.conf.d/99radioconv-eol"
    warn "  sudo apt-get update"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)       USER_NAME="$2"; shift 2 ;;
        --prefix)     PREFIX="$2"; shift 2 ;;
        --config-dir) CONFIG_DIR="$2"; shift 2 ;;
        --state-dir)  STATE_DIR="$2"; shift 2 ;;
        --port)       PORT="$2"; shift 2 ;;
        --source)     SOURCE="$2"; shift 2 ;;
        --fix-eol-apt) FIX_EOL_APT=1; shift ;;
        --no-service) INSTALL_SERVICE=0; shift ;;
        --no-check)   CHECK=0; shift ;;
        --uninstall)  UNINSTALL=1; shift ;;
        --purge)      PURGE=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run this with sudo (it installs a systemd unit)"

id "${USER_NAME}" >/dev/null 2>&1 || die "no such user: ${USER_NAME}"
GROUP_NAME="$(id -gn "${USER_NAME}")"

# --------------------------------------------------------------------- remove
if [[ $UNINSTALL -eq 1 ]]; then
    log "stopping and removing the service"
    systemctl stop radioconv.service 2>/dev/null || true
    systemctl disable radioconv.service 2>/dev/null || true
    rm -f /etc/systemd/system/radioconv.service
    systemctl daemon-reload 2>/dev/null || true
    rm -rf "${PREFIX}"
    if [[ $PURGE -eq 1 ]]; then
        warn "purging ${CONFIG_DIR} and ${STATE_DIR}"
        rm -rf "${CONFIG_DIR}" "${STATE_DIR}"
    else
        log "kept ${CONFIG_DIR} and ${STATE_DIR} (use --purge to delete them)"
    fi
    log "done"
    exit 0
fi

# ---------------------------------------------------------------------- source
if [[ -z "${SOURCE}" ]]; then
    if [[ -f "${DEFAULT_SOURCE}/pyproject.toml" ]]; then
        SOURCE="${DEFAULT_SOURCE}"
    else
        # Do NOT fall back to a bare package name: this project is not published
        # to any index, so `pip install hls-radio-converter` would fail with a
        # confusing "No matching distribution found".
        die "cannot find the project source.

Run this script from a git checkout:

  git clone ${PROJECT_URL}.git
  sudo ./HLS-Radio-Converter/packaging/install.sh

or point it at a copy you already have:

  sudo ./install.sh --source /path/to/HLS-Radio-Converter"
    fi
fi
if [[ -d "${SOURCE}" ]]; then
    [[ -f "${SOURCE}/pyproject.toml" ]] || die "${SOURCE} does not look like the project (no pyproject.toml)"
    log "installing from ${SOURCE}"
fi

# ---------------------------------------------------------------------- python
PYTHON="$(command -v python3 || true)"
[[ -n "${PYTHON}" ]] || die "python3 not found; install it first (apt install python3 python3-venv)"
"${PYTHON}" - <<'PY' || die "Python 3.9 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
log "using $("${PYTHON}" --version)"

# `import venv` is not enough on Debian. The `venv` module ships with python3
# itself, while `ensurepip` comes from the python3-venv package, so a bare
# `python3 -m venv` fails with "ensurepip is not available" even though the
# import succeeds - exactly the trap this check used to fall into. Test the
# thing that actually has to work.
if ! "${PYTHON}" -c "import ensurepip" 2>/dev/null; then
    die "python3 cannot create virtualenvs (ensurepip is missing); run: apt install python3-venv"
fi

# ------------------------------------------------------------------ eol apt
# Debian 11 (bullseye) is what Raspberry Pi OS and OSMC still ship, and it went
# end-of-life in August 2026. apt then fails on individual packages with bare
# 404s that never mention the cause, so check before we depend on apt at all.
if eol_apt_repo_detected; then
    if [[ $FIX_EOL_APT -eq 1 ]]; then
        apply_eol_apt_fix
    else
        report_eol_apt
    fi
fi

# ------------------------------------------------------------------ install
log "creating ${PREFIX}"
install -d -o "${USER_NAME}" -g "${GROUP_NAME}" "${PREFIX}"
install -d -o "${USER_NAME}" -g "${GROUP_NAME}" "${STATE_DIR}"
install -d "${CONFIG_DIR}"

VENV="${PREFIX}/venv"
if [[ ! -x "${VENV}/bin/python" ]]; then
    log "creating virtualenv (${VENV})"
    "${PYTHON}" -m venv "${VENV}"
fi
"${VENV}/bin/python" -m pip install --quiet --upgrade pip wheel

# Install our package, adding a C toolchain if the build needs one.
#
# `cffi` (pulled in by cryptography) and `psutil` publish no 32-bit ARM wheels,
# so on a Pi this compiles from source - and Raspberry Pi OS ships no compiler by
# default. Rather than failing halfway through with a wall of gcc errors, notice
# and install the toolchain, then retry once. Doing it lazily keeps the common
# case (x86_64/aarch64, everything from wheels) free of extra apt packages.
install_package() {
    log "installing the package from ${SOURCE}"
    "${VENV}/bin/python" -m pip install --quiet --upgrade "${SOURCE}"
}

if ! install_package; then
    if have_apt && ! command -v cc >/dev/null 2>&1; then
        log "install failed and no C compiler is present - installing build tools"
        apt-get update -qq
        apt-get install -y -qq build-essential python3-dev libffi-dev
        install_package
    else
        die "pip install failed; see the output above"
    fi
fi
"${VENV}/bin/radioconv" --version

# -------------------------------------------------------------------- ffmpeg
# Not optional: almost every station is AAC, which must be decoded and re-encoded
# to MP3, and only ffmpeg does that. On 32-bit ARM the imageio-ffmpeg wheel
# supplies nothing at all, so without this step the service installs cleanly and
# then fails on nearly every real stream.
FFMPEG_PATH="$(detect_ffmpeg "${VENV}")"
if [[ -z "${FFMPEG_PATH}" ]] && have_apt; then
    if wheel_provides_ffmpeg; then
        log "no ffmpeg found; installing the system package"
    else
        log "no ffmpeg found and the wheel has no build for $(uname -m); installing the system package"
    fi
    apt-get update -qq
    apt-get install -y -qq ffmpeg
    FFMPEG_PATH="$(detect_ffmpeg "${VENV}")"
fi
if [[ -z "${FFMPEG_PATH}" ]]; then
    die "ffmpeg is required but could not be found. Install it (apt install ffmpeg) and re-run."
fi
log "ffmpeg: ${FFMPEG_PATH}"

# -------------------------------------------------------------------- config
if [[ ! -f "${CONFIG_DIR}/config.toml" ]]; then
    log "writing ${CONFIG_DIR}/config.toml"
    {
        echo "# HLS Radio Converter configuration."
        echo "# Every value here is optional - these are the built-in defaults."
        echo "# Any setting can also be given as RADIOCONV_<NAME> (e.g. RADIOCONV_PORT)."
        echo "# See packaging/config.toml.example for the full annotated list."
        echo
        echo "host = \"0.0.0.0\""
        echo "port = ${PORT:-8123}"
        echo "bitrate = 192000"
        echo "output_codec = \"mp3\""
        echo "prefer_passthrough = true"
        echo "variant_selection = \"highest\""
        echo "max_sessions = 4"
        echo "linger_seconds = 25.0"
        echo "log_level = \"INFO\""
    } > "${CONFIG_DIR}/config.toml"
else
    log "keeping the existing ${CONFIG_DIR}/config.toml"
fi

# ------------------------------------------------------------------- systemd
if [[ $INSTALL_SERVICE -eq 1 ]]; then
    UNIT_SRC="${SCRIPT_DIR}/radioconv.service"
    [[ -f "${UNIT_SRC}" ]] || die "cannot find radioconv.service next to this script"
    UNIT_DST="/etc/systemd/system/radioconv.service"

    log "installing ${UNIT_DST}"
    sed -e "s|@USER@|${USER_NAME}|g" \
        -e "s|@GROUP@|${GROUP_NAME}|g" \
        -e "s|@STATE_DIR@|${STATE_DIR}|g" \
        -e "s|@CONFIG_DIR@|${CONFIG_DIR}|g" \
        -e "s|@EXEC_START@|${VENV}/bin/radioconv serve --config ${CONFIG_DIR}/config.toml|g" \
        "${UNIT_SRC}" > "${UNIT_DST}"
    chmod 0644 "${UNIT_DST}"

    systemctl daemon-reload
    systemctl enable radioconv.service >/dev/null
    log "starting the service"
    systemctl restart radioconv.service

    sleep 2
    if systemctl is-active --quiet radioconv.service; then
        log "service is running"
    else
        warn "the service is not running; recent log lines follow"
        journalctl -u radioconv.service -n 30 --no-pager || true
    fi
fi

# -------------------------------------------------------------------- finish
if [[ $CHECK -eq 1 ]]; then
    log "checking that this machine can run radioconv"
    "${VENV}/bin/radioconv" check --skip-port || warn "some checks failed - see above"
fi

IP_ADDR="$(detect_lan_ip)" || true
[[ -n "${IP_ADDR}" ]] || IP_ADDR="<this-machine-ip>"
EFFECTIVE_PORT="${PORT:-$(detect_config_port "${CONFIG_DIR}/config.toml")}"
[[ -n "${EFFECTIVE_PORT}" ]] || EFFECTIVE_PORT=8123
RADIOCONV_BIN="${VENV}/bin/radioconv"
BASE_URL="http://${IP_ADDR}:${EFFECTIVE_PORT}"

cat <<EOF

$(log "installed")

  Config      : ${CONFIG_DIR}/config.toml
  Binary      : ${RADIOCONV_BIN}
  Service     : systemctl status radioconv
  Logs        : journalctl -u radioconv -f

  Test page   : ${BASE_URL}/

EOF

# There is no station list to print: the whole point of this project is that a
# radio is given one encoded stream URL and nothing else is configured. So say
# how to produce that URL, and be explicit that we cannot know it for them.
cat <<EOF
Give each radio the encoded URL of the stream it should play:

  ${BASE_URL}/<percent-encoded-m3u8-url>

Print the exact string for your own stream with:

  ${RADIOCONV_BIN} url '<your-m3u8-url>' --host ${IP_ADDR} --port ${EFFECTIVE_PORT}

The VLC-style "listen" link on most radio station websites contains the .m3u8
URL to paste in above. Check a stream works before rebooting a radio with:

  ${RADIOCONV_BIN} probe '<your-m3u8-url>'

EOF
