import sqlite3
import os
import secrets
import hashlib
import hmac
import math
import base64
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
from cryptography.fernet import Fernet

# ── Reversible AES-256 / Fernet Encryption for Passwords ──
APP_SALT = b"qrprint_secure_salt_v1_2026_bd"
raw_cipher_secret = os.environ.get("DB_ENCRYPTION_KEY") or os.environ.get("SUPERADMIN_PASSWORD") or "qrprint_super_secret_cipher_key_default"
derived_cipher_key = base64.urlsafe_b64encode(hashlib.pbkdf2_hmac("sha256", raw_cipher_secret.encode("utf-8"), APP_SALT, 50_000))
_cipher = Fernet(derived_cipher_key)

def encrypt_password(password: str) -> str:
    """Encrypts password using AES (Fernet) so it is safe in DB but decipherable for Super Admin."""
    if not password:
        return ""
    if password.startswith("enc$"):
        return password
    token = _cipher.encrypt(password.encode("utf-8")).decode("utf-8")
    return f"enc${token}"

def decrypt_password(stored_password: str) -> str:
    """Decrypts AES-encrypted password to plain text for Super Admin view & copy."""
    if not stored_password:
        return ""
    if stored_password.startswith("enc$"):
        try:
            token = stored_password[4:]
            return _cipher.decrypt(token.encode("utf-8")).decode("utf-8")
        except Exception:
            return stored_password
    if stored_password.startswith("pbkdf2_sha256$"):
        return "[লকড / এডিটে নতুন দিন]"
    return stored_password

def hash_password(password: str) -> str:
    """Alias for encrypt_password for backward compatibility."""
    return encrypt_password(password)

