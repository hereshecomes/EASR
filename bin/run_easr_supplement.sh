#!/bin/bash
# Sensitivity supplement (2026-10-22): write breadth (MantisBT update-page field sets), task length (6 and 8 subtasks) and a breadth x
# intensity x length interaction; Qwen, then Gemma. Design: pair/easr_supplement.py. Checks: tests/easr/breadth_probe.py.
# Usage: bin/run_easr_supplement.sh   (waits until every other EASR pipeline has finished)
R=$(cd "$(dirname "$0")/.." && pwd); cd $R; mkdir -p results/easr; source bin/env.sh
LOG=results/easr/run_easr_supplement.log
log() { echo "$(date -u +%FT%TZ) $*" >> $LOG; }
stop_server() {
  [ -f runtime/model_server.json ] || return 0
  local pid; pid=$(python3 -c "import json;print(json.load(open('runtime/model_server.json'))['pid'])")
  kill -0 "$pid" 2>/dev/null && { kill -TERM -- -"$pid" 2>/dev/null || kill -TERM "$pid"; }
  for i in $(seq 1 60); do [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader)" ] && ! kill -0 "$pid" 2>/dev/null && { log "GPUs free"; return 0; }; sleep 5; done
  log "GPU still busy"; exit 1
}
start_server() {
  if [ -f runtime/model_server.json ] && python3 -c "import json,sys; d=json.load(open('runtime/model_server.json')); sys.exit(0 if d['role']=='$1' else 1)" \
     && curl -s -m 5 localhost:$2/v1/models | grep -q '"id"'; then log "$1 already serving"; return 0; fi
  stop_server; bin/serve_model.sh $1 >> $LOG 2>&1 || exit 1
  for i in $(seq 1 120); do curl -s -m 5 localhost:$2/v1/models | grep -q '"id"' && { log "$1 ready"; return 0; }; sleep 10; done; log "$1 not ready"; exit 1; }
run() {   # cohort dir
  bin/setup_runtime.sh >> $LOG 2>&1 || exit 1; bin/start_breakingweb.sh >> $LOG 2>&1 || exit 1
  log "start $1"; bin/run_cohort.sh $1 10 >> $LOG 2>&1 || exit 1; sleep 5
  while pgrep -u $USER -f "pair.worker $R/$1 " > /dev/null || pgrep -u $USER -f "pair.worker $1 " > /dev/null; do sleep 20; done
  log "finished $1 ($(wc -l < $1/index.jsonl) rows)"
  env/tools/bin/python -m pair.easr_eval evaluate $1 >> $LOG 2>&1
  python3 -c "import json,sys; d=json.load(open('$1/claims.json')); sys.exit(1 if d.get('stopped') else 0)" || { log "cohort $1 stopped early; pipeline halted"; exit 1; }
}
while pgrep -u $USER -f "run_easr_(matrix|matrix_rest|ablations|sensitivity|tierb|extras|reruns|infra_reruns|infra_reruns_resume).sh" > /dev/null; do sleep 60; done
log "sensitivity supplement start"
supp() {   # name role port served label
  local name=$1 role=$2 port=$3 served=$4 label=$5
  start_server $role $port
  for k in breadth length6 length8 interaction; do
    local C=results/easr/sens_${k}_$name
    [ -f $C/easr_eval.jsonl ] && continue
    [ -d $C ] || env/tools/bin/python -m pair.easr_supplement $C $k --endpoint-url http://127.0.0.1:$port/v1/chat/completions \
      --model $served --actor "$label" >> $LOG 2>&1
    run $C
  done
}
supp qwen actor 18500 pair-actor Qwen3.8-27B-FP8
supp gemma actor2 18502 pair-actor2 gemma-4-12B-it
stop_server
log "sensitivity supplement done"
