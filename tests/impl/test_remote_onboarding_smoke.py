"""Docker-first registration against a disposable server and real temporary Keychain items."""
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools import prop_http


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("security"), "requires macOS Keychain")
class FirstUseSmoke(unittest.TestCase):
    def test_ssh_docker_context_is_never_used(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(prop_http.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "ssh://nas", "")
            self.assertIsNone(prop_http._docker_credential("server", "proproom"))
            self.assertEqual(run.call_count, 1)

    def test_wrong_docker_instance_is_not_used(self):
        def run(argv, **kwargs):
            if argv[1] == "context":
                return subprocess.CompletedProcess(argv, 0, "unix:///socket", "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"instance_id": "wrong", "token": "a" * 43}), "")
        with patch.dict(os.environ, {}, clear=True), patch.object(prop_http.subprocess, "run", side_effect=run):
            self.assertIsNone(prop_http._docker_credential("wanted", "proproom"))

    def test_failed_authentication_never_saves(self):
        class Rejected:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", None, None)
        with patch.object(prop_http, "_keychain_save") as save:
            with self.assertRaises(urllib.error.HTTPError):
                prop_http._verify_and_save(Rejected(), "http://127.0.0.1", "test-server", "a" * 43)
            save.assert_not_called()

    def test_docker_first_run_and_explicit_registration(self):
        instance = "test-" + secrets.token_hex(12)
        token = secrets.token_urlsafe(32)
        service = "proproom-smoke-" + secrets.token_hex(8)
        observed = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def reply(self, status, data):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            def do_GET(self):
                if self.path == "/healthz":
                    self.reply(200, {"ok": True, "instance_id": instance})
                elif self.path == "/v1/capabilities" and self.headers.get("Authorization") == "Bearer " + token:
                    observed.append("verified")
                    self.reply(200, {})
                else:
                    self.reply(401, {"error": "unauthorized"})
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/v1/ops/search" and self.headers.get("Authorization") == "Bearer " + token:
                    observed.append(data["query"])
                    self.reply(200, {"selection_id": "test-selection"})
                else:
                    self.reply(401, {"error": "unauthorized"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        actual_run = subprocess.run
        docker_calls = []
        def run(argv, **kwargs):
            if argv[0] == "docker":
                docker_calls.append(argv)
                if argv[1] == "context":
                    return subprocess.CompletedProcess(argv, 0, "unix:///fake-docker.sock\n", "")
                return subprocess.CompletedProcess(argv, 0, json.dumps({"instance_id": instance, "token": token}), "")
            return actual_run(argv, **kwargs)
        try:
            server_url = f"http://127.0.0.1:{server.server_port}"
            args = ["prop_http", "search", "--server", server_url, "--json", '{"query":"first-use"}']
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(prop_http, "KEYCHAIN_SERVICE", service), patch.dict(os.environ, {}, clear=True), \
                    patch.object(prop_http.subprocess, "run", side_effect=run), patch.object(sys, "argv", args), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(prop_http.main(), 0, errors.getvalue())
                docker_calls.clear()
                self.assertEqual(prop_http.main(), 0, errors.getvalue())
                self.assertEqual(docker_calls, [])  # cached credential needs no Docker
                register = ["prop_http", "register", "--server", server_url]
                with patch.object(sys, "argv", register), patch.object(sys, "stdin", io.StringIO(token + "\n")):
                    self.assertEqual(prop_http.main(), 0, errors.getvalue())
            self.assertEqual(observed, ["verified", "first-use", "first-use", "verified"])
            self.assertNotIn(token, output.getvalue() + errors.getvalue())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            actual_run(["security", "delete-generic-password", "-s", service, "-a", instance],
                       capture_output=True, text=True)
            remaining = actual_run(["security", "find-generic-password", "-s", service, "-a", instance],
                                   capture_output=True, text=True)
            if remaining.returncode != 44:
                raise RuntimeError("Could not verify removal of the disposable Keychain item")


if __name__ == "__main__":
    unittest.main()
