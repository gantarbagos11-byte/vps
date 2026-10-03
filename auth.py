import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "auth.db"
SESSION_COOKIE = "migsock_session"
DEFAULT_SESSION_TTL = 12 * 60 * 60
SESSION_TTL_MIN = 5 * 60
SESSION_TTL_MAX = 30 * 24 * 60 * 60

# Bootstrap admin requested for this build. Only the salted scrypt hash is stored.
BOOTSTRAP_ADMIN_USERNAME = "chikovalen"
BOOTSTRAP_ADMIN_SALT = bytes.fromhex("e67a08f02b97b6b2135fe8b445e365a9")
BOOTSTRAP_ADMIN_HASH = bytes.fromhex("14bf4d770defe098e4197e22527bc6d54e4f8444de07e1fc5250a25af2ef662d")

SESSIONS = {}


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
            ("session_ttl_seconds", str(DEFAULT_SESSION_TTL)),
        )
        row = conn.execute(
            "SELECT username FROM users WHERE username = ? COLLATE NOCASE",
            (BOOTSTRAP_ADMIN_USERNAME,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(username,password_salt,password_hash,role,enabled,created_at) VALUES(?,?,?,?,?,?)",
                (BOOTSTRAP_ADMIN_USERNAME, BOOTSTRAP_ADMIN_SALT, BOOTSTRAP_ADMIN_HASH, "admin", 1, time.time()),
            )
        conn.commit()


def _hash_password(password: str, salt: bytes | None = None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt, digest


def verify_password(password: str, salt: bytes, expected: bytes) -> bool:
    _, actual = _hash_password(password, salt)
    return hmac.compare_digest(actual, expected)


def create_user(username: str, password: str, role: str = "user"):
    username = username.strip()
    role = role if role in {"user", "admin"} else "user"
    if not username or len(username) > 64:
        raise ValueError("Username tidak valid")
    if len(password) < 8:
        raise ValueError("Password minimal 8 karakter")
    salt, digest = _hash_password(password)
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO users(username,password_salt,password_hash,role,enabled,created_at) VALUES(?,?,?,?,?,?)",
                (username, salt, digest, role, 1, time.time()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("Username sudah terdaftar")


def list_users():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT username, role, enabled, created_at FROM users ORDER BY role DESC, username COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]


def authenticate(username: str, password: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT username,password_salt,password_hash,role,enabled FROM users WHERE username = ? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
    if not row or not row["enabled"]:
        return None
    if not verify_password(password, row["password_salt"], row["password_hash"]):
        return None
    return {"username": row["username"], "role": row["role"]}


def get_session_ttl():
    try:
        with _connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", ("session_ttl_seconds",)).fetchone()
        value = int(row[0]) if row else DEFAULT_SESSION_TTL
    except Exception:
        value = DEFAULT_SESSION_TTL
    return max(SESSION_TTL_MIN, min(SESSION_TTL_MAX, value))


def set_session_ttl(seconds: int):
    seconds = int(seconds)
    if seconds < SESSION_TTL_MIN or seconds > SESSION_TTL_MAX:
        raise ValueError("Durasi session harus antara 5 menit dan 30 hari")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("session_ttl_seconds", str(seconds)),
        )
        conn.commit()
    return seconds


def _cleanup_sessions():
    now = time.time()
    for token, session in list(SESSIONS.items()):
        if session["expires"] <= now:
            SESSIONS.pop(token, None)


def create_session(user):
    _cleanup_sessions()
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {
        "username": user["username"],
        "role": user["role"],
        "expires": time.time() + get_session_ttl(),
    }
    return token


def get_session(token):
    if not token:
        return None
    _cleanup_sessions()
    session = SESSIONS.get(token)
    if not session:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT enabled,role FROM users WHERE username = ? COLLATE NOCASE",
            (session["username"],),
        ).fetchone()
    if not row or not row["enabled"]:
        SESSIONS.pop(token, None)
        return None
    session["role"] = row["role"]
    session["expires"] = time.time() + get_session_ttl()
    return {"username": session["username"], "role": session["role"]}


def destroy_session(token):
    if token:
        SESSIONS.pop(token, None)


def set_enabled(username: str, enabled: bool):
    with _connect() as conn:
        row = conn.execute("SELECT role FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if not row:
            raise ValueError("User tidak ditemukan")
        if row["role"] == "admin" and not enabled:
            admins = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
            if admins <= 1:
                raise ValueError("Admin aktif terakhir tidak boleh dinonaktifkan")
        conn.execute("UPDATE users SET enabled=? WHERE username = ? COLLATE NOCASE", (1 if enabled else 0, username))
        conn.commit()
    # Invalidate active sessions for the account immediately when disabled.
    if not enabled:
        for token, session in list(SESSIONS.items()):
            if session["username"].casefold() == username.casefold():
                SESSIONS.pop(token, None)


def delete_user(username: str):
    with _connect() as conn:
        row = conn.execute("SELECT role FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if not row:
            raise ValueError("User tidak ditemukan")
        if row["role"] == "admin":
            admins = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
            if admins <= 1:
                raise ValueError("Admin terakhir tidak boleh dihapus")
        conn.execute("DELETE FROM users WHERE username = ? COLLATE NOCASE", (username,))
        conn.commit()
    for token, session in list(SESSIONS.items()):
        if session["username"].casefold() == username.casefold():
            SESSIONS.pop(token, None)


def change_password(username: str, new_password: str):
    if len(new_password) < 8:
        raise ValueError("Password minimal 8 karakter")
    salt, digest = _hash_password(new_password)
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE users SET password_salt=?, password_hash=? WHERE username = ? COLLATE NOCASE",
            (salt, digest, username),
        )
        if cur.rowcount != 1:
            raise ValueError("User tidak ditemukan")
        conn.commit()
    for token, session in list(SESSIONS.items()):
        if session["username"].casefold() == username.casefold():
            SESSIONS.pop(token, None)
