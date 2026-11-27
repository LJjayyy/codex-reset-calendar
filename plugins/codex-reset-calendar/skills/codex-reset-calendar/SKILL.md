---
name: codex-reset-calendar
description: Use when setting up, checking, verifying, manually syncing, or stopping Codex reset-card expiry reminders in Apple Calendar on this Mac.
---

Use the bundled `../../scripts/manage.py` relative to this skill's directory. Run it locally with `/usr/bin/python3`.

- For status, run `status`. Verify `running` and report the latest check from `lastCheck` in the user's time zone.
- To verify calendar events, run `verify`. It reads the current cards and checks for a matching event at each expiry time in the iCloud calendar.
- If the user asks to sync now or stop syncing, run `sync` or `stop`. The stop command leaves existing calendar events in place.
- For setup, run `install` in the logged-in macOS desktop session when the user asks to enable sync. macOS may ask the user to allow full calendar access. Verify setup by running `status` and checking for a successful last check. If a previous setup was stopped, run `install` again to resume.
