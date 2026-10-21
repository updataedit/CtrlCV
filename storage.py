"""SQLite-backed clipboard history."""
import hashlib
import sqlite3
import time
from dataclasses import dataclass


@dataclass
class Item:
    id: int
    kind: str          # 'text' | 'image' | 'files'
    text: str          # text content / file paths joined by \n / image description
    pinned: bool
    created: float
    thumb: bytes | None = None


class Store:
    def __init__(self, path: str, max_items: int = 500):
        self.max_items = max_items
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                kind    TEXT NOT NULL,
                text    TEXT NOT NULL DEFAULT '',
                html    TEXT,
                data    BLOB,
                thumb   BLOB,
                hash    TEXT NOT NULL UNIQUE,
                pinned  INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL
            )""")
        self.db.commit()

    @staticmethod
    def _hash(kind: str, src: bytes) -> str:
        return kind + ":" + hashlib.sha1(src).hexdigest()

    def _upsert(self, kind, hash_src: bytes, text, html=None, data=None, thumb=None) -> int:
        h = self._hash(kind, hash_src)
        now = time.time()
        row = self.db.execute("SELECT id FROM items WHERE hash=?", (h,)).fetchone()
        if row:
            # Same content copied again: bump it to the top, refresh rich data.
            self.db.execute("UPDATE items SET created=?, html=COALESCE(?, html) WHERE id=?", (now, html, row[0]))
            item_id = row[0]
        else:
            cur = self.db.execute(
                "INSERT INTO items(kind, text, html, data, thumb, hash, created) VALUES (?,?,?,?,?,?,?)",
                (kind, text, html, data, thumb, h, now))
            item_id = cur.lastrowid
        self._trim()
        self.db.commit()
        return item_id

    def add_text(self, text: str, html: str | None = None) -> int:
        return self._upsert("text", text.encode("utf-8", "surrogatepass"), text, html=html)

    def add_image(self, pixel_bytes: bytes, png: bytes, thumb: bytes, desc: str) -> int:
        return self._upsert("image", pixel_bytes, desc, data=png, thumb=thumb)

    def add_files(self, paths: list[str]) -> int:
        joined = "\n".join(paths)
        return self._upsert("files", joined.encode("utf-8", "surrogatepass"), joined)

    def _trim(self):
        self.db.execute("""
            DELETE FROM items WHERE pinned=0 AND id NOT IN (
                SELECT id FROM items WHERE pinned=0 ORDER BY created DESC LIMIT ?)""",
                        (self.max_items,))

    def list(self) -> list[Item]:
        rows = self.db.execute(
            "SELECT id, kind, text, pinned, created, thumb FROM items ORDER BY pinned DESC, created DESC")
        return [Item(r[0], r[1], r[2], bool(r[3]), r[4], r[5]) for r in rows]

    def get_payload(self, item_id: int):
        """-> (kind, text, html, data) or None"""
        return self.db.execute("SELECT kind, text, html, data FROM items WHERE id=?", (item_id,)).fetchone()

    def update_text(self, item_id: int, text: str):
        """Replace a text item's content (user edit). Rich formatting no longer matches, so drop it."""
        h = self._hash("text", text.encode("utf-8", "surrogatepass"))
        dup = self.db.execute("SELECT id, pinned FROM items WHERE hash=? AND id!=?", (h, item_id)).fetchone()
        if dup:  # edited into an existing entry: merge them
            self.db.execute("DELETE FROM items WHERE id=?", (dup[0],))
            if dup[1]:
                self.db.execute("UPDATE items SET pinned=1 WHERE id=?", (item_id,))
        self.db.execute("UPDATE items SET text=?, html=NULL, hash=? WHERE id=? AND kind='text'", (text, h, item_id))
        self.db.commit()

    def touch(self, item_id: int):
        self.db.execute("UPDATE items SET created=? WHERE id=?", (time.time(), item_id))
        self.db.commit()

    def toggle_pin(self, item_id: int):
        self.db.execute("UPDATE items SET pinned=1-pinned WHERE id=?", (item_id,))
        self.db.commit()

    def delete(self, item_id: int):
        self.db.execute("DELETE FROM items WHERE id=?", (item_id,))
        self.db.commit()

    def clear(self):
        """Delete everything except pinned items."""
        self.db.execute("DELETE FROM items WHERE pinned=0")
        self.db.commit()
        self.db.execute("VACUUM")
