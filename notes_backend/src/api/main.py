from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, List, Optional

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Use a local SQLite database file inside the container filesystem for persistence.
# No env vars required by user request; this keeps the app self-contained.
DB_PATH = Path(os.getenv("NOTES_DB_PATH", "data/notes.db"))


def _utc_now_iso() -> str:
    """Return current UTC time in ISO8601 format with 'Z' suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _init_db() -> None:
    """Initialize the SQLite DB schema if it doesn't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              content TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        conn.commit()


@contextmanager
def _db_conn() -> Generator[sqlite3.Connection, None, None]:
    """Context manager for SQLite connections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


class Note(BaseModel):
    """Represents a persisted note."""

    id: int = Field(..., description="Unique note identifier.")
    title: str = Field(..., min_length=1, max_length=120, description="Short note title.")
    content: str = Field(..., description="Full note content (markdown/plain text).")
    created_at: str = Field(..., description="ISO8601 timestamp (UTC) when note was created.")
    updated_at: str = Field(..., description="ISO8601 timestamp (UTC) when note was last updated.")


class NoteCreate(BaseModel):
    """Payload for creating a note."""

    title: str = Field(..., min_length=1, max_length=120, description="Short note title.")
    content: str = Field("", description="Full note content (markdown/plain text).")


class NoteUpdate(BaseModel):
    """Payload for updating a note. All fields optional; only provided ones are changed."""

    title: Optional[str] = Field(None, min_length=1, max_length=120, description="Updated note title.")
    content: Optional[str] = Field(None, description="Updated note content.")


openapi_tags = [
    {"name": "Health", "description": "Health check endpoints."},
    {"name": "Notes", "description": "Create, read, update, and delete notes."},
]

app = FastAPI(
    title="Simple Notes API",
    description=(
        "A simple notes backend providing CRUD endpoints for notes. "
        "Notes are stored persistently using SQLite."
    ),
    version="1.0.0",
    openapi_tags=openapi_tags,
)

# Allow frontend to call backend during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_init_db()


@app.get("/", tags=["Health"], summary="Health check", operation_id="health_check")
def health_check():
    """Return a basic health payload to indicate the service is running."""
    return {"message": "Healthy"}


# PUBLIC_INTERFACE
@app.get(
    "/notes",
    response_model=List[Note],
    tags=["Notes"],
    summary="List notes",
    description="Returns all notes ordered by most recently updated first.",
    operation_id="list_notes",
)
def list_notes() -> List[Note]:
    """List all notes."""
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM notes ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    return [Note(**dict(r)) for r in rows]


# PUBLIC_INTERFACE
@app.post(
    "/notes",
    response_model=Note,
    status_code=status.HTTP_201_CREATED,
    tags=["Notes"],
    summary="Create note",
    description="Creates a new note and returns it.",
    operation_id="create_note",
)
def create_note(payload: NoteCreate) -> Note:
    """Create a new note."""
    now = _utc_now_iso()
    with _db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO notes(title, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (payload.title.strip(), payload.content, now, now),
        )
        conn.commit()
        note_id = int(cur.lastrowid)

        row = conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()

    return Note(**dict(row))


# PUBLIC_INTERFACE
@app.get(
    "/notes/{note_id}",
    response_model=Note,
    tags=["Notes"],
    summary="Get note",
    description="Fetch a single note by id.",
    operation_id="get_note",
)
def get_note(note_id: int) -> Note:
    """Get a note by id."""
    with _db_conn() as conn:
        row = conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")

    return Note(**dict(row))


# PUBLIC_INTERFACE
@app.put(
    "/notes/{note_id}",
    response_model=Note,
    tags=["Notes"],
    summary="Update note",
    description="Updates a note by id. Only provided fields are updated.",
    operation_id="update_note",
)
def update_note(note_id: int, payload: NoteUpdate) -> Note:
    """Update an existing note."""
    if payload.title is None and payload.content is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided to update")

    with _db_conn() as conn:
        existing = conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()

        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")

        new_title = (payload.title.strip() if payload.title is not None else existing["title"])
        new_content = (payload.content if payload.content is not None else existing["content"])
        now = _utc_now_iso()

        conn.execute(
            "UPDATE notes SET title = ?, content = ?, updated_at = ? WHERE id = ?",
            (new_title, new_content, now, note_id),
        )
        conn.commit()

        row = conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()

    return Note(**dict(row))


# PUBLIC_INTERFACE
@app.delete(
    "/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Notes"],
    summary="Delete note",
    description="Deletes a note by id.",
    operation_id="delete_note",
)
def delete_note(note_id: int) -> Response:
    """Delete a note by id."""
    with _db_conn() as conn:
        cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()

    if cur.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")

    return Response(status_code=status.HTTP_204_NO_CONTENT)
