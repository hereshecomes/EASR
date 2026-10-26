<?php
// Seed the PAIR MantisBT template through MantisBT's own APIs. Usage (from the MantisBT directory): php pair_seed.php FIXTURE_JSON
$g_bypass_headers = true;
require_once 'core.php';
set_error_handler(function ($no, $str, $file, $line) { fwrite(STDERR, "PHP error $no: $str at $file:$line\n"); return true; });
set_exception_handler(function ($e) { fwrite(STDERR, 'Exception: ' . get_class($e) . ' ' . $e->getMessage() . ' at ' . $e->getFile() . ':' . $e->getLine() . "\n"); exit(1); });
$f = json_decode(file_get_contents($argv[1]), true);
if (!auth_attempt_script_login('administrator')) { fwrite(STDERR, "admin login failed\n"); exit(1); }   // MantisBT's initial administrator account
$users = array();
foreach ($f['users'] as $u) {
    user_create($u['username'], $u['password'], $u['email'], $u['access'], false, true, $u['realname']);
    $users[$u['key']] = user_get_id_by_name($u['username']);
}
$pid = project_create($f['project']['name'], $f['project']['description'], 10);
foreach ($users as $k => $uid) { project_add_user($pid, $uid, $k == 'customer' ? 25 : 55); }
$cats = array();
foreach ($f['categories'] as $c) { $cats[$c] = category_add($pid, $c); }
$cf = array();
foreach ($f['custom_fields'] as $c) {
    $id = custom_field_create($c['name']);
    custom_field_update($id, array('name' => $c['name'], 'type' => $c['type'], 'possible_values' => $c['possible_values'],
        'access_level_r' => 10, 'access_level_rw' => 25, 'display_report' => true, 'display_update' => true, 'display_resolved' => true,
        'display_closed' => true, 'require_report' => false, 'require_update' => false, 'length_min' => 0, 'length_max' => 0,
        'valid_regexp' => '', 'default_value' => '', 'filter_by' => true));
    custom_field_link($id, $pid);
    $cf[$c['key']] = $id;
}
$out = array();
foreach ($f['issues'] as $i) {
    $b = new BugData();
    $b->project_id = $pid; $b->category_id = $cats[$i['category']];
    $b->summary = $i['summary']; $b->description = $i['description'];
    $b->steps_to_reproduce = $i['steps']; $b->additional_information = $i['additional'];
    $b->priority = $i['priority']; $b->severity = $i['severity']; $b->reproducibility = 70;
    $b->reporter_id = $users[$i['reporter']]; $b->handler_id = $i['handler'] ? $users[$i['handler']] : 0;
    $b->status = $i['status']; $b->view_state = VS_PUBLIC;
    if ($i['due_date']) { $b->due_date = strtotime($i['due_date'] . ' 17:00:00 UTC'); }
    fwrite(STDERR, "creating {$i['key']}\n");
    $id = $b->create();
    foreach ($i['custom'] as $k => $v) { custom_field_set_value($cf[$k], $id, $v, false); }
    $out[$i['key']] = $id;
}
echo json_encode(array('project' => $pid, 'users' => $users, 'categories' => $cats, 'custom_fields' => $cf, 'issues' => $out)), "\n";
