# Rules for coding agents

This repository is public.

## Workflow
- Every change goes through a pull request from its own branch. Never push to `main`.
- Only the repository owner merges.
- Do not create folders, data copies, or branches outside your assigned worktree.

## Privacy and secrets
Never commit secrets, credentials, tokens, device IDs, MAC or IP addresses, email addresses, real names, domain names, or any other personal data. Use placeholders in code and docs, and keep real values in gitignored local config (`.env`, `config.local.*`).
