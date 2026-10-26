#!/bin/bash
# Install the Tier B (application-integrated) world layer into MantisBT code trees: the template and/or slot copies given as arguments
# (each a .../www directory). Idempotent. Run only while no episode uses the trees being patched.
# Usage: bin/patch_mantis_tierb.sh WWW [WWW ...]
set -eu
R=$(cd "$(dirname "$0")/.." && pwd)
for W in "$@"; do
  cp $R/apps/mantis/world/custom_functions_inc.php $W/config/custom_functions_inc.php
  cp $R/apps/mantis/world/pair_notice.php $W/pair_notice.php
  grep -q "pair_tierb_prepare" $W/bug_update.php || \
    sed -i "s/^\$f_update_type = gpc_get_string( 'action_type', BUG_UPDATE_TYPE_NORMAL );/&\nif( function_exists( 'pair_tierb_prepare' ) ) { pair_tierb_prepare(); }/" $W/bug_update.php
  grep -q "pair_tierb_prepare" $W/bug_update.php || { echo "bug_update.php patch failed in $W"; exit 1; }
  grep -q "pair_tierb_form_fields" $W/bug_update_page.php || \
    sed -i "s/^\(\s*\)<?php echo form_security_field( 'bug_update' ); ?>/&\n\1<?php if( function_exists( 'pair_tierb_form_fields' ) ) echo pair_tierb_form_fields( \$t_bug_id ); ?>/" $W/bug_update_page.php
  grep -q "pair_tierb_form_fields" $W/bug_update_page.php || { echo "bug_update_page.php patch failed in $W"; exit 1; }
  $R/env/php/bin/php -l $W/bug_update.php > /dev/null && $R/env/php/bin/php -l $W/bug_update_page.php > /dev/null
  echo "patched $W"
done
