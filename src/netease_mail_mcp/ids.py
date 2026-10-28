"""Opaque message ids.

Format: ``<b64url(account)>.<b64url(folder)>.<uidvalidity>.<uid>`` where ``folder`` is the raw
(modified UTF-7) IMAP name. Base64url has no ``.``, so splitting is unambiguous. The account is
part of the id so that tools taking ids (read, thread, download, manage) always act on the
mailbox the id came from, even with several accounts configured.

An id is stable while the folder's UIDVALIDITY is unchanged; tools reject ids whose
UIDVALIDITY no longer matches. Sequence numbers are never used.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from .errors import InvalidIdError

_B64_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _b64e(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> str:
    if not _B64_RE.match(text):
        raise ValueError("bad base64url")
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


@dataclass(frozen=True, order=True)
class MessageRef:
    account: str
    folder: str  # raw IMAP name (modified UTF-7)
    uidvalidity: int
    uid: int

    def encode(self) -> str:
        return f"{_b64e(self.account)}.{_b64e(self.folder)}.{self.uidvalidity}.{self.uid}"


def encode_id(account: str, folder: str, uidvalidity: int, uid: int) -> str:
    return MessageRef(account, folder, uidvalidity, uid).encode()


def decode_id(value: str) -> MessageRef:
    text = (value or "").strip()
    parts = text.split(".")
    bad = InvalidIdError(
        f"邮件 id 无效：{value!r}。id 必须原样使用 search_messages 等工具返回的值。",
        hint="Invalid message id; pass the exact `id` returned by search_messages.",
    )
    if len(parts) != 4:
        raise bad
    try:
        account = _b64d(parts[0])
        folder = _b64d(parts[1])
        uidvalidity = int(parts[2])
        uid = int(parts[3])
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise bad from None
    if (
        "@" not in account
        or not folder
        or any(ch in folder or ch in account for ch in "\r\n\0")
        or uidvalidity <= 0
        or uid <= 0
    ):
        raise bad
    return MessageRef(account.lower(), folder, uidvalidity, uid)
