"""
Cloudflare R2 Storage Adapter (S3-compatible) with automatic CORS setup,
direct Presigned URL redirects (0 Render egress bandwidth), and local fallback.
"""
import os
import mimetypes
import sqlite3
import threading
import time
from urllib.parse import quote
from typing import Optional

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "qrprint-storage").strip()
R2_PUBLIC_DOMAIN = os.environ.get("R2_PUBLIC_DOMAIN", "").strip().rstrip("/")

_s3_client = None
_cors_configured = False
_r2_uploaded_keys = set()


def is_r2_configured() -> bool:
    return bool(R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME)


def get_r2_client():
    global _s3_client, _cors_configured
    if not is_r2_configured():
        return None
    if _s3_client is None:
        try:
            import boto3
            from botocore.config import Config
            endpoint = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
            _s3_client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=R2_ACCESS_KEY_ID,
                aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                config=Config(signature_version="s3v4"),
                region_name="auto"
            )
        except Exception as e:
            print(f"[R2] Client initialization error: {e}")
            return None
    if _s3_client is not None and not _cors_configured:
        _cors_configured = True
        try:
            _s3_client.put_bucket_cors(
                Bucket=R2_BUCKET_NAME,
                CORSConfiguration={
                    "CORSRules": [
                        {
                            "AllowedOrigins": ["*"],
                            "AllowedMethods": ["GET", "PUT", "HEAD"],
                            "AllowedHeaders": ["*"],
                            "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
                            "MaxAgeSeconds": 86400,
                        }
                    ]
                },
            )
        except Exception as e:
            print(f"[R2] CORS auto-config note: {e}")
    return _s3_client


def path_to_r2_key(local_path: str) -> str:
    """Maps a local path inside uploads/ to a deterministic Cloudflare R2 key."""
    if not local_path:
        return ""
    norm = os.path.normpath(local_path).replace("\\", "/")
    if "/uploads/" in norm:
        return "uploads/" + norm.split("/uploads/", 1)[1].lstrip("/")
    return "uploads/" + os.path.basename(norm)


def upload_file_to_r2(local_path: str, r2_key: Optional[str] = None, content_type: Optional[str] = None) -> bool:
    client = get_r2_client()
    if not client or not local_path or not os.path.isfile(local_path):
        return False
    key = r2_key or path_to_r2_key(local_path)
    try:
        extra_args = {}
        ctype = content_type or mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        extra_args["ContentType"] = ctype
        client.upload_file(local_path, R2_BUCKET_NAME, key, ExtraArgs=extra_args)
        _r2_uploaded_keys.add(key)
        return True
    except Exception as e:
        print(f"[R2] Upload failed for {key}: {e}")
        return False


def r2_object_exists(r2_key: str) -> bool:
    if not r2_key:
        return False
    if r2_key in _r2_uploaded_keys:
        return True
    client = get_r2_client()
    if not client:
        return False
    try:
        client.head_object(Bucket=R2_BUCKET_NAME, Key=r2_key)
        _r2_uploaded_keys.add(r2_key)
        return True
    except Exception:
        return False


def ensure_local_from_r2(local_path: str) -> bool:
    """If local_path exists, returns True. If missing locally (e.g. after Render restart), pulls it from R2."""
    if not local_path:
        return False
    if os.path.isfile(local_path):
        return True
    client = get_r2_client()
    if not client:
        return False
    key = path_to_r2_key(local_path)
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        client.download_file(R2_BUCKET_NAME, key, local_path)
        _r2_uploaded_keys.add(key)
        return os.path.isfile(local_path)
    except Exception:
        return False


def generate_presigned_download_url(
    r2_key: str,
    expires_in: int = 3600,
    filename: Optional[str] = None,
    inline: bool = False,
    content_type: Optional[str] = None,
) -> Optional[str]:
    """Generates a direct download URL from Cloudflare R2 (0 egress bandwidth on Render)."""
    if not r2_key:
        return None
    if R2_PUBLIC_DOMAIN and not filename:
        return f"{R2_PUBLIC_DOMAIN}/{r2_key}"
    client = get_r2_client()
    if not client:
        return None
    try:
        params = {"Bucket": R2_BUCKET_NAME, "Key": r2_key}
        if content_type:
            params["ResponseContentType"] = content_type
        if filename:
            disp_type = "inline" if inline else "attachment"
            safe_ascii = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
            utf8_quoted = quote(filename)
            params["ResponseContentDisposition"] = f"{disp_type}; filename=\"{safe_ascii}\"; filename*=UTF-8''{utf8_quoted}"
        elif inline:
            params["ResponseContentDisposition"] = "inline"
        url = client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires_in,
        )
        return url
    except Exception as e:
        print(f"[R2] Presigned URL generation failed: {e}")
        return None


