"""Static checks for CI supply-chain hardening."""
import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
FULL_SHA = re.compile(r"@[0-9a-f]{40}\b")


def test_ci_actions_are_sha_pinned_and_security_scans_run():
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "actions/checkout@v4" not in workflow
    assert "actions/setup-python@v5" not in workflow
    assert "gitleaks/gitleaks-action@v2" not in workflow
    assert "gitleaks/gitleaks-action@" not in workflow
    assert len(FULL_SHA.findall(workflow)) >= 2
    assert "pytest==" in workflow
    assert "bandit==" in workflow
    assert "bandit -r bin" in workflow
    assert "go install github.com/zricethezav/gitleaks/v8@v8.30.1" in workflow
    assert "gitleaks detect --source ." in workflow


def test_dependabot_tracks_github_actions():
    config = (REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")

    assert 'package-ecosystem: "github-actions"' in config
    assert 'directory: "/"' in config
