#!/bin/zsh
# Run a list of corpus strategies on Tradewind, one at a time (memory), and log the verdicts.
#   tools/run_corpus_batch.sh <slugs file> <log file>
S=/private/tmp/claude-501/-Users-edarchimbaud-dev-pwb/9b98f010-554f-4aad-907c-7eca85dbf02a/scratchpad
set -a; . /Users/edarchimbaud/dev/pwb/data-api/.env; set +a
export PWB_HF_OFFLINE=1 TQDM_DISABLE=1; unset PWB_API_KEY
cd /Users/edarchimbaud/dev/pwb/tradewind || exit 1
while read -r slug; do
  [ -z "$slug" ] && continue
  echo "== $slug $(date -u +%H:%M)"
  $S/venv_tw/bin/python tools/run_corpus_strategy.py "$slug" 2>&1 | sed -E 's/hf_[A-Za-z0-9]+/hf_***/g; s/pwb_api_key=[A-Za-z0-9]+/***/g' | grep -v -i "key=\|token\|FutureWarning\|warnings.warn" | grep "bars in\|IDENTICAL\|DIFFERS\|DATES\|Error\|error\|no strategy" | cut -c1-240
done < "$1" >> "$2" 2>&1
echo "BATCH DONE $(date -u +%H:%M)" >> "$2"
