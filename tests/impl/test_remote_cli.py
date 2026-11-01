"""Docker-first registration uses the matching instance and does not expose credentials."""
import io
import json
import subprocess
import sys

import pytest

from tools import prop_http as remote_cli

TOKEN = "ab" * 22


def test_remote_cli_sends_request_without_logging_token(monkeypatch, capsys):
    calls = []
    monkeypatch.setenv("PROP_SERVER", "http://nas.test:8765")
    monkeypatch.setattr(sys, "argv", ["prop_http", "search", "--json", '{"query":"US dollar"}'])
    monkeypatch.setattr(remote_cli, "_credential", lambda opener, server, container: TOKEN)

    class Response(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return Response(b'{"selection_id":"S1"}')

    monkeypatch.setattr(remote_cli.urllib.request, "build_opener", lambda *args: Opener())
    assert remote_cli.main() == 0
    request, timeout = calls[0]
    assert request.full_url == "http://nas.test:8765/v1/ops/search" and timeout == 300
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert json.loads(request.data) == {"query": "US dollar"}
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err and json.loads(output.out) == {"selection_id": "S1"}


def test_remote_cli_needs_a_server_and_reads_no_credentials_without_one(monkeypatch, capsys):
    monkeypatch.delenv("PROP_SERVER", raising=False)
    monkeypatch.setattr(sys, "argv", ["prop_http", "search"])
    monkeypatch.setattr(remote_cli, "_credential", lambda *a: (_ for _ in ()).throw(AssertionError("read token")))
    assert remote_cli.main() == 2
    assert "PROP_SERVER" in capsys.readouterr().err


def test_remote_cli_rejects_non_object_without_reading_credentials(monkeypatch, capsys):
    monkeypatch.setenv("PROP_SERVER", "http://nas.test:8765")
    monkeypatch.setattr(sys, "argv", ["prop_http", "search", "--json", "[]"])
    monkeypatch.setattr(remote_cli, "_credential", lambda *a: (_ for _ in ()).throw(AssertionError("read token")))
    assert remote_cli.main() == 2
    assert "must be an object" in capsys.readouterr().err


def test_docker_credential_is_bound_to_server_instance(monkeypatch):
    def run(argv, **kwargs):
        if argv[1] == "context":
            return subprocess.CompletedProcess(argv, 0, "unix:///var/run/docker.sock\n", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"instance_id": "server-A", "token": TOKEN}), "")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(remote_cli.subprocess, "run", run)
    assert remote_cli._docker_credential("server-B", "proproom") is None
    assert remote_cli._docker_credential("server-A", "proproom") == TOKEN


def test_docker_never_falls_back_to_ssh(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ssh://somewhere", "")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(remote_cli.subprocess, "run", run)
    assert remote_cli._docker_credential("server-A", "proproom") is None
    assert len(calls) == 1 and calls[0][1] == "context"


def test_first_use_verifies_docker_token_before_keychain_save(monkeypatch):
    calls = []
    monkeypatch.setattr(remote_cli, "_instance_id", lambda opener, server: "server-A")
    monkeypatch.setattr(remote_cli, "_keychain_read", lambda instance: None)
    monkeypatch.setattr(remote_cli, "_docker_credential", lambda instance, container: TOKEN)
    monkeypatch.setattr(remote_cli, "_keychain_save", lambda instance, token: calls.append(("save", instance, token)))

    class Response(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()

    class Opener:
        def open(self, request, timeout):
            calls.append(("verify", request.get_header("Authorization")))
            return Response(b"{}")

    assert remote_cli._credential(Opener(), "http://nas", "proproom") == TOKEN
    assert calls == [("verify", f"Bearer {TOKEN}"), ("save", "server-A", TOKEN)]


def test_missing_docker_does_not_save_or_generate_credential(monkeypatch):
    monkeypatch.setattr(remote_cli, "_instance_id", lambda opener, server: "server-A")
    monkeypatch.setattr(remote_cli, "_keychain_read", lambda instance: None)
    monkeypatch.setattr(remote_cli, "_docker_credential", lambda *a: None)
    monkeypatch.setattr(remote_cli, "_keychain_save", lambda *a: pytest.fail("saved a credential"))
    with pytest.raises(ValueError, match="First-time registration"):
        remote_cli._credential(None, "http://nas", "proproom")


def test_keychain_save_never_places_token_in_process_arguments(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(remote_cli.subprocess, "run", run)
    monkeypatch.setattr(remote_cli, "_keychain_read", lambda instance: TOKEN)
    remote_cli._keychain_save("server-A", TOKEN)
    assert calls[0][0] == ["security", "-i"]
    assert TOKEN not in repr(calls[0][0]) and TOKEN in calls[0][1]["input"]
