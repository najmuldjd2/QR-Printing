"""
Cloudflare R2 Storage Adapter (S3-compatible) with automatic CORS setup,
direct Presigned URL redirects (0 Render egress bandwidth), and local fallback.
"""
import os
import mimetypes
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
