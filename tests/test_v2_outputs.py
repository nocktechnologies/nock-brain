"""Tests for v2 review/export/health tools."""
import json


def fact(content="[DIRECTIVE] Kevin wants stable memory rules in AGENTS.md", line=5):
    return {
        "id": "fact-1",
        "kind": "directive",
        "scope": "global",
        "status": "current",
        "confidence": 0.9,
        "content": content,
        "source_file": "session.jsonl",
        "source_date": "2026-06-11",
        "session": "s1",
        "session_anchor": "/tmp/session.jsonl:5",
        "created_at": "2026-06-11T00:00:00Z",
        "last_seen_at": "2026-06-11T00:00:00Z",
        "subject": "user",
        "evidence": [{"event_id": "event-5", "path": "/tmp/session.jsonl", "line": line}],
    }


def test_review_candidates_are_human_gated(review_promotions):
    candidates = review_promotions.candidates_from_facts([fact()])

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["status"] == "pending"
    assert candidate["proposed_target"] == "AGENTS.md"
    assert candidate["actions"] == ["approve", "edit", "reject", "defer"]
    assert candidate["risk_level"] in {"low", "medium", "high"}
    assert candidate["evidence"] == fact()["evidence"]


def test_review_cli_writes_json_and_markdown(review_promotions, tmp_path):
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([fact()]))
    output_dir = tmp_path / "review"

    code = review_promotions.run(["--facts", str(facts_file), "--output", str(output_dir)])

    assert code == 0
    data = json.loads((output_dir / "promotion-candidates.json").read_text())
    assert data[0]["proposed_target"] == "AGENTS.md"
    assert "approve" in (output_dir / "promotion-candidates.md").read_text()


def test_obsidian_export_writes_vault(export_obsidian, tmp_path):
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([fact("[DECISION] Kevin chose NockBrain v2", line=9)]))
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "s1.md").write_text("# Session s1\n")
    vault = tmp_path / "vault"

    code = export_obsidian.run([
        "--facts", str(facts_file),
        "--sessions", str(sessions_dir),
        "--vault", str(vault),
    ])

    assert code == 0
    assert (vault / "index.md").exists()
    assert (vault / "sessions" / "s1.md").exists()
    fact_notes = list((vault / "facts").glob("*.md"))
    assert len(fact_notes) == 1
    assert "session.jsonl:9" in fact_notes[0].read_text()


def test_graph_export_contains_stable_nodes_and_edges(export_graph, tmp_path):
    graph = export_graph.graph_from_facts([fact("[BUG] Found and fixed parser bug", line=2)])

    node_ids = {node["id"] for node in graph["nodes"]}
    edge_types = {edge["type"] for edge in graph["edges"]}
    assert "fact:fact-1" in node_ids
    assert "session:s1" in node_ids
    assert "source:session.jsonl" in node_ids
    assert {"DERIVED_FROM", "SUPPORTS", "MENTIONS"} <= edge_types

    out = tmp_path / "graph.json"
    code = export_graph.run(["--facts", str(tmp_path / "missing.json"), "--output", str(out)])
    assert code == 1


def test_graph_cli_writes_graph(export_graph, tmp_path):
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([fact()]))
    out = tmp_path / "graph.json"

    code = export_graph.run(["--facts", str(facts_file), "--output", str(out)])

    assert code == 0
    graph = json.loads(out.read_text())
    assert graph["format"] == "graphify-compatible"
    assert graph["nodes"]
    assert graph["edges"]


def test_health_report_counts_privacy_and_readiness(nockbrain_health, tmp_path):
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        json.dumps({
            "id": "event-1",
            "privacy": {"scrubbed": True, "excluded": False},
            "content": "[REDACTED_SECRET]",
        }) + "\n"
    )
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([fact()]))
    notes_dir = tmp_path / "sessions"
    notes_dir.mkdir()
    (notes_dir / "s1.md").write_text("# Session s1\n")
    stats_file = tmp_path / "stats.json"
    stats_file.write_text(json.dumps({"denied_results": 2}))

    report = nockbrain_health.build_report(events_file, facts_file, notes_dir, stats_file)

    assert report["events"]["count"] == 1
    assert report["privacy"]["scrubbed_events"] == 1
    assert report["privacy"]["denied_results"] == 2
    assert report["facts"]["count"] == 1
    assert report["notes"]["count"] == 1
    assert report["recall_ready"] is True


def test_health_live_value_scan_reports_secret_hits_without_values(nockbrain_health, tmp_path):
    env_file = tmp_path / ".env"
    live_value = "live-secret-value-that-should-not-appear-in-report"
    env_file.write_text(f"NOCKCC_API_KEY={live_value}\nPUBLIC_FLAG=true\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "events.jsonl").write_text(f"tool output leaked {live_value}\n")

    report = nockbrain_health.build_report(env_paths=[env_file], scan_roots=[artifacts])
    dumped = json.dumps(report)

    assert report["privacy"]["live_secret_findings"] == 1
    assert report["privacy"]["live_secret_locations"] == [
        {"path": str(artifacts / "events.jsonl"), "line": 1, "key": "NOCKCC_API_KEY"}
    ]
    assert live_value not in dumped
