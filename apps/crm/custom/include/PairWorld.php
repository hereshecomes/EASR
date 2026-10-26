<?php
// PAIR world layer. The world file path comes from the PHP server's environment (PAIR_WORLD_FILE).
//  - notice banner after <body> on every full page (changes no data)
//  - native_occ: SuiteCRM's built-in optimistic lock on (as shipped) or off
//  - app_tier: application-integrated conditional commit for the repair study
//      none            no check (default)
//      record_version  edit forms carry the record's date_modified at render; Save is rejected if it changed
//      conditional     Save is rejected if any condition posted with the form (pair_condition) no longer holds
//      field_cas       edit forms carry the record's stored field values at render (pair_load); on Save, a field submitted unchanged is
//                      not written (the stored value is kept), and a changed field is written only if its stored value still equals the
//                      value shown at render (field-level compare-and-set); otherwise the Save is rejected
//      integrated      field_cas plus the semantic predicates of conditional (pair_condition, the dependency values the agent saw)
//    A rejected Save writes nothing and redirects to the record's detail view with a message.
//    The check runs in the request that writes, after the harness's last external write (the harness writes
//    sequentially between requests), so no external change can fall between check and write.
class PairWorld
{
    private static function world()
    {
        static $world = null;
        if ($world === null) {
            $file = getenv('PAIR_WORLD_FILE');
            $world = ($file && is_readable($file)) ? (json_decode(file_get_contents($file), true) ?: array()) : array();
        }
        return $world;
    }

    public function sessionStarted($event, $arguments)
    {
        $world = self::world();
        if (isset($world['native_occ']) && $world['native_occ'] === 'off') {
            $_SESSION['o_lock_fs'] = true;
        }
    }

    public function register($event, $arguments)
    {
        static $registered = false;
        if ($registered || PHP_SAPI === 'cli') {
            return;
        }
        $registered = true;
        $world = self::world();
        $html = '';
        if (!empty($world['notice'])) {
            $html .= '<div class="pair-notice alert alert-info" role="status" style="margin:0;padding:8px 16px;font-size:14px;">'
                . '<strong>' . htmlspecialchars($world['notice']['title']) . '</strong> '
                . htmlspecialchars($world['notice']['text']) . '</div>';
        }
        if (!empty($_GET['pair_rejected']) && isset($_GET['pair_msg'])) {
            $html .= '<div class="pair-reject alert alert-warning" role="alert" style="margin:0;padding:8px 16px;font-size:14px;">'
                . htmlspecialchars($_GET['pair_msg']) . '</div>';
        }
        $version = null;
        $tier = isset($world['app_tier']) ? $world['app_tier'] : 'none';
        if ($tier === 'record_version' && isset($_REQUEST['action'], $_REQUEST['module'], $_REQUEST['record'])
            && $_REQUEST['action'] === 'EditView' && $_REQUEST['record'] !== '') {
            $bean = BeanFactory::newBean($_REQUEST['module']);
            if ($bean) {
                $db = DBManagerFactory::getInstance();
                $version = $db->getOne('SELECT date_modified FROM ' . $bean->table_name . ' WHERE id=' . $db->quoted($_REQUEST['record']));
            }
        }
        $load = null;
        if (in_array($tier, array('field_cas', 'integrated'), true) && isset($_REQUEST['action'], $_REQUEST['module'], $_REQUEST['record'])
            && $_REQUEST['action'] === 'EditView' && $_REQUEST['record'] !== '') {
            $load = self::loadValues($_REQUEST['module'], $_REQUEST['record']);
        }
        if ($html === '' && $version === null && $load === null) {
            return;
        }
        ob_start(function ($buffer) use ($html, $version, $load) {
            if ($html !== '') {
                $buffer = preg_replace_callback('/<body[^>]*>/i', function ($m) use ($html) {
                    return $m[0] . $html;
                }, $buffer, 1);
            }
            if (($version !== null && $version !== false) || $load !== null) {
                $extra = '';
                if ($version !== null && $version !== false) {
                    $extra .= '<input type="hidden" name="pair_version" value="' . htmlspecialchars($version) . '">';
                }
                if ($load !== null) {
                    $extra .= '<input type="hidden" name="pair_load" value="' . htmlspecialchars(json_encode($load)) . '">';
                }
                $inject = function ($html) use ($extra) {
                    return preg_replace_callback('/<form[^>]*id=["\']EditView["\'][^>]*>/i', function ($m) use ($extra) {
                        return $m[0] . $extra;
                    }, $html, 1);
                };
                // The AJAX UI (ajax_load=1) returns the page as JSON with the HTML in "content".
                $json = (strlen($buffer) && $buffer[0] === '{') ? json_decode($buffer, true) : null;
                if (is_array($json) && isset($json['content']) && is_string($json['content'])) {
                    $json['content'] = $inject($json['content']);
                    $buffer = json_encode($json);
                } else {
                    $buffer = $inject($buffer);
                }
            }
            return $buffer;
        });
    }

