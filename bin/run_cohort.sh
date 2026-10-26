#!/bin/bash
# Start N cohort workers (slots OFFSET..OFFSET+N-1; OFFSET defaults to 0) in the background for one frozen cohort directory.
# Usage: bin/run_cohort.sh results/<cohort> N [OFFSET]
set -eu
R=$(cd "$(dirname "$0")/.." && pwd)
. $R/bin/env.sh
C=$R/$1; N=$2; OFF=${3:-0}
test -f $C/manifest.json
test ! -f $C/launch.json || { echo "cohort already launched: $(cat $C/launch.json)"; exit 1; }
PIDS=()
for i in $(seq 0 $((N-1))); do
  setsid nohup $R/env/tools/bin/python -m pair.worker $C $((OFF+i)) > $C/worker_$((OFF+i)).log 2>&1 < /dev/null &
  PIDS+=($!)
done
python3 -c "import json,sys,time;json.dump({'workers':int(sys.argv[1]),'pids':[int(p) for p in sys.argv[2:]],'started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())},open('$C/launch.json','w'))" $N "${PIDS[@]}"
cat $C/launch.json
