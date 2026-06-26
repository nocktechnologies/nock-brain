"""N8392-A: the JSONL->facts path must not mint facts out of raw tool I/O or
bus/dump artifacts.

A nightly cron runs refine-sessions.fact_from_event() over EVERY sanitized
event, including raw tool_use.input JSON and raw bus-dump message text. The
inferred 'merge' pattern fired on "PR #6 merged" text buried inside those
blobs and minted them as 0.85-confidence facts (~19% of the store). These tests
lock in that those raw artifacts are dropped while genuine facts still mint.
"""


def event(
    content,
    kind="message",
    surface="text",
    line=7,
    timestamp="2026-06-11T05:00:00Z",
    actor="user",
):
    return {
        "id": f"event-{line}",
        "source": {
            "adapter": "claude-jsonl",
            "path": "/Users/kevin/.claude/projects/demo/session.jsonl",
            "line": line,
            "session_id": "s1",
            "timestamp": timestamp,
        },
        "actor": actor,
        "surface": surface,
        "kind": kind,
        "content": content,
        "metadata": {},
        "privacy": {"scrubbed": False, "excluded": False, "policy_version": "v1"},
    }


# (a) raw tool_use.input command JSON containing "PR #6 merged" -> no fact.
def test_tool_use_input_command_json_mints_no_fact(refine_sessions):
    raw = (
        '{"command":"CRM_AGENT_NAME=mira bash '
        '../../core/bus/send-telegram.sh 8663043855 PR #6 merged"}'
    )
    fact = refine_sessions.fact_from_event(
        event(raw, kind="tool_call", surface="tool_use.input", actor="assistant")
    )
    assert fact is None
    assert refine_sessions.facts_from_events([
        event(raw, kind="tool_call", surface="tool_use.input", actor="assistant")
    ]) == []


# (b) bus-dump message text starting '=== AGENT MESSAGE' -> no fact, even though
# it arrives as a plain message/text event and dodges the surface check.
def test_bus_dump_message_text_mints_no_fact(refine_sessions):
    raw = (
        "=== AGENT MESSAGE from codex-fit ===\n"
        "Heads up: N8031 PR #6 merged to main, please rebase."
    )
    fact = refine_sessions.fact_from_event(event(raw, kind="message", surface="text"))
    assert fact is None
    assert refine_sessions.facts_from_events([
        event(raw, kind="message", surface="text")
    ]) == []


# (c) a genuine tagged decision in a message/text event still mints a fact.
def test_genuine_tagged_decision_still_mints(refine_sessions):
    fact = refine_sessions.fact_from_event(
        event("[DECISION] Kevin approved the pricing model", kind="message", surface="text")
    )
    assert fact is not None
    assert fact["kind"] == "decision"


# (d) genuine prose (no tag) still mints a fact.
def test_genuine_prose_decision_still_mints(refine_sessions):
    fact = refine_sessions.fact_from_event(
        event("Kevin decided to ship the consumer tier", kind="message", surface="text")
    )
    assert fact is not None
    assert fact["kind"] == "decision"


# (e) is_structural_noise unit checks: noise prefixes are flagged, genuine facts
# (including a [CORRECTION] that contains 'CRM_AGENT_NAME=' as a substring) are
# spared, and empty/whitespace input is not noise.
def test_is_structural_noise_flags_raw_artifacts(scrub):
    assert scrub.is_structural_noise('{"command":"echo hi"}') is True
    assert scrub.is_structural_noise('[{"role":"user"}]') is True
    assert scrub.is_structural_noise("=== AGENT MESSAGE from x ===\nbody") is True
    assert scrub.is_structural_noise("=== TELEGRAM from kevin ===\nhi") is True
    assert scrub.is_structural_noise("=== SYSTEM ===\nboot") is True
    assert scrub.is_structural_noise("=== anything generic ===") is True
    assert scrub.is_structural_noise("```python\nprint()\n```") is True
    assert scrub.is_structural_noise("<command-name>foo</command-name>") is True
    assert scrub.is_structural_noise("12\thello world line dump") is True
    # leading whitespace is stripped before the prefix check
    assert scrub.is_structural_noise('   {"command":"x"}') is True


# (f) N8392-A hardening: longer '=====' dump banners + raw harness-notification
# blobs are also caught (coverage gaps the adversarial review found).
def test_is_structural_noise_flags_extended_dump_families(scrub):
    assert scrub.is_structural_noise("===== #8129 FULL =====\nbody") is True
    assert scrub.is_structural_noise("==================== Nock #8085") is True
    assert scrub.is_structural_noise("===== N8322 =====") is True
    assert scrub.is_structural_noise("<task-notification>\n<task-id>abc</task-id>") is True
    assert scrub.is_structural_noise("<system-reminder>\nbackground context") is True


# (g) the [TAG] escape hatch: a genuine tagged fact is spared even if its body
# later contains a dump-like '===' line, and JSON-array noise is NOT spared by it.
def test_genuine_tag_escape_hatch(scrub):
    assert scrub.is_structural_noise("[INSIGHT] recurring lesson === see prior notes ===") is False
    assert scrub.is_structural_noise("[ARCHITECTURE] split the bus === before === after") is False
    # '[{"' starts with '[' but is JSON-array noise, NOT a [TAG] — still caught.
    assert scrub.is_structural_noise('[{"text":"x"}]') is True


def test_is_structural_noise_spares_genuine_facts(scrub):
    spared = [
        "[CORRECTION] fixed boot-health-probe.sh diary surface bug: "
        "CRM_AGENT_NAME=mira was passing author_surface=mara-nockos",
        "[DECISION] Kevin approved the pricing model",
        "[DIRECTIVE] Keep memory recall under 800 tokens",
        "Kevin decided to ship the consumer tier",
        "PR #6 merged after Kevin's review",  # genuine prose, not a dump prefix
    ]
    for content in spared:
        assert scrub.is_structural_noise(content) is False


def test_is_structural_noise_handles_empty_and_whitespace(scrub):
    assert scrub.is_structural_noise("") is False
    assert scrub.is_structural_noise("   ") is False
    assert scrub.is_structural_noise("\n\t  \n") is False
