#!/usr/bin/env python3
"""Rebuild and atomically promote the live NockBrain store in one command (N8070).

The proven v2 chain (ingest -> refine -> review -> health -> sign -> export)
ships as separate hand-run CLIs in ``bin/``. Running them by hand, in order, is
why the live store silently rotted to a stale v1 snapshot: a missed step left
recall dead until a manual rebuild. This command runs the whole chain into a
STAGING directory, applies a HARD health gate, signs, exports, and only then
atomically promotes the result into the live store -- backing up what was there.

Safety properties (in priority order):
  1. The live store is NEVER written until the staging build passes the health
     gate AND is signed AND exported. A staging store with live-secret findings
     or that is not recall-ready ABORTS with a non-zero exit and leaves the live
     store completely untouched.
  2. Ingest always lands in staging, never directly in the live store.
  3. Promotion is backup-then-swap: existing live artifacts are copied to
     timestamped ``.bak`` paths before staging artifacts move into place.
  4. ``--dry-run`` builds + gates + signs + exports into staging but performs no
     swap, so it can never alter the live store.

Usage:
    python3 bin/rebuild-store.py                     # build + promote
    python3 bin/rebuild-store.py --dry-run           # build + gate, no swap
    python3 bin/rebuild-store.py --since 14          # 14-day transcript window
    python3 bin/rebuild-store.py --source ~/.claude/projects --source /other
    python3 bin/rebuild-store.py --print-schedule     # emit example launchd plist
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _store import secure_copyfile, secure_mkdir  # noqa: E402

DEFAULT_STORE_DIR = Path.home() / ".nock-brain"
DEFAULT_SOURCE_ROOTS = [Path.home() / ".claude" / "projects"]
DEFAULT_SINCE_DAYS = 7

# Live artifacts that get backed up before a swap and replaced on promote.
# Each is (name-in-store, staging-source-name). Some live names differ only by
# being a file vs directory; staging mirrors the same layout.
PROMOTE_ARTIFACTS = ["facts.json", "sessions", "review", "vault", "graph.json"]


class RebuildError(RuntimeError):
    """Raised to abort the rebuild with a clear operator-facing message."""


# --- subprocess plumbing ---------------------------------------------------

def _run_cli(script: str, args: list[str]) -> subprocess.CompletedProcess:
    """Run a sibling bin/ CLI, streaming nothing, capturing output.

    Raises RebuildError on non-zero exit with the captured stderr/stdout so the
    operator sees exactly which stage failed and why.
    """
    cmd = [sys.executable, str(BIN_DIR / script), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RebuildError(f"{script} failed (exit {proc.returncode}): {detail}")
    return proc


# --- transcript discovery --------------------------------------------------

def discover_transcripts(source_roots: list[Path], since_days: int) -> list[Path]:
    """Find *.jsonl transcripts under the source roots modified within the window.

    The underlying ingest CLI has no time filter, so the window is applied here
    by file mtime. ``since_days <= 0`` means "no window" (take everything).
    """
    cutoff = 0.0
    if since_days > 0:
        cutoff = time.time() - since_days * 86400
    found: list[Path] = []
    seen: set[Path] = set()
    for root in source_roots:
        root = root.expanduser()
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else sorted(root.rglob("*.jsonl"))
        for path in candidates:
            if not path.is_file() or path.suffix != ".jsonl":
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(path)
    return found


# --- staging build steps ---------------------------------------------------

def stage_paths(staging_dir: Path) -> dict[str, Path]:
    return {
        "events": staging_dir / "events.jsonl",
        "ingest_stats": staging_dir / "ingest-stats.json",
        "facts": staging_dir / "facts.json",
        "sessions": staging_dir / "sessions",
        "review": staging_dir / "review",
        "vault": staging_dir / "vault",
        "graph": staging_dir / "graph.json",
    }


def build_staging(
    staging_dir: Path,
    transcripts: list[Path],
    *,
    key_path: Path,
    pub_path: Path,
) -> dict[str, Any]:
    """Run ingest -> refine -> review -> health into ``staging_dir``.

    Returns a dict with the health report and counts. Does NOT sign/export here;
    the caller signs + exports only after the health gate passes so a failing
    gate does no wasted (or misleading) signing work.
    """
    secure_mkdir(staging_dir)
    sp = stage_paths(staging_dir)

    # 1. Ingest recent transcripts into staging events (NEVER the live store).
    ingest_args = ["--output", str(sp["events"])]
    ingest_args.extend(str(p) for p in transcripts)
    ingest_proc = _run_cli("ingest-jsonl.py", ingest_args)
    ingest_report = json.loads(ingest_proc.stdout) if ingest_proc.stdout.strip() else {}
    ingest_stats = ingest_report.get("stats", {})
    # Persist stats so the health scan can read denied_* counters.
    sp["ingest_stats"].write_text(
        json.dumps(ingest_stats, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 2a. Refine events -> staging facts + session notes.
    _run_cli(
        "refine-sessions.py",
        [
            "--events", str(sp["events"]),
            "--facts", str(sp["facts"]),
            "--notes-dir", str(sp["sessions"]),
        ],
    )

    # 2b. Review promotions -> staging review queue.
    _run_cli(
        "review-promotions.py",
        ["--facts", str(sp["facts"]), "--output", str(sp["review"])],
    )

    # 3. Health on staging (JSON). This is the gate input.
    health_proc = _run_cli(
        "nockbrain-health.py",
        [
            "--facts", str(sp["facts"]),
            "--notes-dir", str(sp["sessions"]),
            "--stats", str(sp["ingest_stats"]),
            "--json",
        ],
    )
    health = json.loads(health_proc.stdout)

    return {
        "health": health,
        "ingest_stats": ingest_stats,
        "stage_paths": sp,
        "key_path": key_path,
        "pub_path": pub_path,
    }


def health_gate(health: dict[str, Any]) -> None:
    """HARD GATE: abort unless staging is clean and recall-ready.

    Abort condition (either is fatal):
      * privacy.live_secret_findings > 0  -> secrets would be promoted
      * recall_ready is not True          -> store has no usable facts / is malformed

    Raises RebuildError (caught in main -> non-zero exit) leaving live untouched.
    """
    findings = int(health.get("privacy", {}).get("live_secret_findings", 0))
    recall_ready = health.get("recall_ready", False)
    if findings > 0:
        locations = health.get("privacy", {}).get("live_secret_locations", [])
        raise RebuildError(
            f"HEALTH GATE FAILED: {findings} live-secret finding(s) in staging "
            f"store; refusing to promote. Locations: {locations}"
        )
    if recall_ready is not True:
        fact_count = health.get("facts", {}).get("count", 0)
        malformed = health.get("facts", {}).get("malformed", [])
        raise RebuildError(
            "HEALTH GATE FAILED: staging store is not recall-ready "
            f"(facts={fact_count}, malformed={len(malformed)}); refusing to promote."
        )


def sign_and_export(build: dict[str, Any]) -> None:
    """Sign staging facts in place, then export Obsidian vault + graph from staging."""
    sp = build["stage_paths"]

    # 4. Sign staging facts.json in place with the live key (Ed25519).
    _run_cli(
        "sign-facts.py",
        [
            "--facts", str(sp["facts"]),
            "--key", str(build["key_path"]),
            "--pub", str(build["pub_path"]),
        ],
    )

    # 5. Export Obsidian vault + graph from the signed staging facts.
    _run_cli(
        "export-obsidian.py",
        [
            "--facts", str(sp["facts"]),
            "--sessions", str(sp["sessions"]),
            "--review", str(sp["review"]),
            "--vault", str(sp["vault"]),
        ],
    )
    _run_cli(
        "export-graph.py",
        ["--facts", str(sp["facts"]), "--output", str(sp["graph"])],
    )


# --- atomic promote --------------------------------------------------------

def _backup_path(live_path: Path, stamp: str) -> Path:
    return live_path.with_name(f"{live_path.name}.bak-{stamp}")


def _move_into_place(staging_src: Path, live_dst: Path) -> None:
    """Replace live_dst with staging_src (already-backed-up). Handles file or dir."""
    if live_dst.exists() or live_dst.is_symlink():
        if live_dst.is_dir() and not live_dst.is_symlink():
            shutil.rmtree(live_dst)
        else:
            live_dst.unlink()
    secure_mkdir(live_dst.parent)
    # shutil.move handles cross-device; staging may be a tempdir on another fs.
    shutil.move(str(staging_src), str(live_dst))


def promote(build: dict[str, Any], store_dir: Path) -> dict[str, Any]:
    """Back up current live artifacts to timestamped .bak paths, then swap in staging.

    Returns a summary of {backed_up: [...], promoted: [...]}.
    """
    sp = build["stage_paths"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    secure_mkdir(store_dir)

    staging_for = {
        "facts.json": sp["facts"],
        "sessions": sp["sessions"],
        "review": sp["review"],
        "vault": sp["vault"],
        "graph.json": sp["graph"],
    }

    backed_up: list[str] = []
    # Back up everything that currently exists FIRST, so a mid-swap failure still
    # leaves a recoverable snapshot of the prior live store.
    for name in PROMOTE_ARTIFACTS:
        live_path = store_dir / name
        if live_path.exists():
            bak = _backup_path(live_path, stamp)
            if live_path.is_dir():
                shutil.copytree(live_path, bak)
            else:
                secure_copyfile(live_path, bak)
            backed_up.append(str(bak))

    promoted: list[str] = []
    for name in PROMOTE_ARTIFACTS:
        src = staging_for[name]
        if not src.exists():
            continue
        _move_into_place(src, store_dir / name)
        promoted.append(name)

    return {"stamp": stamp, "backed_up": backed_up, "promoted": promoted}


# --- summary / schedule ----------------------------------------------------

def render_summary(
    *,
    transcripts: int,
    health: dict[str, Any],
    dry_run: bool,
    promote_result: dict[str, Any] | None,
) -> str:
    findings = health.get("privacy", {}).get("live_secret_findings", 0)
    lines = [
        "NockBrain rebuild summary",
        f"- Transcripts ingested: {transcripts}",
        f"- Facts (staging): {health.get('facts', {}).get('count', 0)}",
        f"- Notes (staging): {health.get('notes', {}).get('count', 0)}",
        f"- Live-secret findings: {findings}",
        f"- Recall ready: {str(health.get('recall_ready', False)).lower()}",
    ]
    if dry_run:
        lines.append("- Mode: DRY RUN -- staging built + gated; live store NOT touched")
    elif promote_result is not None:
        lines.append(f"- Promoted: {', '.join(promote_result['promoted']) or '(none)'}")
        lines.append(f"- Backups ({promote_result['stamp']}): {len(promote_result['backed_up'])} artifact(s)")
    return "\n".join(lines) + "\n"


def example_plist(store_dir: Path) -> str:
    script = BIN_DIR / "rebuild-store.py"
    return f"""<!-- Example weekly launchd job (NOT installed). Save to
     ~/Library/LaunchAgents/io.nocktechnologies.nockbrain-rebuild.plist
     then: launchctl load <that path>. Runs Sundays at 03:30 local. -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.nocktechnologies.nockbrain-rebuild</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{script}</string>
    <string>--since</string><string>14</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>30</integer>
  </dict>
  <key>StandardOutPath</key><string>{store_dir}/rebuild.log</string>
  <key>StandardErrorPath</key><string>{store_dir}/rebuild.err</string>
