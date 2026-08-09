#!/usr/bin/env python3
"""Export NockBrain facts as a Graphify-compatible conversation-memory graph."""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _store import (
    secure_mkdir,
    secure_write_json,
    secure_write_text,
    secure_write_text_verified,
)

STOPWORDS = {
    "about", "after", "also", "code", "fact", "fixed", "found", "from", "into",
    "kevin", "nockbrain", "should", "that", "the", "this", "user", "with",
}


def load_facts(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def node(node_id: str, node_type: str, label: str, **props) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "label": label, **props}


def edge(source: str, target: str, edge_type: str, **props) -> dict[str, Any]:
    return {"id": f"{source}->{edge_type}->{target}", "source": source, "target": target, "type": edge_type, **props}


def concepts(text: str) -> list[str]:
    terms = []
    for term in re.findall(r"[a-z0-9]{4,}", text.lower()):
        if term not in STOPWORDS and term not in terms:
            terms.append(term)
    return terms[:8]


def graph_from_facts(facts: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    def add_node(item: dict[str, Any]) -> None:
        nodes[item["id"]] = item

    def add_edge(item: dict[str, Any]) -> None:
        edges[item["id"]] = item

    for fact in facts:
        fact_id = f"fact:{fact.get('id', '')}"
        session_id = f"session:{fact.get('session', 'unknown') or 'unknown'}"
        source_name = fact.get("source_file", "unknown") or "unknown"
        source_id = f"source:{source_name}"

        add_node(node(
            fact_id,
            "fact",
            fact.get("content", "")[:120],
            kind=fact.get("kind", ""),
            status=fact.get("status", ""),
            confidence=fact.get("confidence", 0),
        ))
        add_node(node(session_id, "session", fact.get("session", "unknown") or "unknown"))
        add_node(node(source_id, "source", source_name))
        add_edge(edge(fact_id, source_id, "DERIVED_FROM"))
        add_edge(edge(session_id, fact_id, "SUPPORTS"))

        for concept in concepts(fact.get("content", "")):
            concept_id = f"concept:{concept}"
            add_node(node(concept_id, "concept", concept))
            add_edge(edge(fact_id, concept_id, "MENTIONS"))

    return {
        "format": "graphify-compatible",
        "schema_version": "nockbrain.graph.v1",
        "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
        "edges": sorted(edges.values(), key=lambda item: item["id"]),
    }


def write_projection_receipt(path: Path, artifacts: list[dict[str, Any]]) -> bool:
    """Write the S4 projection receipt (plain writer — never self-verifying).

    Returns True when every artifact passed readback verification.
    """
    all_verified = all(artifact.get("verified") for artifact in artifacts)
    secure_write_json(
        path,
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "artifacts": artifacts,
            "all_verified": all_verified,
        },
        indent=2,
    )
    return all_verified


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a Graphify-compatible memory graph")
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help="write a readback-verified projection receipt JSON here; "
        "exits non-zero if any artifact cannot be verified",
    )
    args = parser.parse_args(argv)

    if not args.facts.exists():
        print(f"Facts file not found: {args.facts}")
        return 1

    graph = graph_from_facts(load_facts(args.facts))
    payload = json.dumps(graph, indent=2, ensure_ascii=False)
    secure_mkdir(args.output.parent)

    if args.receipt is None:
        # No receipt requested: byte-identical to the historical export path.
        secure_write_text(args.output, payload, encoding="utf-8")
        print(f"Wrote graph with {len(graph['nodes'])} node(s) and {len(graph['edges'])} edge(s)")
        return 0

    artifacts = [secure_write_text_verified(args.output, payload, encoding="utf-8")]
    all_verified = write_projection_receipt(args.receipt, artifacts)
    print(f"Wrote graph with {len(graph['nodes'])} node(s) and {len(graph['edges'])} edge(s)")
    if not all_verified:
        ambiguous = [a["path"] for a in artifacts if not a.get("verified")]
        print(f"AMBIGUOUS: {len(ambiguous)} artifact(s) failed readback: {', '.join(ambiguous)}")
        return 1
    print(f"Receipt: {args.receipt} (all artifacts verified)")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
