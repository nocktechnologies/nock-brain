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
    # User-context queries (previously a documented gap, now matched).
    ("what does the founder want for the launch", True),
    ("what did kevin want us to prioritize", True),
    ("what was their direction on pricing", True),
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


def test_user_context_natural_phrasing(classifier):
    # Previously a documented gap (the regex required specific verb forms);
    # now natural user-context questions are matched.
    recall, reason, _cats = classifier.classify("what does the founder want for the launch")
    assert recall is True
    assert "user_context" in reason


def test_broadened_user_context_does_not_overfire_on_operational(classifier):
    # Broadening user-context matching must NOT start firing on operational prompts.
    for prompt in ("merge PR 5", "dispatch the agent", "write a function", "run the tests"):
        recall, _reason, _ = classifier.classify(prompt)
        assert recall is False, f"{prompt!r} should not trigger recall"
