<?php
declare(strict_types=1);

// Load this module only after the existing webhook secret-header check.
function hl_settings(array $cfg): array {
    $s = $cfg['house_lights'] ?? [];
    if (($s['enabled'] ?? false) !== true || !is_int($cfg['allowed_id'] ?? null)
        || !is_int($s['owner_chat_id'] ?? null) || $s['owner_chat_id'] <= 0 || $s['owner_chat_id'] !== $cfg['allowed_id']
        || !is_string($s['decision_key'] ?? null) || strlen($s['decision_key']) < 32
        || $s['decision_key'] === 'REPLACE_WITH_PRIVATE_OWNER_DECISION_KEY'
        || in_array($s['decision_key'], [$cfg['token'] ?? '', $cfg['secret'] ?? ''], true)
        || !preg_match('/^[0-9]+:[A-Za-z0-9_-]{20,}$/D', $cfg['token'] ?? '')) {
        throw new RuntimeException('House Lights is unavailable.');
    }
    $people = parse_url($s['people_url'] ?? '');
    $decision = parse_url($s['decision_url'] ?? '');
    if (!is_array($people) || !is_array($decision) || ($people['scheme'] ?? '') !== 'https'
        || !isset($people['host']) || isset($people['user']) || isset($people['pass'])
        || isset($people['query']) || isset($people['fragment'])
        || ($decision['scheme'] ?? '') !== 'https' || ($decision['host'] ?? '') !== $people['host']
        || ($decision['port'] ?? 443) !== ($people['port'] ?? 443)
        || isset($decision['user']) || isset($decision['pass']) || isset($decision['query']) || isset($decision['fragment'])
        || !str_ends_with($people['path'] ?? '', '/people')
        || ($decision['path'] ?? '') !== substr($people['path'], 0, -7).'/owner/decision') {
        throw new RuntimeException('Configure the fixed House Lights URLs.');
    }
    return $s;
}

function hl_post(string $url, array $body, array $headers = []): array {
    $response = '';
    $c = curl_init($url);
    curl_setopt_array($c, [CURLOPT_POST => true, CURLOPT_POSTFIELDS => json_encode($body, JSON_THROW_ON_ERROR),
        CURLOPT_HTTPHEADER => array_merge(['Content-Type: application/json'], $headers),
        CURLOPT_RETURNTRANSFER => false, CURLOPT_FOLLOWLOCATION => false, CURLOPT_PROXY => '',
        CURLOPT_CONNECTTIMEOUT => 1, CURLOPT_TIMEOUT => 3, CURLOPT_PROTOCOLS => CURLPROTO_HTTPS,
        CURLOPT_SSL_VERIFYPEER => true, CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_WRITEFUNCTION => static function($handle, string $part) use (&$response): int {
            if (strlen($response) + strlen($part) > 32768) return 0;
            $response .= $part;
            return strlen($part);
        }]);
    $ok = curl_exec($c);
    $status = curl_getinfo($c, CURLINFO_RESPONSE_CODE);
    curl_close($c);
    if ($ok === false || $status !== 200) throw new RuntimeException('House Lights response is unconfirmed.');
    $value = json_decode($response, true, 16, JSON_THROW_ON_ERROR);
    if (!is_array($value)) throw new RuntimeException('House Lights response is invalid.');
    return $value;
}

function hl_telegram(array $cfg, string $method, array $body, ?callable $post = null): array {
    if (!in_array($method, ['sendMessage', 'answerCallbackQuery', 'editMessageReplyMarkup'], true)
        || !preg_match('/^[0-9]+:[A-Za-z0-9_-]{20,}$/D', $cfg['token'] ?? '')) {
        throw new RuntimeException('House Lights bot configuration is unavailable.');
    }
    $value = ($post ?? 'hl_post')('https://api.telegram.org/bot'.$cfg['token'].'/'.$method, $body, []);
    if (($value['ok'] ?? false) !== true) throw new RuntimeException('Telegram result is unconfirmed.');
    return $value;
}

