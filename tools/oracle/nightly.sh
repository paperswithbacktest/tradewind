#!/usr/bin/env bash
# The corpus oracle's nightly slice, on the data host (spec/03-oracle.md, "Nightly").
#
#   tools/oracle/nightly.sh            # tonight's seventh: crc32(slug) % 7 == day of week - 1
#   tools/oracle/nightly.sh 3/7        # a given slice
#   tools/oracle/nightly.sh all        # the whole set (~240 h of reference compute: ~30 h at 4 jobs)
#
# Each reference runs in the environment of the pwb-toolbox that produced it (two of them for
# v1: papers tree 51945b9 with 0.1.51, fe89474 with 0.1.52; see oracle/v1.md), with Tradewind
# taken from $TW_SRC. Results go to $OUT/<date>-<slice>/<tree>/ (results.jsonl, summary.json).
#
# Environment (defaults are the data host's paths):
#   TW_SRC      Tradewind sources under test (src/ of a checkout of main)
#   PAPERS      a git clone of the papers repository holding both trees; a partial clone is
#               enough (git clone --filter=blob:none --no-checkout, then fetch both commits)
#   PY_0151     python of an environment built from tools/oracle/env/requirements-0151.txt
#   PY_0152     python of an environment built from tools/oracle/env/requirements-0152.txt
#   JOBS        parallel strategies (4)
#   OUT         results root
# Exit 1 if any slice has an unexpected verdict (or stopped on the disk guard).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TW_SRC="${TW_SRC:-$HOME/tradewind/src}"
PAPERS="${PAPERS:-$HOME/oracle/papers}"
PY_0151="${PY_0151:-$HOME/remeasure/venv0151/bin/python}"
PY_0152="${PY_0152:-$HOME/remeasure/venv0152/bin/python}"
JOBS="${JOBS:-4}"
OUT="${OUT:-$HOME/tradewind-oracle-runs}"
SLICE="${1:-$(( $(date +%u) - 1 ))/7}"

if [ "$SLICE" = all ]; then shard=(); tag=all; else shard=(--shard "$SLICE"); tag="${SLICE/\//of}"; fi
stamp="$(date -u +%Y%m%d)-$tag"
rc=0
for pair in "51945b907e5c6f34a0bcab3708141a17ebca83e8:$PY_0151" "fe89474c75ec386ec2340c1d2c4cfcda043eef60:$PY_0152"; do
  tree="${pair%%:*}"; py="${pair#*:}"
  out="$OUT/$stamp/${tree:0:7}"
  mkdir -p "$out"
  nice -n 10 "$py" "$HERE/run.py" --subset all "${shard[@]}" --tree "$tree" --papers "$PAPERS" \
    --python "$py" --tradewind-src "$TW_SRC" --jobs "$JOBS" --timeout 4800 --min-free-gb 25 \
    --out-dir "$out" >> "$out/run.log" 2>&1 || rc=1
  tail -n 1 "$out/run.log"
done
exit $rc
