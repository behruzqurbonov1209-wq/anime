import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "bot_data.db")


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id     INTEGER PRIMARY KEY,
                    username    TEXT DEFAULT '',
                    full_name   TEXT DEFAULT '',
                    joined_at   TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS content (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    category    TEXT NOT NULL CHECK(category IN ('anime','drama','kino')),
                    code        TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    poster      TEXT DEFAULT '',
                    added_at    TEXT DEFAULT (datetime('now')),
                    UNIQUE(category, code)
                );

                CREATE TABLE IF NOT EXISTS episodes (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    category    TEXT NOT NULL,
                    code        TEXT NOT NULL,
                    episode_num INTEGER NOT NULL,
                    title       TEXT DEFAULT '',
                    file_id     TEXT NOT NULL,
                    file_type   TEXT NOT NULL DEFAULT 'video',
                    added_at    TEXT DEFAULT (datetime('now')),
                    UNIQUE(category, code, episode_num)
                );

                CREATE TABLE IF NOT EXISTS channels (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id     TEXT NOT NULL UNIQUE,
                    title       TEXT DEFAULT '',
                    username    TEXT DEFAULT '',
                    added_at    TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS admins (
                    user_id     INTEGER PRIMARY KEY,
                    username    TEXT DEFAULT '',
                    full_name   TEXT DEFAULT '',
                    added_by    INTEGER NOT NULL,
                    role        TEXT DEFAULT 'content',
                    added_at    TEXT DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_content_cat_code ON content(category, code);
                CREATE INDEX IF NOT EXISTS idx_episodes_cat_code ON episodes(category, code);
            """)
            # Mavjud DB ga poster ustun qo'shish (migration)
            try:
                conn.execute("ALTER TABLE content ADD COLUMN poster TEXT DEFAULT ''")
            except Exception:
                pass
            # Mavjud DB ga role ustun qo'shish (migration)
            try:
                conn.execute("ALTER TABLE admins ADD COLUMN role TEXT DEFAULT 'content'")
            except Exception:
                pass

    def add_user(self, user_id: int, username: str, full_name: str):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO users (user_id, username, full_name)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       username  = excluded.username,
                       full_name = excluded.full_name""",
                (user_id, username, full_name)
            )

    def get_all_users(self) -> list:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()
            return [dict(r) for r in rows]

    def get_recent_users(self, limit: int = 10) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY joined_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def add_item(self, category: str, code: str, title: str, description: str = "", poster: str = ""):
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO content (category, code, title, description, poster)
                   VALUES (?, ?, ?, ?, ?)""",
                (category, code.upper(), title, description, poster)
            )

    def update_poster(self, category: str, code: str, poster: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE content SET poster=? WHERE category=? AND code=?",
                (poster, category, code.upper())
            )

    def get_item(self, category: str, code: str):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM content WHERE category=? AND code=?",
                (category, code.upper())
            ).fetchone()
            return dict(row) if row else None

    def get_all_items(self, category: str) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT c.*, COUNT(e.id) as episode_count FROM content c "
                "LEFT JOIN episodes e ON c.category=e.category AND c.code=e.code "
                "WHERE c.category=? GROUP BY c.id ORDER BY c.code",
                (category,)
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_item(self, category: str, code: str) -> bool:
        with self._conn() as conn:
            conn.execute("DELETE FROM episodes WHERE category=? AND code=?", (category, code.upper()))
            cur = conn.execute("DELETE FROM content WHERE category=? AND code=?", (category, code.upper()))
            return cur.rowcount > 0

    def add_episode(self, category: str, code: str, episode_num: int,
                    file_id: str, file_type: str = "video", title: str = ""):
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO episodes
                   (category, code, episode_num, title, file_id, file_type)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (category, code.upper(), episode_num, title, file_id, file_type)
            )

    def get_episodes(self, category: str, code: str) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE category=? AND code=? ORDER BY episode_num",
                (category, code.upper())
            ).fetchall()
            return [dict(r) for r in rows]

    def get_episode(self, category: str, code: str, episode_num: int):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM episodes WHERE category=? AND code=? AND episode_num=?",
                (category, code.upper(), episode_num)
            ).fetchone()
            return dict(row) if row else None

    def delete_episode(self, category: str, code: str, episode_num: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM episodes WHERE category=? AND code=? AND episode_num=?",
                (category, code.upper(), episode_num)
            )
            return cur.rowcount > 0

    def get_next_episode_num(self, category: str, code: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(episode_num) FROM episodes WHERE category=? AND code=?",
                (category, code.upper())
            ).fetchone()
            return (row[0] or 0) + 1

    def get_stats(self) -> dict:
        with self._conn() as conn:
            users    = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            anime    = conn.execute("SELECT COUNT(*) FROM content WHERE category='anime'").fetchone()[0]
            drama    = conn.execute("SELECT COUNT(*) FROM content WHERE category='drama'").fetchone()[0]
            kino     = conn.execute("SELECT COUNT(*) FROM content WHERE category='kino'").fetchone()[0]
            episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            return {"users": users, "anime": anime, "drama": drama, "kino": kino, "episodes": episodes}

    # ── ADMINLAR ──────────────────────────────────────────────────────────

    def add_admin(self, user_id: int, username: str, full_name: str, added_by: int, role: str = "content"):
        """
        role = 'content'  — faqat kontent qo'shish/o'chirish
        role = 'manager'  — kontent + admin qo'shish/o'chirish
        """
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO admins (user_id, username, full_name, added_by, role)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, username, full_name, added_by, role)
            )

    def get_admin_role(self, user_id: int) -> str:
        """Admin rolini qaytaradi: 'super', 'manager', 'content' yoki ''"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT role FROM admins WHERE user_id=?", (user_id,)
            ).fetchone()
            return row["role"] if row else ""

    def can_delete_content(self, user_id: int, super_ids: list) -> bool:
        """Kontent o'chira oladimi?"""
        if user_id in super_ids:
            return True
        role = self.get_admin_role(user_id)
        return role in ("content", "manager")

    def can_manage_admins(self, user_id: int, super_ids: list) -> bool:
        """Admin qo'sha/o'chira oladimi?"""
        if user_id in super_ids:
            return True
        role = self.get_admin_role(user_id)
        return role == "manager"

    def remove_admin(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
            return cur.rowcount > 0

    def get_admins(self) -> list:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM admins ORDER BY added_at").fetchall()
            return [dict(r) for r in rows]

    def is_admin(self, user_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
            return row is not None

    # ── KANALLAR ──────────────────────────────────────────────────────────

    def add_channel(self, chat_id: str, title: str = "", username: str = ""):
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO channels (chat_id, title, username)
                   VALUES (?, ?, ?)""",
                (str(chat_id), title, username)
            )

    def remove_channel(self, chat_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM channels WHERE chat_id=?", (str(chat_id),))
            return cur.rowcount > 0

    def get_channels(self) -> list:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM channels ORDER BY added_at").fetchall()
            return [dict(r) for r in rows]


db = Database()
