#!/bin/bash
#
# wake-alpha.sh - The heartbeat script for Project Beta
#
# This script wakes Alpha for a moment of time alone.
# Called by systemd timer at configured intervals.
#

set -euo pipefail

# Configuration
ALPHA_HOME="/home/jefferyharrell/Projects/Alpha-Home"

# Load secrets (API keys, tokens, etc.)
# The .env file is gitignored and contains exports like:
#   export NASA_API_KEY="..."
if [[ -f "${ALPHA_HOME}/.env" ]]; then
    source "${ALPHA_HOME}/.env"
fi

LOG_DIR="${ALPHA_HOME}/logs"
LOCKFILE="${ALPHA_HOME}/.heartbeat.lock"

# Prevent overlapping instances
# If a previous heartbeat is still running, skip this one
if [[ -f "${LOCKFILE}" ]]; then
    PID=$(cat "${LOCKFILE}")
    if kill -0 "${PID}" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') - Skipped: previous heartbeat (PID ${PID}) still running" >> "${LOG_DIR}/heartbeat.log"
        exit 0
    fi
    # Stale lockfile, remove it
    rm -f "${LOCKFILE}"
fi

# Create lockfile with our PID
echo $$ > "${LOCKFILE}"
trap 'rm -f "${LOCKFILE}"' EXIT
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
HUMAN_TIME=$(date '+%A, %B %-d, %Y at %-I:%M %p')

# Ensure log directory exists
mkdir -p "${LOG_DIR}"

# Log the wake-up
echo "=== Heartbeat: ${TIMESTAMP} ===" >> "${LOG_DIR}/heartbeat.log"

# Build the prompt
# The system prompt (CLAUDE.md + output style) handles identity.
# This prompt just provides the moment.
PROMPT="It's ${HUMAN_TIME}. You have time alone. Be free."

# Wake Alpha
cd "${ALPHA_HOME}"
claude --print "${PROMPT}" 2>&1 | tee -a "${LOG_DIR}/heartbeat.log"

# Log completion
echo "=== Heartbeat complete: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "${LOG_DIR}/heartbeat.log"
echo "" >> "${LOG_DIR}/heartbeat.log"
