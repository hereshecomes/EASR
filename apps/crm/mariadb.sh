#!/bin/bash
# Private MariaDB server for the CRM instances (no root). Data lives on local disk under
# $PAIR_RT/mariadb and is rebuilt from fixtures, so it is never evidence.
# Usage: apps/crm/mariadb.sh init|start|stop|status|client [args]
set -eu
R=$(cd "$(dirname "$0")/../.." && pwd)
RT=${PAIR_RT:-/tmp/pair_rebuild_rt}
M=$R/env/mariadb
D=$RT/mariadb
SOCK=$D/mysql.sock
PORT=18400
ARGS=(--no-defaults --basedir=$M --datadir=$D/data --socket=$SOCK --port=$PORT --bind-address=127.0.0.1
      --pid-file=$D/mariadbd.pid --log-error=$D/error.log --innodb-buffer-pool-size=2G
      --innodb-flush-log-at-trx-commit=2 --max-connections=300 --skip-name-resolve
      --character-set-server=utf8mb4 --collation-server=utf8mb4_general_ci --sql-mode=)
case "${1:-status}" in
  init)
    test ! -d $D/data || { echo "data dir exists"; exit 1; }
    mkdir -p $D
    $M/scripts/mariadb-install-db --no-defaults --basedir=$M --datadir=$D/data --auth-root-authentication-method=normal --skip-test-db > $D/init.log 2>&1
    echo initialized ;;
  start)
    setsid nohup $M/bin/mariadbd "${ARGS[@]}" > $D/stdout.log 2>&1 < /dev/null &
    for i in $(seq 1 60); do $M/bin/mariadb-admin --no-defaults --socket=$SOCK -uroot ping > /dev/null 2>&1 && { echo "up after ${i}s"; exit 0; }; sleep 1; done
    echo "did not start"; tail -20 $D/error.log; exit 1 ;;
  stop)
    $M/bin/mariadb-admin --no-defaults --socket=$SOCK -uroot shutdown ;;
  status)
    $M/bin/mariadb-admin --no-defaults --socket=$SOCK -uroot status ;;
  client)
    shift; exec $M/bin/mariadb --no-defaults --socket=$SOCK -uroot "$@" ;;
esac
