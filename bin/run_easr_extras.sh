#!/bin/bash
# Extra EASR experiments: server-side faults (misleading success feedback, partial commit; BreakingWeb-derived; spec 11.6, E5) and the
# contract-quality study (spec 9.3). Qwen and Gemma.
# Usage: bin/run_easr_extras.sh   (waits until every other EASR and testing pipeline has finished)
R=$(cd "$(dirname "$0")/.." && pwd); cd $R; mkdir -p results/easr; source bin/env.sh
LOG=results/easr/run_easr_extras.log
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
BWOPS=$(env/tools/bin/python -c "from pair import easr_eval as E; print(','.join(k for k, o in E.OPS.items() if o['app'] == 'breakingweb'))")
while pgrep -u $USER -f "run_easr_(matrix|ablations|sensitivity|tierb).sh|run_testing_gemma_blocks.sh" > /dev/null; do sleep 60; done
log "extras start: restart the BreakingWeb backend with the fault layer (no session is active now)"
pkill -f "uvicorn pair_bw_app:app --host 127.0.0.1 --port 18700" ; sleep 3
bin/start_breakingweb.sh >> $LOG 2>&1 || exit 1
extras() {   # name role port served label
  local n=$1 role=$2 port=$3 served=$4 label=$5
  start_server $role $port
  local EP="--endpoint-url http://127.0.0.1:$port/v1/chat/completions --model $served --actor $label"
  if [ ! -f results/easr/faults_$n/easr_eval.jsonl ]; then
    [ -d results/easr/faults_$n ] || env/tools/bin/python -m pair.easr_eval manifest results/easr/faults_$n --no-clean --classes F_silent_fail,F_partial_commit \
      --arms B0_vanilla,B1_prompted_verify,B4_postcondition,B7_repair_v1,EASR_v2,A6_no_postcommit --ops $BWOPS --role easr_faults $EP >> $LOG 2>&1
    run results/easr/faults_$n
  fi
  if [ ! -f results/easr/contract_$n/easr_eval.jsonl ]; then
    [ -d results/easr/contract_$n ] || env/tools/bin/python -m pair.easr_eval manifest results/easr/contract_$n --classes B_dependency,C_untouched,D_both \
      --arms CQ_omit10,CQ_omit25,CQ_omit50,CQ_omit_targeted,CQ_extra_dep,CQ_missing_dep --ops $OPS --role easr_contract $EP >> $LOG 2>&1
    run results/easr/contract_$n
  fi
}
extras qwen actor 18500 pair-actor Qwen3.8-27B-FP8
extras gemma actor2 18502 pair-actor2 gemma-4-12B-it
stop_server
log "extras done"
