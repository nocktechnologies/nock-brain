"""Tests for the recall classifier — the gate that decides whether a prompt
needs past-session context. It must fire on memory-seeking prompts and stay
quiet on operational ones (so it never adds latency or noise to normal work)."""
import pytest


# (prompt, expect_recall) — the embedded self-test cases plus edge cases.
RECALL_CASES = [
    ("what did we decide about pricing", True),
    ("have we seen this bug before", True),
    ("what happened with the auth migration", True),
    ("is the pricing plan still current", True),
    ("status of the demo", True),
    ("last time we deployed what broke", True),
    ("why did we choose Seatbelt over LD_PRELOAD", True),
]

SKIP_CASES = [
    ("yes", False),
    ("ok thanks", False),
    ("shoot", False),
    ("merge PR 223", False),
    ("dispatch the agent on the audit", False),
    ("review the open PRs", False),
    ("write a test for the heartbeat function", False),
    ("heartbeat check", False),
    ("", False),
    ("short", False),  # under the length floor
]


@pytest.mark.parametrize("prompt,expect", RECALL_CASES + SKIP_CASES)
def test_classify_decision(classifier, prompt, expect):
    recall, _reason, _cats = classifier.classify(prompt)
    assert recall is expect, f"{prompt!r} -> {recall}, expected {expect}"


def test_classify_returns_categories_on_match(classifier):
    recall, reason, cats = classifier.classify("what did we decide about pricing")
    assert recall is True
    assert cats, "a matched prompt should report at least one category"
    assert reason.startswith("matched:")


def test_skip_pattern_beats_trigger_words(classifier):
    # Starts with an operational verb -> skipped even though it mentions a PR.
    recall, reason, _ = classifier.classify("merge the PR we discussed last time")
    assert recall is False
    assert reason == "skip_pattern"


def test_embedded_self_test_still_passes(classifier):
    # The script ships its own smoke test; keep it honest from CI too.
    assert classifier.run_tests() is True


@pytest.mark.xfail(
    reason="Known gap: the regex USER_PATTERNS require specific verb forms "
    "(e.g. 'wants', 'said'), so natural user-context queries like this are missed. "
    "Tracked for the functional-improvement phase (broaden user-context matching).",
    strict=True,
)
def test_user_context_natural_phrasing_gap(classifier):
    recall, _reason, _cats = classifier.classify("what does the founder want for the launch")
    assert recall is True
