#!/usr/bin/env bash
# Manual operator-only TIFA probe; not part of the web server.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 AUDIO.wav OUTPUT_DIR" >&2
  echo "Put UTF-8 lyrics in a same-stem .txt file next to the audio." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIPELINE="$HOME/.local/share/vc-ktv/pipeline"
PYTHON="$PIPELINE/runtime/tifa/bin/python"
TIFA="$PIPELINE/models/forced_alignment/sources/TIFA"
MODEL="$PIPELINE/models/forced_alignment/tifa/TIFA-1.0-ST/model.pt"

audio="$(realpath -e -- "$1")"
transcript="$(dirname "$audio")/$(basename "$audio" | sed 's/\.[^.]*$/.txt/')"
test -s "$transcript" || { echo "Missing verified lyric text: $transcript" >&2; exit 2; }
test -x "$PYTHON" || { echo "Missing experimental TIFA Python env" >&2; exit 2; }
test -f "$TIFA/infer.py" || { echo "Missing pinned TIFA checkout" >&2; exit 2; }
test -f "$MODEL" || { echo "Missing TIFA-1.0-ST model" >&2; exit 2; }

mkdir -p -- "$2"
output="$(realpath -e -- "$2")"
echo "Audio: $audio"
echo "Lyrics: $transcript"
echo "Output: $output"
echo "Review TIFA timing before importing it into the VC KTV room."

language="$(printenv TIFA_LANGUAGE || echo zh)"
exec "$PYTHON" "$TIFA/infer.py" "$audio" \
  -m "$MODEL" -l "$language" -o "$output" \
  --batch-size 1 --num-workers 0 --stat
