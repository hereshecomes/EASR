#!/bin/bash
# Restore the local runtime of the PAIR applications. The container's local disk ($PAIR_RT) does not survive a
# container restart; templates are cached on persistent storage ($R/env/runtime_cache) and rebuilt from the
# committed recipes when the cache is missing. Idempotent; safe to run before every cohort.
set -eu
R=$(cd "$(dirname "$0")/.." && pwd); cd $R; . bin/env.sh
CACHE=$R/env/runtime_cache; mkdir -p $CACHE $PAIR_RT
log() { echo "$(date -u +%FT%TZ) setup_runtime: $*"; }
# 1. MariaDB (CRM, issue tracker)
if ! apps/crm/mariadb.sh status > /dev/null 2>&1; then
  [ -d $PAIR_RT/mariadb/data ] || apps/crm/mariadb.sh init
  apps/crm/mariadb.sh start
  apps/crm/mariadb.sh client -e "CREATE USER IF NOT EXISTS 'pair'@'127.0.0.1' IDENTIFIED BY 'pair-test-db-2026'; GRANT ALL ON *.* TO 'pair'@'127.0.0.1'; FLUSH PRIVILEGES;"
  log "mariadb up"
fi
# 2. Shop template
if [ ! -f $PAIR_RT/oscar_template.sqlite3 ]; then
  if [ -f $CACHE/oscar_template.sqlite3 ]; then cp $CACHE/oscar_template.sqlite3 $PAIR_RT/; log "shop template restored"
  else env/oscar/bin/python apps/oscar/build_template.py $PAIR_RT/oscar_template.sqlite3 tasks/fixtures/oscar_catalogue.json
       cp $PAIR_RT/oscar_template.sqlite3 $CACHE/; log "shop template built"; fi
fi
# 3. CRM template (SuiteCRM 7.14.9 install + seed + PAIR custom code, frozen as SQL)
if [ ! -f $PAIR_RT/crm_template.sql ] || [ ! -d $PAIR_RT/crm_template/www ]; then
  if [ -f $CACHE/crm_template.tar ]; then tar -xf $CACHE/crm_template.tar -C $PAIR_RT; log "crm template restored"
  else apps/crm/build_template.sh; tar -cf $CACHE/crm_template.tar -C $PAIR_RT crm_template crm_template.sql; log "crm template built"; fi
fi
if ! apps/crm/mariadb.sh client -e "SELECT 1 FROM pair_crm_template.users LIMIT 1" > /dev/null 2>&1; then
  apps/crm/mariadb.sh client -e "CREATE DATABASE IF NOT EXISTS pair_crm_template"
  apps/crm/mariadb.sh client pair_crm_template < $PAIR_RT/crm_template.sql; log "crm template database loaded"
fi
# 4. Issue tracker template (MantisBT 2.28.4 install + PAIR world layer + seed, frozen as SQL)
if [ ! -f $PAIR_RT/mantis_template.sql ] || [ ! -d $PAIR_RT/mantis_template/www ]; then
  if [ -f $CACHE/mantis_template.tar ]; then tar -xf $CACHE/mantis_template.tar -C $PAIR_RT; log "mantis template restored"
  else apps/mantis/build_template.sh; tar -cf $CACHE/mantis_template.tar -C $PAIR_RT mantis_template/www mantis_template.sql; log "mantis template built"; fi
fi
log "ready"
