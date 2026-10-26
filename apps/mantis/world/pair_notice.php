<?php
// Included at the top of every page ($g_top_include_page): the world's notice banner, if any.
$pair_w = function_exists('pair_world') ? pair_world() : array();
if (!empty($pair_w['notice'])) {
    echo '<div class="pair-notice alert alert-info" role="status" style="margin:0;padding:8px 16px;font-size:14px;"><strong>'
        . htmlspecialchars($pair_w['notice']['title']) . '</strong> ' . htmlspecialchars($pair_w['notice']['text']) . '</div>';
}
if (!empty($_GET['pair_rejected']) && isset($_GET['pair_msg'])) {
    echo '<div class="pair-notice pair-reject alert alert-warning" role="alert" style="margin:0;padding:8px 16px;font-size:14px;">'
        . htmlspecialchars($_GET['pair_msg']) . '</div>';
}