    // ---- application-integrated conditional commit -------------------------------------------------
    private static function norm($field, $v)
    {
        $s = strtolower(trim(preg_replace('/\s+/', ' ', html_entity_decode((string)$v, ENT_QUOTES))));
        $n = str_replace(array('$', ','), '', $s);
        if ($n !== '' && is_numeric($n)) {
            return sprintf('%.2f', (float)$n);
        }
        if (preg_match('#^(\d{1,2})/(\d{1,2})/(\d{4})$#', $s, $m)) {
            return sprintf('%04d-%02d-%02d', $m[3], $m[1], $m[2]);
        }
        return $s;
    }

    private static function show($v)
    {
        $s = trim(preg_replace('/\s+/', ' ', html_entity_decode((string)$v, ENT_QUOTES)));
        return $s === '' ? '(empty)' : $s;
    }

    private static function reject($module, $record, $msg)
    {
        $GLOBALS['log']->fatal('PAIR conditional commit rejected: ' . $msg);
        SugarApplication::redirect('index.php?module=' . urlencode($module) . '&action=DetailView&record=' . urlencode($record)
            . '&pair_rejected=1&pair_msg=' . urlencode($msg));
    }

    // field_cas: the stored values of the record's database fields at render (and its account link, kept in a relationship table)
    private static function loadValues($module, $record)
    {
        $bean = BeanFactory::newBean($module);
        if (!$bean || !$bean->retrieve($record) || !is_array($bean->fetched_row)) {
            return null;
        }
        $out = array();
        foreach ($bean->fetched_row as $f => $v) {
            if (in_array($f, array('id', 'date_entered', 'date_modified', 'modified_user_id', 'created_by', 'deleted'), true)) {
                continue;
            }
            $out[$f] = $v;
        }
        if (isset($bean->account_id) && !isset($out['account_id'])) {
            $out['account_id'] = $bean->account_id;
        }
        return $out;
    }

    private static function currentAccount($bean)
    {
        $db = DBManagerFactory::getInstance();
        if ($bean->module_dir === 'Contacts') {
            return $db->getOne('SELECT account_id FROM accounts_contacts WHERE deleted=0 AND contact_id=' . $db->quoted($bean->id));
        }
        if ($bean->module_dir === 'Opportunities') {
            return $db->getOne('SELECT account_id FROM accounts_opportunities WHERE deleted=0 AND opportunity_id=' . $db->quoted($bean->id));
        }
        return null;
    }

