#!/usr/bin/env bash
set -eu

# Most modules use native ESM; the standalone TV player scripts are classic JS.
for file in static/*.js; do
  case "$file" in
    static/tv.js|static/tv-overlay.js) node --check "$file" ;;
    *) node --input-type=module --check < "$file" ;;
  esac
done
node --test tests/test_*.cjs
node --check experiments/bilibili-overlay/overlay.js
