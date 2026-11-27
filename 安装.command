#!/bin/zsh
set -eu
bundle_root=${0:A:h}
exec /usr/bin/python3 "$bundle_root/plugins/codex-reset-calendar/scripts/manage.py" install \
  --marketplace "$bundle_root/.agents/plugins/marketplace.json"
