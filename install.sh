#!/usr/bin/env bash
# nock-brain installer
# Wires the memory-inject hook into Claude Code settings.

set -euo pipefail

BRAIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS_FILE="${HOME}/.claude/settings.json"
FACTS_DIR="${HOME}/.nock-brain"

echo "nock-brain installer"
echo "===================="

# Create and harden facts directory. Existing stores are migrated by dropping
# group/other access across the local memory tree.
mkdir -p -m 700 "$FACTS_DIR"
chmod -R go-rwx "$FACTS_DIR"
echo "[1/4] Created ${FACTS_DIR}"

# Make scripts executable
chmod +x "$BRAIN_DIR"/bin/*.py "$BRAIN_DIR"/hooks/*.sh
echo "[2/4] Made scripts executable"

# Check for transcript sources
TRANSCRIPT_DIR=""
if [[ -d "${HOME}/.memsearch/memory" ]] && ls "${HOME}/.memsearch/memory/"*.md &>/dev/null; then
    TRANSCRIPT_DIR="${HOME}/.memsearch/memory"
    echo "[3/4] Found memsearch transcripts at ${TRANSCRIPT_DIR}"
elif [[ -d "${FACTS_DIR}/transcripts" ]] && ls "${FACTS_DIR}/transcripts/"*.md &>/dev/null; then
    TRANSCRIPT_DIR="${FACTS_DIR}/transcripts"
    echo "[3/4] Found transcripts at ${TRANSCRIPT_DIR}"
else
    echo "[3/4] No transcripts found yet."
    echo "      Place session transcript .md files in ${FACTS_DIR}/transcripts/"
    echo "      or install the memsearch plugin for automatic transcripts."
fi

# Extract facts if transcripts exist
if [[ -n "$TRANSCRIPT_DIR" ]]; then
    echo "      Extracting facts..."
    python3 "$BRAIN_DIR/bin/extract-facts.py" --dir "$TRANSCRIPT_DIR" --output "$FACTS_DIR/facts.json"
fi

# Wire hook into settings
if [[ -f "$SETTINGS_FILE" ]]; then
    HAS_HOOKS=$(python3 -c "
import json
with open('$SETTINGS_FILE') as f:
    d = json.load(f)
hooks = d.get('hooks', {}).get('UserPromptSubmit', [])
for h in hooks:
    for hh in h.get('hooks', []):
        if 'memory-inject' in hh.get('command', ''):
            print('yes')
            break
" 2>/dev/null || echo "")

    if [[ "$HAS_HOOKS" == "yes" ]]; then
        echo "[4/4] Hook already installed in settings.json"
    else
        python3 -c "
import json

with open('$SETTINGS_FILE') as f:
    settings = json.load(f)

hooks = settings.setdefault('hooks', {})
ups = hooks.setdefault('UserPromptSubmit', [])
ups.append({
    'matcher': '',
    'hooks': [{
        'type': 'command',
        'command': 'bash $BRAIN_DIR/hooks/memory-inject.sh'
    }]
})

with open('$SETTINGS_FILE', 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')
"
        echo "[4/4] Hook installed in ${SETTINGS_FILE}"
    fi
else
    echo "[4/4] No settings.json found at ${SETTINGS_FILE}"
    echo "      Create it or add the hook manually:"
    echo '      {"hooks":{"UserPromptSubmit":[{"matcher":"","hooks":[{"type":"command","command":"bash '"$BRAIN_DIR"'/hooks/memory-inject.sh"}]}]}}'
fi

echo ""
echo "Done. Restart Claude Code for the hook to take effect."
echo ""
echo "Usage:"
echo "  Ingest JSONL:    python3 ${BRAIN_DIR}/bin/ingest-jsonl.py --output ${FACTS_DIR}/events.jsonl ~/.claude/projects/.../session.jsonl"
echo "  Refine events:   python3 ${BRAIN_DIR}/bin/refine-sessions.py --events ${FACTS_DIR}/events.jsonl --facts ${FACTS_DIR}/facts.json --notes-dir ${FACTS_DIR}/sessions"
echo "  Review queue:    python3 ${BRAIN_DIR}/bin/review-promotions.py --facts ${FACTS_DIR}/facts.json --output ${FACTS_DIR}/review"
echo "  Obsidian vault:  python3 ${BRAIN_DIR}/bin/export-obsidian.py --facts ${FACTS_DIR}/facts.json --sessions ${FACTS_DIR}/sessions --review ${FACTS_DIR}/review --vault ${FACTS_DIR}/vault"
echo "  Graph export:    python3 ${BRAIN_DIR}/bin/export-graph.py --facts ${FACTS_DIR}/facts.json --output ${FACTS_DIR}/graph.json"
echo "  Health report:   python3 ${BRAIN_DIR}/bin/nockbrain-health.py --events ${FACTS_DIR}/events.jsonl --facts ${FACTS_DIR}/facts.json --notes-dir ${FACTS_DIR}/sessions"
echo "  Extract facts:   python3 ${BRAIN_DIR}/bin/extract-facts.py"
echo "  Query facts:     python3 ${BRAIN_DIR}/bin/query-facts.py 'your query'"
echo "  Budget recall:   python3 ${BRAIN_DIR}/bin/budget-recall.py 'your query'"
echo "  Test classifier: python3 ${BRAIN_DIR}/bin/recall-classifier.py --test"
