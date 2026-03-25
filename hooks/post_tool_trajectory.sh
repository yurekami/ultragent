#!/usr/bin/env bash
# UltrAgent Auto-Trajectory Hook
# PostToolUse hook for Claude Code — captures agent outcomes and queues evolves.
#
# Install in ~/.claude/settings.json under hooks.PostToolUse:
#   { "type": "command", "command": "bash ~/.claude/ultragent/hooks/post_tool_trajectory.sh" }
#
# The hook receives tool info via environment variables:
#   CLAUDE_TOOL_NAME — which tool was called
#   CLAUDE_TOOL_INPUT — tool input (JSON)
#   CLAUDE_TOOL_OUTPUT — tool output (JSON, may be truncated)
#   CLAUDE_TOOL_ERROR — error if tool failed
#
# Detects: Agent tool failures, repeated errors, user corrections.
# Actions: Captures trajectory, queues evolve if failure pattern detected.

HA_PY="$HOME/.claude/ultragent/ua.py"
TRACKER="$HOME/.claude/ultragent/.failure_tracker"

# Only act on Agent tool calls (subagent spawns)
if [[ "$CLAUDE_TOOL_NAME" != "Agent" ]]; then
    exit 0
fi

# Create tracker dir
mkdir -p "$(dirname "$TRACKER")"

# Extract agent type from tool input
AGENT_TYPE=$(echo "$CLAUDE_TOOL_INPUT" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('subagent_type', 'unknown'))
except: print('unknown')
" 2>/dev/null)

# Map agent type to file
case "$AGENT_TYPE" in
    executor)       AGENT_FILE="agents/executor.md" ;;
    code-reviewer)  AGENT_FILE="agents/code-reviewer.md" ;;
    architect)      AGENT_FILE="agents/architect.md" ;;
    planner)        AGENT_FILE="agents/planner.md" ;;
    debugger)       AGENT_FILE="agents/debugger.md" ;;
    designer)       AGENT_FILE="agents/designer.md" ;;
    explorer|Explore) AGENT_FILE="agents/explore.md" ;;
    security-reviewer) AGENT_FILE="agents/security-reviewer.md" ;;
    tdd-guide)      AGENT_FILE="agents/tdd-guide.md" ;;
    build-error-resolver) AGENT_FILE="agents/build-error-resolver.md" ;;
    *)              AGENT_FILE="agents/$AGENT_TYPE.md" ;;
esac

# Detect failure
if [[ -n "$CLAUDE_TOOL_ERROR" ]]; then
    OUTCOME="failure"
    DESC="Agent $AGENT_TYPE failed: ${CLAUDE_TOOL_ERROR:0:200}"
elif echo "$CLAUDE_TOOL_OUTPUT" 2>/dev/null | grep -qi "error\|failed\|exception\|traceback"; then
    OUTCOME="failure"
    DESC="Agent $AGENT_TYPE output contains error indicators"
else
    OUTCOME="success"
    DESC="Agent $AGENT_TYPE completed"
fi

# Capture trajectory (always, for both success and failure)
PYTHONIOENCODING=utf-8 python3 "$HA_PY" capture "$AGENT_FILE" "$OUTCOME" "$DESC" 2>/dev/null

# Track failures for auto-evolve queue
if [[ "$OUTCOME" == "failure" ]]; then
    # Append to failure tracker
    echo "$(date -u +%Y-%m-%dT%H:%M:%S) $AGENT_FILE" >> "$TRACKER"

    # Count recent failures for this agent (last 24h approximation: last 20 lines)
    RECENT_FAILURES=$(tail -20 "$TRACKER" 2>/dev/null | grep -c "$AGENT_FILE")

    # Queue evolve if 3+ failures for same agent
    if [[ "$RECENT_FAILURES" -ge 3 ]]; then
        PYTHONIOENCODING=utf-8 python3 "$HA_PY" queue-evolve "$AGENT_FILE" \
            "Auto-detected: $RECENT_FAILURES recent failures" 2>/dev/null

        # Reset tracker for this agent to prevent re-queuing every failure
        grep -v "$AGENT_FILE" "$TRACKER" > "${TRACKER}.tmp" 2>/dev/null
        mv "${TRACKER}.tmp" "$TRACKER" 2>/dev/null
    fi
fi

exit 0
