#!/usr/bin/env bash
# UltrAgent User Correction Detector
# PreToolUse hook — detects when user corrects agent output and captures trajectory.
#
# Install in ~/.claude/settings.json under hooks.PreToolUse:
#   { "type": "command", "command": "bash ~/.claude/ultragent/hooks/user_correction_detector.sh" }
#
# Looks for correction patterns in the conversation context.
# This is a lightweight heuristic — not perfect, but captures signal.

HA_PY="$HOME/.claude/ultragent/ua.py"
LAST_AGENT_FILE="$HOME/.claude/ultragent/.last_agent"

# Only track after Agent tool calls
if [[ "$CLAUDE_TOOL_NAME" == "Agent" ]]; then
    # Store which agent was last used
    AGENT_TYPE=$(echo "$CLAUDE_TOOL_INPUT" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('subagent_type', 'unknown'))
except: print('unknown')
" 2>/dev/null)
    echo "$AGENT_TYPE" > "$LAST_AGENT_FILE" 2>/dev/null
fi

exit 0
