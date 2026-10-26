#!/bin/bash
# Build the MantisBT 2.28.4 template (D4, issue tracker): HTTP install into MariaDB, PAIR world switch, fixture seed, freeze to SQL.
set -eu
R=$(cd "$(dirname "$0")/../.." && pwd); cd $R; . bin/env.sh
SRC=${MANTIS_SRC:-$R/external/mantisbt-2.28.4}
T=$PAIR_RT/mantis_template; W=$T/www
rm -rf $T; mkdir -p $T/sessions; cp -a $SRC $W
sed "s#__DIR__#$T#" apps/crm/php.ini.tmpl | sed 's/opcache.validate_timestamps = 0/opcache.validate_timestamps = 1/' > $T/php.ini
apps/crm/mariadb.sh client -e "DROP DATABASE IF EXISTS pair_mantis_template; CREATE DATABASE pair_mantis_template CHARACTER SET utf8mb4;"
PHP_CLI_SERVER_WORKERS=4 setsid $R/env/php/bin/php -c $T/php.ini -S 127.0.0.1:18490 -t $W > $T/install_server.log 2>&1 < /dev/null &
SRV=$!
for i in $(seq 1 30); do curl -s -m 2 -o /dev/null http://127.0.0.1:18490/admin/install.php && break; sleep 1; done
curl -s -m 600 -X POST http://127.0.0.1:18490/admin/install.php \
  --data-urlencode "install=2" --data-urlencode "db_type=mysqli" --data-urlencode "hostname=127.0.0.1:18400" \
  --data-urlencode "db_username=pair" --data-urlencode "db_password=pair-test-db-2026" --data-urlencode "database_name=pair_mantis_template" \
  --data-urlencode "admin_username=pair" --data-urlencode "admin_password=pair-test-db-2026" --data-urlencode "db_exists=1" \
  --data-urlencode "timezone=UTC" --data-urlencode "path=http://127.0.0.1:18490/" --data-urlencode "go=Install/Upgrade Database" > $T/install.log 2>&1 || true
kill -TERM -- -$SRV 2>/dev/null || kill $SRV
test -f $W/config/config_inc.php || { echo "install failed"; grep -o "BAD\|ERROR[^<]*" $T/install.log | head -5; exit 1; }
echo installed
# PAIR world layer and settings; the native conflict check becomes switchable per world.
cp apps/mantis/world/custom_functions_inc.php $W/config/custom_functions_inc.php
cp apps/mantis/world/pair_notice.php $W/pair_notice.php
sed -i 's/^if( \$t_existing_bug->last_updated != \$t_updated_bug->last_updated ) {/if( $t_existing_bug->last_updated != $t_updated_bug->last_updated \&\& !pair_native_occ_off() ) {/' $W/bug_update.php
grep -q "pair_native_occ_off" $W/bug_update.php || { echo "conflict-check patch failed"; exit 1; }
cat >> $W/config/config_inc.php <<'CFG'
$g_path = getenv('PAIR_MANTIS_URL') ?: 'http://127.0.0.1:18490/';
$g_top_include_page = '%absolute_path%pair_notice.php';
$g_allow_signup = OFF; $g_lost_password_feature = OFF; $g_enable_email_notification = OFF; $g_signup_use_captcha = OFF;
$g_default_timezone = 'UTC'; $g_show_realname = ON; $g_log_level = LOG_NONE; $g_display_errors = array(E_USER_ERROR => DISPLAY_ERROR_HALT);
CFG
bin/patch_mantis_tierb.sh $W            # Tier B world layer (no-op unless a world sets app_tier)
cp apps/mantis/seed.php $W/pair_seed.php
( cd $W && $R/env/php/bin/php -c $T/php.ini -d display_errors=stderr -d error_reporting=E_ALL pair_seed.php $R/tasks/fixtures/mantis_records.json > $T/seed.json 2> $T/seed.err ) || { cat $T/seed.err | tail -20; exit 1; }
rm -f $W/pair_seed.php; cat $T/seed.json
$R/env/mariadb/bin/mariadb-dump --no-defaults --socket=$PAIR_RT/mariadb/mysql.sock -uroot --single-transaction --skip-extended-insert pair_mantis_template > $PAIR_RT/mantis_template.sql
echo "frozen $(wc -c < $PAIR_RT/mantis_template.sql) bytes"
