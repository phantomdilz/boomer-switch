"""Prepare a reviewed existing webhook without replacing its other features."""

import argparse
import hashlib
from pathlib import Path
import re


def php_string(value):
    if not isinstance(value, str) or not (value.startswith('/') or re.match(r'^[A-Za-z]:[\\/]', value)) or any(c in value for c in '\0\r\n'):
        raise ValueError('Use an absolute private PHP file path.')
    return "'"+value.replace('\\', '\\\\').replace("'", "\\'")+"'"


def patch_webhook(source, *, bridge_path, settings_path, expected_sha256):
    if hashlib.sha256(source.encode('utf-8')).hexdigest() != expected_sha256:
        raise ValueError('The existing webhook differs from its reviewed source.')
    marker = '// House Lights owner actions.'
    if marker in source or 'function hl_' in source:
        raise ValueError('The webhook already has House Lights integration. Review its current source.')
    callback = "if ($cq = $u['callback_query'] ?? null) {"
    guard = "if ($_SERVER['REQUEST_METHOD'] !== 'POST' || !hash_equals($cfg['secret'], $_SERVER['HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN'] ?? '')) { http_response_code(403); exit; }"
    if source.count(callback) != 1 or guard not in source[:source.index(callback)]:
        raise ValueError('Review the webhook authentication and callback anchors.')
    hook = (marker+'\n$hlEnabled = is_readable('+php_string(settings_path)+') && is_readable('+php_string(bridge_path)+');\n'
            'if ($hlEnabled) {\n  $cfg[\'house_lights\'] = require '+php_string(settings_path)+';\n'
            '  require_once '+php_string(bridge_path)+';\n'
            '  if (is_array($u) && hl_dispatch($u, $cfg)) exit;\n'
            '} elseif (is_string($u[\'callback_query\'][\'data\'] ?? null) && str_starts_with($u[\'callback_query\'][\'data\'], \'hl:\')) {\n'
            '  http_response_code(200); exit;\n}\n')
    source = source.replace(callback, hook+callback)
    pattern = r"(if \(\$cmd==='/start' \|\| \$cmd==='/help'\) reply\()((?:'(?:\\.|[^'\\])*')|(?:\"(?:\\.|[^\"\\])*\"))(\);)"
    source, count = re.subn(pattern, lambda m: m[1]+m[2]+" . ($hlEnabled ? hl_help($cfg, $m) : '')"+m[3], source)
    if count != 1:
        raise ValueError('Review the existing help anchor before patching.')
    return source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--bridge', required=True)
    parser.add_argument('--settings', required=True)
    parser.add_argument('--expected-sha256', required=True)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve() or args.output.exists():
        parser.error('Keep the source unchanged and use a new output file.')
    result = patch_webhook(args.source.read_bytes().decode('utf-8'), bridge_path=args.bridge, settings_path=args.settings, expected_sha256=args.expected_sha256)
    args.output.write_text(result, encoding='utf-8')


if __name__ == '__main__':
    main()
