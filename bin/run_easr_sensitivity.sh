#!/bin/bash
# EASR sensitivity and mechanism experiments (spec sections 13-14): Qwen and Gemma; timing, intensity, repeated changes (E8), composite tasks (E7).
# Usage: bin/run_easr_sensitivity.sh   (waits until bin/run_easr_ablations.sh has finished)
R=$(cd "$(dirname "$0")/.." && pwd); cd $R; mkdir -p results/easr; source bin/env.sh
LOG=results/easr/run_easr_sensitivity.log
log() { echo "$(date -u +%FT%TZ) $*" >> $LOG; }
stop_server() {
  [ -f runtime/model_server.json ] || return 0
  local pid; pid=$(python3 -c "import json;print(json.load(open('runtime/model_server.json'))['pid'])")
  kill -0 "$pid" 2>/dev/null && { kill -TERM -- -"$pid" 2>/dev/null || kill -TERM "$pid"; }
  for i in $(seq 1 60); do [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader)" ] && { log "GPUs free"; return 0; }; sleep 5; done
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
OPS=$(env/tools/bin/python -c "from pair import easr_eval as E; print(','.join(E.OPS))")
KOPS=$(env/tools/bin/python -c "from pair import easr_eval as E; print(','.join(E.COMPOSITES))")
cohort() {   # dir arms classes ops port served label
  if [ ! -f $1/easr_eval.jsonl ]; then
    [ -d $1 ] || env/tools/bin/python -m pair.easr_eval manifest $1 --arms $2 --classes $3 --ops $4 --no-clean --role easr_sensitivity \
      --endpoint-url http://127.0.0.1:$5/v1/chat/completions --model $6 --actor "$7" >> $LOG 2>&1
    run $1
  fi
}
experiments() {   # name role port served label
  local n=$1 role=$2 port=$3 served=$4 label=$5
  start_server $role $port
  cohort results/easr/sens_timing_intensity_$n B3_refresh_retry,B7_repair_v1,EASR_v2 \
    T_start_untouched,T_post_untouched,T_start_dep,T_post_dep,I3_dep_untouched_same $OPS $port $served "$label"
  cohort results/easr/e8_repeated_$n B3_refresh_retry,B6_full_replan,B7_repair_v1,EASR_v2 \
    E8_dep_flip,E8_untouched_twice,E8_dep_then_same $OPS $port $served "$label"
  [ -d results/easr/e7_composite_$n ] || env/tools/bin/python -m pair.easr_eval manifest results/easr/e7_composite_$n \
    --arms B3_refresh_retry,B6_full_replan,B7_repair_v1,EASR_v2,A8_full_replanning --classes B_dependency,C_untouched,D_both --ops $KOPS \
    --role easr_e7 --endpoint-url http://127.0.0.1:$port/v1/chat/completions --model $served --actor "$label" >> $LOG 2>&1
  [ -f results/easr/e7_composite_$n/easr_eval.jsonl ] || run results/easr/e7_composite_$n
}
while pgrep -u $USER -f run_easr_matrix.sh > /dev/null || pgrep -u $USER -f run_easr_ablations.sh > /dev/null; do sleep 60; done
log "ablations finished; sensitivity and mechanism experiments start"
experiments qwen actor 18500 pair-actor "Qwen3.8-27B-FP8"
experiments gemma actor2 18502 pair-actor2 "gemma-4-12B-it"
stop_server
log "EASR sensitivity and mechanism experiments done"
