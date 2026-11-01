"""Exercise the container-local registration command without loading a model or a live library."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import proproom.core
import proproom.core.paths
import proproom.api


class DockerCredentialSmoke(unittest.TestCase):
    def test_credential_reads_existing_instance_without_booting(self):
        fake_bootstrap = types.ModuleType("proproom.core.bootstrap")
        fake_bootstrap.boot = lambda: self.fail("credential must not boot or mutate the library")
        fake_bootstrap.read_token = lambda home: home.token.read_text().strip()
        fake_client = types.ModuleType("proproom.api.client")
        fake_client._instance_id = lambda home: "test-instance"
        fake_registry = types.ModuleType("proproom.api.registry")
        fake_registry.REGISTRY = {}
        fake_registry.OpContext = object
        modules = {"proproom.core.bootstrap": fake_bootstrap, "proproom.api.client": fake_client,
                   "proproom.api.registry": fake_registry}
        path = Path(__file__).resolve().parents[2] / "proproom" / "api" / "cli.py"
        spec = importlib.util.spec_from_file_location("proproom.api._credential_smoke", path)
        cli = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules), patch.object(proproom.core, "bootstrap", fake_bootstrap, create=True), \
                patch.object(proproom.api, "client", fake_client, create=True):
            spec.loader.exec_module(cli)
            with tempfile.TemporaryDirectory(prefix="proproom-credential-test-") as directory:
                token_path = Path(directory) / "instance.token"
                token_path.write_text("test-token\n")
                home = types.SimpleNamespace(token=token_path)
                output = io.StringIO()
                with patch.object(proproom.core.paths, "resolve_home", return_value=home), contextlib.redirect_stdout(output):
                    cli.main(["credential"])
                self.assertEqual(json.loads(output.getvalue()), {"instance_id": "test-instance", "token": "test-token"})
                token_path.unlink()
                errors = io.StringIO()
                with patch.object(proproom.core.paths, "resolve_home", return_value=home), contextlib.redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as exc:
                        cli.main(["credential"])
                self.assertEqual(exc.exception.code, 2)
                self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "not_initialized")
                self.assertFalse(token_path.exists())


if __name__ == "__main__":
    unittest.main()
