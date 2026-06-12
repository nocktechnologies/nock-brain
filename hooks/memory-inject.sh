#!/usr/bin/env bash
# memory-inject.sh — Claude Code UserPromptSubmit hook
#
# Chains: recall-classifier -> budget-recall -> systemMessage injection.
# Only fires when the prompt pattern suggests past-session context would help.
# Operational prompts (heartbeats, dispatch commands) are filtered out.
#
# Output: {"systemMessage": "..."} or {} if no recall needed.
# Designed to complete in <2s.

set -uo pipefail

INPUT=$(cat)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CLASSIFIER="${BRAIN_ROOT}/bin/recall-classifier.py"
BUDGET_RECALL="${BRAIN_ROOT}/bin/budget-recall.py"

PROMPT=$(printf '%s' "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('prompt', d.get('message', '')))
except:
    print('')
" 2>/dev/null)

if [[ -z "$PROMPT" ]] || [[ "${#PROMPT}" -lt 15 ]]; then
    echo '{}'
    exit 0
fi

if ! [[ -f "$CLASSIFIER" ]] || ! [[ -f "$BUDGET_RECALL" ]]; then
    echo '{}'
    exit 0
fi

FACTS_FILE="${HOME}/.nock-brain/facts.json"
if ! [[ -f "$FACTS_FILE" ]]; then
    echo '{}'
    exit 0
fi

RESULT=$(printf '%s' "$PROMPT" | python3 "$CLASSIFIER" 2>/dev/null)
EXIT_CODE=$?

if [[ $EXIT_CODE -ne 0 ]]; then
    echo '{}'
    exit 0
fi

RECALL=$(python3 "$BUDGET_RECALL" --budget 800 --facts "$FACTS_FILE" -- "$PROMPT" 2>/dev/null)
if [[ -z "$RECALL" ]] || [[ "$RECALL" == "No matching facts found." ]]; then
    echo '{}'
    exit 0
fi

python3 -c "
import json, sys
recall = sys.stdin.read()
if recall.strip():
    msg = '[nock-brain] Relevant facts from past sessions:\n' + recall
    print(json.dumps({'systemMessage': msg}))
else:
    print('{}')
" <<< "$RECALL"
