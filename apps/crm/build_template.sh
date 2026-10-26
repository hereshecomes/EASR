#!/bin/bash
# Build the SuiteCRM template from scratch: silent install (apps/crm/config_si.php), fixture seed (apps/crm/seed.php),
# PAIR custom code (apps/crm/custom), then freeze the database to $PAIR_RT/crm_template.sql.
set -eu
R=$(cd "$(dirname "$0")/../.." && pwd); cd $R; . bin/env.sh
W=$PAIR_RT/crm_template/www
rm -rf $PAIR_RT/crm_template; mkdir -p $PAIR_RT/crm_template/sessions
cp -a env/suitecrm-src/SuiteCRM-7.14.9 $W
cp apps/crm/config_si.php $W/config_si.php
sed "s#__DIR__#$PAIR_RT/crm_template#" apps/crm/php.ini.tmpl | sed 's/opcache.validate_timestamps = 0/opcache.validate_timestamps = 1/' > $PAIR_RT/crm_template/php.ini
cd $W
# The silent installer runs over HTTP, as a browser request would (CLI invocation is rejected by its input checks).
setsid $R/env/php/bin/php -c $PAIR_RT/crm_template/php.ini -S 127.0.0.1:18420 -t $W > $PAIR_RT/crm_template/install_server.log 2>&1 < /dev/null &
SRV=$!
for i in $(seq 1 30); do curl -s -m 2 -o /dev/null http://127.0.0.1:18420/ && break; sleep 1; done
curl -s -m 1200 "http://127.0.0.1:18420/install.php?goto=SilentInstall&cli=true" > $PAIR_RT/crm_template/install.log 2>&1 || true
kill -TERM -- -$SRV 2>/dev/null || kill $SRV
grep -q "installer_locked.*true" config.php || { echo "silent install failed"; tail -30 $PAIR_RT/crm_template/install.log; exit 1; }
cp $R/apps/crm/seed.php $W/pair_seed.php
$R/env/php/bin/php -c $PAIR_RT/crm_template/php.ini pair_seed.php $R/tasks/fixtures/crm_records.json > $PAIR_RT/crm_template/seed.log 2>&1
rm -f $W/pair_seed.php
cp -a $R/apps/crm/custom/. $W/custom/
cd $R; env/tools/bin/python -c "import pair.apps.crm as c; print(c.freeze_template())"
