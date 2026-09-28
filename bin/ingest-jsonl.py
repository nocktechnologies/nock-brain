#!/usr/bin/env python3
"""Ingest raw Claude Code JSONL transcripts into sanitized evidence events.

This is the v2 entry point before fact extraction. It preserves source anchors
and treats tool_use inputs as first-class evidence, while denying private paths,
private tools/endpoints, and scrubbing secrets before events are returned or
written. A paired Bash transcribe.py result is the one tool-result exception:
its transcript body becomes a user message event after transcriber diagnostics
are removed.

Usage:
    python3 bin/ingest-jsonl.py ~/.claude/projects/.../session.jsonl
    python3 bin/ingest-jsonl.py --output ~/.nock-brain/events.jsonl session.jsonl
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _scrub import scrub_secrets
from _store import secure_write_text
from _channel_frame import CHANNEL_FRAME_RE

DEFAULT_PATH_DENYLIST = [
    "agents/*/private/**",
    "*/agents/*/private/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "*token*",
    "**/*token*",
    "*secret*",
    "**/*secret*",
    "credentials*",
    "**/credentials*",
    "id_rsa*",
    "**/id_rsa*",
    "*.pem",
    "**/*.pem",
]

DEFAULT_TOOL_DENYLIST = [
    "nockcc_diary_*",
    "*nockcc_diary_*",
    "nockcc_private_*",
    "*nockcc_private_*",
]

DEFAULT_ENDPOINT_DENYLIST = [
    "*/api/brain/diary/*",
    "*/api/brain/private/*",
]
TRANSCRIBE_DIAGNOSTIC_RE = re.compile(
    r"^(?:\[transcribe\] (?:DEEPGRAM_API_KEY not set|(?:file )?error:)|Usage: transcribe\.py(?:\s|$))"
)
# Shell substitutions or command separators can mix unrelated output into a result.
TRANSCRIBE_UNSAFE_COMMAND_RE = re.compile(r"\$\(|`|[<>]\(|[\r\n]")
TRANSCRIBE_UNSAFE_WORD_CHARS = frozenset("$`*?{}[]~\\")


def new_stats() -> dict[str, int]:
    return {
        "lines_read": 0,
        "events_written": 0,
        "sidechain_excluded": 0,
        "denied_paths": 0,
        "denied_tools": 0,
        "denied_endpoints": 0,
        "denied_results": 0,
        "denied_result_paths": 0,
        "denied_result_endpoints": 0,
        "secrets_redacted": 0,
        "malformed_lines": 0,
        "channel_wrappers_unwrapped": 0,
        "channel_wrappers_dropped": 0,
        "transcribe_results_promoted": 0,
        "transcribe_results_dropped": 0,
    }


def normalize_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content", "")
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": content or ""}]


def json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


_CHANNEL_FRAME_RE = CHANNEL_FRAME_RE
_CHANNEL_NAME_RE = re.compile(r"\bchannel=(?P<quote>[\"'])(?P<channel>[^\"']*)(?P=quote)")
_CHANNEL_TEXT_PATHS = (
    ("message", "text"),
    ("message", "caption"),
    ("telegram", "message", "text"),
    ("telegram", "message", "caption"),
    ("update", "message", "text"),
    ("update", "message", "caption"),
    ("body",),
    ("subject",),
    ("envelope", "body"),
    ("envelope", "subject"),
    ("attestation", "envelope", "body"),
    ("attestation", "envelope", "subject"),
    ("data", "body"),
    ("data", "subject"),
)


def _nested_text(value: object, path: tuple[str, ...]) -> str | None:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def unwrap_channel_user_text(text: str) -> str | None:
    """Extract a human turn from a resident-channel envelope.

    Channel transport receipts are untrusted structural data, not conversation
    text. The resident-channel frame's matching marker ID binds its body to the
    envelope. Known Telegram text/caption and NockCC body/subject fields take
    precedence; a non-JSON frame body is a plain human turn. Non-channel text
    is returned unchanged.
    """
    if not text.lstrip().startswith("<channel "):
        return text
    match = _CHANNEL_FRAME_RE.fullmatch(text.strip())
    if match is None:
        return None
    channel = _CHANNEL_NAME_RE.search(match.group("opening_tag"))
    if channel is not None and channel.group("channel") == "engine":
        return None
    body = match.group("body").strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body or None
    if isinstance(payload, str):
        return payload.strip() or None
    if isinstance(payload, list):
        return None
    if not isinstance(payload, dict):
        return body
    for path in _CHANNEL_TEXT_PATHS:
        human_text = _nested_text(payload, path)
        if human_text is not None:
            return human_text
    return None


def _matches_any(value: str, patterns: list[str]) -> bool:
    cleaned = value.strip().strip("'\"")
    candidates = {cleaned, cleaned.lstrip("/"), cleaned.removeprefix("./"), Path(cleaned).name}
    return any(
        fnmatch.fnmatch(candidate.casefold(), pattern.casefold())
        for candidate in candidates
        for pattern in patterns
    )


def extract_candidate_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in {"file_path", "path", "filename"} and isinstance(inner, str):
                paths.append(inner)
            paths.extend(extract_candidate_paths(inner))
    elif isinstance(value, list):
        for item in value:
            paths.extend(extract_candidate_paths(item))
    elif isinstance(value, str):
        # Pull obvious absolute or repo-relative file paths out of shell text.
        paths.extend(re.findall(r"(?:/[\w@%+=:,./-]+|agents/[\w@%+=:,./-]+)", value))
        paths.extend(re.findall(r"(?<![\w/.-])(?:\./)?\.env(?:\.[\w.-]+)?\b", value))
    return paths


def denied_by_path(value: Any, patterns: list[str] | None = None) -> bool:
    denylist = patterns or DEFAULT_PATH_DENYLIST
    return any(_matches_any(path, denylist) for path in extract_candidate_paths(value))


def denied_by_tool(tool_name: str, patterns: list[str] | None = None) -> bool:
    denylist = patterns or DEFAULT_TOOL_DENYLIST
    return _matches_any(tool_name, denylist)


def denied_by_endpoint(value: Any, patterns: list[str] | None = None) -> bool:
    denylist = patterns or DEFAULT_ENDPOINT_DENYLIST
    text = json_text(value)
    candidates = re.findall(r"https?://[^\s'\"<>]+|/api/[A-Za-z0-9_./-]+", text)
    return any(_matches_any(candidate, denylist) for candidate in candidates)


def event_source(path: Path, line_number: int, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "adapter": "claude-jsonl",
        "path": str(path),
        "line": line_number,
        "session_id": raw.get("sessionId", ""),
        "timestamp": raw.get("timestamp", ""),
    }


def make_event(
    path: Path,
    line_number: int,
    raw: dict[str, Any],
    actor: str,
    surface: str,
    kind: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    scrubbed, redactions = scrub_secrets(content)
    if stats is not None:
        stats["secrets_redacted"] += redactions
    return {
        "id": f"{raw.get('uuid', '')}:{line_number}:{surface}:{kind}",
        "source": event_source(path, line_number, raw),
        "actor": actor,
        "surface": surface,
        "kind": kind,
        "content": scrubbed,
        "metadata": metadata or {},
        "privacy": {
            "scrubbed": redactions > 0,
            "excluded": False,
            "policy_version": "v1",
        },
    }


def line_events(
    raw: dict[str, Any],
    path: Path,
    line_number: int,
    stats: dict[str, int],
    include_sidechain: bool = False,
    denied_tool_use_ids: set[str] | None = None,
    transcribe_tool_use_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    if raw.get("isSidechain") and not include_sidechain:
        stats["sidechain_excluded"] += 1
        return []

    line_type = raw.get("type", "")
    events: list[dict[str, Any]] = []

    if line_type in {"user", "assistant"}:
        message = raw.get("message", {}) if isinstance(raw.get("message"), dict) else {}
        actor = message.get("role") or line_type
        for part in normalize_parts(message):
            part_type = part.get("type", "text")
            if part_type == "text":
                text = json_text(part.get("text", ""))
                if line_type == "user" and actor == "user" and text.lstrip().startswith("<channel "):
                    text = unwrap_channel_user_text(text)
                    if text is None:
                        stats["channel_wrappers_dropped"] += 1
                        continue
                    stats["channel_wrappers_unwrapped"] += 1
                if text:
                    events.append(
                        make_event(path, line_number, raw, actor, "text", "message", text, stats=stats)
                    )
            elif part_type == "tool_use":
                tool_name = part.get("name", "")
                tool_input = part.get("input", {})
                tool_use_id = part.get("id", "")
                if denied_by_path(tool_input):
                    stats["denied_paths"] += 1
                    if tool_use_id and denied_tool_use_ids is not None:
                        denied_tool_use_ids.add(tool_use_id)
                    continue
                if denied_by_tool(tool_name):
                    stats["denied_tools"] += 1
                    if tool_use_id and denied_tool_use_ids is not None:
                        denied_tool_use_ids.add(tool_use_id)
                    continue
                if denied_by_endpoint(tool_input):
                    stats["denied_endpoints"] += 1
                    if tool_use_id and denied_tool_use_ids is not None:
                        denied_tool_use_ids.add(tool_use_id)
                    continue
                command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
                if tool_name == "Bash" and isinstance(command, str):
                    if TRANSCRIBE_UNSAFE_COMMAND_RE.search(command):
                        command_tokens = []
                    else:
                        try:
                            lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&")
                            lexer.whitespace_split = True
                            lexer.commenters = ""
                            command_tokens = list(lexer)
                        except ValueError:
                            command_tokens = []
                    script_path = Path(command_tokens[1]) if len(command_tokens) > 1 else Path()
                    trusted_transcriber_path = (
                        script_path.is_absolute()
                        and ".." not in script_path.parts
                        and script_path.name == "transcribe.py"
                        and script_path.parent.name == "scripts"
                        and (
                            script_path.parent.parent.name == "mira-home"
                            or script_path.parent.parent.name.startswith("mira-home-")
                        )
                    )
                    runs_transcribe = (
                        len(command_tokens) == 3
                        and re.fullmatch(
                            r"python(?:\d+(?:\.\d+)*)?",
                            Path(command_tokens[0]).name,
                            re.IGNORECASE,
                        )
                        and trusted_transcriber_path
                        and not command_tokens[1].startswith("-")
                        and not any(char in TRANSCRIBE_UNSAFE_WORD_CHARS for char in command_tokens[0])
                        and not any(char in TRANSCRIBE_UNSAFE_WORD_CHARS for char in command_tokens[1])
                    )
                    if runs_transcribe and tool_use_id and transcribe_tool_use_ids is not None:
                        transcribe_tool_use_ids.add(tool_use_id)
                events.append(
                    make_event(
                        path,
                        line_number,
                        raw,
                        "assistant",
                        "tool_use.input",
                        "tool_call",
                        json_text(tool_input),
                        metadata={
                            "tool_name": tool_name,
                            "tool_use_id": part.get("id", ""),
                            "is_sidechain": bool(raw.get("isSidechain")),
                        },
                        stats=stats,
                    )
                )
            elif part_type == "tool_result":
                tool_use_id = part.get("tool_use_id", "")
                if denied_tool_use_ids is not None and tool_use_id in denied_tool_use_ids:
                    stats["denied_results"] += 1
                    continue
                is_transcribe_result = (
                    transcribe_tool_use_ids is not None
                    and tool_use_id in transcribe_tool_use_ids
                )
                if is_transcribe_result:
                    transcribe_tool_use_ids.remove(tool_use_id)
                    if part.get("is_error"):
                        stats["transcribe_results_dropped"] += 1
                        continue
                    result_content = part.get("content", "")
                    if isinstance(result_content, str):
                        content = result_content
                    elif isinstance(result_content, list):
                        content = "\n".join(
                            item["text"]
                            for item in result_content
                            if isinstance(item, dict)
                            and item.get("type") == "text"
                            and isinstance(item.get("text"), str)
                        )
                    else:
                        content = ""
                    content = "\n".join(
                        line
                        for line in content.splitlines()
                        if not TRANSCRIBE_DIAGNOSTIC_RE.match(line.lstrip())
                    ).strip()
                    if not content:
                        stats["transcribe_results_dropped"] += 1
                        continue
                else:
                    content = json_text(part.get("content", ""))
                denied_result = False
                if denied_by_path(content):
                    stats["denied_result_paths"] += 1
                    denied_result = True
                if denied_by_endpoint(content):
                    stats["denied_result_endpoints"] += 1
                    denied_result = True
                if denied_result:
                    stats["denied_results"] += 1
                    if is_transcribe_result:
                        stats["transcribe_results_dropped"] += 1
                    continue
                metadata = {
                    "tool_use_id": part.get("tool_use_id", ""),
                    "is_sidechain": bool(raw.get("isSidechain")),
                }
                if is_transcribe_result:
                    metadata.update({"tool_name": "Bash", "transcription_script": "transcribe.py"})
                events.append(
                    make_event(
                        path,
                        line_number,
                        raw,
                        "user" if is_transcribe_result else "tool",
                        "text" if is_transcribe_result else "tool_result.content",
                        "message" if is_transcribe_result else "tool_result",
                        content,
                        metadata=metadata,
                        stats=stats,
                    )
                )
                if is_transcribe_result:
                    stats["transcribe_results_promoted"] += 1

    elif line_type == "system" and raw.get("compactMetadata"):
        events.append(
            make_event(
                path,
                line_number,
                raw,
                "system",
                "system",
                "compaction",
                json_text(raw.get("compactMetadata", {})),
                metadata={"compact_boundary": True},
                stats=stats,
            )
        )

    elif line_type == "pr-link":
        events.append(
            make_event(
                path,
                line_number,
                raw,
                "system",
                "pr-link",
                "provenance",
                json_text({k: raw.get(k) for k in ("prNumber", "prRepository", "prUrl")}),
                stats=stats,
            )
        )

    return events


def ingest_file(path: Path | str, include_sidechain: bool = False) -> dict[str, Any]:
    transcript = Path(path)
    stats = new_stats()
    events: list[dict[str, Any]] = []
    denied_tool_use_ids: set[str] = set()
    transcribe_tool_use_ids: set[str] = set()
    with transcript.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            stats["lines_read"] += 1
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                stats["malformed_lines"] += 1
                continue
            events.extend(
                line_events(
                    raw,
                    transcript,
                    line_number,
                    stats,
                    include_sidechain,
                    denied_tool_use_ids,
                    transcribe_tool_use_ids,
                )
            )
    stats["events_written"] = len(events)
    return {"events": events, "stats": stats}


def iter_input_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.jsonl")))
        else:
            files.append(path)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest Claude Code JSONL transcripts")
    parser.add_argument("paths", nargs="+", type=Path, help="JSONL transcript file(s) or directories")
    parser.add_argument("--output", type=Path, default=None, help="Write sanitized events as JSONL")
    parser.add_argument("--include-sidechain", action="store_true")
    parser.add_argument("--pretty", action="store_true", help="Print full result JSON to stdout")
    args = parser.parse_args()

    all_events: list[dict[str, Any]] = []
    total = new_stats()
    for file_path in iter_input_files(args.paths):
        result = ingest_file(file_path, include_sidechain=args.include_sidechain)
        all_events.extend(result["events"])
        for key, value in result["stats"].items():
            total[key] = total.get(key, 0) + value

    if args.output:
        secure_write_text(
            args.output,
            "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in all_events),
        )

    total["events_written"] = len(all_events)
    result = {"events": all_events, "stats": total}
    if args.pretty:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(json.dumps({"stats": total}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
