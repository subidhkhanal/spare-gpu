"""Request handlers. Each takes the signed-in user (or None) and the request's values."""

import html
import os
import subprocess
from pathlib import Path

from . import db
from .config import UPLOAD_DIR


class Forbidden(Exception):
    pass


def list_notes(conn, user: str, term: str) -> str:
    rows = db.notes_for(conn, user, term)
    items = "".join(f"<li>{html.escape(r['title'])}</li>" for r in rows)
    return f"<h1>Notes matching {html.escape(term)}</h1><ul>{items}</ul>"


def delete(conn, user: str, note_id: int) -> None:
    note = db.get_note(conn, note_id)
    if note is None or note["owner"] != user:
        raise Forbidden("not your note")
    db.delete_note(conn, note_id)


def attachment(user: str, name: str) -> bytes:
    base = (Path(UPLOAD_DIR) / user).resolve()
    path = (base / name).resolve()
    if base not in path.parents:
        raise Forbidden("outside your uploads")
    return path.read_bytes()


def export(user: str, fmt: str) -> bytes:
    if fmt not in ("pdf", "txt"):
        raise ValueError("unknown format")
    return subprocess.run(["notes-export", "--user", user, "--format", fmt], capture_output=True, check=True).stdout


def set_cookie_header(session: str) -> str:
    return f"session={session}; Secure; HttpOnly; SameSite=Lax; Path=/"
