#!/bin/bash
# Runs many stark_direwolf vs tabula_v12 seasons sequentially, appending
# one JSON line per season to the results file. Alternates which seat each
# agent plays (p1/p2) across seeds to cancel out any first-seat advantage.
set -uo pipefail
cd /Users/mschaeffer/workspace/sea-of-colours-hackathon
export SNOWFLAKE_DEFAULT_CONNECTION_NAME=sea_of_colours_game
export SOC_BACKEND=file

SEEDS=(101 102 103 104 105 106 107 108 109 110 111 112 113 114 115 116 117 118 119 120)
OUT="reports/seasons/val_vs_v12_results.jsonl"
: > "$OUT"

i=0
for seed in "${SEEDS[@]}"; do
  i=$((i+1))
  if (( i % 2 == 0 )); then
    p1=tabula_v12
    p2=stark_direwolf
  else
    p1=stark_direwolf
    p2=tabula_v12
  fi
  name="val_vs_v12_s${seed}"
  echo "=== [$i/${#SEEDS[@]}] seed=$seed p1=$p1 p2=$p2 ===" >&2
  python3 scripts/soc.py season --p1 "$p1" --p2 "$p2" --seed "$seed" \
    --name "$name" --quiet --json > "$OUT.tmp" 2> "reports/seasons/val_vs_v12_seed${seed}.log"
  python3 scripts/_val_extract_result.py "$OUT.tmp" "$OUT" "$seed" "$p1" "$p2"
  rm -f "$OUT.tmp"
done
echo "DONE all seeds" >&2
