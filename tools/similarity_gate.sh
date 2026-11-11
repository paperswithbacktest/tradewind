#!/usr/bin/env bash
# Similarity gate (clean-room rule 4 of CLEANROOM.md), run by the release workflow and by CI.
#
# Installs the reference named in the allow-list (backtrader, pinned by version and sha256)
# into a throwaway virtual environment that nothing else uses, and runs tools/similarity.py
# against it, generic Python discounted with the standard-library base the allow-list names.
# The tool reads the reference by program and prints scores and Tradewind locations only;
# it exits 1 on any range of 41 tokens or more (or AST statement of 40 nodes or more) that
# is not in the allow-list. Nothing here imports, prints or copies the reference.
#
#   tools/similarity_gate.sh            the gate on src/tradewind
#   tools/similarity_gate.sh --control  then a negative control: the same gate on a copy of
#                                       the tree with engine.py planted under a new name
#                                       must FAIL, or the gate is vacuous
#
# Environment: PYTHON (default python; its minor version must be the allow-list's base
# Python), ALLOWLIST (default spec/similarity-allowlist.json), TARGET (default
# src/tradewind), JSON_OUT (optional path for the full report, target locations only).
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python}
ALLOW=${ALLOWLIST:-spec/similarity-allowlist.json}
TARGET=${TARGET:-src/tradewind}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

META=$("$PY" -I -c '
import json, sys
a = json.load(open(sys.argv[1], encoding="utf-8"))
r, b = a["reference"], a["base"]
print(r["package"], r["version"], r["sha256"], b["python"], " ".join(b["stdlib"]))
' "$ALLOW")
read -r PKG VERSION SHA PYVER STDMODS <<<"$META"

HAVE=$("$PY" -I -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if [ "$HAVE" != "$PYVER" ]; then
  echo "the allow-list was calibrated on the standard library of Python $PYVER, this is $HAVE"
  exit 2
fi

# the reference: pinned and hash-checked, never compiled, never imported
"$PY" -m venv "$WORK/env"
echo "$PKG==$VERSION --hash=sha256:$SHA" >"$WORK/requirements.txt"
"$WORK/env/bin/pip" install -q --disable-pip-version-check --no-compile --no-deps \
  --require-hashes -r "$WORK/requirements.txt"
REF="$("$WORK/env/bin/python" -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/$PKG"
[ -d "$REF" ] || { echo "reference package directory not found after install"; exit 2; }
echo "reference: $PKG $VERSION (sha256 $SHA), installed in a throwaway environment"

STD=$("$PY" -I -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')
BASE=()
for m in $STDMODS; do
  [ -e "$STD/$m" ] || { echo "base module $m not found in $STD"; exit 2; }
  BASE+=(--base "$STD/$m")
done
echo "base: $(echo "$STDMODS" | wc -w | tr -d ' ') modules of the Python $HAVE standard library"
echo

gate() {
  "$PY" -I tools/similarity.py --ref "$REF" "${BASE[@]}" --target "$1" --allowlist "$ALLOW" \
    ${JSON_OUT:+--json "$JSON_OUT"}
}

gate "$TARGET"

if [ "${1:-}" = "--control" ]; then
  echo
  echo "negative control: engine.py planted as _planted_copy.py must fail the gate"
  cp -R "$TARGET" "$WORK/planted"
  cp "$TARGET/engine.py" "$WORK/planted/_planted_copy.py"
  if JSON_OUT="" gate "$WORK/planted" >"$WORK/control.txt"; then
    echo "the gate passed a planted copy: it is vacuous"
    exit 1
  fi
  grep -E "NEW|ERROR|^gate:" "$WORK/control.txt"
  grep -qE "NEW +[^ ]*_planted_copy\.py" "$WORK/control.txt" || {
    echo "the gate failed, but not on the planted file"
    exit 1
  }
  echo "negative control: the gate failed on the planted copy, as it must"
fi
