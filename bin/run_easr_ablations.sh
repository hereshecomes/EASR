#!/bin/bash
# EASR ablations (spec section 9): Qwen and Gemma, variants A1-A9 on every intervention task (A0 = EASR_v2 of the main cohorts).
# Usage: bin/run_easr_ablations.sh   (waits until bin/run_easr_matrix.sh has finished)
R=$(cd "$(dirname "$0")/.." && pwd); cd $R; mkdir -p results/easr; source bin/env.sh
LOG=results/easr/run_easr_ablations.log
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
ARMS=A1_no_dependencies,A2_intended_only,A3_dom_only,A4_syntactic_overlap,A5_hold_instead_of_rebase,A6_no_postcommit,A7_postcommit_on_signal,A8_full_replanning,A9_global_freshness
CLASSES=A_convergent,B_dependency,C_untouched,C_same_field,D_both,B_race,C_race
ablate() {   # name role port served actor_label
  local name=$1 role=$2 port=$3 served=$4 label=$5
  start_server $role $port
  if [ ! -f results/easr/ablation_$name/easr_eval.jsonl ]; then
    [ -d results/easr/ablation_$name ] || env/tools/bin/python -m pair.easr_eval manifest results/easr/ablation_$name --arms $ARMS --classes $CLASSES --ops $OPS \
      --role easr_ablation --no-clean --endpoint-url http://127.0.0.1:$port/v1/chat/completions --model $served --actor "$label" >> $LOG 2>&1
    run results/easr/ablation_$name
  fi
}
while pgrep -u $USER -f run_easr_matrix.sh > /dev/null; do sleep 60; done
log "matrix pipeline finished; ablations start"
ablate gemma actor2 18502 pair-actor2 "gemma-4-12B-it"
ablate qwen actor 18500 pair-actor "Qwen3.8-27B-FP8"
stop_server
log "EASR ablations done"