def verify_password(stored_password: str, provided_password: str) -> bool:
    """Verifies password against stored AES ciphertext, PBKDF2 hash, or legacy plaintext."""
    if not stored_password or not provided_password:
        return False
    if stored_password.startswith("enc$"):
        dec = decrypt_password(stored_password)
        return hmac.compare_digest(dec, provided_password)
    if stored_password.startswith("pbkdf2_sha256$"):
        try:
            _, iter_str, salt, expected_hex = stored_password.split("$")
            iterations = int(iter_str)
            computed = hashlib.pbkdf2_hmac("sha256", provided_password.encode("utf-8"), salt.encode("utf-8"), iterations)
            return hmac.compare_digest(computed.hex(), expected_hex)
        except Exception:
            return False
    return hmac.compare_digest(stored_password, provided_password)

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "qrprint.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # 1. Shops Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS shops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        owner_name TEXT,
        phone TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        api_key TEXT UNIQUE NOT NULL,
        bkash_number TEXT,
        nagad_number TEXT,
        allow_counter_cash INTEGER DEFAULT 1,
        rate_bw_single REAL DEFAULT 3.0,
        rate_bw_double REAL DEFAULT 5.0,
        rate_color_single REAL DEFAULT 10.0,
        rate_color_double REAL DEFAULT 18.0,
        rate_photo_4x6 REAL DEFAULT 30.0,
        rate_photo_a4 REAL DEFAULT 40.0,
        is_active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    );
    """)
    
    # 2. Orders Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY,
        shop_id INTEGER NOT NULL,
        customer_name TEXT,
        customer_phone TEXT,
        file_path TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        file_type TEXT NOT NULL,
        page_count INTEGER DEFAULT 1,
        copies INTEGER DEFAULT 1,
        color_mode TEXT DEFAULT 'bw',
        paper_size TEXT DEFAULT 'A4',
        duplex TEXT DEFAULT 'single',
        page_range TEXT DEFAULT 'all',
        total_price REAL NOT NULL,
        payment_method TEXT DEFAULT 'counter_cash',
        trx_id TEXT,
        status TEXT DEFAULT 'pending_payment',
        created_at TEXT NOT NULL,
        printed_at TEXT,
        FOREIGN KEY (shop_id) REFERENCES shops (id)
    );
    """)
    
    # 3. Printers Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS printers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id INTEGER NOT NULL,
        printer_name TEXT NOT NULL,
        printer_type TEXT DEFAULT 'bw',
        is_online INTEGER DEFAULT 0,
        last_seen TEXT,
        FOREIGN KEY (shop_id) REFERENCES shops (id)
    );
    """)
    
    # Migration: add printer_target & orientation to orders table if not present
    order_cols = [r[1] for r in cursor.execute("PRAGMA table_info(orders)").fetchall()]
    if "printer_target" not in order_cols:
        cursor.execute("ALTER TABLE orders ADD COLUMN printer_target TEXT DEFAULT ''")
    if "orientation" not in order_cols:
        cursor.execute("ALTER TABLE orders ADD COLUMN orientation TEXT DEFAULT 'portrait'")

    # Migration: shops table extensions
    cols = [r[1] for r in cursor.execute("PRAGMA table_info(shops)").fetchall()]
    if "rate_photo_a4" not in cols:
        cursor.execute("ALTER TABLE shops ADD COLUMN rate_photo_a4 REAL DEFAULT 40.0")
    if "remove_bg_api_key" not in cols:
        cursor.execute("ALTER TABLE shops ADD COLUMN remove_bg_api_key TEXT DEFAULT ''")
    if "file_retention_days" not in cols:
        cursor.execute("ALTER TABLE shops ADD COLUMN file_retention_days REAL DEFAULT 1.0")

    # 4. File Transfers Table (customer -> shop inbox, shop -> customer share links)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transfers (
        id TEXT PRIMARY KEY,
        shop_id INTEGER NOT NULL,
        direction TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        file_size INTEGER DEFAULT 0,
        sender_name TEXT,
        note TEXT,
        one_time INTEGER DEFAULT 0,
        download_count INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        FOREIGN KEY (shop_id) REFERENCES shops (id)
    );
    """)

    # 5. QR transfer rooms (phone <-> PC, no login needed)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transfer_rooms (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS room_files (
        id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        file_size INTEGER DEFAULT 0,
        uploader TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (room_id) REFERENCES transfer_rooms (id)
    );
    """)

    # 6. System Settings Table (Super Admin credentials & global controls)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS login_lockouts (
        identifier TEXT PRIMARY KEY,
        failed_attempts INTEGER DEFAULT 0,
        locked_until TEXT DEFAULT '',
        last_attempt TEXT NOT NULL,
        ip_address TEXT DEFAULT ''
    );
    """)

    default_sa_phone = os.environ.get("SUPERADMIN_PHONE", "01900000000").strip()
    default_sa_pass = os.environ.get("SUPERADMIN_PASSWORD", "admin123456").strip()
    hashed_sa_pass = hash_password(default_sa_pass)
    cursor.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('superadmin_phone', ?)", (default_sa_phone,))
    cursor.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('superadmin_password', ?)", (hashed_sa_pass,))

    # Auto-migrate any unencrypted passwords to AES encryption
    try:
        shops_to_migrate = cursor.execute("SELECT id, password FROM shops").fetchall()
        for s in shops_to_migrate:
            pw = s["password"] or ""
            if pw and not pw.startswith("enc$") and not pw.startswith("pbkdf2_sha256$"):
                cursor.execute("UPDATE shops SET password = ? WHERE id = ?", (encrypt_password(pw), s["id"]))
        sa_row = cursor.execute("SELECT value FROM system_settings WHERE key = 'superadmin_password'").fetchone()
        if sa_row:
            sa_pw = sa_row["value"] or ""
            if sa_pw and not sa_pw.startswith("enc$") and not sa_pw.startswith("pbkdf2_sha256$"):
                cursor.execute("UPDATE system_settings SET value = ? WHERE key = 'superadmin_password'", (encrypt_password(sa_pw),))
    except Exception as e:
        pass

    conn.commit()
    
    # Seed a demo shop if no shops exist
    cursor.execute("SELECT COUNT(*) as count FROM shops")
    if cursor.fetchone()["count"] == 0:
        demo_api_key = "qsp_demo_satata_101"
        now = datetime.now().isoformat()
        cursor.execute("""
        INSERT INTO shops (
            shop_code, name, owner_name, phone, password, api_key, 
            bkash_number, nagad_number, allow_counter_cash,
            rate_bw_single, rate_bw_double, rate_color_single, rate_color_double, rate_photo_4x6,
            created_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """, (
            "demo101", "সততা ফটোকপি ও প্রিন্ট জোন", "রফিকুল ইসলাম", "01700000000", hash_password("123456"), demo_api_key,
            "01700000000", "01700000000", 1,
            3.0, 5.0, 10.0, 18.0, 30.0,
            now
        ))
        conn.commit()
        print(f"[*] Demo shop created! Code: demo101, Pass: 123456, API Key: {demo_api_key}")
        
    conn.close()

