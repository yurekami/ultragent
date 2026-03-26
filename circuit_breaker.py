#!/usr/bin/env python3
"""
UltrAgent Circuit Breaker — Prevent wasting budget during API outages.

Inspired by DeepTutor's ErrorRateTracker + CircuitBreaker pattern.
Tracks error rates per model provider with sliding window, and opens
a circuit breaker when failures exceed threshold.

Three states:
  closed  → normal operation
  open    → all calls blocked (API is down, don't waste money)
  half-open → testing recovery (allow one call)

Usage:
    python circuit_breaker.py status           # show circuit states
    python circuit_breaker.py record <model> <success|failure>
    python circuit_breaker.py check <model>    # can we call this model?
    python circuit_breaker.py reset [model]    # reset circuit state
"""

import argparse
import json
import sys
import time
from pathlib import Path

# ─── Paths ───────────────────────────────────────────────────────────────────

ULTRAGENT_DIR = Path.home() / ".claude" / "ultragent"
CIRCUIT_STATE_FILE = ULTRAGENT_DIR / "circuit_breaker_state.json"

# ─── Configuration ───────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "failure_threshold": 5,        # failures before circuit opens
    "recovery_timeout_s": 120,     # seconds to wait before half-open test
    "window_size_s": 300,          # sliding window for error rate (5 min)
    "error_rate_threshold": 0.5,   # 50% failure rate triggers alert
}

# ─── State Management ────────────────────────────────────────────────────────

