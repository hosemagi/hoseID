#!/bin/zsh
# Truncate ~/.ollama/logs/server.log when it exceeds a size threshold.
#
# Context (see vision-model-findings.md §A8): the Ollama desktop app spawns llama-server with
# --log-verbosity 4, hardcoded, with nothing rotating the output. It reached 6.2 GB by 2026-08-04
# and regrows at roughly 110 MB/day.
#
# Truncate in place rather than delete: the `ollama serve` process holds the file open as stdout
# and stderr, so unlinking it would not reclaim space until Ollama restarts (which evicts both
# resident Sage tiers). `: >` keeps the descriptor valid and logging continues normally.
#
# This does NOT touch --log-verbosity, which cannot be changed without restarting the app.

set -u

LOG="${HOME}/.ollama/logs/server.log"
MAX_BYTES=${OLLAMA_LOG_MAX_BYTES:-524288000}   # 500 MB
STAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

[[ -f "$LOG" ]] || exit 0

SIZE=$(stat -f%z "$LOG" 2>/dev/null || echo 0)
if (( SIZE > MAX_BYTES )); then
    : > "$LOG"
    echo "$STAMP rotate_ollama_log: truncated server.log at ${SIZE} bytes (threshold ${MAX_BYTES})"
else
    echo "$STAMP rotate_ollama_log: no action, ${SIZE} bytes under threshold ${MAX_BYTES}"
fi
