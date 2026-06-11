"""Tests for raw Claude Code JSONL ingestion.

These protect the v2 blockers: tool_use inputs are evidence, but private paths,
private tools/endpoints, and secrets are filtered before persistence.
"""
import json


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_tool_use_input_becomes_first_class_evidence(ingest_jsonl, tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [
        {
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will send the update."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {
                            "command": "cat <<'EOF' >/tmp/msg.txt\nShip the memory compiler\nEOF\nbash send-message.sh mira normal"
                        },
                    },
                ],
            },
        }
    ])

    result = ingest_jsonl.ingest_file(transcript)

    tool_events = [e for e in result["events"] if e["surface"] == "tool_use.input"]
    assert len(tool_events) == 1
    assert tool_events[0]["kind"] == "tool_call"
    assert "Ship the memory compiler" in tool_events[0]["content"]
    assert tool_events[0]["metadata"]["tool_name"] == "Bash"
    assert tool_events[0]["source"]["line"] == 1


def test_user_pasted_secret_is_scrubbed_without_path_or_tool_match(ingest_jsonl, tmp_path):
    transcript = tmp_path / "session.jsonl"
    token = "123456789:" + "AAExampleTelegramBotTokenSecret"
    write_jsonl(transcript, [
        {
            "type": "user",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:00Z",
            "message": {"role": "user", "content": f"Here is the bot token: {token}"},
        }
    ])

    result = ingest_jsonl.ingest_file(transcript)

    assert result["events"]
    assert token not in json.dumps(result["events"])
    assert "[REDACTED_SECRET]" in result["events"][0]["content"]
    assert result["stats"]["secrets_redacted"] == 1


def test_private_tool_payload_never_persists(ingest_jsonl, tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [
        {
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_private",
                        "name": "mcp__nockcc__nockcc_diary_create",
                        "input": {"body": "private diary payload that must not persist"},
                    }
                ],
            },
        }
    ])

    result = ingest_jsonl.ingest_file(transcript)

    assert "private diary payload" not in json.dumps(result["events"])
    assert result["events"] == []
    assert result["stats"]["denied_tools"] == 1


def test_private_endpoint_payload_never_persists(ingest_jsonl, tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [
        {
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_endpoint",
                        "name": "Bash",
                        "input": {
                            "command": "curl -X POST https://cc.example/api/brain/private/register/ -d '{\"note\":\"private register payload\"}'"
                        },
                    }
                ],
            },
        }
    ])

    result = ingest_jsonl.ingest_file(transcript)

    assert "private register payload" not in json.dumps(result["events"])
    assert result["events"] == []
    assert result["stats"]["denied_endpoints"] == 1


def test_private_path_payload_never_persists(ingest_jsonl, tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [
        {
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_path",
                        "name": "Write",
                        "input": {
                            "file_path": "/Users/kevin/Dev/claude-remote-manager/agents/mira/private/note.md",
                            "content": "private path payload that must not persist",
                        },
                    }
                ],
            },
        }
    ])

    result = ingest_jsonl.ingest_file(transcript)

    assert "private path payload" not in json.dumps(result["events"])
    assert result["events"] == []
    assert result["stats"]["denied_paths"] == 1


def test_sidechain_lines_are_excluded_by_default(ingest_jsonl, tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [
        {
            "type": "assistant",
            "isSidechain": True,
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:00Z",
            "message": {"role": "assistant", "content": "subagent noise"},
        }
    ])

    result = ingest_jsonl.ingest_file(transcript)

    assert result["events"] == []
    assert result["stats"]["sidechain_excluded"] == 1


def test_tool_results_keep_pairing_metadata(ingest_jsonl, tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [
        {
            "type": "user",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:00Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_transcribe",
                        "content": "Kevin said: finalize the memory spec",
                    }
                ],
            },
        }
    ])

    result = ingest_jsonl.ingest_file(transcript)

    assert len(result["events"]) == 1
    assert result["events"][0]["kind"] == "tool_result"
    assert result["events"][0]["metadata"]["tool_use_id"] == "toolu_transcribe"
    assert "finalize the memory spec" in result["events"][0]["content"]


def test_denied_private_tool_result_never_persists(ingest_jsonl, tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [
        {
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_private_get",
                        "name": "mcp__nockcc__nockcc_private_get",
                        "input": {"key": "diary"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:01Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_private_get",
                        "content": "private register answer that must not persist",
                    }
                ],
            },
        },
    ])

    result = ingest_jsonl.ingest_file(transcript)

    assert result["events"] == []
    assert "private register answer" not in json.dumps(result["events"])
    assert result["stats"]["denied_tools"] == 1
    assert result["stats"]["denied_results"] == 1


def test_denied_private_path_tool_result_never_persists(ingest_jsonl, tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [
        {
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_read_private",
                        "name": "Read",
                        "input": {"file_path": "agents/mira/private/DIARY_BRIEF.md"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:01Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_read_private",
                        "content": "private diary brief result that must not persist",
                    }
                ],
            },
        },
    ])

    result = ingest_jsonl.ingest_file(transcript)

    assert result["events"] == []
    assert "private diary brief result" not in json.dumps(result["events"])
    assert result["stats"]["denied_paths"] == 1
    assert result["stats"]["denied_results"] == 1


def test_tool_result_content_gets_defense_in_depth_denials(ingest_jsonl, tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [
        {
            "type": "user",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:00Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_allowed",
                        "content": "read /api/brain/private/register/ and agents/mira/private/DIARY_BRIEF.md",
                    }
                ],
            },
        }
    ])

    result = ingest_jsonl.ingest_file(transcript)

    assert result["events"] == []
    assert result["stats"]["denied_result_paths"] == 1
    assert result["stats"]["denied_result_endpoints"] == 1


def test_bare_common_secret_prefixes_are_scrubbed(ingest_jsonl, tmp_path):
    transcript = tmp_path / "session.jsonl"
    secrets = [
        "ghp_" + "abcdefghijklmnopqrstuvwxyz123456",
        "sk-ant-api03-" + "abcdefghijklmnopqrstuvwxyz1234567890",
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "xoxb-" + "123456789012-123456789012-abcdefghijklmnopqrstuvwx",
    ]
    write_jsonl(transcript, [
        {
            "type": "user",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:00Z",
            "message": {"role": "user", "content": " ".join(secrets)},
        }
    ])

    result = ingest_jsonl.ingest_file(transcript)
    dumped = json.dumps(result["events"])

    for secret in secrets:
        assert secret not in dumped
    assert result["stats"]["secrets_redacted"] == len(secrets)


def test_credentials_key_and_pem_paths_are_denied(ingest_jsonl, tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [
        {
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-06-11T01:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_credentials",
                        "name": "Read",
                        "input": {"file_path": "/Users/kevin/.aws/credentials-prod"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_key",
                        "name": "Read",
                        "input": {"file_path": "/Users/kevin/.ssh/id_rsa_backup"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_pem",
                        "name": "Read",
                        "input": {"file_path": "/Users/kevin/certs/client.pem"},
                    },
                ],
            },
        }
    ])

    result = ingest_jsonl.ingest_file(transcript)

    assert result["events"] == []
    assert result["stats"]["denied_paths"] == 3
