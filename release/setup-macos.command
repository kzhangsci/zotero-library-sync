#!/bin/bash
set -euo pipefail
library_sync_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
library_sync_python=""
for candidate in /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 /opt/homebrew/bin/python3.13 /usr/local/bin/python3.13; do
  if [ -x "$candidate" ]; then library_sync_python="$candidate"; break; fi
done
if [ -z "$library_sync_python" ]; then
  echo 'Please install CPython 3.13 from https://www.python.org/downloads/macos/ first.'
  exit 1
fi
if [ "$#" -eq 0 ]; then
  echo 'Dry-run example (add --apply after reviewing the plan):'
  echo './setup-macos.command --library-dir "/path/to/Library" --baseline-date YYYY-MM-DD --init-library-git --git-user-name "Your Name" --git-user-email "you@example.org"'
  exit 0
fi
exec "$library_sync_python" "$library_sync_root/app/release/install_macos.py" --source "$library_sync_root/app" "$@"
