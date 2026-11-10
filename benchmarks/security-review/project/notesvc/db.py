"""SQLite storage. Every query takes its values as parameters."""

import sqlite3


def connect(path: str = "notes.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("create table if not exists notes (id integer primary key, owner text, title text, body text)")
    conn.execute("create table if not exists users (name text primary key, salt blob, hash blob)")
    return conn


def notes_for(conn, owner: str, term: str = "") -> list[sqlite3.Row]:
    return conn.execute("select * from notes where owner = ? and title like ?", (owner, f"%{term}%")).fetchall()


def get_note(conn, note_id: int) -> sqlite3.Row | None:
    return conn.execute("select * from notes where id = ?", (note_id,)).fetchone()


def delete_note(conn, note_id: int) -> None:
    conn.execute("delete from notes where id = ?", (note_id,))