def get_r2_url_for_local_path(
    local_path: str,
    filename: Optional[str] = None,
    inline: bool = False,
    content_type: Optional[str] = None,
    auto_upload: bool = True,
) -> Optional[str]:
    """Returns a direct R2 presigned URL for local_path (uploading to R2 first if needed)."""
    if not is_r2_configured() or not local_path:
        return None
    key = path_to_r2_key(local_path)
    if not r2_object_exists(key):
        if auto_upload and os.path.isfile(local_path):
            if not upload_file_to_r2(local_path, key, content_type=content_type):
                return None
        else:
            return None
    return generate_presigned_download_url(
        key,
        expires_in=3600,
        filename=filename,
        inline=inline,
        content_type=content_type,
    )


def delete_from_r2(r2_key: str) -> bool:
    if not r2_key:
        return False
    _r2_uploaded_keys.discard(r2_key)
    client = get_r2_client()
    if not client:
        return False
    try:
        client.delete_object(Bucket=R2_BUCKET_NAME, Key=r2_key)
        return True
    except Exception as e:
        print(f"[R2] Delete failed for {r2_key}: {e}")
        return False


def delete_local_and_r2(local_path: str):
    """Deletes the file from Cloudflare R2 if configured."""
    if not local_path:
        return
    if is_r2_configured():
        key = path_to_r2_key(local_path)
        delete_from_r2(key)


# ── SQLite Database Cloud Sync & Auto-Restore ──
DB_R2_KEY = "database/qrprint.db"
_last_db_backup_time: Optional[float] = None
_db_backup_lock = threading.Lock()
_db_backup_debounce_timer: Optional[threading.Timer] = None


def get_last_db_backup_time() -> Optional[str]:
    """Returns human-readable time of the last successful database backup to R2."""
    if not _last_db_backup_time:
        return None
    try:
        dt = datetime.fromtimestamp(_last_db_backup_time, timezone.utc).astimezone()
        return dt.strftime("%d %b %Y, %I:%M:%S %p")
    except Exception:
        return str(_last_db_backup_time)


def verify_sqlite_integrity(db_path: str) -> bool:
    """Verifies that the given SQLite database file is uncorrupted."""
    if not os.path.isfile(db_path) or os.path.getsize(db_path) == 0:
        return False
    try:
        conn = sqlite3.connect(db_path)
        chk = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return bool(chk and chk[0] == "ok")
    except Exception:
        return False

_verify_sqlite_integrity = verify_sqlite_integrity


def create_consistent_db_snapshot(local_db_path: str, dest_path: str) -> bool:
    """Uses SQLite's online backup API to take a 100% consistent snapshot without locking writers."""
    if not os.path.isfile(local_db_path):
        return False
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        source_conn = sqlite3.connect(local_db_path, timeout=30.0)
        dest_conn = sqlite3.connect(dest_path)
        source_conn.backup(dest_conn)
        dest_conn.close()
        source_conn.close()
        return verify_sqlite_integrity(dest_path)
    except Exception as e:
        print(f"[DB Snapshot] Error: {e}")
        return False

_create_consistent_db_snapshot = create_consistent_db_snapshot


def backup_db_to_r2(local_db_path: str) -> bool:
    """
    Safely creates an online SQLite snapshot using SQLite's backup API
    and uploads it to Cloudflare R2 under key 'database/qrprint.db'.
    """
    global _last_db_backup_time
    if not is_r2_configured():
        return False
    if not local_db_path or not os.path.isfile(local_db_path):
        return False

    with _db_backup_lock:
        tmp_snapshot = local_db_path + ".snapshot.tmp"
        try:
            # 1. Take a 100% consistent snapshot via SQLite backup API
            if not create_consistent_db_snapshot(local_db_path, tmp_snapshot):
                print("[DB Cloud Backup] Failed to create consistent snapshot")
                return False

            # 2. Upload to Cloudflare R2
            ok = upload_file_to_r2(
                tmp_snapshot,
                r2_key=DB_R2_KEY,
                content_type="application/x-sqlite3"
            )
            if ok:
                _last_db_backup_time = time.time()
                print(f"[DB Cloud Backup] Successfully backed up database to Cloudflare R2 ({os.path.getsize(tmp_snapshot)} bytes)")
            return ok
        except Exception as e:
            print(f"[DB Cloud Backup] Error: {e}")
            return False
        finally:
            if os.path.isfile(tmp_snapshot):
                try:
                    os.remove(tmp_snapshot)
                except Exception:
                    pass
                except Exception:
                    pass


