#!/usr/bin/env bash
# Stream full-RGB video to a 9-panel (384x192) frame and report what landed.
#
# This is the v1.38.0 acceptance test: the CPU is out of the pixel path, so the
# numbers that matter are frames_completed and frames_dropped from the panel
# itself. Do NOT judge this by bench_stream.py's loss column -- with the
# gateware pixel filter on, that column reads a CPU counter which is always
# zero, because the CPU never sees a pixel packet.
#
#   ./tools/test_9panel_rgb.sh [host] [fps] [video]
#
# With no video argument it generates a 20 s testsrc2 clip with ffmpeg.
# Requires the panel to be configured 384x192 (grid 3x3 of 128x64).
set -euo pipefail

HOST="${1:-192.168.1.70}"
FPS="${2:-30}"
VIDEO="${3:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${VIDEO}" ]]; then
    VIDEO="$(mktemp -d)/test384.mp4"
    echo "Generating ${VIDEO}"
    ffmpeg -y -v error -f lavfi -i "testsrc2=size=384x192:rate=30:duration=20" \
           -pix_fmt yuv420p "${VIDEO}"
fi

stat_of() { curl -s -m 10 "http://${HOST}/api/bitmap/stats" | python3 -c \
    "import json,sys; d=json.load(sys.stdin); print(d['$1'])"; }

echo "panel:  $(curl -s -m 10 "http://${HOST}/api/status" | python3 -c \
    'import json,sys; d=json.load(sys.stdin); print(d["display_width"],"x",d["display_height"],"grid",d["grid"])')"
echo "filter: $(curl -s -m 10 "http://${HOST}/api/hwfilter")"

before_done="$(stat_of frames_completed)"
before_drop="$(stat_of frames_dropped)"

python3 "${HERE}/tools/send_video.py" "${VIDEO}" \
    --host "${HOST}" --width 384 --height 192 --fps "${FPS}" 2>&1 | tail -1

after_done="$(stat_of frames_completed)"
after_drop="$(stat_of frames_dropped)"

echo "completed: $(( after_done - before_done ))   dropped: $(( after_drop - before_drop ))"
echo "killed in gateware: $(curl -s -m 10 "http://${HOST}/api/hwfilter" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["killed"])')"
