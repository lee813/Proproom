#!/usr/bin/env python3
"""Register a Docker instance once, then call its Proproom API without SSH."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

KEYCHAIN_SERVICE = "proproom-http"
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


def _instance_id(opener, server: str) -> str:
    with opener.open(f"{server}/healthz", timeout=10) as response:
        health = json.load(response)
    instance = health.get("instance_id")
    if not health.get("ok") or not isinstance(instance, str) or not IDENTIFIER.fullmatch(instance):
        raise ValueError("The Proproom server is not healthy or did not identify itself")
    return instance


def _keychain_read(instance: str) -> str | None:
    result = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                             "-a", instance, "-w"], capture_output=True, text=True)
    if result.returncode == 44:
        return None
    if result.returncode:
        raise ValueError("The Mac Keychain could not be accessed")
    secret = result.stdout.strip()
    if not TOKEN.fullmatch(secret):
        raise ValueError("The saved Proproom credential is invalid")
    return secret


def _keychain_save(instance: str, token: str) -> None:
    # security -i reads commands on stdin; the token is not placed in argv or shell history.
    command = f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {instance} -w {token}\n"
    result = subprocess.run(["security", "-i"], input=command, capture_output=True, text=True)
    if result.returncode or _keychain_read(instance) != token:
        raise ValueError("Could not securely save the Proproom credential in the Mac Keychain")


def _docker_credential(instance: str, container: str) -> str | None:
    """Read a credential through an already-authorized non-SSH Docker context; never start a container."""
    try:
        context = subprocess.run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                                 capture_output=True, text=True)
        if context.returncode:
            return None
        endpoints = [context.stdout.strip(), os.environ.get("DOCKER_HOST", "")]
        if not endpoints[0] or any(e.lower().startswith("ssh://") for e in endpoints):
            return None
        if not IDENTIFIER.fullmatch(container) or container.startswith("-"):
            raise ValueError("Invalid Docker container name")
        result = subprocess.run(["docker", "exec", "--", container, "prop", "credential"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or data.get("instance_id") != instance:
            return None
        token = data.get("token")
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ValueError("The Docker container returned an invalid credential")
        return token
    except json.JSONDecodeError as e:
        raise ValueError("The Docker container returned an invalid registration response") from e


def _verify_and_save(opener, server: str, instance: str, token: str) -> None:
    if not TOKEN.fullmatch(token):
        raise ValueError("The supplied Proproom credential is invalid")
    request = urllib.request.Request(f"{server}/v1/capabilities", headers={"Authorization": f"Bearer {token}"})
    with opener.open(request, timeout=30) as response:
        json.load(response)
    _keychain_save(instance, token)


def _credential(opener, server: str, container: str) -> str:
    instance = _instance_id(opener, server)
    if token := _keychain_read(instance):
        return token
    token = _docker_credential(instance, container)
    if token is None:
        raise ValueError("First-time registration needs the Docker-issued token; run register and enter it securely, "
                         "or connect a non-SSH Docker context to the same container")
    _verify_and_save(opener, server, instance, token)
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", help="register, or a Proproom operation such as search, remote, fetch or share")
    parser.add_argument("--json", default="{}", help="Operation arguments as a JSON object")
    parser.add_argument("--server", default=os.environ.get("PROP_SERVER"),
                        help="Proproom base URL, e.g. http://host:8765 (default: $PROP_SERVER)")
    parser.add_argument("--container", default=os.environ.get("PROP_DOCKER_CONTAINER", "proproom"))
    args = parser.parse_args()
    try:
        if not args.server:
            raise ValueError("Set PROP_SERVER or pass --server with the Proproom base URL")
        data = json.loads(args.json)
        if not isinstance(data, dict):
            raise ValueError("--json must be an object")
        if not args.operation.replace("_", "").isalnum():
            raise ValueError("Invalid operation name")
        # The management token must not be sent to an HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        server = args.server.rstrip("/")
        if args.operation == "register":
            instance = _instance_id(opener, server)
            token = getpass.getpass("Docker-issued Proproom token: ") if sys.stdin.isatty() else sys.stdin.readline().strip()
            _verify_and_save(opener, server, instance, token)
            print(json.dumps({"registered": True, "instance_id": instance}))
            return 0
        secret = _credential(opener, server, args.container)
        request = urllib.request.Request(
            f"{server}/v1/ops/{args.operation}", data=json.dumps(data).encode(), method="POST",
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json", "X-Prop-Actor": "cli"})
        with opener.open(request, timeout=300) as response:
            result = json.load(response)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except urllib.error.HTTPError as e:
        sys.stderr.write(e.read().decode("utf-8", errors="replace") + "\n")
    except (OSError, ValueError, urllib.error.URLError) as e:
        sys.stderr.write(f"Proproom HTTP request failed: {e}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