# DB Helpers
def get_shop_by_code(shop_code: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    clean = (shop_code or "").strip().lower()
    cursor.execute("SELECT * FROM shops WHERE LOWER(shop_code) = ? AND is_active = 1", (clean,))
    shop = cursor.fetchone()
    if not shop:
        cursor.execute("SELECT * FROM shops WHERE LOWER(shop_code) = ?", (clean,))
        shop = cursor.fetchone()
    conn.close()
    if shop:
        d = dict(shop)
        d["password"] = decrypt_password(d.get("password") or "")
        return d
    return None

def get_shop_by_id(shop_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shops WHERE id = ?", (shop_id,))
    shop = cursor.fetchone()
    conn.close()
    if shop:
        d = dict(shop)
        d["password"] = decrypt_password(d.get("password") or "")
        return d
    return None

def get_shop_by_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shops WHERE api_key = ? AND is_active = 1", (api_key,))
    shop = cursor.fetchone()
    if not shop and api_key == "qsp_demo_satata_101":
        # Always fallback to demo shop if master demo key is used
        cursor.execute("SELECT * FROM shops WHERE shop_code = 'demo101' AND is_active = 1")
        shop = cursor.fetchone()
    conn.close()
    return dict(shop) if shop else None

def authenticate_shop(phone: str, password: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shops WHERE phone = ? AND is_active = 1", (phone.strip(),))
    rows = cursor.fetchall()
    for row in rows:
        if verify_password(row["password"], password.strip()):
            if not row["password"].startswith("enc$"):
                new_enc = encrypt_password(password.strip())
                conn.execute("UPDATE shops SET password = ? WHERE id = ?", (new_enc, row["id"]))
                conn.commit()
            conn.close()
            d = dict(row)
            d["password"] = password.strip()
            return d
    conn.close()
    return None


def get_shop_by_credentials_any_status(phone: str, password: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM shops WHERE phone = ?", (phone.strip(),))
    rows = cursor.fetchall()
    for row in rows:
        if verify_password(row["password"], password.strip()):
            if not row["password"].startswith("enc$"):
                new_enc = encrypt_password(password.strip())
                conn.execute("UPDATE shops SET password = ? WHERE id = ?", (new_enc, row["id"]))
                conn.commit()
            conn.close()
            d = dict(row)
            d["password"] = password.strip()
            return d
    conn.close()
    return None

def update_shop_settings(shop_id: int, data: Dict[str, Any]):
    conn = get_db()
    cursor = conn.cursor()
    try:
        ret_days = max(0.02, min(30.0, float(data.get("file_retention_days", 1.0))))
    except (TypeError, ValueError):
        ret_days = 1.0
    cursor.execute("""
    UPDATE shops SET 
        name = ?,
        owner_name = ?,
        bkash_number = ?,
        nagad_number = ?,
        allow_counter_cash = ?,
        rate_bw_single = ?,
        rate_bw_double = ?,
        rate_color_single = ?,
        rate_color_double = ?,
        rate_photo_4x6 = ?,
        rate_photo_a4 = ?,
        remove_bg_api_key = ?,
        file_retention_days = ?
    WHERE id = ?
    """, (
        data.get("name"),
        data.get("owner_name"),
        data.get("bkash_number"),
        data.get("nagad_number"),
        data.get("allow_counter_cash", 1),
        data.get("rate_bw_single", 3.0),
        data.get("rate_bw_double", 5.0),
        data.get("rate_color_single", 10.0),
        data.get("rate_color_double", 18.0),
        data.get("rate_photo_4x6", 30.0),
        data.get("rate_photo_a4", 40.0),
        data.get("remove_bg_api_key", ""),
        ret_days,
        shop_id
    ))
    conn.commit()
    conn.close()

def create_order(order_data: Dict[str, Any]) -> str:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO orders (
        id, shop_id, customer_name, customer_phone, file_path, original_filename,
        file_type, page_count, copies, color_mode, paper_size, duplex,
        page_range, orientation, printer_target, total_price, payment_method, trx_id, status, created_at
    ) VALUES (
        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
    )
    """, (
        order_data["id"],
        order_data["shop_id"],
        order_data.get("customer_name", ""),
        order_data.get("customer_phone", ""),
        order_data["file_path"],
        order_data["original_filename"],
        order_data["file_type"],
        order_data.get("page_count", 1),
        order_data.get("copies", 1),
        order_data.get("color_mode", "bw"),
        order_data.get("paper_size", "A4"),
        order_data.get("duplex", "single"),
        order_data.get("page_range", "all"),
        order_data.get("orientation", "portrait"),
        order_data.get("printer_target", ""),
        order_data["total_price"],
        order_data.get("payment_method", "counter_cash"),
        order_data.get("trx_id", ""),
        order_data.get("status", "pending_payment"),
        order_data.get("created_at", datetime.now().isoformat())
    ))
    conn.commit()
    conn.close()
    return order_data["id"]

def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order = cursor.fetchone()
    conn.close()
    return dict(order) if order else None

def update_order_details(order_id: str, updates: Dict[str, Any]):
    """Allows shop admin to edit print parameters (copies, color, paper_size, page_range, printer_target, total_price, etc.)"""
    allowed_keys = {
        "copies", "color_mode", "paper_size", "duplex", "page_range",
        "orientation", "total_price", "printer_target", "customer_name"
    }
    filtered = {k: v for k, v in updates.items() if k in allowed_keys}
    if not filtered:
        return
    set_clause = ", ".join([f"{k} = ?" for k in filtered.keys()])
    values = list(filtered.values()) + [order_id]
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE orders SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()

def update_order_status(order_id: str, status: str, printed_at: Optional[str] = None):
    conn = get_db()
    cursor = conn.cursor()
    if printed_at:
        cursor.execute("UPDATE orders SET status = ?, printed_at = ? WHERE id = ?", (status, printed_at, order_id))
    else:
        cursor.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()

def mark_order_file_purged(order_id: str):
    """Clears file_path in DB once the underlying print file has been permanently deleted."""
    conn = get_db()
    conn.execute("UPDATE orders SET file_path = '' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()

def delete_order_record(order_id: str, shop_id: int):
    """Completely removes an order row from the database for the given shop."""
    conn = get_db()
    conn.execute("DELETE FROM orders WHERE id = ? AND shop_id = ?", (order_id, shop_id))
    conn.commit()
    conn.close()

def get_shop_history_orders_with_files(shop_id: int) -> List[Dict[str, Any]]:
    """Returns all completed/cancelled orders for a shop that still have a stored file_path."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, file_path FROM orders WHERE shop_id = ? AND status IN ('completed', 'cancelled') AND file_path != ''",
        (shop_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_completed_orders_to_purge(now_ts: float) -> List[Dict[str, Any]]:
    """Returns completed orders whose printed_at timestamp has exceeded their shop's file_retention_days."""
    conn = get_db()
    rows = conn.execute("""
        SELECT o.id, o.file_path, o.printed_at, COALESCE(s.file_retention_days, 1.0) AS retention_days
        FROM orders o
        LEFT JOIN shops s ON o.shop_id = s.id
        WHERE o.status = 'completed' AND o.file_path != '' AND o.printed_at IS NOT NULL
    """).fetchall()
    conn.close()
    expired = []
    for r in rows:
        d = dict(r)
        try:
            dt = datetime.fromisoformat(str(d["printed_at"]).replace("Z", "+00:00"))
            age_sec = now_ts - dt.timestamp()
            max_sec = float(d.get("retention_days") or 1.0) * 86400.0
            if age_sec >= max_sec:
                expired.append(d)
        except Exception:
            pass
    return expired

def get_shop_orders(shop_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE shop_id = ? ORDER BY created_at DESC LIMIT ?", (shop_id, limit))
    orders = cursor.fetchall()
    conn.close()
    return [dict(r) for r in orders]

def get_shop_stats(shop_id: int) -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as total_orders, COALESCE(SUM(total_price), 0) as total_revenue FROM orders WHERE shop_id = ? AND status IN ('paid', 'printing', 'completed')", (shop_id,))
    totals = cursor.fetchone()
    
    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("SELECT COUNT(*) as today_orders, COALESCE(SUM(total_price), 0) as today_revenue FROM orders WHERE shop_id = ? AND status IN ('paid', 'printing', 'completed') AND created_at LIKE ?", (shop_id, f"{today}%"))
    today_stats = cursor.fetchone()
    
    conn.close()
    return {
        "total_orders": totals["total_orders"],
        "total_revenue": totals["total_revenue"],
        "today_orders": today_stats["today_orders"],
        "today_revenue": today_stats["today_revenue"]
    }


# ── File transfer helpers ──
def create_transfer(t: Dict[str, Any]) -> str:
    conn = get_db()
    conn.execute("""
    INSERT INTO transfers (
        id, shop_id, direction, original_filename, stored_path, file_size,
        sender_name, note, one_time, created_at, expires_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        t["id"], t["shop_id"], t["direction"], t["original_filename"], t["stored_path"],
        t.get("file_size", 0), t.get("sender_name", ""), t.get("note", ""),
        1 if t.get("one_time") else 0, t["created_at"], t["expires_at"]
    ))
    conn.commit()
    conn.close()
    return t["id"]


def get_transfer(transfer_id: str) -> Optional[sqlite3.Row]:
    conn = get_db()
    row = conn.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    conn.close()
    return row


def list_shop_transfers(shop_id: int, limit: int = 100) -> List[sqlite3.Row]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM transfers WHERE shop_id = ? ORDER BY created_at DESC LIMIT ?",
        (shop_id, limit)
    ).fetchall()
    conn.close()
    return rows


def count_transfers(shop_id: int, direction: str) -> int:
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM transfers WHERE shop_id = ? AND direction = ?", (shop_id, direction)
    ).fetchone()[0]
    conn.close()
    return n


def increment_transfer_download(transfer_id: str):
    conn = get_db()
    conn.execute("UPDATE transfers SET download_count = download_count + 1 WHERE id = ?", (transfer_id,))
    conn.commit()
    conn.close()


def delete_transfer_row(transfer_id: str):
    conn = get_db()
    conn.execute("DELETE FROM transfers WHERE id = ?", (transfer_id,))
    conn.commit()
    conn.close()


def get_expired_transfers(now_iso: str) -> List[sqlite3.Row]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM transfers WHERE expires_at < ?", (now_iso,)).fetchall()
    conn.close()
    return rows


# ── QR transfer rooms ──
def create_room(room_id: str, created_at: str, expires_at: str):
    conn = get_db()
    conn.execute("INSERT INTO transfer_rooms (id, created_at, expires_at) VALUES (?, ?, ?)", (room_id, created_at, expires_at))
    conn.commit()
    conn.close()


def get_room(room_id: str) -> Optional[sqlite3.Row]:
    conn = get_db()
    row = conn.execute("SELECT * FROM transfer_rooms WHERE id = ?", (room_id,)).fetchone()
    conn.close()
    return row


def delete_room(room_id: str):
    conn = get_db()
    conn.execute("DELETE FROM room_files WHERE room_id = ?", (room_id,))
    conn.execute("DELETE FROM transfer_rooms WHERE id = ?", (room_id,))
    conn.commit()
    conn.close()


def get_expired_rooms(now_iso: str) -> List[sqlite3.Row]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM transfer_rooms WHERE expires_at < ?", (now_iso,)).fetchall()
    conn.close()
    return rows


def add_room_file(f: Dict[str, Any]):
    conn = get_db()
    conn.execute("""
    INSERT INTO room_files (id, room_id, original_filename, stored_path, file_size, uploader, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (f["id"], f["room_id"], f["original_filename"], f["stored_path"], f["file_size"], f.get("uploader", ""), f["created_at"]))
    conn.commit()
    conn.close()


def list_room_files(room_id: str) -> List[sqlite3.Row]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM room_files WHERE room_id = ? ORDER BY created_at DESC", (room_id,)).fetchall()
    conn.close()
    return rows


def get_room_file(file_id: str) -> Optional[sqlite3.Row]:
    conn = get_db()
    row = conn.execute("SELECT * FROM room_files WHERE id = ?", (file_id,)).fetchone()
    conn.close()
    return row


def delete_room_file(file_id: str):
    conn = get_db()
    conn.execute("DELETE FROM room_files WHERE id = ?", (file_id,))
    conn.commit()
    conn.close()


# ── Shop Printers Helpers ──
DEFAULT_SHOP_PRINTERS = [
    ("EPSON Color Inkjet (রঙিন ও ফটো প্রিন্টার)", "color"),
    ("Canon / HP LaserJet (সাদাকালো ফাস্ট প্রিন্টার)", "bw"),
]

def get_shop_printers(shop_id: int) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM printers WHERE shop_id = ? ORDER BY is_online DESC, id ASC",
        (shop_id,)
    ).fetchall()
    default_names = {p[0] for p in DEFAULT_SHOP_PRINTERS}
    has_real = any(r["printer_name"] not in default_names for r in rows)

    # If this shop only has placeholder printers, check if an agent synced real PC printers to any shop
    if not has_real:
        global_real = conn.execute(
            "SELECT DISTINCT printer_name, printer_type FROM printers WHERE printer_name NOT IN (?, ?) ORDER BY id ASC",
            tuple(default_names)
        ).fetchall()
        if global_real:
            for p_name in default_names:
                conn.execute("DELETE FROM printers WHERE shop_id = ? AND printer_name = ?", (shop_id, p_name))
            now = datetime.now().isoformat()
            for gr in global_real:
                conn.execute(
                    "INSERT INTO printers (shop_id, printer_name, printer_type, is_online, last_seen) VALUES (?, ?, ?, 1, ?)",
                    (shop_id, gr["printer_name"], gr["printer_type"] or "all", now)
                )
            conn.commit()
            rows = conn.execute(
                "SELECT * FROM printers WHERE shop_id = ? ORDER BY is_online DESC, id ASC",
                (shop_id,)
            ).fetchall()

    if not rows:
        now = datetime.now().isoformat()
        for p_name, p_type in DEFAULT_SHOP_PRINTERS:
            conn.execute(
                "INSERT INTO printers (shop_id, printer_name, printer_type, is_online, last_seen) VALUES (?, ?, ?, 1, ?)",
                (shop_id, p_name, p_type, now)
            )
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM printers WHERE shop_id = ? ORDER BY is_online DESC, id ASC",
            (shop_id,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_shop_printer(shop_id: int, printer_name: str, printer_type: str = "all") -> int:
    name = (printer_name or "").strip()[:100]
    if not name:
        return 0
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM printers WHERE shop_id = ? AND LOWER(printer_name) = LOWER(?)",
        (shop_id, name)
    ).fetchone()
    now = datetime.now().isoformat()
    if existing:
        conn.execute("UPDATE printers SET printer_type = ?, is_online = 1, last_seen = ? WHERE id = ?", (printer_type, now, existing["id"]))
        pid = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO printers (shop_id, printer_name, printer_type, is_online, last_seen) VALUES (?, ?, ?, 1, ?)",
            (shop_id, name, printer_type, now)
        )
        pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid

def delete_shop_printer(shop_id: int, printer_id: int):
    conn = get_db()
    conn.execute("DELETE FROM printers WHERE id = ? AND shop_id = ?", (printer_id, shop_id))
    conn.commit()
    conn.close()

def sync_agent_printers(shop_id: int, printer_names: List[str]):
    if not printer_names:
        return
    conn = get_db()
    now = datetime.now().isoformat()
    # Remove initial placeholder printers once real PC printers are synced
    for p_name, _ in DEFAULT_SHOP_PRINTERS:
        conn.execute("DELETE FROM printers WHERE shop_id = ? AND printer_name = ?", (shop_id, p_name))

    for raw_name in printer_names:
        name = str(raw_name or "").strip()[:100]
        if not name:
            continue
        existing = conn.execute(
            "SELECT id FROM printers WHERE shop_id = ? AND LOWER(printer_name) = LOWER(?)",
            (shop_id, name)
        ).fetchone()
        if existing:
            conn.execute("UPDATE printers SET is_online = 1, last_seen = ? WHERE id = ?", (now, existing["id"]))
        else:
            conn.execute(
                "INSERT INTO printers (shop_id, printer_name, printer_type, is_online, last_seen) VALUES (?, ?, 'all', 1, ?)",
                (shop_id, name, now)
            )
    conn.commit()
    conn.close()


# ── SUPER ADMIN & SHOP CONTROL HELPERS ──

def get_superadmin_credentials() -> Dict[str, str]:
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM system_settings WHERE key IN ('superadmin_phone', 'superadmin_password')").fetchall()
    conn.close()
    data = {r["key"]: r["value"] for r in rows}
    raw_pass = data.get("superadmin_password") or os.environ.get("SUPERADMIN_PASSWORD", "admin123456").strip()
    return {
        "phone": data.get("superadmin_phone") or os.environ.get("SUPERADMIN_PHONE", "01900000000").strip(),
        "password": decrypt_password(raw_pass),
    }


def update_superadmin_credentials(phone: str, password: str):
    conn = get_db()
    hashed = hash_password(password.strip()) if not password.startswith("pbkdf2_sha256$") else password.strip()
    conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('superadmin_phone', ?)", (phone.strip(),))
    conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('superadmin_password', ?)", (hashed,))
    conn.commit()
    conn.close()


def authenticate_superadmin(phone: str, password: str) -> bool:
    creds = get_superadmin_credentials()
    env_phone = os.environ.get("SUPERADMIN_PHONE", "").strip()
    env_pass = os.environ.get("SUPERADMIN_PASSWORD", "").strip()
    p = (phone or "").strip()
    pw = (password or "").strip()
    if p == creds["phone"] and verify_password(creds["password"], pw):
        if not creds["password"].startswith("enc$"):
            update_superadmin_credentials(p, pw)
        return True
    if env_phone and env_pass and p == env_phone and verify_password(env_pass, pw):
        return True
    return False


def get_all_shops_with_stats() -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute("""
        SELECT s.*,
               (SELECT COUNT(*) FROM orders o WHERE o.shop_id = s.id) AS total_orders,
               (SELECT COALESCE(SUM(o.total_price), 0) FROM orders o WHERE o.shop_id = s.id AND o.status IN ('paid', 'printing', 'completed')) AS total_revenue
        FROM shops s
        ORDER BY s.id DESC
    """).fetchall()
    conn.close()
    res = []
    for r in rows:
        d = dict(r)
        d["password"] = decrypt_password(d.get("password") or "")
        res.append(d)
    return res


def update_shop_by_superadmin(shop_id: int, shop_code: str, name: str, owner_name: str, phone: str, password: str, is_active: int = 1):
    conn = get_db()
    hashed = hash_password(password.strip()) if not password.startswith("pbkdf2_sha256$") else password.strip()
    conn.execute("""
        UPDATE shops
        SET shop_code = ?, name = ?, owner_name = ?, phone = ?, password = ?, is_active = ?
        WHERE id = ?
    """, (shop_code.strip().lower(), name.strip(), owner_name.strip(), phone.strip(), hashed, 1 if is_active else 0, shop_id))
    conn.commit()
    conn.close()


def toggle_shop_active(shop_id: int, is_active: int):
    conn = get_db()
    conn.execute("UPDATE shops SET is_active = ? WHERE id = ?", (1 if is_active else 0, shop_id))
    conn.commit()
    conn.close()


def delete_shop_by_superadmin(shop_id: int):
    conn = get_db()
    conn.execute("DELETE FROM printers WHERE shop_id = ?", (shop_id,))
    conn.execute("DELETE FROM transfers WHERE shop_id = ?", (shop_id,))
    conn.execute("DELETE FROM orders WHERE shop_id = ?", (shop_id,))
    conn.execute("DELETE FROM shops WHERE id = ?", (shop_id,))
    conn.commit()
    conn.close()


# ── Login Rate Limiting & Account Lockout (5 attempts -> 10 mins lock) ──
def is_account_locked(identifier: str) -> Tuple[bool, int]:
    """Checks if account is locked. Returns (is_locked, remaining_minutes)."""
    if not identifier:
        return False, 0
    clean_id = identifier.strip().lower()
    conn = get_db()
    row = conn.execute("SELECT * FROM login_lockouts WHERE identifier = ?", (clean_id,)).fetchone()
    conn.close()
    if not row or not row["locked_until"]:
        return False, 0
    now = datetime.now(timezone.utc)
    try:
        locked_until = datetime.fromisoformat(row["locked_until"])
        if locked_until > now:
            remaining_secs = (locked_until - now).total_seconds()
            return True, max(1, int(math.ceil(remaining_secs / 60.0)))
    except Exception:
        pass
    return False, 0


def record_login_failure(identifier: str, ip: str = "") -> Tuple[int, bool, int]:
    """Records failed attempt. If >= 5, locks for 10 minutes. Returns (attempts, is_locked, lock_mins)."""
    if not identifier:
        return 1, False, 0
    clean_id = identifier.strip().lower()
    conn = get_db()
    row = conn.execute("SELECT * FROM login_lockouts WHERE identifier = ?", (clean_id,)).fetchone()
    now = datetime.now(timezone.utc)
    
    attempts = (row["failed_attempts"] if row else 0) + 1
    locked_until_str = ""
    is_locked = False
    lock_mins = 0

    if attempts >= 5:
        is_locked = True
        lock_mins = 10
        locked_until = now + timedelta(minutes=10)
        locked_until_str = locked_until.isoformat()

    conn.execute("""
    INSERT INTO login_lockouts (identifier, failed_attempts, locked_until, last_attempt, ip_address)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(identifier) DO UPDATE SET
        failed_attempts = ?,
        locked_until = ?,
        last_attempt = ?,
        ip_address = ?
    """, (clean_id, attempts, locked_until_str, now.isoformat(), ip, attempts, locked_until_str, now.isoformat(), ip))
    conn.commit()
    conn.close()
    return attempts, is_locked, lock_mins


def record_login_success(identifier: str):
    """Clears failed login attempts for an identifier on successful login."""
    if not identifier:
        return
    clean_id = identifier.strip().lower()
    conn = get_db()
    conn.execute("DELETE FROM login_lockouts WHERE identifier = ?", (clean_id,))
    conn.commit()
    conn.close()


def list_locked_accounts() -> List[Dict[str, Any]]:
    """Returns list of currently locked accounts for Super Admin dashboard."""
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    rows = conn.execute("SELECT * FROM login_lockouts WHERE locked_until > ? ORDER BY last_attempt DESC", (now_iso,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def unlock_account(identifier: str) -> bool:
    """Instantly unlocks an account by Super Admin."""
    if not identifier:
        return False
    clean_id = identifier.strip().lower()
    conn = get_db()
    conn.execute("DELETE FROM login_lockouts WHERE identifier = ?", (clean_id,))
    conn.commit()
    conn.close()
    return True