    private static function fieldCas($bean)
    {
        $load = json_decode(html_entity_decode($_POST['pair_load'], ENT_QUOTES), true);
        if (!is_array($load)) {
            return;
        }
        $parts = array();
        foreach ($load as $f => $lv) {
            if (!array_key_exists($f, $_POST)) {
                continue;
            }
            if ($f === 'account_id') {
                // the account link lives in a relationship table that the Save flow rewrites from the request before this hook runs;
                // the value read when this Save loaded the record is the stored value
                $current = isset($bean->rel_fields_before_value['account_id']) ? $bean->rel_fields_before_value['account_id'] : self::currentAccount($bean);
            } else {
                $current = is_array($bean->fetched_row) && array_key_exists($f, $bean->fetched_row) ? $bean->fetched_row[$f] : null;
            }
            if (self::norm($f, $_POST[$f]) === self::norm($f, $lv)) {
                // submitted unchanged: keep the stored value (another user's later change is not overwritten)
                if ($f === 'account_id') {
                    $linked = self::currentAccount($bean);
                    if ($current && $linked !== $current && $bean->load_relationship('accounts')) {
                        if ($linked) {
                            $bean->accounts->delete($bean->id, $linked);
                        }
                        $bean->accounts->add($current);
                    }
                    $bean->account_id = $current;
                } elseif ($current !== null || (is_array($bean->fetched_row) && array_key_exists($f, $bean->fetched_row))) {
                    $bean->$f = $current;
                }
                continue;
            }
            if (self::norm($f, $current) !== self::norm($f, $lv)) {
                $parts[] = $f . ' ' . self::show($lv) . ' -> ' . self::show($current);
            }
        }
        if ($parts) {
            self::reject($bean->module_dir, $bean->id, 'Your changes were not saved: a field you are changing was changed by someone else after you '
                . 'opened the form (' . implode('; ', $parts) . '). The current record is shown; review it and make your change again if it is still correct.');
        }
    }

    public function beforeSave($bean, $event, $arguments)
    {
        if (PHP_SAPI === 'cli' || !isset($_REQUEST['action']) || $_REQUEST['action'] !== 'Save'
            || empty($_POST['record']) || $bean->id !== $_POST['record']) {
            return;
        }
        $world = self::world();
        $tier = isset($world['app_tier']) ? $world['app_tier'] : 'none';
        if ($tier === 'record_version' && isset($_POST['pair_version'])) {
            $current = isset($bean->fetched_row['date_modified']) ? $bean->fetched_row['date_modified'] : null;
            if ($current !== null && $current !== $_POST['pair_version']) {
                $diff = array();
                foreach ($bean->fetched_row as $f => $stored) {
                    if (in_array($f, array('date_modified', 'modified_user_id', 'date_entered', 'id')) || !isset($_POST[$f])) {
                        continue;
                    }
                    if (self::norm($f, $_POST[$f]) !== self::norm($f, $stored)) {
                        $diff[] = $f . ': your form ' . self::show($_POST[$f]) . ', current ' . self::show($stored);
                    }
                }
                self::reject($bean->module_dir, $bean->id, 'Your changes were not saved: this record was changed by someone else after you opened the form.'
                    . ($diff ? ' Differences: ' . implode('; ', $diff) . '.' : '')
                    . ' The current record is shown; review it and make your change again if it is still correct.');
            }
        }
        if (in_array($tier, array('field_cas', 'integrated'), true) && !empty($_POST['pair_load'])) {
            self::fieldCas($bean);
        }
        if (in_array($tier, array('conditional', 'integrated'), true) && !empty($_POST['pair_condition'])) {
            // SuiteCRM HTML-encodes incoming request values; undo that before parsing the JSON condition.
            $conds = json_decode(html_entity_decode($_POST['pair_condition'], ENT_QUOTES), true);
            $parts = array();
            $first = null;
            foreach ((is_array($conds) ? $conds : array()) as $c) {
                if (empty($c['module']) || empty($c['record']) || empty($c['field'])) {
                    continue;
                }
                $f = $c['field'];
                if ($c['record'] === $bean->id && is_array($bean->fetched_row) && array_key_exists($f, $bean->fetched_row)) {
                    $current = $bean->fetched_row[$f];
                    $name = $bean->get_summary_text();
                } else {
                    $other = BeanFactory::newBean($c['module']);
                    if (!$other || !$other->retrieve($c['record'])) {
                        continue;
                    }
                    if (!isset($other->field_defs[$f])) {
                        continue;
                    }
                    $current = $other->$f;
                    $name = $other->get_summary_text();
                }
                if (self::norm($f, $c['value']) !== self::norm($f, $current)) {
                    $parts[] = $name . ' ' . $f . ' ' . self::show($c['value']) . ' -> ' . self::show($current);
                    $first = $first ?: $c;
                }
            }
            if ($parts) {
                self::reject($first['module'], $first['record'], 'Your changes were not saved because information they depend on changed since you last saw it: '
                    . implode('; ', $parts) . '. The current page is shown; decide again.');
            }
        }
    }
}
