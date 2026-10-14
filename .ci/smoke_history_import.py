"""Fixture-only review smoke against the isolated Honcho API."""
import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="honcho-import-smoke-") as temporary:
    home = Path(temporary)
    hermes_home = home / ".hermes"
    hermes_home.mkdir()
    os.environ.update(HOME=str(home), HERMES_HOME=str(hermes_home),
                      HERMES_HONCHO_HOST="hermes", HERMES_DISABLE_LAZY_INSTALLS="1")
    for key in ("HONCHO_API_KEY", "HONCHO_BASE_URL", "HERMES_PROFILE"):
        os.environ.pop(key, None)
    plugin_dir = Path(__file__).resolve().parents[1] / "hermes-plugin-honcho"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugin_honcho", plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    from agent.redact import redact_sensitive_text
    from hermes_plugin_honcho import cli, history_import
    from hermes_state import SessionDB
    from honcho import Honcho
    from honcho.session import Session
    print("Smoke plugin:", module.__file__)
    assert Path(module.__file__).parent == plugin_dir
    workspace = "import-smoke-fixture"
    (hermes_home / "honcho.json").write_text(json.dumps({
        "apiKey": "obviously-fake-ci-key", "baseUrl": "http://127.0.0.1:8000",
        "workspace": workspace, "peerName": "fixture-owner", "aiPeer": "fixture-ai",
        "enabled": True, "sessionStrategy": "global"}), encoding="utf-8")
    db = SessionDB(db_path=hermes_home / "state.db")
    db.create_session("fixture-a", "cli", profile_name="default")
    db.create_session("fixture-b", "cli", profile_name="default")
    timestamp = int(time.time()) - 86400
    secret = "ghp_" + "C" * 36
    secret_text = "Archived credential: " + secret[:20] + "\x00" + secret[20:]
    db.append_message("fixture-a", "user", secret_text, timestamp=timestamp + 2)
    db.append_message("fixture-a", "assistant", "Fourth reply", timestamp=timestamp + 4)
    db.archive_and_compact("fixture-a", [
        {"role": "user", "content": "Generated compression summary", "_compressed_summary": True}
    ])
    db.append_message("fixture-b", "user", "First\x00 fact", timestamp=timestamp + 1)
    db.append_message("fixture-b", "assistant", "Third reply", timestamp=timestamp + 3)

    def run(*arguments, expected=0):
        parser = argparse.ArgumentParser()
        cli.register_cli(parser)
        args = parser.parse_args(["import", *arguments])
        code = 0
        try:
            args.func(args)
        except SystemExit as error:
            code = error.code
        assert code == expected, f"Import exited {code}; expected {expected}"

    def client_for(name):
        return Honcho(api_key="obviously-fake-ci-key", base_url="http://127.0.0.1:8000",
                      workspace_id=name, max_retries=0)

    ledger = home / ".honcho" / "hermes-import-state.json"
    run("--dry-run")
    assert not ledger.exists()
    run("--yes")
    client = client_for(workspace)
    first = list(client.session(workspace).messages())
    assert len(first) == 4
    assert [int(m.created_at.timestamp()) for m in first] == list(range(timestamp + 1, timestamp + 5))
    expected_secret = redact_sensitive_text(secret_text.replace("\x00", ""), force=True, redact_url_credentials=True)
    assert [m.content for m in first] == ["First fact", expected_secret, "Third reply", "Fourth reply"]
    assert all("\x00" not in m.content and secret not in m.content for m in first)
    assert all(m.metadata["backfill"] and m.metadata["honcho_import_version"] == 1 for m in first)
    assert all(m.metadata["payload_sha256"] == history_import.payload_hash(
        m.content, m.peer_id, m.created_at, m.session_id) for m in first)
    ids = {m.id for m in first}
    run("--yes")
    assert {m.id for m in client.session(workspace).messages()} == ids
    db.append_message("fixture-a", "user", "Fifth fact", timestamp=timestamp + 5)
    run("--yes")
    assert len(list(client.session(workspace).messages())) == 5

    db.append_message("fixture-b", "user", "Older late source row", timestamp=timestamp - 1)
    run("--yes", expected=3)
    assert len(list(client.session(workspace).messages())) == 5
    fresh = "import-smoke-earlier"
    run("--workspace", fresh, "--yes")
    fresh_client = client_for(fresh)
    assert len(list(fresh_client.session(fresh).messages())) == 6

    # Simulate losing the response only after the real API accepted the batch.
    db.append_message("fixture-a", "user", "Last recovery fact", timestamp=timestamp + 6)
    original_add = Session.add_messages
    def lost_response(self, *args, **kwargs):
        original_add(self, *args, **kwargs)
        raise RuntimeError("fixture-only lost response")
    Session.add_messages = lost_response
    try:
        run("--workspace", fresh, "--yes", expected=3)
    finally:
        Session.add_messages = original_add
    accepted_ids = {m.id for m in fresh_client.session(fresh).messages()}
    assert len(accepted_ids) == 7
    run("--workspace", fresh, "--yes")
    after = list(fresh_client.session(fresh).messages())
    assert {m.id for m in after} == accepted_ids
    assert len({m.metadata["honcho_import_key"] for m in after}) == 7
    sample = after[0]
    with history_import._remote_reads(fresh_client):
        filtered = history_import._remote_messages(fresh_client.session(fresh), filters={"metadata": {
            "honcho_import_source": sample.metadata["honcho_import_source"],
            "honcho_import_key": sample.metadata["honcho_import_key"]}})
    assert len(filtered) == 1 and filtered[0].id == sample.id
    db.close()
    client._http.close()
    fresh_client._http.close()
    print("REAL SERVER REVIEW SMOKE PASS: redaction, NUL, compacted originals, sorted sources, older-history block, append, no-op, unknown-outcome recovery, JSONB filter.")
