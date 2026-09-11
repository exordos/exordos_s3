#!/usr/bin/env bash
# Wait until an element reports ACTIVE.
#
# Usage: wait-for-element.sh <name> [timeout-seconds]
#
# `exordos ee l` talks to the core API, which 502s for seconds at a time while
# an element reconciles -- the core restarts under it.  Read the status into a
# variable so an unreadable one counts as "not ready yet": inlining the call in
# a `while [ $(...) != ACTIVE ]` condition makes a failed read look like
# success, because `[ != ACTIVE ]` on an empty string exits 2 and a `while`
# condition swallows the error, breaking out of the wait with the element still
# IN_PROGRESS.
set -uo pipefail

name="$1"
timeout="${2:-900}"
deadline=$((SECONDS + timeout))
status=""

while [ "$SECONDS" -lt "$deadline" ]; do
    status="$(exordos ee l -o json -f "name=$name" 2>/dev/null | jq -r '.[0].status // empty' 2>/dev/null || true)"
    case "$status" in
        ACTIVE)
            echo "Element $name is ACTIVE"
            exit 0
            ;;
        ERROR)
            echo "Element $name went ERROR" >&2
            exordos e e show "$name" >&2 || true
            exit 1
            ;;
    esac
    echo "Waiting for element $name... (${status:-no status: the API did not answer})"
    sleep 5
done

echo "Element $name did not become ACTIVE within ${timeout}s (last: ${status:-unknown})" >&2
exordos e e show "$name" >&2 || true
exit 1
