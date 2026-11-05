"""Exact-user Microsoft access policy. This data is never served as an asset."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Settings


class AccessPolicyError(RuntimeError):
    pass


def allowed_emails(settings: Settings) -> frozenset[str]:
    path = Path(settings.auth_allowed_users_file)
    if not path.is_absolute():
        path = settings.base_dir / path
    try:
        records = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AccessPolicyError("The Microsoft user allowlist could not be read.") from exc
    if not isinstance(records, list) or not records:
        raise AccessPolicyError("The Microsoft user allowlist must be a non-empty JSON array.")
    emails: set[str] = set()
    for record in records:
        email = record.get("email") if isinstance(record, dict) else None
        if not isinstance(email, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
            raise AccessPolicyError("The Microsoft user allowlist contains an invalid email.")
        email = email.strip().casefold()
        if email in emails:
            raise AccessPolicyError("The Microsoft user allowlist contains duplicate emails.")
        emails.add(email)
    return frozenset(emails)
