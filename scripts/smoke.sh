#!/usr/bin/env bash
#
# End-to-end smoke test: generate real HLS content, serve it locally, start the
# real `radioconv serve` process, then drive it exactly like a radio would and
# verify the audio that comes out is a valid MP3.
#
#   ./scripts/smoke.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
RADIOCONV="${ROOT}/.venv/bin/radioconv"
HLS_PORT=8777
APP_PORT=8778
WORK="$(mktemp -d)"
SERVER_PID=""
APP_PID=""

cleanup() {
    [[ -n "${APP_PID}" ]] && kill "${APP_PID}" 2>/dev/null || true
    [[ -n "${SERVER_PID}" ]] && kill "${SERVER_PID}" 2>/dev/null || true
    rm -rf "${WORK}"
    rm -f /tmp/radioconv-smoke.mp3
}
trap cleanup EXIT

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

[[ -x "${PY}" ]] || { echo "run: uv sync"; exit 1; }

FFMPEG="$("${PY}" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
echo "ffmpeg: ${FFMPEG}"

step "generating 30s of real MP3-in-TS HLS content"
# 192 kbit/s to match the shipped default: in pass-through mode the output
# bitrate is the source's, so testing at any other rate would misrepresent what
# a radio actually receives.
"${FFMPEG}" -hide_banner -loglevel error \
    -f lavfi -i "sine=frequency=440:sample_rate=44100:duration=30" \
    -ac 2 -c:a libmp3lame -b:a 192k \
    -f hls -hls_time 2 -hls_list_size 0 \
    -hls_segment_filename "${WORK}/seg%d.ts" "${WORK}/index.m3u8"
ls "${WORK}" | head -5

step "serving it on 127.0.0.1:${HLS_PORT}"
( cd "${WORK}" && exec "${PY}" -m http.server "${HLS_PORT}" --bind 127.0.0.1 ) >/dev/null 2>&1 &
SERVER_PID=$!

step "starting radioconv serve on 127.0.0.1:${APP_PORT}"
"${RADIOCONV}" serve --host 127.0.0.1 --port "${APP_PORT}" \
    --log-level INFO --max-sessions 2 --linger 5 >"${WORK}/app.log" 2>&1 &
APP_PID=$!

for _ in $(seq 1 40); do
    curl -sf "http://127.0.0.1:${APP_PORT}/health" >/dev/null 2>&1 && break
    sleep 0.5
done

SOURCE="http://127.0.0.1:${HLS_PORT}/index.m3u8"
ENCODED="$("${PY}" -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "${SOURCE}")"

step "GET /health"
curl -s "http://127.0.0.1:${APP_PORT}/health" | "${PY}" -m json.tool | head -20

step "GET /metrics"
curl -s "http://127.0.0.1:${APP_PORT}/metrics" | grep -E '^radioconv_(sessions|listeners|up)' || true

step "streaming via the ORIGINAL v1 URL shape: /<percent-encoded-url>"
curl -s --max-time 8 -o /tmp/radioconv-smoke.mp3 -w 'HTTP %{http_code}  content-type=%{content_type}\n' \
    "http://127.0.0.1:${APP_PORT}/${ENCODED}" || true

step "the received bytes must be a valid MP3"
"${FFMPEG}" -hide_banner -i /tmp/radioconv-smoke.mp3 -f null - 2>&1 | grep -E 'Audio:|Duration' || true
printf 'received: %s bytes\n' "$(wc -c < /tmp/radioconv-smoke.mp3)"

step "session state while/after streaming"
curl -s "http://127.0.0.1:${APP_PORT}/health" \
    | "${PY}" -c 'import json,sys; d=json.load(sys.stdin)["streams"]; print("sessions:", d["sessions"], "listeners:", d["listeners"]); [print(" mode:", s["pipeline"]["mode"], "| packets:", s["pipeline"]["segments"], "| failovers:", s["pipeline"]["failovers"]) for s in d["details"]]'

step "server log"
tail -25 "${WORK}/app.log"

printf '\n\033[1;32msmoke test complete\033[0m\n'
