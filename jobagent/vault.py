"""Password manager access (Bitwarden or 1Password CLI). Secrets stay in memory: never printed, logged or written."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field


class VaultError(RuntimeError):
    pass


@dataclass(frozen=True)
class Login:
    username: str
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return f"Login(username={self.username!r}, password=<hidden>)"


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True).stdout
    except FileNotFoundError:
        raise VaultError(f"{cmd[0]} is not installed") from None
    except subprocess.CalledProcessError as e:
        raise VaultError(f"{cmd[0]} failed (exit {e.returncode}): {e.stderr.strip()[:120]}") from None
    except subprocess.TimeoutExpired:
        raise VaultError(f"{cmd[0]} timed out") from None


def get_login(kind: str, ref: str, run=_run) -> Login:
    """ref = Bitwarden item id, or a 1Password item name/id. The ref is a pointer, not a secret."""
    if kind == "bitwarden":
        login = json.loads(run(["bw", "get", "item", ref])).get("login") or {}
        user, pw = login.get("username"), login.get("password")
    elif kind == "1password":
        fields = {f["id"]: f.get("value") for f in json.loads(run(["op", "item", "get", ref, "--format", "json"])).get("fields", [])}
        user, pw = fields.get("username"), fields.get("password")
    else:
        raise VaultError("VAULT must be bitwarden or 1password to log in")
    if not user or not pw:
        raise VaultError(f"item {ref} has no username/password")
    return Login(user, pw)


def scrub(text: str, login: Login) -> str:
    for secret in (login.password, login.username):
        text = text.replace(secret, "<redacted>") if secret else text
    return text
