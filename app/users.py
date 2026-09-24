"""Username/password user store for AUTH_MODE=password.

For deployments with no identity provider available. There is no database and no
new dependency: users live in a JSON file on the server and passwords are stored
only as salted scrypt hashes.

Hashing uses hashlib.scrypt from the standard library. It is memory-hard, so it
resists GPU cracking far better than a plain SHA, and it ships with Python - no
bcrypt/argon2 wheel to install on the App Service plan.

Manage users from the command line:

    python -m app.users add    rfp@firstsource.com
    python -m app.users add    rfp@firstsource.com --password "..."   (scripted)
    python -m app.users list
    python -m app.users remove rfp@firstsource.com

The users file contains password hashes and belongs on the server only - it is
git-ignored by default.
"""

from __future__ import annotations

import base64
import getpass
import hmac
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Dict, Optional

from .config import Settings, get_settings

log = logging.getLogger(__name__)

# scrypt parameters. n=2**14 with r=8 keeps a single verification around a
# hundred milliseconds - unnoticeable on login, expensive in bulk.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_BYTES = 32
_SALT_BYTES = 16

MIN_PASSWORD_LENGTH = 12


class UserError(RuntimeError):
    """The user store could not be read, or the request was invalid."""


def _users_path(settings: Settings) -> Path:
    path = Path(settings.auth_users_file)
    return path if path.is_absolute() else settings.base_dir / path


def _normalise(username: str) -> str:
    """Usernames are case-insensitive: nobody should be locked out by capitals."""
    return (username or "").strip().lower()


def _hash(password: str, salt: bytes) -> bytes:
    return __import__("hashlib").scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_BYTES,
    )


def load_users(settings: Settings | None = None) -> Dict[str, dict]:
    settings = settings or get_settings()
    path = _users_path(settings)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - a broken file must not crash sign-in
        log.error("Could not read the users file %s: %s", path, exc)
        return {}


def _save_users(users: Dict[str, dict], settings: Settings) -> None:
    path = _users_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(users, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # best effort; a no-op on some Windows setups
    except OSError:
        pass


def set_password(
    username: str, password: str, settings: Settings | None = None
) -> None:
    """Create or update a user. Rejects passwords short enough to be guessed."""
    settings = settings or get_settings()
    username = _normalise(username)
    if not username:
        raise UserError("A username is required.")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise UserError(
            f"The password must be at least {MIN_PASSWORD_LENGTH} characters."
        )

    salt = secrets.token_bytes(_SALT_BYTES)
    users = load_users(settings)
    users[username] = {
        "algorithm": "scrypt",
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(_hash(password, salt)).decode("ascii"),
    }
    _save_users(users, settings)
    log.info("Password set for %s.", username)


def remove_user(username: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    users = load_users(settings)
    if _normalise(username) not in users:
        return False
    users.pop(_normalise(username))
    _save_users(users, settings)
    return True


def verify(username: str, password: str, settings: Settings | None = None) -> bool:
    """Check a password. Always does the full hashing work, even for an unknown
    user, so response time cannot be used to discover who has an account."""
    settings = settings or get_settings()
    record = load_users(settings).get(_normalise(username))

    if record is None:
        # Hash against a throwaway salt so the timing matches a real check.
        _hash(password or "", secrets.token_bytes(_SALT_BYTES))
        return False

    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        candidate = __import__("hashlib").scrypt(
            (password or "").encode("utf-8"), salt=salt,
            n=int(record.get("n", _SCRYPT_N)), r=int(record.get("r", _SCRYPT_R)),
            p=int(record.get("p", _SCRYPT_P)), dklen=len(expected),
        )
    except Exception as exc:  # noqa: BLE001 - a malformed record is not a login
        log.warning("Could not verify the stored password for %s: %s", username, exc)
        return False

    return hmac.compare_digest(candidate, expected)


def user_record(username: str, settings: Settings | None = None) -> Optional[dict]:
    return load_users(settings).get(_normalise(username))


# --------------------------------------------------------------------- CLI
def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.users", description="Manage username/password sign-in."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Create a user, or change an existing password.")
    add.add_argument("username")
    add.add_argument("--password", help="Skip the prompt (for scripted setup).")

    sub.add_parser("list", help="List usernames (never passwords).")

    remove = sub.add_parser("remove", help="Delete a user.")
    remove.add_argument("username")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "add":
        password = args.password
        if not password:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Repeat password: "):
                print("Passwords did not match.")
                return 1
        try:
            set_password(args.username, password, settings)
        except UserError as exc:
            print(str(exc))
            return 1
        print(f"Saved {_normalise(args.username)} to {_users_path(settings)}")
        return 0

    if args.command == "list":
        users = load_users(settings)
        if not users:
            print(f"No users yet. Add one:\n  python -m app.users add you@firstsource.com")
            return 0
        print(f"{len(users)} user(s) in {_users_path(settings)}:")
        for name in sorted(users):
            print(" ", name)
        return 0

    if args.command == "remove":
        if remove_user(args.username, settings):
            print(f"Removed {_normalise(args.username)}.")
            return 0
        print(f"No such user: {_normalise(args.username)}")
        return 1

    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli(sys.argv[1:]))
