#!/usr/bin/env python3
"""Report local NockBrain store health."""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import REQUIRED_FACT_FIELDS

SENSITIVE_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
SENSITIVE_ENV_NAMES = {"API_KEY", "TOKEN", "SECRET", "PASSWORD"}


def load_json(path: Path | None, default: Any) -> Any:
    if not path or not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_events(path: Path | None) -> tuple[list[dict[str, Any]], int]:
    if not path or not path.exists():
        return [], 0
    events = []
    malformed = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                malformed += 1
    return events, malformed


def load_degradations(path: Path | None, *, now: Any = None,
                      recent_hours: float = 24.0) -> dict[str, Any]:
    """Aggregate recall-degradation events (recall-degradations.jsonl).

    A degraded read is visible on stderr, but stderr nobody watches is still a
    silent outage — this turns the events into numbers a health check can flag
    on: total, count in the recent window, and the latest timestamp."""
    from datetime import datetime, timedelta, timezone

    total = 0
    recent = 0
    last_at = ""
    if path and path.exists():
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=recent_hours)
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                stamp = str(event.get("at", ""))
            except (json.JSONDecodeError, AttributeError):
                continue
            total += 1
            if stamp > last_at:
                last_at = stamp
            try:
                at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                if at >= cutoff:
                    recent += 1
            except ValueError:
                continue
    return {"path": str(path) if path else "", "total": total,
            "recent_24h": recent, "last_at": last_at}


def contradiction_queue_health(path: Path | None, *, max_age_h: float = 26.0,
                               now: Any = None) -> dict[str, Any]:
    """Freshness of the nightly contradiction queue.

    The F2 failure mode: the schedule fires, the artifact never appears, and
    nothing alarms — the queue just quietly stays absent or ages out. This
    turns that silence into numbers: exists, age_hours, and stale (missing or
    older than one nightly cycle + slack)."""
    from datetime import datetime, timezone

    exists = bool(path and path.exists())
    age_h = None
    generated_at = ""
    if exists:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            generated_at = str(doc.get("generated_at", ""))
        except (OSError, json.JSONDecodeError):
            pass
        now = now or datetime.now(timezone.utc)
        try:
            stamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age_h = (now - stamp).total_seconds() / 3600.0
        except ValueError:
            age_h = None
    stale = (not exists) or age_h is None or age_h > max_age_h
    return {"path": str(path) if path else "", "exists": exists,
            "generated_at": generated_at,
            "age_hours": round(age_h, 1) if age_h is not None else None,
            "max_age_hours": max_age_h, "stale": stale}


def malformed_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bad = []
    for fact in facts:
        missing = sorted(field for field in REQUIRED_FACT_FIELDS if field not in fact)
        if missing:
            bad.append({"id": fact.get("id", ""), "missing": missing})
    return bad


def note_count(notes_dir: Path | None) -> int:
    if not notes_dir or not notes_dir.exists():
        return 0
    return len(list(notes_dir.glob("*.md")))


def is_sensitive_env_key(key: str) -> bool:
    normalized = key.strip().upper()
    return normalized in SENSITIVE_ENV_NAMES or normalized.endswith(SENSITIVE_ENV_SUFFIXES)


