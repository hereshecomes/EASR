<?php
// PAIR world layer for MantisBT (no data changes): the world file comes from the PHP server environment (PAIR_WORLD_FILE).
function pair_world() {
    static $w = null;
    if ($w === null) {
        $file = getenv('PAIR_WORLD_FILE');
        $w = ($file && is_readable($file)) ? (json_decode(file_get_contents($file), true) ?: array()) : array();
    }
    return $w;
}
// MantisBT's built-in conflicting-edit check (last_updated) is on as shipped; a world may switch it off.
function pair_native_occ_off() {
    $w = pair_world();
    return isset($w['native_occ']) && $w['native_occ'] === 'off';
}

// ---- application-integrated tier (EASR evaluation, Tier B); world "app_tier" ------------------------------------------------------------
//   field_cas   the edit form carries the issue's stored values at render (pair_load); on update, a field submitted unchanged is not written
//               (removed from the request before bug_update.php reads it), and a changed field is written only if its stored value still
//               equals the value shown at render (field-level compare-and-set); otherwise the update is rejected
//   integrated  field_cas plus semantic predicates (pair_condition: the dependency values the agent saw, posted by the client)
// MantisBT's own whole-issue check (last_updated) is the entity-level reference (native_occ on); these tiers replace it.
function pair_app_tier() {
    $w = pair_world();
    return isset($w['app_tier']) ? $w['app_tier'] : 'none';
}
function pair_tierb_on() {
    return in_array(pair_app_tier(), array('field_cas', 'integrated'), true);
}
function pair_tierb_values($p_bug_id) {
    $b = bug_get($p_bug_id, true);
    $v = array();
    foreach (array('priority', 'severity', 'reproducibility', 'status', 'resolution', 'view_state', 'projection', 'eta', 'category_id', 'handler_id') as $f) {
        $v[$f] = (string)$b->$f;
    }
    foreach (array('summary', 'os', 'os_build', 'platform', 'build', 'version', 'fixed_in_version', 'target_version',
                   'description', 'steps_to_reproduce', 'additional_information') as $f) {
        $v[$f] = (string)$b->$f;
    }
    foreach (custom_field_get_linked_ids($b->project_id) as $t_id) {
        $v['custom_field_' . $t_id] = (string)custom_field_get_value($t_id, $p_bug_id);
    }
    return $v;
}
function pair_tierb_form_fields($p_bug_id) {
    if (!pair_tierb_on()) {
        return '';
    }
    return '<input type="hidden" name="pair_load" value="' . htmlspecialchars(json_encode(pair_tierb_values($p_bug_id))) . '" />';
}
function pair_tierb_norm($v) {
    $s = strtolower(trim(preg_replace('/\s+/', ' ', (string)$v)));
    $n = str_replace(array('$', ','), '', $s);
    return ($n !== '' && is_numeric($n)) ? sprintf('%.2f', (float)$n) : $s;
}
function pair_tierb_display($p_bug_id, $p_field) {
    $b = bug_get($p_bug_id, true);
    $enum = array('priority', 'severity', 'reproducibility', 'status', 'resolution');
    if (in_array($p_field, $enum, true)) {
        return get_enum_element($p_field, $b->$p_field);
    }
    if ($p_field === 'category') {
        return category_full_name($b->category_id, false);
    }
    if ($p_field === 'handler') {
        return $b->handler_id ? user_get_realname($b->handler_id) : '';
    }
    if (in_array($p_field, array('summary', 'description', 'steps_to_reproduce', 'additional_information'), true)) {
        return (string)$b->$p_field;
    }
    $names = array('contract_tier' => 'Contract tier', 'customer_account' => 'Customer account');
    if (isset($names[$p_field])) {
        return (string)custom_field_get_value(custom_field_get_id_from_name($names[$p_field]), $p_bug_id);
    }
    return null;
}
function pair_tierb_reject($p_bug_id, $p_msg) {
    header('Location: view.php?id=' . (int)$p_bug_id . '&pair_rejected=1&pair_msg=' . urlencode($p_msg));
    exit;
}
// called at the top of bug_update.php, before it reads the request
function pair_tierb_prepare() {
    if (!pair_tierb_on() || !isset($_POST['pair_load'], $_POST['bug_id'])) {
        return;
    }
    $t_bug_id = (int)$_POST['bug_id'];
    $t_load = json_decode($_POST['pair_load'], true);
    if (!is_array($t_load)) {
        return;
    }
    $t_cur = pair_tierb_values($t_bug_id);
    $t_parts = array();
    foreach ($t_load as $f => $lv) {
        if (!array_key_exists($f, $_POST)) {
            continue;
        }
        if (pair_tierb_norm($_POST[$f]) === pair_tierb_norm($lv)) {
            unset($_POST[$f], $_REQUEST[$f]);
            if (strpos($f, 'custom_field_') === 0) {
                unset($_POST[$f . '_presence'], $_REQUEST[$f . '_presence']);
            }
            continue;
        }
        if (isset($t_cur[$f]) && pair_tierb_norm($t_cur[$f]) !== pair_tierb_norm($lv)) {
            $t_parts[] = $f . ' ' . $lv . ' -> ' . $t_cur[$f];
        }
    }
    if ($t_parts) {
        pair_tierb_reject($t_bug_id, 'Your changes were not saved: a field you are changing was changed by someone else after you opened the form ('
            . implode('; ', $t_parts) . '). The current issue is shown; review it and make your change again if it is still correct.');
    }
    if (pair_app_tier() === 'integrated' && !empty($_POST['pair_condition'])) {
        $t_conds = json_decode($_POST['pair_condition'], true);
        $t_failed = array();
        foreach ((is_array($t_conds) ? $t_conds : array()) as $c) {
            if (empty($c['record']) || empty($c['field']) || (isset($c['role']) && $c['role'] !== 'dependency')) {
                continue;
            }
            $t_now = pair_tierb_display((int)$c['record'], $c['field']);
            if ($t_now !== null && pair_tierb_norm($t_now) !== pair_tierb_norm($c['value'])) {
                $t_failed[] = 'issue ' . (int)$c['record'] . ' ' . $c['field'] . ' ' . $c['value'] . ' -> ' . $t_now;
            }
        }
        if ($t_failed) {
            pair_tierb_reject($t_bug_id, 'Your changes were not saved because information they depend on changed since you last saw it: '
                . implode('; ', $t_failed) . '. The current page is shown; decide again.');
        }
    }
    // the field-level check replaces the whole-issue last_updated check for this request
    $_POST['last_updated'] = (string)bug_get_field($t_bug_id, 'last_updated');
    $_REQUEST['last_updated'] = $_POST['last_updated'];
}

// ---- update-page breadth (sensitivity supplement 2026-10-22: write-footprint breadth); world "update_fields" ---------------------------
// The standard fields shown on bug_update_page.php, and therefore submitted by its form. bug_update.php keeps the stored value of every
// field the form does not carry, so this list sets how broad a save's write footprint is. Custom fields are shown as configured.
// No frozen world sets it.
$t_pair_w = pair_world();
if (isset($t_pair_w['update_fields']) && is_array($t_pair_w['update_fields'])) {
    config_set_global('bug_update_page_fields', $t_pair_w['update_fields']);
}
unset($t_pair_w);
