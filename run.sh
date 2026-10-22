#!/usr/bin/env bash
# Keep the Signal bot running 24/7.
#
# A bare `python3 main.py` dies for good the first time the process exits —
# a crash, an out-of-memory kill, or even a brief network blip that drops a
# Telegram client (which makes main() return cleanly with no error). This
# wrapper restarts it automatically and logs every start/exit to bot.log so
# the reason is always captured.
#
# Run it instead of `python3 main.py`:
#     bash run.sh
# (inside tmux, then detach with Ctrl+B then D)

cd "$(dirname "$0")" || exit 1

while true; do
  echo "=== $(date -u '+%Y-%m-%d %H:%M:%S UTC') starting main.py ===" | tee -a bot.log
  # -u: unbuffered so logs flush immediately even mid-crash.
  python3 -u main.py 2>&1 | tee -a bot.log
  code=${PIPESTATUS[0]}
  echo "=== $(date -u '+%Y-%m-%d %H:%M:%S UTC') main.py exited (code ${code}); restarting in 5s ===" | tee -a bot.log
  sleep 5
done
