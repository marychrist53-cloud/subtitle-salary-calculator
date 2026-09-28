# Security and local data

Keep Telegram tokens and Google service-account keys outside Git. The default `.env`, key file, JSON state, and backup paths are ignored. If you choose custom paths, add ignore rules before staging files. If a credential has already been published, remove it from use and replace it at its provider; adding an ignore rule does not remove it from Git history.

Every Telegram account can use the bot; there is no chat ID allowlist. Group members share destination settings. Undo operations verify the submitting user and chat, and require confirmation. The ignored `record_metadata.json` stores Telegram IDs, display names, and timestamps for undo ownership checks; queued files also contain subtitle text. Limit access to the Google spreadsheets and local state directory.

For a security report, use the repository owner's private contact or GitHub private vulnerability reporting if enabled. Do not post tokens, private keys, salary data, or complete request logs in public issues.