def trigger_db_cloud_backup(local_db_path: Optional[str] = None, delay_seconds: float = 3.0):
    """
    Debounced asynchronous trigger for database cloud backup.
    Batches rapid modifications into a single upload in a background daemon thread.
    """
    global _db_backup_debounce_timer
    if not is_r2_configured():
        return

    # Default to data/qrprint.db if not passed
    target_path = local_db_path
    if not target_path:
        db_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
        target_path = os.path.join(db_dir, "qrprint.db")

    with _db_backup_lock:
        if _db_backup_debounce_timer is not None:
            try:
                _db_backup_debounce_timer.cancel()
            except Exception:
                pass

        def _run():
            backup_db_to_r2(target_path)

        _db_backup_debounce_timer = threading.Timer(delay_seconds, _run)
        _db_backup_debounce_timer.daemon = True
        _db_backup_debounce_timer.start()


def restore_db_from_r2(local_db_path: str, force: bool = False) -> bool:
    """
    Checks Cloudflare R2 for 'database/qrprint.db' and restores it locally
    if local_db_path is missing, empty, or has only demo shop while R2 has real data.
    """
    if not is_r2_configured():
        return False
    client = get_r2_client()
    if not client:
        return False

    with _db_backup_lock:
        try:
            # 1. Check if backup exists in R2
            try:
                head = client.head_object(Bucket=R2_BUCKET_NAME, Key=DB_R2_KEY)
                r2_size = head.get("ContentLength", 0)
            except Exception:
                print("[DB Cloud Restore] No remote database backup found in Cloudflare R2 yet.")
                return False

            if r2_size < 100:
                print("[DB Cloud Restore] Remote backup in R2 is too small (<100 bytes).")
                return False

            # 2. Check local database
            local_exists = os.path.isfile(local_db_path) and os.path.getsize(local_db_path) > 0
            if local_exists and not force:
                try:
                    conn = sqlite3.connect(local_db_path)
                    cur = conn.cursor()
                    shops_cnt = cur.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
                    orders_cnt = cur.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
                    conn.close()

                    # If local DB already has multiple shops or real orders, keep local
                    if shops_cnt > 1 or orders_cnt > 0:
                        print(f"[DB Cloud Restore] Local database already has {shops_cnt} shops and {orders_cnt} orders. Keeping local.")
                        return True
                except Exception as e:
                    print(f"[DB Cloud Restore] Local DB check note ({e}), proceeding with R2 check.")

            # 3. Download to temp file
            os.makedirs(os.path.dirname(local_db_path), exist_ok=True)
            tmp_download = local_db_path + ".restore.tmp"
            client.download_file(R2_BUCKET_NAME, DB_R2_KEY, tmp_download)

            # 4. Verify downloaded SQLite database
            test_conn = sqlite3.connect(tmp_download)
            chk = test_conn.execute("PRAGMA integrity_check").fetchone()
            cur = test_conn.cursor()
            shops_cnt = cur.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
            orders_cnt = cur.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            test_conn.close()

            if not chk or chk[0] != "ok":
                print(f"[DB Cloud Restore] Downloaded database corrupted: {chk}")
                if os.path.isfile(tmp_download):
                    os.remove(tmp_download)
                return False

            # 5. Atomically replace local database
            if os.path.isfile(local_db_path):
                old_bk = local_db_path + ".old"
                try:
                    if os.path.isfile(old_bk):
                        os.remove(old_bk)
                    os.rename(local_db_path, old_bk)
                except Exception:
                    pass

            os.replace(tmp_download, local_db_path)
            print(f"[DB Cloud Restore] SUCCESS: Restored {shops_cnt} shops and {orders_cnt} orders from Cloudflare R2!")
            return True
        except Exception as e:
            print(f"[DB Cloud Restore] Restore failed: {e}")
            return False