def clean_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def load_env_secret_values(env_paths: list[Path] | None) -> dict[str, str]:
    secrets: dict[str, str] = {}
    for env_path in env_paths or []:
        if not env_path.exists():
            continue
        with env_path.open(encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                key = key.strip()
                value = clean_env_value(value)
                if is_sensitive_env_key(key) and len(value) >= 8:
                    secrets[key] = value
    return secrets


def iter_scan_files(scan_roots: list[Path] | None) -> list[Path]:
    files: list[Path] = []
    for root in scan_roots or []:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(path for path in sorted(root.rglob("*")) if path.is_file())
    return files


def scan_live_secret_values(env_paths: list[Path] | None, scan_roots: list[Path] | None) -> list[dict[str, Any]]:
    secrets = load_env_secret_values(env_paths)
    if not secrets:
        return []

    findings: list[dict[str, Any]] = []
    for path in iter_scan_files(scan_roots):
        try:
            with path.open(encoding="utf-8", errors="ignore") as handle:
                for line_number, line in enumerate(handle, start=1):
                    for key, value in secrets.items():
                        if value in line:
                            findings.append({"path": str(path), "line": line_number, "key": key})
        except OSError:
            continue
    return findings


def build_report(
    events_path: Path | None = None,
    facts_path: Path | None = None,
    notes_dir: Path | None = None,
    stats_path: Path | None = None,
    env_paths: list[Path] | None = None,
    scan_roots: list[Path] | None = None,
    degradations_path: Path | None = None,
    degradation_threshold: int = 3,
    contradictions_path: Path | None = None,
    contradictions_max_age_h: float = 26.0,
) -> dict[str, Any]:
    events, malformed_event_lines = load_events(events_path)
    facts = load_json(facts_path, [])
    stats = load_json(stats_path, {}) if stats_path else {}
    bad_facts = malformed_facts(facts)
    live_secret_locations = scan_live_secret_values(env_paths, scan_roots)

    scrubbed_events = [event for event in events if event.get("privacy", {}).get("scrubbed")]
    excluded_events = [event for event in events if event.get("privacy", {}).get("excluded")]
    redacted_content = [event for event in events if "[REDACTED_SECRET]" in str(event.get("content", ""))]

    report = {
        "events": {
            "path": str(events_path) if events_path else "",
            "count": len(events),
            "malformed_lines": malformed_event_lines,
        },
        "facts": {
            "path": str(facts_path) if facts_path else "",
            "count": len(facts),
            "malformed": bad_facts,
        },
        "notes": {
            "path": str(notes_dir) if notes_dir else "",
            "count": note_count(notes_dir),
        },
        "privacy": {
            "scrubbed_events": len(scrubbed_events),
            "excluded_events": len(excluded_events),
            "redacted_content_events": len(redacted_content),
            "denied_paths": int(stats.get("denied_paths", 0)),
            "denied_tools": int(stats.get("denied_tools", 0)),
            "denied_endpoints": int(stats.get("denied_endpoints", 0)),
            "denied_results": int(stats.get("denied_results", 0)),
            "denied_result_paths": int(stats.get("denied_result_paths", 0)),
            "denied_result_endpoints": int(stats.get("denied_result_endpoints", 0)),
            "secrets_redacted": int(stats.get("secrets_redacted", 0)),
            "live_secret_findings": len(live_secret_locations),
            "live_secret_locations": live_secret_locations,
        },
        "recall_ready": bool(facts) and not bad_facts,
    }
    degradations = load_degradations(degradations_path)
    degradations["threshold"] = degradation_threshold
    # N recent degraded reads = fleet memory silently blind on every recall.
    degradations["flagged"] = degradations["recent_24h"] >= degradation_threshold
    report["recall_degradations"] = degradations
    if contradictions_path is not None:
        report["contradiction_queue"] = contradiction_queue_health(
            contradictions_path, max_age_h=contradictions_max_age_h)
    return report


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "NockBrain health",
        f"- Events: {report['events']['count']} ({report['events']['malformed_lines']} malformed lines)",
        f"- Facts: {report['facts']['count']} ({len(report['facts']['malformed'])} malformed)",
        f"- Notes: {report['notes']['count']}",
        (
            "- Privacy: "
            f"{report['privacy']['scrubbed_events']} scrubbed events, "
            f"{report['privacy']['denied_paths']} denied paths, "
            f"{report['privacy']['denied_tools']} denied tools, "
            f"{report['privacy']['denied_endpoints']} denied endpoints, "
            f"{report['privacy']['denied_results']} denied results, "
            f"{report['privacy']['live_secret_findings']} live secret findings"
        ),
        f"- Recall ready: {str(report['recall_ready']).lower()}",
    ]
    degradations = report.get("recall_degradations", {})
    if degradations.get("flagged"):
        lines.append(
            f"- RECALL DEGRADED: {degradations['recent_24h']} degraded read(s) in 24h "
            f"(threshold {degradations['threshold']}, last {degradations['last_at']})"
        )
    else:
        lines.append(
            f"- Recall degradations (24h): {degradations.get('recent_24h', 0)}"
        )
    queue = report.get("contradiction_queue")
    if queue is not None:
        if queue["stale"]:
            lines.append(
                "- CONTRADICTION QUEUE STALE: "
                + ("missing" if not queue["exists"]
                   else f"age {queue['age_hours']}h > {queue['max_age_hours']}h")
                + " — the nightly is firing without producing output"
            )
        else:
            lines.append(
                f"- Contradiction queue: fresh ({queue['age_hours']}h old)"
            )
    return "\n".join(lines) + "\n"


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report NockBrain local store health")
    parser.add_argument("--events", type=Path, default=None)
    parser.add_argument("--facts", type=Path, default=Path.home() / ".nock-brain" / "facts.json")
    parser.add_argument("--notes-dir", type=Path, default=Path.home() / ".nock-brain" / "sessions")
    parser.add_argument("--stats", type=Path, default=None)
    parser.add_argument("--env-file", action="append", type=Path, default=[])
    parser.add_argument("--scan-root", action="append", type=Path, default=[])
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text")
    parser.add_argument("--degradations", type=Path, default=None,
                        help="recall-degradations.jsonl (default: next to --facts)")
    parser.add_argument("--degradation-threshold", type=int, default=3,
                        help="recent degraded reads that flag RECALL DEGRADED")
    parser.add_argument("--expect-contradictions", action="store_true",
                        help="flag when the nightly contradiction queue is "
                             "missing or older than --contradictions-max-age-h")
    parser.add_argument("--contradictions", type=Path, default=None,
                        help="contradiction-candidates.json (default: "
                             "<facts dir>/review/contradiction-candidates.json)")
    parser.add_argument("--contradictions-max-age-h", type=float, default=26.0)
    args = parser.parse_args(argv)

    degradations = args.degradations or args.facts.parent / "recall-degradations.jsonl"
    contradictions = None
    if args.expect_contradictions:
        contradictions = args.contradictions or (
            args.facts.parent / "review" / "contradiction-candidates.json")
    report = build_report(args.events, args.facts, args.notes_dir, args.stats,
                          args.env_file, args.scan_root,
                          degradations_path=degradations,
                          degradation_threshold=args.degradation_threshold,
                          contradictions_path=contradictions,
                          contradictions_max_age_h=args.contradictions_max_age_h)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_text(report), end="")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
