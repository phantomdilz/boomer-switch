# Owner notifications

Guest verification uses email. New verified pending requests use the existing owner bot.
Each notice has signed Approve and Deny buttons and an Open People link.
The existing bot's private `/help` reply also includes the People link.
People keeps its existing website sign-in and remains the manual fallback.
No notification or email starts from an invitation preview GET.

The website claims each notice in its existing delivery ledger before the provider attempt.
Transport loss remains `unknown`; the application never retries that send automatically.
Guest inbox proof still completes when an owner notification fails.
The request stays visible in People. A definite pre-send pause remains recoverable.
Existing sent, unknown, or manual notices do not replay on deployment.
If notifications are not configured, production uses People only. It does not email the owner.

## Private configuration

Keep the existing bot token, webhook secret, and owner user ID in the existing canonical bot config.
Do not copy them into a code release or website config.
The generic module reads the existing `token` and integer `allowed_id` fields.
Add a separate private PHP settings file that returns:

```php
<?php return [
    'enabled' => false,
    'owner_chat_id' => 1, // Replace locally with the existing owner's private DM ID.
    'people_url' => 'https://example.invalid/house-lights/people',
    'decision_url' => 'https://example.invalid/house-lights/owner/decision',
    'decision_key' => 'REPLACE_WITH_PRIVATE_OWNER_DECISION_KEY',
];
```

This is an ordinary bot private DM: `owner_chat_id` must equal the canonical `allowed_id`.
The module rejects a different positive ID before sending any guest details.
It also rejects callbacks from another user, another chat, a group, or an inline message.
Use one new random owner-action key of at least 32 characters.
It must differ from the home-agent key, bot token, and existing webhook secret.
Put the same scoped key in the website's private `owner_notifications.decision_key`.
It authorizes only signed pending-request decisions, not website login or device commands.

Set the website's `owner_notifications.command` to an absolute PHP array command:

```text
PRIVATE_PHP -f PRIVATE_BRIDGE/owner_telegram_cli.php -- EXISTING_BOT_CONFIG PRIVATE_OWNER_SETTINGS
```

All paths must be absolute. The command reads one bounded JSON payload from stdin.
It emits exactly `SENT` after a confirmed private Telegram response and exits zero.
Append `--check` for a non-messaging configuration, identity-binding, and cURL availability check; success emits exactly `READY`.
No token or key enters command arguments, stdout, or logs.
The command has a five-second outer limit. Each fixed HTTPS request has a one-second connect limit,
a three-second total limit, a 32 KiB response cap, verified TLS, no proxy, no redirect, and no retry.

## Existing webhook integration

Install `hosting/owner_telegram.php` and `hosting/owner_telegram_cli.php` together in a private bridge directory.
Keep the existing canonical bot config unchanged.
Use `hosting/prepare_owner_bot.py` with the reviewed existing source, its exact SHA-256,
the absolute private bridge/settings paths, and a new output file.
The tool refuses a changed or already-patched input and leaves the original file unchanged.
Review the prepared delta: the new handler follows the existing rejecting webhook-secret guard,
precedes its generic callback branch, and appends the People link to existing help.
RSS, reactions, URL commands, and unrelated callbacks retain their existing code.
The new handler consumes only the `hl:` callback namespace.
Existing help replies keep the existing bot's delivery behavior; the at-most-once guarantee applies to pending notices.

The callback producer verifies the existing Telegram secret, canonical owner user, and private owner chat.
It forwards only `callback_data` over fixed authenticated HTTPS to `POST /owner/decision`.
The endpoint accepts no email, revision, identity header, browser cookie, or free-form People action.
It rejects browser Origin headers and the home-agent key.
The website verifies a per-action HMAC and looks up the persisted pending revision.
It commits the canonical People change and receipt in the same SQLite transaction.
Pending buttons have no time expiry; a handled or changed request cannot be overwritten.
A repeated identical action reports that it was already handled and points to current People state.
It does not report that a removed guest still has approval.

## Hosting and rollout

Do not infer PHP-FPM identity from CLI PHP or the account shell.
Before enabling the integration, use a bounded check of these owned files in the actual webhook pool.
Check its effective identity and readability of the existing bot config, new settings, and bridge.
Expose only Boolean results through the private diagnostic path; do not print identifiers or config values.
Match the existing private config's owner/group and narrow read/search rights.
If a named ACL is needed, grant only the actual pool identity the required file read and directory search.
The pool needs no website database access, home key, or private website configuration access.
Verify the foreground notice command separately as the website's actual Passenger identity.

Extend only the dedicated component's public Apache POST exception with the exact `/owner/decision` path.
Leave People behind its current login. Do not exempt the whole prefix or bot root from authentication.
Keep request bodies, callback data, Authorization, cookies, and Referer out of logs and error reports.
The existing webhook secret/header gate must stay unchanged.

Prepare immutable code and bot backups first. Install private files and verify access before selecting the patched webhook.
Read the current private website JSON at promotion and add only `owner_notifications`.
Preserve current sender, transport signing, state path, permissions, and home configuration.
Enable the private bot settings only after review and explicit deployment approval.
The Passenger `people_write_enabled` switch also pauses bot decisions; `mail_enabled` pauses notification sends.
Rollback selects the prior webhook and website release without restoring old permission data.
Never replay unknown notifications or wipe callback receipts during rollback.

Tests stand in for Telegram and mail. They send nothing. Set `BOOMER_TEST_PHP` to an installed PHP executable when running them.
The [Telegram Bot API](https://core.telegram.org/bots/api#inlinekeyboardbutton) limits callback data to 64 bytes. These action payloads are 54 bytes.
