<?php
declare(strict_types=1);

// Keep this command outside the document root. The token stays in the existing bot config.
try {
    if (PHP_SAPI !== 'cli' || !in_array($argc, [3, 4], true) || ($argc === 4 && $argv[3] !== '--check')
        || !preg_match('~^(?:/|[A-Za-z]:[\\\\/])~', $argv[1]) || !preg_match('~^(?:/|[A-Za-z]:[\\\\/])~', $argv[2])) exit(1);
    $cfg = require $argv[1];
    $cfg['house_lights'] = require $argv[2];
    require __DIR__.'/owner_telegram.php';
    if ($argc === 4) {
        hl_settings($cfg);
        if (!function_exists('curl_init')) exit(1);
        echo 'READY'; exit;
    }
    $raw = file_get_contents('php://stdin', false, null, 0, 4097);
    if (strlen($raw) > 4096) exit(1);
    $notice = json_decode($raw, true, 16, JSON_THROW_ON_ERROR);
    hl_notice($cfg, $notice);
    echo 'SENT';
} catch (Throwable $e) { exit(1); }
