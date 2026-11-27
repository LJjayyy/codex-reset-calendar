---
name: codex-reset-calendar
description: Use when setting up, checking, verifying, or manually syncing Codex reset-card expiry reminders in Apple Calendar on this Mac, or when the user toggles this plugin on or off.
---

Use the bundled `../../scripts/manage.py` relative to this skill's directory. Run it locally with `/usr/bin/python3`.

- For first-time setup, run `install` in the logged-in macOS desktop session. This is the one-time step triggered from the plugin's **设置**: it asks for full calendar access, reads the cards, verifies the events, and starts the background worker. Verify setup with `status` and a successful `lastCheck`.
- After setup, the user controls syncing only through the plugin's on/off switch. The worker watches the Codex config: turning the plugin on syncs right away, turning it off pauses syncing and leaves existing calendar events untouched. The worker keeps running after Codex exits and syncs normally every 10 minutes while on.
- For status, run `status`. Verify `running` and report the latest check from `lastCheck` in the user's time zone. A paused plugin reports `running: false`; this is expected when the switch is off.
- To verify calendar events, run `verify`. It reads the current cards and checks for a matching event at each expiry time in the iCloud calendar.
- If the user asks to sync now, run `sync`. When the plugin is off or its state is unknown, syncing is skipped and the last successful sync result is kept.
- To upgrade an existing installation, use **设置** to run `install` from the new version. If the upgrade reports that Codex could not read the cards, check `status` for the new background job and report that it will retry while the plugin is on. The optional `--marketplace` flag is for development and legacy migrations.