def _load_state() -> dict:
    """Load circuit breaker state from disk."""
    if not CIRCUIT_STATE_FILE.exists():
        return {"models": {}, "config": DEFAULT_CONFIG}
    try:
        return json.loads(CIRCUIT_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return {"models": {}, "config": DEFAULT_CONFIG}


def _save_state(state: dict) -> None:
    """Save circuit breaker state to disk."""
    CIRCUIT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CIRCUIT_STATE_FILE.write_text(
        json.dumps(state, indent=2, default=str),
        encoding="utf-8",
    )


def _get_model_state(state: dict, model: str) -> dict:
    """Get or create state for a specific model."""
    if model not in state["models"]:
        state["models"][model] = {
            "circuit": "closed",     # closed | open | half-open
            "failure_count": 0,
            "success_count": 0,
            "consecutive_failures": 0,
            "last_failure_time": 0,
            "last_success_time": 0,
            "total_calls": 0,
            "recent_calls": [],      # [(timestamp, success_bool), ...]
            "opened_at": None,
            "opened_count": 0,       # how many times circuit has opened
        }
    return state["models"][model]


def _cleanup_old_calls(model_state: dict, window_s: int) -> None:
    """Remove calls older than the sliding window."""
    cutoff = time.time() - window_s
    model_state["recent_calls"] = [
        c for c in model_state["recent_calls"] if c[0] > cutoff
    ]


# ─── Core Operations ────────────────────────────────────────────────────────

def record_call(model: str, success: bool) -> dict:
    """
    Record an API call result and update circuit state.
    Returns: {model, circuit_state, error_rate, action}
    """
    state = _load_state()
    cfg = state.get("config", DEFAULT_CONFIG)
    ms = _get_model_state(state, model)
    now = time.time()

    # Record the call
    ms["total_calls"] += 1
    ms["recent_calls"].append((now, success))
    _cleanup_old_calls(ms, cfg["window_size_s"])

    if success:
        ms["success_count"] += 1
        ms["last_success_time"] = now
        ms["consecutive_failures"] = 0

        # Half-open recovery: success → close circuit
        if ms["circuit"] == "half-open":
            ms["circuit"] = "closed"
            ms["failure_count"] = 0
    else:
        ms["failure_count"] += 1
        ms["last_failure_time"] = now
        ms["consecutive_failures"] += 1

        # Check if circuit should open
        if ms["consecutive_failures"] >= cfg["failure_threshold"]:
            if ms["circuit"] != "open":
                ms["circuit"] = "open"
                ms["opened_at"] = now
                ms["opened_count"] += 1

        # Half-open: failure → reopen
        if ms["circuit"] == "half-open":
            ms["circuit"] = "open"
            ms["opened_at"] = now

    # Compute error rate
    recent_total = len(ms["recent_calls"])
    recent_failures = sum(1 for _, s in ms["recent_calls"] if not s)
    error_rate = recent_failures / recent_total if recent_total > 0 else 0

    _save_state(state)

    return {
        "model": model,
        "circuit_state": ms["circuit"],
        "error_rate": round(error_rate, 3),
        "consecutive_failures": ms["consecutive_failures"],
        "action": "blocked" if ms["circuit"] == "open" else "allowed",
    }


def check_circuit(model: str) -> dict:
    """
    Check if a call to this model is allowed.
    Also handles open → half-open transition after recovery timeout.
    Returns: {allowed, circuit_state, error_rate, reason}
    """
    state = _load_state()
    cfg = state.get("config", DEFAULT_CONFIG)
    ms = _get_model_state(state, model)
    now = time.time()

    _cleanup_old_calls(ms, cfg["window_size_s"])

    # Compute error rate
    recent_total = len(ms["recent_calls"])
    recent_failures = sum(1 for _, s in ms["recent_calls"] if not s)
    error_rate = recent_failures / recent_total if recent_total > 0 else 0

    if ms["circuit"] == "closed":
        result = {
            "allowed": True,
            "circuit_state": "closed",
            "error_rate": round(error_rate, 3),
            "reason": "circuit closed, normal operation",
        }
    elif ms["circuit"] == "open":
        elapsed = now - (ms.get("opened_at") or now)
        if elapsed >= cfg["recovery_timeout_s"]:
            ms["circuit"] = "half-open"
            _save_state(state)
            result = {
                "allowed": True,
                "circuit_state": "half-open",
                "error_rate": round(error_rate, 3),
                "reason": f"circuit entering half-open after {elapsed:.0f}s recovery",
            }
        else:
            remaining = cfg["recovery_timeout_s"] - elapsed
            result = {
                "allowed": False,
                "circuit_state": "open",
                "error_rate": round(error_rate, 3),
                "reason": f"circuit OPEN — {ms['consecutive_failures']} consecutive failures, "
                          f"recovery in {remaining:.0f}s",
            }
    elif ms["circuit"] == "half-open":
        result = {
            "allowed": True,
            "circuit_state": "half-open",
            "error_rate": round(error_rate, 3),
            "reason": "circuit half-open, testing recovery (one call allowed)",
        }
    else:
        result = {"allowed": True, "circuit_state": "unknown", "error_rate": 0, "reason": "unknown state"}

    return result


def reset_circuit(model: str | None = None) -> str:
    """Reset circuit state for a model or all models."""
    state = _load_state()
    if model:
        if model in state["models"]:
            del state["models"][model]
            _save_state(state)
            return f"Circuit reset for {model}"
        return f"No state found for {model}"
    else:
        state["models"] = {}
        _save_state(state)
        return "All circuit states reset"


def get_status() -> dict:
    """Get status of all tracked models."""
    state = _load_state()
    cfg = state.get("config", DEFAULT_CONFIG)
    now = time.time()
    result = {}

    for model, ms in state.get("models", {}).items():
        _cleanup_old_calls(ms, cfg["window_size_s"])
        recent_total = len(ms["recent_calls"])
        recent_failures = sum(1 for _, s in ms["recent_calls"] if not s)
        error_rate = recent_failures / recent_total if recent_total > 0 else 0

        result[model] = {
            "circuit": ms["circuit"],
            "error_rate": round(error_rate, 3),
            "total_calls": ms["total_calls"],
            "consecutive_failures": ms["consecutive_failures"],
            "opened_count": ms["opened_count"],
        }

        if ms["circuit"] == "open" and ms.get("opened_at"):
            elapsed = now - ms["opened_at"]
            remaining = max(0, cfg["recovery_timeout_s"] - elapsed)
            result[model]["recovery_in_s"] = round(remaining)

    return result


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="UltrAgent Circuit Breaker")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show circuit states for all models")

    rec = sub.add_parser("record", help="Record an API call result")
    rec.add_argument("model", help="Model name (e.g., opus, sonnet)")
    rec.add_argument("result", choices=["success", "failure"])

    chk = sub.add_parser("check", help="Check if a model call is allowed")
    chk.add_argument("model", help="Model name")

    rst = sub.add_parser("reset", help="Reset circuit state")
    rst.add_argument("model", nargs="?", help="Model name (omit for all)")

    args = parser.parse_args()

    if args.command == "status":
        status = get_status()
        if not status:
            print("No models tracked yet.")
            return
        print("=== Circuit Breaker Status ===")
        for model, info in status.items():
            circuit = info["circuit"]
            marker = {"closed": "OK", "open": "BLOCKED", "half-open": "TESTING"}.get(circuit, "?")
            print(f"  [{marker:>8}] {model:<15} err={info['error_rate']:.0%}  "
                  f"calls={info['total_calls']}  consec_fail={info['consecutive_failures']}  "
                  f"opened={info['opened_count']}x"
                  + (f"  recovery_in={info.get('recovery_in_s', '?')}s" if circuit == "open" else ""))

    elif args.command == "record":
        success = args.result == "success"
        result = record_call(args.model, success)
        status = "OK" if result["action"] == "allowed" else "BLOCKED"
        print(f"[{status}] {args.model}: circuit={result['circuit_state']}  "
              f"err={result['error_rate']:.0%}  consec_fail={result['consecutive_failures']}")

    elif args.command == "check":
        result = check_circuit(args.model)
        status = "ALLOWED" if result["allowed"] else "BLOCKED"
        print(f"[{status}] {args.model}: {result['reason']}")

    elif args.command == "reset":
        msg = reset_circuit(args.model)
        print(msg)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
