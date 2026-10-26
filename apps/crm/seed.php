<?php
// Seed the PAIR CRM template through SuiteCRM beans so relationships are stored natively.
// Usage (from the SuiteCRM directory): php seed.php FIXTURE_JSON
if (!defined('sugarEntry')) {
    define('sugarEntry', true);
}
require_once 'include/entryPoint.php';
global $current_user;
$current_user = BeanFactory::getBean('Users', '1');
$f = json_decode(file_get_contents($argv[1]), true);

$a = $f['agent_user'];
$u = BeanFactory::newBean('Users');
$u->new_with_id = true;
$u->id = $a['id'];
$u->user_name = $a['user_name'];
$u->first_name = $a['first_name'];
$u->last_name = $a['last_name'];
$u->status = 'Active';
$u->employee_status = 'Active';
$u->is_admin = 0;
$u->user_hash = User::getPasswordHash($a['password']);
$u->save();
$u->setPreference('ut', 1, 0, 'global');        // timezone wizard already done
$u->setPreference('timezone', 'UTC', 0, 'global');
$u->savePreferencesToDB();

$assigned = $a['id'];
foreach ($f['accounts'] as $r) {
    $b = BeanFactory::newBean('Accounts');
    $b->new_with_id = true;
    foreach ($r as $k => $v) { $b->$k = $v; }
    $b->assigned_user_id = $assigned;
    $b->save();
}
foreach ($f['contacts'] as $r) {
    $b = BeanFactory::newBean('Contacts');
    $b->new_with_id = true;
    foreach ($r as $k => $v) { if ($k !== 'key') { $b->$k = $v; } }
    $b->assigned_user_id = $assigned;
    $b->save();
}
foreach ($f['notes'] as $r) {
    $b = BeanFactory::newBean('Notes');
    $b->new_with_id = true;
    $b->id = $r['id'];
    $b->name = $r['name'];
    $b->description = $r['description'];
    $b->parent_type = 'Contacts';
    $b->parent_id = $r['contact'];
    $b->contact_id = $r['contact'];
    $b->assigned_user_id = $assigned;
    $b->save();
}
foreach ($f['opportunities'] as $r) {
    $b = BeanFactory::newBean('Opportunities');
    $b->new_with_id = true;
    foreach ($r as $k => $v) { if ($k !== 'key') { $b->$k = $v; } }
    $b->currency_id = '-99';
    $b->assigned_user_id = $assigned;
    $b->save();
}
echo json_encode(array('users' => 1, 'accounts' => count($f['accounts']), 'contacts' => count($f['contacts']),
                       'notes' => count($f['notes']), 'opportunities' => count($f['opportunities']))) . "\n";