</dict>
</plist>

# Or as a weekly cron line (crontab -e):
30 3 * * 0 {sys.executable} {script} --since 14 >> {store_dir}/rebuild.log 2>&1
"""


# --- orchestration ---------------------------------------------------------

def rebuild(
    *,
    store_dir: Path = DEFAULT_STORE_DIR,
    source_roots: list[Path] | None = None,
    since_days: int = DEFAULT_SINCE_DAYS,
    dry_run: bool = False,
    staging_dir: Path | None = None,
    key_path: Path | None = None,
    pub_path: Path | None = None,
) -> dict[str, Any]:
    """End-to-end rebuild. Returns a result dict; raises RebuildError on gate/stage failure.

    The live store is only mutated by promote(), which is skipped on dry_run and
    never reached if the health gate raises.
    """
    source_roots = source_roots or list(DEFAULT_SOURCE_ROOTS)
    key_path = key_path or (store_dir / "signing-key")
    pub_path = pub_path or (store_dir / "signing-key.pub")

    transcripts = discover_transcripts(source_roots, since_days)
    if not transcripts:
        raise RebuildError(
            f"No transcripts found under {[str(r) for r in source_roots]} "
            f"within {since_days} day(s); refusing to build an empty store."
        )

    owns_staging = staging_dir is None
    if owns_staging:
        staging_dir = Path(tempfile.mkdtemp(prefix="nockbrain-staging-"))
    else:
        secure_mkdir(staging_dir)

    try:
        build = build_staging(
            staging_dir, transcripts, key_path=key_path, pub_path=pub_path
        )
        # HARD GATE before any signing/export work or any live write.
        health_gate(build["health"])
        sign_and_export(build)

        promote_result = None
        if not dry_run:
            promote_result = promote(build, store_dir)

        summary = render_summary(
            transcripts=len(transcripts),
            health=build["health"],
            dry_run=dry_run,
            promote_result=promote_result,
        )
        return {
            "health": build["health"],
            "transcripts": len(transcripts),
            "dry_run": dry_run,
            "promote": promote_result,
            "summary": summary,
            "staging_dir": str(staging_dir),
        }
    finally:
        # Clean up only staging we created; a caller-supplied dir is theirs.
        if owns_staging:
            shutil.rmtree(staging_dir, ignore_errors=True)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild + atomically promote the live NockBrain store (N8070)"
    )
    parser.add_argument(
        "--store-dir", type=Path, default=DEFAULT_STORE_DIR,
        help="Live store directory (default ~/.nock-brain)",
    )
    parser.add_argument(
        "--source", action="append", type=Path, default=None,
        help="Transcript source root (repeatable; default ~/.claude/projects)",
    )
    parser.add_argument(
        "--since", type=int, default=DEFAULT_SINCE_DAYS,
        help="Ingest transcripts modified within N days (0 = no window; default 7)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build + health-gate + sign + export into staging, but do NOT promote",
    )
    parser.add_argument(
        "--staging-dir", type=Path, default=None,
        help="Explicit staging directory (default: a fresh tempdir)",
    )
    parser.add_argument(
        "--print-schedule", action="store_true",
        help="Print an example weekly launchd plist + cron line and exit (installs nothing)",
    )
    args = parser.parse_args(argv)

    if args.print_schedule:
        print(example_plist(args.store_dir.expanduser()))
        return 0

    try:
        result = rebuild(
            store_dir=args.store_dir.expanduser(),
            source_roots=args.source,
            since_days=args.since,
            dry_run=args.dry_run,
            staging_dir=args.staging_dir,
        )
    except RebuildError as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 1

    print(result["summary"], end="")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
