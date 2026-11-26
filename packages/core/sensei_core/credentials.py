"""Credential broker (Slice 35, DESIGN.md Threat 6): API keys come from the macOS Keychain.

A key is read at call time, for one request, and never stored on a client,
written to the repo or models.toml, logged, or traced. A missing key returns
None and the caller fails closed; nothing falls back to an environment
variable or a file.

Add the Kimi key (it prompts, so the key stays out of shell history):

    security add-generic-password -a sensei -s sensei-kimi-api-key -U -w
"""

from __future__ import annotations

import shutil
import subprocess

KEYCHAIN_ACCOUNT = "sensei"
KIMI_KEY_SERVICE = "sensei-kimi-api-key"


def keychain_secret(service: str, account: str = KEYCHAIN_ACCOUNT, *, timeout: float = 10.0) -> str | None:
    """The Keychain password for (service, account), or None if absent or unreadable."""
    security = shutil.which("security")
    if security is None:  # not macOS: there is no Keychain, so there is no key
        return None
    try:
        out = subprocess.run([security, "find-generic-password", "-a", account, "-s", service, "-w"],
                             capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    secret = out.stdout.strip() if out.returncode == 0 else ""
    return secret or None