function hl_owner(array $cfg, array $from, array $chat): bool {
    try { $s = hl_settings($cfg); } catch (Throwable $e) { return false; }
    return ($from['id'] ?? null) === $cfg['allowed_id'] && ($chat['type'] ?? '') === 'private'
        && ($chat['id'] ?? null) === $s['owner_chat_id'];
}

function hl_notice(array $cfg, array $notice, ?callable $post = null): void {
    $s = hl_settings($cfg);
    $keys = array_keys($notice); sort($keys);
    if ($keys !== ['approve', 'deny', 'email', 'name', 'people_url'] || $notice['people_url'] !== $s['people_url']
        || !is_string($notice['name']) || strlen($notice['name']) > 480
        || !is_string($notice['email']) || strlen($notice['email']) > 254
        || !preg_match('/^hl:a:[A-Za-z0-9_-]{32}:[A-Za-z0-9_-]{16}$/D', $notice['approve'])
        || !preg_match('/^hl:d:[A-Za-z0-9_-]{32}:[A-Za-z0-9_-]{16}$/D', $notice['deny'])
        || substr($notice['approve'], 5, 32) !== substr($notice['deny'], 5, 32)) {
        throw new RuntimeException('Use a valid owner notice.');
    }
    $sent = hl_telegram($cfg, 'sendMessage', ['chat_id' => $s['owner_chat_id'],
        'text' => "House Lights access request\nName: ".$notice['name']."\nEmail: ".$notice['email'],
        'link_preview_options' => ['is_disabled' => true],
        'reply_markup' => ['inline_keyboard' => [
            [['text' => 'Approve', 'callback_data' => $notice['approve']], ['text' => 'Deny', 'callback_data' => $notice['deny']]],
            [['text' => 'Open People', 'url' => $s['people_url']]]]]], $post);
    if (!is_int($sent['result']['message_id'] ?? null) || $sent['result']['message_id'] <= 0
        || ($sent['result']['chat']['id'] ?? null) !== $s['owner_chat_id']
        || ($sent['result']['chat']['type'] ?? '') !== 'private') {
        throw new RuntimeException('Owner notification is unconfirmed. Review People.');
    }
}

function hl_help(array $cfg, array $message): string {
    if (!hl_owner($cfg, $message['from'] ?? [], $message['chat'] ?? [])) return '';
    return "\nHouse Lights People: ".hl_settings($cfg)['people_url'];
}

function hl_dispatch(array $update, array $cfg, ?callable $post = null): bool {
    $cq = $update['callback_query'] ?? null;
    if (is_array($cq) && is_string($cq['data'] ?? null) && str_starts_with($cq['data'], 'hl:')) {
        if (!hl_owner($cfg, $cq['from'] ?? [], $cq['message']['chat'] ?? [])) return true;
        $s = hl_settings($cfg);
        $text = 'Could not confirm this action. Open People.';
        $handled = false;
        try {
            $result = ($post ?? 'hl_post')($s['decision_url'], ['callback_data' => $cq['data']], ['Authorization: Bearer '.$s['decision_key']]);
            $status = $result['result'] ?? '';
            if (!in_array($status, ['approved', 'denied', 'already_handled'], true)) throw new RuntimeException('Invalid action result.');
            $text = $status === 'already_handled' ? 'Already handled. Open People for current access.'
                : ($status === 'approved' ? 'Request approved.' : 'Request denied.');
            $handled = true;
        } catch (Throwable $e) { /* Keep the canonical People fallback and do not retry. */ }
        try {
            hl_telegram($cfg, 'answerCallbackQuery', ['callback_query_id' => $cq['id'], 'text' => $text], $post);
            if ($handled) hl_telegram($cfg, 'editMessageReplyMarkup', ['chat_id' => $s['owner_chat_id'],
                'message_id' => $cq['message']['message_id'],
                'reply_markup' => ['inline_keyboard' => [[['text' => 'Open People', 'url' => $s['people_url']]]]]], $post);
        } catch (Throwable $e) { /* A repeated tap reconciles the same canonical decision. */ }
        return true;
    }
    return false;
}
