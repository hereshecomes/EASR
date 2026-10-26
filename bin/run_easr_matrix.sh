#!/bin/bash
# EASR model-based evaluation (main matrix, paper Section 4): one agent at a time on both GPUs; per new agent a clean validation cohort first.
# Usage: bin/run_easr_matrix.sh [agent ...]   agents: qwen gemma granite mistral phi4 (default: all, in this order)
R=$(cd "$(dirname "$0")/.." && pwd); cd $R; mkdir -p results/easr; source bin/env.sh
LOG=results/easr/run_easr_matrix.log
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
ARMS=B0_vanilla,B1_prompted_verify,B2_preaction_verify,B3_refresh_retry,B4_postcondition,B5_dependency_only,B6_full_replan,B7_repair_v1,EASR_v2
CLASSES=A_convergent,B_dependency,C_untouched,C_same_field,D_both,B_race,C_race
agent() {   # name role port served actor_label [context_window] [validate]
  local name=$1 role=$2 port=$3 served=$4 label=$5 ctx=$6 validate=$7
  local extra=""; [ -n "$ctx" ] && extra="--context-window $ctx"
  start_server $role $port
  if [ "$validate" = "1" ] && [ ! -f results/easr/validate_$name/easr_eval.jsonl ]; then
    env/tools/bin/python -m pair.easr_eval manifest results/easr/validate_$name --arms B0_vanilla,EASR_v2 --classes none --ops $OPS --role easr_eval_validation \
      --endpoint-url http://127.0.0.1:$port/v1/chat/completions --model $served --actor "$label" $extra >> $LOG 2>&1
    run results/easr/validate_$name
  fi
  if [ ! -f results/easr/main_$name/easr_eval.jsonl ]; then
    [ -d results/easr/main_$name ] || env/tools/bin/python -m pair.easr_eval manifest results/easr/main_$name --arms $ARMS --classes $CLASSES --ops $OPS \
      --role easr_eval_main --endpoint-url http://127.0.0.1:$port/v1/chat/completions --model $served --actor "$label" $extra >> $LOG 2>&1
    run results/easr/main_$name
  fi
}
for a in ${@:-qwen gemma granite mistral phi4}; do
  case $a in
    qwen)    agent qwen actor 18500 pair-actor "Qwen3.8-27B-FP8" "" 0 ;;
    gemma)   agent gemma actor2 18502 pair-actor2 "gemma-4-12B-it" "" 1 ;;
    granite) agent granite granite 18503 pair-granite "granite-3.3-8b-instruct" "" 1 ;;
    mistral) agent mistral mistral 18505 pair-mistral "Mistral-Nemo-Instruct-2407" "" 1 ;;
    phi4)    agent phi4 phi4 18504 pair-phi4 "phi-4" 16384 1 ;;
  esac
done
stop_server
log "EASR matrix done: $*"
