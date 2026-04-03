# Paperclip Router for Codex

Multi-account Codex account router for Paperclip with automatic failover, usage-aware skipping, and Windows DPAPI-encrypted local sessions.

## What it does

Paperclip Router for Codex lets you use multiple Codex Subscription accounts inside Paperclip.

When one account hits a rate limit or runs out of available usage, the router automatically skips it and switches to the next available account.

This helps Paperclip agents keep running with less manual account switching.

## Features

- Multi-account routing for Codex Subscription accounts
- Automatic failover on rate limit
- Usage-aware account skipping
- 5h and weekly remaining usage display
- Active account highlight in the GUI
- Windows DPAPI-encrypted local session storage
- Custom Codex executable path support
- Paperclip-ready `router-codex.bat` launcher

## How it works

1. Add your Codex Subscription accounts in the Paperclip Router app.
2. Log in each account with `Browser login`.
3. Order accounts by priority.
4. Copy the `router-codex.bat` path into your Paperclip agent `Command` field.
5. Paperclip runs through the router instead of calling Codex directly.
6. If the current account is unavailable, the router switches to the next one.

## Files

- `router_manager.py` — desktop GUI for account management, usage view, and Paperclip setup
- `router.py` — runtime router that selects an account and launches Codex
- `router-codex.bat` — Paperclip launcher for Codex routing
- `paths.py` — data/session/config path handling
- `crypto.py` — Windows DPAPI-based encryption helpers

## Setup

### Requirements

- Windows
- Python 3.11+
- Codex installed and available on PATH, or set manually in the app under `Executables`

### Run the app

Run: `python router_manager.py`

### Use with Paperclip

Set your Paperclip agent adapter to `Codex (local)`.

Then set the `Command` field to:

`C:\Users\<you>\.paperclip-router\router-codex.bat`

## Usage behavior

The router skips accounts when:

- the account is disabled
- the account is on cooldown
- cached usage is above your configured auto-skip threshold
- `5h remaining = 0%`
- `weekly remaining = 0%`

## Security

Session data is stored locally and encrypted with Windows DPAPI.

Sensitive local session files are not meant to be committed to GitHub.

Typical local data lives under:

`C:\Users\<you>\.paperclip-router\`

## Screenshots

### Main window
![Main window](screenshots/main-window.jpg)

### Usage dialog
![Usage dialog](screenshots/usage-dialog.jpg)

### Paperclip setup
![Paperclip setup](screenshots/paperclip-setup.jpg)

## Notes

- This repository is the Codex-focused version of the router
- Claude account routing is not part of this release
- OpenAI API and Anthropic API account types may still appear in the app for key-based fallback setups

## Status

Current release: `v1.0.0`

## Author

Built by ShoshiBuilds.
