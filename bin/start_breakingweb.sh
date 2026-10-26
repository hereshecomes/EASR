#!/bin/bash
# Start the BreakingWeb backend (suite D3) with the evaluator-only state endpoint on 127.0.0.1:18700 (idempotent).
R=$(cd "$(dirname "$0")/.." && pwd)
X=${BREAKINGWEB_DIR:-$R/external/BreakingWeb}     # BreakingWeb checkout with its .venv
if curl -s -m 3 -o /dev/null -w "%{http_code}" http://127.0.0.1:18700/docs | grep -q 200; then echo "breakingweb: already up"; exit 0; fi
mkdir -p $R/env/logs
cd $X && nohup env PYTHONPATH=$X:$R/apps/breakingweb PAIR_EVAL_SECRET=${PAIR_EVAL_SECRET:-pair-eval-local} BREAKINGWEB_CONTROLLER_SECRET=pair-controller-local \
  BREAKINGWEB_AUTO_BUILD_FRONTENDS=0 setsid .venv/bin/python -m uvicorn pair_bw_app:app --host 127.0.0.1 --port 18700 --log-level warning < /dev/null \
  > $R/env/logs/breakingweb_$(date -u +%Y%m%dT%H%M%SZ).log 2>&1 &
for i in $(seq 1 60); do curl -s -m 2 -o /dev/null -w "%{http_code}" http://127.0.0.1:18700/docs | grep -q 200 && { echo "breakingweb: ready"; exit 0; }; sleep 1; done
echo "breakingweb: did not come up"; exit 1
