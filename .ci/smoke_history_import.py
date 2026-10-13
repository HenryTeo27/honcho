"""Fixture-only smoke against the isolated CI Honcho server and mock provider."""
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
    from hermes_plugin_honcho import cli, history_import
    from hermes_state import SessionDB
    from honcho import Honcho
    print("Smoke plugin:", module.__file__)
    assert Path(module.__file__).parent == plugin_dir
    workspace = "import-smoke-fixture"
    (hermes_home / "honcho.json").write_text(json.dumps({
        "apiKey": "obviously-fake-ci-key", "baseUrl": "http://127.0.0.1:8000",
        "workspace": workspace, "peerName": "fixture-owner", "aiPeer": "fixture-ai",
        "enabled": True, "sessionStrategy": "global"}), encoding="utf-8")
    db = SessionDB(db_path=hermes_home / "state.db")
    db.create_session("fixture-source", "cli", profile_name="default")
    timestamp = int(time.time()) - 86400
    db.append_message("fixture-source", "user", "Fixture fact: I use Rust.", timestamp=timestamp)
    db.append_message("fixture-source", "assistant", "Fixture reply: noted.", timestamp=timestamp + 1)

    def run(*arguments):
        parser = argparse.ArgumentParser()
        cli.register_cli(parser)
        args = parser.parse_args(["import", *arguments])
        try:
            args.func(args)
        except SystemExit as error:
            assert error.code == 0, f"Import exited {error.code}"

    ledger = home / ".honcho" / "hermes-import-state.json"
    run("--dry-run")
    assert not ledger.exists()
    run("--yes")
    client = Honcho(api_key="obviously-fake-ci-key", base_url="http://127.0.0.1:8000",
                    workspace_id=workspace, max_retries=0)

    def messages():
        return list(client.session(workspace).messages())

    first = messages()
    assert len(first) == 2
    assert {m.peer_id for m in first} == {"fixture-owner", "fixture-ai"}
    assert sorted(int(m.created_at.timestamp()) for m in first) == [timestamp, timestamp + 1]
    assert all(m.metadata["backfill"] and m.metadata["honcho_import_version"] == 1 for m in first)
    keys = {m.metadata["honcho_import_key"] for m in first}
    run("--yes")
    assert {m.id for m in messages()} == {m.id for m in first}
    db.append_message("fixture-source", "user", "Fixture fact: I use dark mode.", timestamp=timestamp + 2)
    run("--yes")
    after = messages()
    assert len(after) == 3
    assert len({m.metadata["honcho_import_key"] for m in after}) == 3
    assert keys <= {m.metadata["honcho_import_key"] for m in after}
    # Exercise actual server JSONB metadata filtering with the importer's recovery reader.
    sample = after[0]
    with history_import._remote_reads(client):
        filtered = history_import._remote_messages(client.session(workspace), filters={"metadata": {
            "honcho_import_source": sample.metadata["honcho_import_source"],
            "honcho_import_key": sample.metadata["honcho_import_key"]}})
    assert len(filtered) == 1 and filtered[0].id == sample.id
    db.close()
    client._http.close()
    print("REAL SERVER SMOKE PASS: timestamps, peers, metadata, no-op, append, JSONB filter.")
