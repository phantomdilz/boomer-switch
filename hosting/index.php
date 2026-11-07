<?php
// Install only inside the dedicated component namespace.
// The server sets BOOMER_BRIDGE_CONFIG to a PHP file outside the document root.
declare(strict_types=1);
ini_set('display_errors', '0');
function unavailable(): never {
    http_response_code(503);
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    echo '{"error":"Website service unavailable."}';
    exit;
}
try {
    $configPath = getenv('BOOMER_BRIDGE_CONFIG');
    if (!$configPath || !is_file($configPath)) { unavailable(); }
    $config = require $configPath;
    // This callable must use the existing site's server-side owner validator.
    // It must not trust request headers or a user-selected identity.
    $owner = ($config['owner_authenticated'])() === true;
    $https = isset($_SERVER['HTTPS']) && $_SERVER['HTTPS'] === 'on';
    if (!$https) { http_response_code(403); exit; }
    $length = (int)($_SERVER['CONTENT_LENGTH'] ?? 0);
    if ($length < 0 || $length > 4096) { http_response_code(413); exit; }
    $body = file_get_contents('php://input', false, null, 0, 4097);
    if ($body === false || strlen($body) > 4096) { http_response_code(413); exit; }
    $path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
    $mount = $config['mount'];
    if (!is_string($path) || !str_starts_with($path, $mount . '/')) { http_response_code(404); exit; }
    $componentCookies = [];
    foreach (['bs_session', 'bs_owner'] as $name) {
        $value = $_COOKIE[$name] ?? null;
        if (is_string($value) && preg_match('/^[A-Za-z0-9_-]{32,128}$/', $value)) {
            $componentCookies[] = $name . '=' . $value;
        }
    }
    $directSource = ($config['direct_source'] ?? false) === true;
    foreach (array_keys($_SERVER) as $name) {
        if ($name === 'HTTP_FORWARDED' || $name === 'HTTP_X_REAL_IP' || $name === 'HTTP_CLIENT_IP' ||
            str_starts_with($name, 'HTTP_X_FORWARDED') || str_starts_with($name, 'HTTP_CF_') || str_starts_with($name, 'HTTP_TRUE_CLIENT')) {
            $directSource = false;
        }
    }
    $env = [
        'BOOMER_DIRECT_SOURCE' => $directSource ? '1' : '0', 'REMOTE_ADDR' => $_SERVER['REMOTE_ADDR'] ?? '',
        'BOOMER_PRIVATE_CONFIG' => $config['python_config'], 'BOOMER_HTTPS' => '1',
        'REQUEST_METHOD' => $_SERVER['REQUEST_METHOD'], 'PATH_INFO' => substr($path, strlen($mount)),
        'SCRIPT_NAME' => $mount, 'CONTENT_LENGTH' => (string)strlen($body),
        'CONTENT_TYPE' => $_SERVER['CONTENT_TYPE'] ?? '',
        'HTTP_COOKIE' => implode('; ', $componentCookies), 'HTTP_ORIGIN' => $_SERVER['HTTP_ORIGIN'] ?? '',
        'HTTP_X_CSRF_TOKEN' => $_SERVER['HTTP_X_CSRF_TOKEN'] ?? '',
        'HTTP_AUTHORIZATION' => $_SERVER['HTTP_AUTHORIZATION'] ?? '',
    ];
    if ($owner) { $env['REMOTE_USER'] = $config['owner_identity']; }
    $process = @proc_open([$config['python'], '-m', 'boomer_switch.website_cgi'],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes, $config['release_directory'], $env, ['bypass_shell' => true]);
    if (!is_resource($process)) { unavailable(); }
    fwrite($pipes[0], $body); fclose($pipes[0]); stream_set_blocking($pipes[1], false); stream_set_blocking($pipes[2], false);
    $output = ''; $stderrBytes = 0; $deadline = microtime(true) + 25;
    do {
        $output .= stream_get_contents($pipes[1]);
        $stderrBytes += strlen(stream_get_contents($pipes[2]));
        $state = proc_get_status($process);
        if (strlen($output) > 262144 || $stderrBytes > 262144 || microtime(true) > $deadline) {
            proc_terminate($process, 9); fclose($pipes[1]); fclose($pipes[2]); proc_close($process); unavailable();
        }
        if ($state['running']) { usleep(10000); }
    } while ($state['running']);
    $output .= stream_get_contents($pipes[1]); fclose($pipes[1]); fclose($pipes[2]); proc_close($process);
    $parts = explode("\r\n\r\n", $output, 2);
    if (count($parts) !== 2 || strlen($output) > 262144) { unavailable(); }
    $lines = explode("\r\n", $parts[0]);
    if (!preg_match('/^Status: ([2345][0-9]{2}) [A-Za-z ]+$/', array_shift($lines), $match)) { unavailable(); }
    http_response_code((int)$match[1]);
    $allowed = ['content-type', 'content-length', 'cache-control', 'referrer-policy', 'x-content-type-options', 'content-security-policy', 'location', 'set-cookie'];
    foreach ($lines as $line) {
        $name = strtolower(explode(':', $line, 2)[0]);
        if (!in_array($name, $allowed, true)) { unavailable(); }
        header($line, false);
    }
    echo $parts[1];
} catch (Throwable $error) { unavailable(); }
