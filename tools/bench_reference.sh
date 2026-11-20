#!/usr/bin/env bash
# The reference benchmark of spec/03: Tradewind against backtrader on the same machine, the same
# seeded bars and the same strategy classes (tools/bench_engine.py), two cases:
#   small  5 symbols x 20 years (5 040 bars)    -- seconds; CI runs it on every push
#   wide   4 000 symbols x 20 years             -- backtrader alone needs ~1 h per scenario
#
#   TW_PY=<python with numpy+pandas> BT_PY=<python of the oracle env> tools/bench_reference.sh small
#   TW_PY=... BT_PY=... tools/bench_reference.sh wide        # after the grand remeasure, idle machine
#
# backtrader runs only in BT_PY's environment (clean-room rule 2); Tradewind is taken from this
# checkout's src/. Give both environments the same numpy and pandas. Results: bench/<case>-<host>-<date>/
# (tradewind.json, backtrader.json, report.md); the report also checks the two engines' final
# values are equal, scenario by scenario.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
CASE="${1:-small}"
TW_PY="${TW_PY:-python}"
BT_PY="${BT_PY:?python of an environment holding backtrader (the oracle environment)}"
case "$CASE" in
  small) args=(--symbols 5 --years 20); tw_rep=9; bt_rep=3 ;;
  wide)  args=(--symbols 4000 --years 20); tw_rep=3; bt_rep=1 ;;
  *) echo "case: small or wide" >&2; exit 2 ;;
esac
out="${OUT:-$ROOT/bench}/$CASE-$(hostname -s)-$(date -u +%Y%m%d)"
mkdir -p "$out"
export TQDM_DISABLE=1 TRADEWIND_SRC="$ROOT/src"
nice -n 0 "$TW_PY" "$HERE/bench_engine.py" "${args[@]}" --repeat "$tw_rep" --json "$out/tradewind.json"
nice -n 0 "$BT_PY" "$HERE/bench_engine.py" --engine backtrader "${args[@]}" --repeat "$bt_rep" --json "$out/backtrader.json"
"$TW_PY" "$HERE/bench_report.py" "$out/backtrader.json" "$out/tradewind.json" | tee "$out/report.md"
