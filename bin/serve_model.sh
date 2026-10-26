#!/bin/bash
# Start one model server on both GPUs (one model at a time).
# Usage: MODELS_DIR=/path/to/checkpoints bin/serve_model.sh actor|actor2|granite|phi4|mistral
# Refuses to start if any GPU process exists or a server receipt is still live.
set -eu
R=$(cd "$(dirname "$0")/.." && pwd)
MODELS=${MODELS_DIR:?set MODELS_DIR to the folder that holds one sub-folder per checkpoint (see README)}
ROLE=${1:?actor, actor2, granite, phi4 or mistral}
RECEIPT=$R/runtime/model_server.json
mkdir -p $R/runtime $R/env/logs
if [ -f $RECEIPT ] && kill -0 "$(python3 -c "import json;print(json.load(open('$RECEIPT'))['pid'])")" 2>/dev/null; then
  echo "a model server from this checkout is still running: $(cat $RECEIPT)"; exit 1; fi
if [ -n "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader)" ]; then
  echo "GPU busy:"; nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv; exit 1; fi
case $ROLE in
  actor) MODEL=$MODELS/Qwen3.8-27B-FP8; NAME=pair-actor; PORT=18500
         EXTRA=(--max-model-len 32768 --max-num-seqs 16 --language-model-only) ;;
  actor2) MODEL=$MODELS/gemma-4-12B-it; NAME=pair-actor2; PORT=18502
         # Second actor for the method-selection pilot (same budgets as the first actor).
         EXTRA=(--max-model-len 32768 --max-num-seqs 16 --language-model-only) ;;
  # EASR evaluation agent families (Llama slot substituted by Granite)
  granite) MODEL=$MODELS/granite-3.3-8b-instruct; NAME=pair-granite; PORT=18503
         EXTRA=(--max-model-len 32768 --max-num-seqs 16) ;;
  phi4) MODEL=$MODELS/phi-4; NAME=pair-phi4; PORT=18504
         EXTRA=(--max-model-len 16384 --max-num-seqs 16) ;;          # the model's own context limit
  mistral) MODEL=$MODELS/Mistral-Nemo-Instruct-2407; NAME=pair-mistral; PORT=18505
         EXTRA=(--max-model-len 32768 --max-num-seqs 16) ;;
  *) echo "unknown role $ROLE"; exit 1 ;;
esac
CMD=($R/env/vllm/bin/vllm serve $MODEL --served-model-name $NAME --host 127.0.0.1 --port $PORT
     --tensor-parallel-size 2 --gpu-memory-utilization 0.90 --enable-prefix-caching --generation-config vllm
     --structured-outputs-config '{"backend":"xgrammar"}' "${EXTRA[@]}")
LOG=$R/env/logs/serve_${ROLE}_$(date -u +%Y%m%dT%H%M%SZ).log
CUDA_VISIBLE_DEVICES=0,1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=8 setsid nohup "${CMD[@]}" > $LOG 2>&1 < /dev/null &
PID=$!
python3 - "$RECEIPT" "$PID" "$ROLE" "$NAME" "$PORT" "$LOG" "${CMD[*]}" <<'PY'
import json, sys, time
path, pid, role, name, port, log, cmd = sys.argv[1:]
json.dump({"pid": int(pid), "role": role, "served_model_name": name, "endpoint": f"http://127.0.0.1:{port}/v1/chat/completions",
           "log": log, "cmd": cmd, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, open(path, "w"), indent=1)
PY
echo "started $ROLE pid $PID log $LOG"
