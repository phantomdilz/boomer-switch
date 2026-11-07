# Rules for coding agents

This repository is **public**. Read all of this before changing anything.

## Workflow
- Every change goes through a pull request from its own branch in its own worktree. Never push to `main`.
- An independent reviewer agent (preferably from a different vendor than the implementer) must approve before merge.
- Only the repository owner merges or approves merges.
- Do not create folders, data copies or branches outside your assigned worktree.
- After a PR is merged (and deployed, if applicable), remove its worktree and local branch.
- A ticket is not Done until its acceptance demo passes.
- Known failures are filed as tracker issues with evidence; never leave one untracked.
- Each PR body lists: scope, tests run, evidence, and the linked tracker issue.

## Privacy and secrets
Never commit secrets, credentials, tokens, device IDs, MAC or IP addresses, email addresses, real names, domain names, or any other personal data. Use placeholders in code and docs, and keep real values in gitignored local config (`.env`, `config.local.*`).
