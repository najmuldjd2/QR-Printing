from __future__ import annotations
import os
import re
import io
import uuid
import json
import base64
import shutil
import logging

logger = logging.getLogger(__name__)
import hmac
import hashlib
import asyncio
import secrets
import mimetypes
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple, Union, Any

import aiofiles
from fastapi import (
    FastAPI, Request, Response, Form, File, UploadFile, Body,
    WebSocket, WebSocketDisconnect, HTTPException, Depends, status
)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

import database as db
import storage
import studio
import utils
from PIL import Image, ImageOps, ImageEnhance, ImageFilter

# Ensure directories exist
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
TRANSFER_DIR = os.path.join(UPLOAD_DIR, "transfers")
ROOM_DIR = os.path.join(UPLOAD_DIR, "rooms")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
DATA_DIR = os.path.join(BASE_DIR, "data")

for _d in (UPLOAD_DIR, TRANSFER_DIR, ROOM_DIR, STATIC_DIR, TEMPLATES_DIR, DATA_DIR):
    os.makedirs(_d, exist_ok=True)

# Limits
MAX_UPLOAD_BYTES = 50 * 1024 * 1024          # print files & transfers: 50 MB
TRANSFER_TTL_HOURS = 24                      # transfer files auto-expire after 24 h
TRANSFER_MAX_PER_DIRECTION = 40              # max pending files per shop (spam / disk protection)
ROOM_TTL_HOURS = 12                          # QR transfer sessions live for 12 h
ROOM_MAX_FILES = 30
ROOM_MAX_TOTAL_BYTES = 300 * 1024 * 1024     # per session
ORDER_FILE_MAX_AGE_HOURS = 72                # abandoned print uploads are removed after 72 h
COMPLETED_RETENTION_MINUTES = 30             # completed print files stay 30 mins for re-print, then auto-delete
PREVIEW_CACHE_MAX_AGE_HOURS = 2

# Initialize database
db.init_db()


# ── Session cookie signing (so nobody can fake a shop login by editing the cookie) ──
def _load_secret() -> bytes:
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key.encode()
    path = os.path.join(DATA_DIR, ".secret")
    try:
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read().strip()
        key = secrets.token_hex(32).encode()
        with open(path, "wb") as f:
            f.write(key)
        return key
    except Exception:
        return secrets.token_hex(32).encode()


SECRET_KEY = _load_secret()


def sign_value(value: str) -> str:
    sig = hmac.new(SECRET_KEY, value.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{value}.{sig}"


def unsign_value(cookie: Optional[str]) -> Optional[str]:
    if not cookie or "." not in cookie:
        return None
    value, sig = cookie.rsplit(".", 1)
    expected = hmac.new(SECRET_KEY, value.encode(), hashlib.sha256).hexdigest()[:32]
    return value if hmac.compare_digest(sig, expected) else None


def base_url(request: Request) -> str:
    host = request.headers.get("host", "localhost:8000")
    proto = request.headers.get("x-forwarded-proto") or ("https" if "onrender.com" in host else request.url.scheme)
    return f"{proto}://{host}"


def set_session(response: Response, request: Request, shop_id: int):
    response.set_cookie(
        key="shop_session", value=sign_value(str(shop_id)), max_age=86400 * 30,
        httponly=True, samesite="lax", secure=base_url(request).startswith("https")
    )


# ── Background housekeeping ──
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_cleanup():
    """Removes expired transfers, completed print files past their shop retention period, and abandoned temp files."""
    now_ts = datetime.now(timezone.utc).timestamp()
    for o in db.get_completed_orders_to_purge(now_ts):
        utils.secure_delete_file(o["file_path"])
        db.mark_order_file_purged(o["id"])
    for t in db.get_expired_transfers(_now_iso()):
        utils.secure_delete_file(t["stored_path"])
        db.delete_transfer_row(t["id"])
    for r in db.get_expired_rooms(_now_iso()):
        for f in db.list_room_files(r["id"]):
            utils.secure_delete_file(f["stored_path"])
        db.delete_room(r["id"])
    utils.cleanup_old_files(ROOM_DIR, 3600, prefix="zip_")
    utils.cleanup_old_files(UPLOAD_DIR, PREVIEW_CACHE_MAX_AGE_HOURS * 3600, prefix="pv_")
    utils.cleanup_old_files(UPLOAD_DIR, ORDER_FILE_MAX_AGE_HOURS * 3600)


async def _cleanup_loop():
    loop_count = 0
    while True:
        try:
            await asyncio.to_thread(run_cleanup)
            loop_count += 1
            # Run periodic cloud backup every 5 loops (~10 minutes)
            if loop_count % 5 == 0:
                await asyncio.to_thread(storage.backup_db_to_r2, db.DB_PATH)
        except Exception as e:
            print(f"[!] Cleanup error: {e}")
        await asyncio.sleep(120)


@asynccontextmanager
async def lifespan(app_: FastAPI):
    # Attempt to restore latest database from Cloudflare R2 on startup
    try:
        await asyncio.to_thread(storage.restore_db_from_r2, db.DB_PATH)
    except Exception as e:
        print(f"[Lifespan] Startup DB restore check skipped: {e}")
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()
    # Final backup to Cloudflare R2 on graceful shutdown
    try:
        await asyncio.to_thread(storage.backup_db_to_r2, db.DB_PATH)
        print("[Lifespan] Final DB backup to Cloudflare R2 completed.")
    except Exception as e:
        print(f"[Lifespan] Final DB backup error: {e}")


app = FastAPI(title="QR Se Print - Auto Print Platform", version="1.1.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# ── Connection Manager for WebSockets ──
class ConnectionManager:
    def __init__(self):
        # shop_id -> list of admin WebSockets
        self.admin_connections: Dict[int, List[WebSocket]] = {}
        # shop_id -> list of agent WebSockets
        self.agent_connections: Dict[int, List[WebSocket]] = {}
        # order_id -> list of customer WebSockets
        self.order_connections: Dict[str, List[WebSocket]] = {}
        # room_id -> list of transfer-room WebSockets
        self.room_connections: Dict[str, List[WebSocket]] = {}
        # list of Super Admin WebSockets
        self.superadmin_connections: List[WebSocket] = []

    async def connect_superadmin(self, websocket: WebSocket):
        await websocket.accept()
        self.superadmin_connections.append(websocket)

    def disconnect_superadmin(self, websocket: WebSocket):
        if websocket in self.superadmin_connections:
            self.superadmin_connections.remove(websocket)

    async def notify_superadmin(self, message: dict):
        dead = []
        for ws in list(self.superadmin_connections):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_superadmin(ws)

    async def connect_admin(self, shop_id: int, websocket: WebSocket):
        await websocket.accept()
        if shop_id not in self.admin_connections:
            self.admin_connections[shop_id] = []
        self.admin_connections[shop_id].append(websocket)

    def disconnect_admin(self, shop_id: int, websocket: WebSocket):
        if shop_id in self.admin_connections and websocket in self.admin_connections[shop_id]:
            self.admin_connections[shop_id].remove(websocket)

    async def connect_agent(self, shop_id: int, websocket: WebSocket):
        await websocket.accept()
        if shop_id not in self.agent_connections:
            self.agent_connections[shop_id] = []
        self.agent_connections[shop_id].append(websocket)

    def disconnect_agent(self, shop_id: int, websocket: WebSocket):
        if shop_id in self.agent_connections and websocket in self.agent_connections[shop_id]:
            self.agent_connections[shop_id].remove(websocket)

    async def connect_order(self, order_id: str, websocket: WebSocket):
        await websocket.accept()
        if order_id not in self.order_connections:
            self.order_connections[order_id] = []
        self.order_connections[order_id].append(websocket)

    def disconnect_order(self, order_id: str, websocket: WebSocket):
        if order_id in self.order_connections and websocket in self.order_connections[order_id]:
            self.order_connections[order_id].remove(websocket)

    async def connect_room(self, room_id: str, websocket: WebSocket):
        await websocket.accept()
        self.room_connections.setdefault(room_id, []).append(websocket)

    def disconnect_room(self, room_id: str, websocket: WebSocket):
        if room_id in self.room_connections and websocket in self.room_connections[room_id]:
            self.room_connections[room_id].remove(websocket)

    async def notify_room(self, room_id: str, message: dict):
        for ws in list(self.room_connections.get(room_id, [])):
            try:
                await ws.send_json(message)
            except Exception:
                self.disconnect_room(room_id, ws)

    async def notify_admin(self, shop_id: int, message: dict):
        if shop_id in self.admin_connections:
            dead = []
            for ws in self.admin_connections[shop_id]:
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.admin_connections[shop_id].remove(ws)

    async def notify_order(self, order_id: str, message: dict):
        if order_id in self.order_connections:
            dead = []
            for ws in self.order_connections[order_id]:
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.order_connections[order_id].remove(ws)

    async def dispatch_print_job_to_agent(self, shop_id: int, job_data: dict) -> bool:
        """Dispatches job directly to local shop agent"""
        if shop_id in self.agent_connections and len(self.agent_connections[shop_id]) > 0:
            for ws in self.agent_connections[shop_id]:
                try:
                    await ws.send_json({"event": "PRINT_JOB", "data": job_data})
                    return True
                except Exception as e:
                    print(f"[!] Error sending job to agent: {e}")
        return False

manager = ConnectionManager()

# Helper for current shop session (cookie based)
def get_current_shop(request: Request) -> Optional[dict]:
    shop_id_cookie = unsign_value(request.cookies.get("shop_session"))
    if shop_id_cookie and shop_id_cookie.isdigit():
        shop = db.get_shop_by_id(int(shop_id_cookie))
        if shop and shop.get("is_active", 1) == 1:
            return dict(shop)
    return None


def set_superadmin_session(response: Response, request: Request):
    response.set_cookie(
        key="superadmin_session", value=sign_value("superadmin"), max_age=86400 * 30,
        httponly=True, samesite="lax", secure=base_url(request).startswith("https")
    )


def is_superadmin(request: Request) -> bool:
    return unsign_value(request.cookies.get("superadmin_session")) == "superadmin"


# ── LANDING & AUTH ROUTES ──

@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    shop = get_current_shop(request)
    return templates.TemplateResponse(request=request, name="index.html", context={
        "shop": shop,
        "superadmin": is_superadmin(request)
    })

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request=request, name="admin_login.html", context={"mode": "register"})

@app.post("/register")
async def register_shop(
    request: Request,
    shop_name: str = Form(...),
    owner_name: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    shop_code: str = Form(...)
):
    clean_code = re.sub(r"[^a-zA-Z0-9_-]", "", shop_code.strip().lower())
    if not clean_code:
        return templates.TemplateResponse(request=request, name="admin_login.html", context={
            "mode": "register",
            "error": "সঠিক শপ কোড দিন (শুধুমাত্র ইংরেজি অক্ষর ও সংখ্যা)।"
        })

    conn = db.get_db()
    cursor = conn.cursor()
    if cursor.execute("SELECT id FROM shops WHERE LOWER(shop_code) = ?", (clean_code,)).fetchone():
        conn.close()
        return templates.TemplateResponse(request=request, name="admin_login.html", context={
            "mode": "register", 
            "error": "এই শপ কোডটি ইতিমধ্যে ব্যবহৃত হয়েছে। অন্য কোড দিন।"
        })
    
    if cursor.execute("SELECT id FROM shops WHERE phone = ?", (phone.strip(),)).fetchone():
        conn.close()
        return templates.TemplateResponse(request=request, name="admin_login.html", context={
            "mode": "register", 
            "error": "এই মোবাইল নম্বরটি ইতিমধ্যে নিবন্ধিত রয়েছে।"
        })
    
    api_key = "qsp_" + secrets.token_hex(16)
    now = datetime.now().isoformat()
    # Create with is_active = 0 (Pending Super Admin Approval)
    hashed_pw = db.hash_password(password.strip())
    cursor.execute("""
    INSERT INTO shops (
        shop_code, name, owner_name, phone, password, api_key, 
        bkash_number, nagad_number, allow_counter_cash,
        rate_bw_single, rate_bw_double, rate_color_single, rate_color_double, rate_photo_4x6,
        is_active, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 3.0, 5.0, 10.0, 18.0, 30.0, 0, ?)
    """, (clean_code, shop_name.strip(), owner_name.strip(), phone.strip(), hashed_pw, api_key, phone.strip(), phone.strip(), now))
    conn.commit()
    shop_id = cursor.lastrowid
    conn.close()
    storage.trigger_db_cloud_backup(db.DB_PATH, delay=1.0)

    # Notify Super Admin in real time
    await manager.notify_superadmin({
        "event": "NEW_SHOP_REGISTRATION",
        "shop": {
            "id": shop_id,
            "name": shop_name.strip(),
            "owner_name": owner_name.strip(),
            "shop_code": clean_code,
            "phone": phone.strip(),
            "password": password.strip(),
            "created_at": now,
        }
    })

    return templates.TemplateResponse(request=request, name="admin_login.html", context={
        "mode": "login",
        "success": "✅ আপনার দোকান রেজিস্ট্রেশনের আবেদন সফলভাবে জমা হয়েছে! অ্যাডমিন (কর্তৃপক্ষ) Allow করলেই আপনি আপনার মোবাইল নম্বর ও পাসওয়ার্ড দিয়ে লগইন করতে পারবেন।"
    })

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="admin_login.html", context={"mode": "login"})

@app.post("/login")
async def login_shop(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...)
):
    clean_phone = phone.strip()
    clean_pass = password.strip()
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")

    # Check brute-force lockout
    is_locked, remaining_mins = db.is_account_locked(clean_phone)
    if is_locked:
        return templates.TemplateResponse(request=request, name="admin_login.html", context={
            "mode": "login",
            "error": f"🔒 পরপর ৫ বার ভুল পাসওয়ার্ড দেওয়ার কারণে এই একাউন্টটি সাময়িকভাবে লক করা হয়েছে ({remaining_mins} মিনিট বাকি)। জরুরি প্রয়োজনে সুপার অ্যাডমিনের সাথে যোগাযোগ করে আনলক করিয়ে নিন।"
        })

    # 1. Check if Super Admin credentials were entered
    if db.authenticate_superadmin(clean_phone, clean_pass):
        db.record_login_success(clean_phone)
        response = RedirectResponse(url="/superadmin", status_code=status.HTTP_303_SEE_OTHER)
        set_superadmin_session(response, request)
        return response

    # 2. Check regular shopkeeper login (distinguish pending vs wrong password)
    any_shop = db.get_shop_by_credentials_any_status(clean_phone, clean_pass)
    if not any_shop:
        attempts, locked_now, lock_mins = db.record_login_failure(clean_phone, client_ip)
        if locked_now:
            error_msg = "🔒 পরপর ৫ বার ভুল পাসওয়ার্ড দেওয়ার কারণে এই একাউন্টটি ১০ মিনিটের জন্য সাময়িকভাবে লক করা হয়েছে। জরুরি প্রয়োজনে সুপার অ্যাডমিনের সাথে যোগাযোগ করে আনলক করিয়ে নিন।"
        else:
            remaining = max(0, 5 - attempts)
            error_msg = f"মোবাইল নম্বর অথবা পাসওয়ার্ড ভুল হয়েছে। (বাকি চেষ্টা: {remaining} বার)"
        return templates.TemplateResponse(request=request, name="admin_login.html", context={
            "mode": "login", 
            "error": error_msg
        })

    # Successful login clears failed attempts
    db.record_login_success(clean_phone)

    if not any_shop.get("is_active"):
        return templates.TemplateResponse(request=request, name="admin_login.html", context={
            "mode": "login", 
            "error": "⏳ আপনার দোকানের আবেদনটি এখনো অ্যাডমিনের অনুমোদনের (Allow) অপেক্ষায় রয়েছে। অ্যাডমিন অনুমোদন করলে লগইন করতে পারবেন।"
        })
    
    response = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    set_session(response, request, any_shop["id"])
    return response

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="shop_session")
    return response


# ── SUPER ADMIN (MASTER OWNER CONTROL PANEL) ──

@app.get("/superadmin", response_class=HTMLResponse)
async def superadmin_dashboard(request: Request, msg: str = "", err: str = "", created: str = ""):
    if not is_superadmin(request):
        return templates.TemplateResponse(request=request, name="superadmin.html", context={
            "authenticated": False,
            "error": err
        })
    shops = db.get_all_shops_with_stats()
    pending_shops = [s for s in shops if not s.get("is_active")]
    active_shops = [s for s in shops if s.get("is_active")]
    sa_creds = db.get_superadmin_credentials()
    created_shop = db.get_shop_by_code(created.strip().lower()) if created else None
    locked_accounts = db.list_locked_accounts()
    db_size_kb = round(os.path.getsize(db.DB_PATH) / 1024, 1) if os.path.exists(db.DB_PATH) else 0
    return templates.TemplateResponse(request=request, name="superadmin.html", context={
        "authenticated": True,
        "shops": shops,
        "pending_shops": pending_shops,
        "active_shops": active_shops,
        "sa_creds": sa_creds,
        "locked_accounts": locked_accounts,
        "base_url": base_url(request),
        "msg": msg,
        "error": err,
        "created_shop": created_shop,
        "is_r2_configured": storage.is_r2_configured(),
        "last_db_backup_time": storage.get_last_db_backup_time(),
        "db_size_kb": db_size_kb,
        "db_path": db.DB_PATH,
    })


@app.post("/superadmin/unlock")
async def superadmin_unlock_account(request: Request, identifier: str = Form(...)):
    if not is_superadmin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    clean_id = identifier.strip()
    db.unlock_account(clean_id)
    return RedirectResponse(
        url=f"/superadmin?msg={clean_id}+একাউন্টটির+লক+সফলভাবে+ছুটিয়ে+দেওয়া+হয়েছে",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/superadmin/login")
async def superadmin_login(request: Request, phone: str = Form(...), password: str = Form(...)):
    clean_phone = phone.strip()
    clean_pass = password.strip()
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")

    is_locked, remaining_mins = db.is_account_locked(clean_phone)
    if is_locked:
        return templates.TemplateResponse(request=request, name="superadmin.html", context={
            "authenticated": False,
            "error": f"🔒 পরপর ৫ বার ভুল পাসওয়ার্ড দেওয়ার কারণে সুপার-অ্যাডমিন লগইন সাময়িকভাবে লক করা হয়েছে ({remaining_mins} মিনিট বাকি)।"
        })

    if db.authenticate_superadmin(clean_phone, clean_pass):
        db.record_login_success(clean_phone)
        response = RedirectResponse(url="/superadmin", status_code=status.HTTP_303_SEE_OTHER)
        set_superadmin_session(response, request)
        return response

    attempts, locked_now, _ = db.record_login_failure(clean_phone, client_ip)
    if locked_now:
        err = "🔒 পরপর ৫ বার ভুল পাসওয়ার্ড দেওয়ার কারণে সুপার-অ্যাডমিন লগইন ১০ মিনিটের জন্য লক করা হয়েছে।"
    else:
        err = f"সুপার অ্যাডমিন মোবাইল নম্বর বা পাসওয়ার্ড ভুল হয়েছে। (বাকি চেষ্টা: {max(0, 5 - attempts)} বার)"
    return templates.TemplateResponse(request=request, name="superadmin.html", context={
        "authenticated": False,
        "error": err
    })


@app.get("/superadmin/logout")
async def superadmin_logout():
    response = RedirectResponse(url="/superadmin", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="superadmin_session")
    return response


@app.post("/superadmin/shop/create")
async def superadmin_create_shop(
    request: Request,
    shop_name: str = Form(...),
    owner_name: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    shop_code: str = Form(...)
):
    if not is_superadmin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    clean_code = re.sub(r"[^a-zA-Z0-9_-]", "", shop_code.strip().lower())
    if not clean_code:
        return RedirectResponse(url="/superadmin?err=সঠিক+শপ+কোড+দিন", status_code=status.HTTP_303_SEE_OTHER)

    conn = db.get_db()
    cursor = conn.cursor()
    if cursor.execute("SELECT id FROM shops WHERE LOWER(shop_code) = ?", (clean_code,)).fetchone():
        conn.close()
        return RedirectResponse(url="/superadmin?err=এই+শপ+কোডটি+ইতিমধ্যে+ব্যবহৃত+হয়েছে", status_code=status.HTTP_303_SEE_OTHER)

    if cursor.execute("SELECT id FROM shops WHERE phone = ?", (phone.strip(),)).fetchone():
        conn.close()
        return RedirectResponse(url="/superadmin?err=এই+মোবাইল+নম্বরটি+ইতিমধ্যে+নিবন্ধিত+রয়েছে", status_code=status.HTTP_303_SEE_OTHER)

    api_key = "qsp_" + secrets.token_hex(16)
    now = datetime.now().isoformat()
    hashed_pw = db.hash_password(password.strip())
    cursor.execute("""
    INSERT INTO shops (
        shop_code, name, owner_name, phone, password, api_key, 
        bkash_number, nagad_number, allow_counter_cash,
        rate_bw_single, rate_bw_double, rate_color_single, rate_color_double, rate_photo_4x6,
        is_active, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 3.0, 5.0, 10.0, 18.0, 30.0, 1, ?)
    """, (clean_code, shop_name.strip(), owner_name.strip(), phone.strip(), hashed_pw, api_key, phone.strip(), phone.strip(), now))
    conn.commit()
    conn.close()
    storage.trigger_db_cloud_backup(db.DB_PATH, delay=1.0)

    return RedirectResponse(url=f"/superadmin?created={clean_code}&msg=নতুন+দোকান+সফলভাবে+তৈরি+হয়েছে", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/superadmin/shop/{shop_id}/update")
async def superadmin_update_shop(
    shop_id: int,
    request: Request,
    shop_name: str = Form(...),
    owner_name: str = Form(""),
    shop_code: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    is_active: Optional[int] = Form(1)
):
    if not is_superadmin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    clean_code = re.sub(r"[^a-zA-Z0-9_-]", "", shop_code.strip().lower())
    if not clean_code:
        return RedirectResponse(url="/superadmin?err=শপ+কোড+সঠিক+নয়", status_code=status.HTTP_303_SEE_OTHER)

    conn = db.get_db()
    dup_code = conn.execute("SELECT id FROM shops WHERE LOWER(shop_code) = ? AND id != ?", (clean_code, shop_id)).fetchone()
    dup_phone = conn.execute("SELECT id FROM shops WHERE phone = ? AND id != ?", (phone.strip(), shop_id)).fetchone()
    conn.close()
    if dup_code:
        return RedirectResponse(url="/superadmin?err=এই+শপ+কোডটি+অন্য+দোকানে+ব্যবহৃত+হচ্ছে", status_code=status.HTTP_303_SEE_OTHER)
    if dup_phone:
        return RedirectResponse(url="/superadmin?err=এই+মোবাইল+নম্বরটি+অন্য+দোকানে+ব্যবহৃত+হচ্ছে", status_code=status.HTTP_303_SEE_OTHER)

    db.update_shop_by_superadmin(shop_id, clean_code, shop_name, owner_name, phone, password, 1 if is_active else 0)
    return RedirectResponse(url="/superadmin?msg=দোকানের+তথ্য+ও+পাসওয়ার্ড+আপডেট+হয়েছে", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/superadmin/shop/{shop_id}/toggle")
async def superadmin_toggle_shop(shop_id: int, request: Request, is_active: int = Form(...)):
    if not is_superadmin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    db.toggle_shop_active(shop_id, is_active)
    shop = db.get_shop_by_id(shop_id)
    if is_active and shop:
        return RedirectResponse(
            url=f"/superadmin?created={shop['shop_code']}&msg=দোকানটি+অনুমোদন+(Allow)+করা+হয়েছে!+এখন+দোকানদার+লগইন+করতে+পারবেন।",
            status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse(url="/superadmin?msg=দোকানের+স্ট্যাটাস+পরিবর্তন+হয়েছে", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/superadmin/shop/{shop_id}/delete")
async def superadmin_delete_shop(shop_id: int, request: Request):
    if not is_superadmin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    db.delete_shop_by_superadmin(shop_id)
    return RedirectResponse(url="/superadmin?msg=দোকানটি+মুছে+ফেলা+হয়েছে", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/superadmin/shop/{shop_id}/impersonate")
async def superadmin_impersonate_shop(shop_id: int, request: Request):
    if not is_superadmin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    shop = db.get_shop_by_id(shop_id)
    if not shop:
        return RedirectResponse(url="/superadmin?err=দোকান+পাওয়া+যায়নি", status_code=status.HTTP_303_SEE_OTHER)
    response = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    set_session(response, request, shop["id"])
    return response


@app.post("/superadmin/credentials")
async def superadmin_update_creds(request: Request, phone: str = Form(...), password: str = Form(...)):
    if not is_superadmin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if len(phone.strip()) < 5 or len(password.strip()) < 4:
        return RedirectResponse(url="/superadmin?err=সঠিক+নম্বর+ও+কমপক্ষে+৪+অক্ষরের+পাসওয়ার্ড+দিন", status_code=status.HTTP_303_SEE_OTHER)
    db.update_superadmin_credentials(phone.strip(), password.strip())
    return RedirectResponse(url="/superadmin?msg=সুপার+অ্যাডমিন+মোবাইল+নম্বর+ও+পাসওয়ার্ড+আপডেট+হয়েছে", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/superadmin/database/download")
async def superadmin_download_database(request: Request):
    if not is_superadmin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not os.path.exists(db.DB_PATH):
        raise HTTPException(status_code=404, detail="Database file not found")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_dir = os.path.dirname(db.DB_PATH)
    snap_path = os.path.join(temp_dir, f"backup_download_{timestamp}_{secrets.token_hex(4)}.db")

    if not storage._create_consistent_db_snapshot(db.DB_PATH, snap_path):
        shutil.copy2(db.DB_PATH, snap_path)

    return FileResponse(
        snap_path,
        media_type="application/x-sqlite3",
        filename=f"qrprint_backup_{timestamp}.db",
        background=BackgroundTask(utils.secure_delete_file, snap_path)
    )


@app.post("/superadmin/database/backup-now")
async def superadmin_backup_now(request: Request):
    if not is_superadmin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not storage.is_r2_configured():
        return RedirectResponse(
            url="/superadmin?err=ক্লাউডফ্লেয়ার+R2+কনফিগার+করা+নেই।+Render-এ+R2_BUCKET_NAME+ইত্যাদি+এনভায়রনমেন্ট+ভেরিয়েবল+সেট+করুন।",
            status_code=status.HTTP_303_SEE_OTHER
        )
    success = await asyncio.to_thread(storage.backup_db_to_r2, db.DB_PATH)
    if success:
        return RedirectResponse(
            url="/superadmin?msg=ক্লাউডফ্লেয়ার+R2-তে+ডেটাবেজ+সফলভাবে+ব্যাকআপ+হয়েছে!",
            status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse(
        url="/superadmin?err=ক্লাউড+ব্যাকআপ+ব্যর্থ+হয়েছে।+সার্ভার+লগ+চেক+করুন।",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/superadmin/database/restore")
async def superadmin_restore_database(request: Request, backup_file: UploadFile = File(...)):
    if not is_superadmin(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not backup_file.filename:
        return RedirectResponse(url="/superadmin?err=কোন+ফাইল+নির্বাচন+করা+হয়নি।", status_code=status.HTTP_303_SEE_OTHER)

    temp_path = os.path.join(os.path.dirname(db.DB_PATH), f"restore_temp_{secrets.token_hex(6)}.db")
    try:
        with open(temp_path, "wb") as f:
            while chunk := await backup_file.read(1024 * 64):
                f.write(chunk)

        # Integrity verification
        if not storage._verify_sqlite_integrity(temp_path):
            utils.secure_delete_file(temp_path)
            return RedirectResponse(
                url="/superadmin?err=ভুল+বা+নষ্ট+ফাইল!+এটি+সঠিক+SQLite+ডেটাবেজ+ফাইল+নয়।",
                status_code=status.HTTP_303_SEE_OTHER
            )

        # Ensure required tables exist
        import sqlite3
        with sqlite3.connect(temp_path) as check_conn:
            tables = [r[0] for r in check_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "shops" not in tables:
                utils.secure_delete_file(temp_path)
                return RedirectResponse(
                    url="/superadmin?err=এই+ফাইলে+প্রয়োজনীয়+টেবিল+(shops)+পাওয়া+যায়নি।",
                    status_code=status.HTTP_303_SEE_OTHER
                )

        # Atomic replacement
        shutil.move(temp_path, db.DB_PATH)

        # Immediately sync the restored database to Cloudflare R2
        await asyncio.to_thread(storage.backup_db_to_r2, db.DB_PATH)

        return RedirectResponse(
            url="/superadmin?msg=ডেটাবেজ+সফলভাবে+রিস্টোর+হয়েছে+এবং+ক্লাউডফ্লেয়ার+R2-তেও+আপডেট+হয়েছে!",
            status_code=status.HTTP_303_SEE_OTHER
        )
    except Exception as e:
        utils.secure_delete_file(temp_path)
        print(f"[Restore DB] Error: {e}")
        return RedirectResponse(
            url=f"/superadmin?err=রিস্টোর+করতে+সমস্যা+হয়েছে:+{str(e)[:50]}",
            status_code=status.HTTP_303_SEE_OTHER
        )


# ── ADMIN DASHBOARD ──

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    shop = get_current_shop(request)
    if not shop:
        return RedirectResponse(url="/login")
    
    await asyncio.to_thread(run_cleanup)
    orders = db.get_shop_orders(shop["id"])
    live_orders = [o for o in orders if o["status"] in ("pending_payment", "paid", "printing")]
    history_orders = [o for o in orders if o["status"] in ("completed", "cancelled")]
    stats = db.get_shop_stats(shop["id"])
    printers = db.get_shop_printers(shop["id"])
    is_agent_online = len(manager.agent_connections.get(shop["id"], [])) > 0
    
    # Base URL for customer link
    customer_url = f"{base_url(request)}/p/{shop['shop_code']}"
    
    return templates.TemplateResponse(request=request, name="admin_dashboard.html", context={
        "shop": shop,
        "orders": orders,
        "live_orders": live_orders,
        "history_orders": history_orders,
        "stats": stats,
        "printers": printers,
        "is_agent_online": is_agent_online,
        "customer_url": customer_url
    })

@app.post("/admin/settings")
async def update_settings(
    request: Request,
    name: str = Form(...),
    owner_name: str = Form(...),
    bkash_number: str = Form(""),
    nagad_number: str = Form(""),
    allow_counter_cash: Optional[int] = Form(0),
    rate_bw_single: float = Form(3.0),
    rate_bw_double: float = Form(5.0),
    rate_color_single: float = Form(10.0),
    rate_color_double: float = Form(18.0),
    rate_photo_4x6: float = Form(30.0),
    rate_photo_a4: float = Form(40.0),
    rate_passport: float = Form(5.0),
    rate_stamp: float = Form(2.0),
    remove_bg_api_key: str = Form(""),
    file_retention_days: float = Form(1.0)
):
    shop = get_current_shop(request)
    if not shop:
        return RedirectResponse(url="/login")
    
    db.update_shop_settings(shop["id"], {
        "name": name.strip(),
        "owner_name": owner_name.strip(),
        "bkash_number": bkash_number.strip(),
        "nagad_number": nagad_number.strip(),
        "allow_counter_cash": 1 if allow_counter_cash else 0,
        "rate_bw_single": rate_bw_single,
        "rate_bw_double": rate_bw_double,
        "rate_color_single": rate_color_single,
        "rate_color_double": rate_color_double,
        "rate_photo_4x6": rate_photo_4x6,
        "rate_photo_a4": rate_photo_a4,
        "rate_passport": rate_passport,
        "rate_stamp": rate_stamp,
        "remove_bg_api_key": remove_bg_api_key.strip(),
        "file_retention_days": file_retention_days
    })
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/admin/qr-standee", response_class=HTMLResponse)
async def qr_standee(request: Request):
    shop = get_current_shop(request)
    if not shop:
        return RedirectResponse(url="/login")
    
    customer_url = f"{base_url(request)}/p/{shop['shop_code']}"
    
    return templates.TemplateResponse(request=request, name="qr_standee.html", context={
        "shop": shop,
        "customer_url": customer_url
    })

@app.post("/admin/order/{order_id}/approve")
async def approve_order(order_id: str, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")

    # Allow optional printer_target override right from the approve action
    try:
        body = await request.json()
        if isinstance(body, dict) and "printer_target" in body:
            pt = str(body.get("printer_target") or "").strip()[:100]
            db.update_order_details(order_id, {"printer_target": pt})
            order["printer_target"] = pt
    except Exception:
        pass
    
    # Mark as paid and ready to print
    db.update_order_status(order_id, "paid")
    
    # Notify customer live
    await manager.notify_order(order_id, {
        "event": "STATUS_CHANGE",
        "status": "paid",
        "message": "পেমেন্ট নিশ্চিত হয়েছে! ফাইলটি এখন প্রিন্টারে পাঠানো হচ্ছে..."
    })
    
    # Dispatch to local shop Windows agent
    job_dispatched = await manager.dispatch_print_job_to_agent(shop["id"], {
        "order_id": order["id"],
        "download_url": f"/download/{order['id']}",
        "filename": order["original_filename"],
        "file_type": order["file_type"],
        "color_mode": order["color_mode"],
        "copies": order["copies"],
        "duplex": order["duplex"],
        "paper_size": order["paper_size"],
        "page_range": order["page_range"],
        "printer_name": order.get("printer_target") or "",
        "orientation": order.get("orientation") or "portrait"
    })
    
    if job_dispatched:
        db.update_order_status(order_id, "printing")
        await manager.notify_order(order_id, {
            "event": "STATUS_CHANGE",
            "status": "printing",
            "message": "স্বয়ংক্রিয়ভাবে প্রিন্ট হচ্ছে..."
        })
    
    # Notify admin UI to refresh
    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id})
    return {"success": True, "dispatched": job_dispatched, "order": {k: v for k, v in order.items() if k != "file_path"}}


@app.post("/admin/order/{order_id}/complete")
async def admin_complete_order(order_id: str, request: Request):
    """Marks an order as completed. Keeps the file for 30 minutes for re-print before auto-deletion."""
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")

    now = _now_iso()
    db.update_order_status(order_id, "completed", printed_at=now)

    await manager.notify_order(order_id, {
        "event": "STATUS_CHANGE",
        "status": "completed",
        "message": "প্রিন্ট সম্পন্ন হয়েছে! ৩০ মিনিট পর আপনার ফাইলটি সার্ভার থেকে স্বয়ংক্রিয়ভাবে মুছে যাবে।"
    })
    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id, "status": "completed"})
    return {"success": True}


@app.post("/admin/order/{order_id}/delete-file")
async def admin_delete_order_file(order_id: str, request: Request):
    """Immediately deletes the print file from local disk and Cloudflare R2 before the retention timer."""
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.get("file_path"):
        utils.secure_delete_file(order["file_path"])
    db.mark_order_file_purged(order_id)
    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id})
    return {"success": True}


@app.post("/admin/order/{order_id}/delete-record")
async def admin_delete_order_record(order_id: str, request: Request):
    """Deletes the print file (if any) AND removes the order row from the previous orders list."""
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.get("file_path"):
        utils.secure_delete_file(order["file_path"])
    db.delete_order_record(order_id, shop["id"])
    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id})
    return {"success": True}


@app.post("/admin/orders/purge-history-files")
async def admin_purge_history_files(request: Request):
    """Deletes all stored files of completed/cancelled orders for the current shop at once."""
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    rows = db.get_shop_history_orders_with_files(shop["id"])
    count = 0
    for r in rows:
        if r.get("file_path"):
            utils.secure_delete_file(r["file_path"])
        db.mark_order_file_purged(r["id"])
        count += 1
    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED"})
    return {"success": True, "purged": count}


@app.post("/admin/order/{order_id}/cancel")
async def cancel_order(order_id: str, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")
        
    db.update_order_status(order_id, "cancelled")
    if order.get("file_path"):
        utils.secure_delete_file(order["file_path"])
    db.mark_order_file_purged(order_id)
    
    await manager.notify_order(order_id, {
        "event": "STATUS_CHANGE",
        "status": "cancelled",
        "message": "অর্ডারটি বাতিল করা হয়েছে।"
    })
    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id})
    return {"success": True}

@app.post("/admin/order/{order_id}/update")
async def edit_order_details(order_id: str, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")
    
    data = await request.json()
    updates = {}
    if "copies" in data:
        updates["copies"] = max(1, min(100, int(data.get("copies", 1))))
    if "color_mode" in data and data["color_mode"] in ("bw", "color"):
        updates["color_mode"] = data["color_mode"]
    if "paper_size" in data and data["paper_size"] in ("A4", "4x6", "NID", "PHOTO"):
        updates["paper_size"] = data["paper_size"]
    if "duplex" in data and data["duplex"] in ("single", "double"):
        updates["duplex"] = data["duplex"]
    if "orientation" in data and data["orientation"] in ("portrait", "landscape"):
        updates["orientation"] = data["orientation"]
    if "page_range" in data:
        updates["page_range"] = str(data["page_range"])[:50]
    if "printer_target" in data:
        updates["printer_target"] = str(data["printer_target"])[:100]
    if "total_price" in data:
        try:
            updates["total_price"] = round(float(data["total_price"]), 2)
        except (ValueError, TypeError):
            pass

    # If color_mode, paper_size, or orientation changed for an NID or Studio order, regenerate printable file
    color_changed = "color_mode" in updates and updates["color_mode"] != order.get("color_mode")
    paper_changed = "paper_size" in updates and updates["paper_size"] != order.get("paper_size")
    orient_changed = "orientation" in updates and updates["orientation"] != order.get("orientation")

    if color_changed or paper_changed or orient_changed:
        new_color = updates.get("color_mode") or order.get("color_mode", "color")
        new_paper = updates.get("paper_size") or order.get("paper_size", "A4")
        new_orient = updates.get("orientation") or order.get("orientation", "portrait")
        orig_path = order["file_path"]
        extra = {}
        try:
            extra = json.loads(order.get("extra_data") or "{}")
        except Exception:
            pass
        if extra.get("is_nid") or order.get("paper_size") == "NID" or "nid_" in orig_path:
            token = extra.get("nid_token")
            if not token and "nid_print_" in orig_path:
                token = orig_path.split("nid_print_")[1].split(".pdf")[0]
            if token:
                front, back = nid_paths(token)
                await asyncio.to_thread(
                    utils.save_nid_sheet, front, back, orig_path,
                    color_mode=new_color,
                    fit_mode=extra.get("fit", "cover"),
                    rot_front=int(extra.get("rot_front", 0)),
                    rot_back=int(extra.get("rot_back", 0)),
                    orientation=new_orient
                )
                if storage.is_r2_configured() and os.path.isfile(orig_path):
                    await asyncio.to_thread(storage.upload_file_to_r2, orig_path)
        elif extra.get("is_studio") or order.get("paper_size") == "PHOTO" or "studio_" in orig_path:
            src_path = resolve_studio_source_path(order, extra)
            if src_path and os.path.isfile(src_path):
                sp = studio.sanitize_params(extra.get("studio"))
                sp["bw"] = (new_color == "bw")
                sp["paper"] = "4x6" if new_paper == "4x6" else "A4"
                extra["studio"] = sp
                updates["extra_data"] = json.dumps(extra)
                p_map = {order_id: src_path}
                sheets, _info = studio.render_sheets(src_path, sp, dpi=300, photos_map=p_map)
                page_count = max(1, len(sheets))
                studio.save_sheet_pdf(sheets, orig_path, 300)
                updates["page_count"] = page_count
                if storage.is_r2_configured() and os.path.isfile(orig_path):
                    await asyncio.to_thread(storage.upload_file_to_r2, orig_path)

    db.update_order_details(order_id, updates)
    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id})
    return {"success": True, "updated": updates}


def ensure_order_pristine_backup(order: dict):
    """
    Guarantees that an untouched pristine copy of the customer's original uploaded file(s)
    is backed up permanently before any admin modifications (crop, rotate, CamScan).
    """
    import shutil
    extra = {}
    try:
        extra = json.loads(order.get("extra_data") or "{}")
    except Exception:
        pass

    orig_path = order.get("file_path") or ""
    updated = False

    # 1. NID Card (front and back images)
    if extra.get("is_nid") or order.get("paper_size") == "NID" or "nid_" in orig_path:
        token = extra.get("nid_token")
        if not token and "nid_print_" in orig_path:
            token = orig_path.split("nid_print_")[1].split(".pdf")[0]
        if token:
            front, back = nid_paths(token)
            p_front = os.path.join(UPLOAD_DIR, f"pristine_nid_{token}_f.jpg")
            p_back = os.path.join(UPLOAD_DIR, f"pristine_nid_{token}_b.jpg")
            if os.path.isfile(front) and not os.path.isfile(p_front):
                try:
                    shutil.copy2(front, p_front)
                    extra["pristine_front"] = p_front
                    updated = True
                except Exception:
                    pass
            if os.path.isfile(back) and not os.path.isfile(p_back):
                try:
                    shutil.copy2(back, p_back)
                    extra["pristine_back"] = p_back
                    updated = True
                except Exception:
                    pass

    # 2. Photo Studio (source photo)
    elif extra.get("is_studio") or order.get("paper_size") == "PHOTO" or "studio_" in orig_path:
        src_name = extra.get("source_photo") or ""
        src_path = os.path.join(UPLOAD_DIR, src_name) if src_name else None
        if src_path and os.path.isfile(src_path):
            p_photo = os.path.join(UPLOAD_DIR, f"pristine_{os.path.basename(src_path)}")
            if not os.path.isfile(p_photo):
                try:
                    shutil.copy2(src_path, p_photo)
                    extra["pristine_photo"] = p_photo
                    updated = True
                except Exception:
                    pass

    # 3. Regular Document (PDF) or Image
    else:
        existing_pristine = extra.get("pristine_path")
        if not existing_pristine or not os.path.isfile(existing_pristine):
            if orig_path and os.path.isfile(orig_path):
                p_file = os.path.join(UPLOAD_DIR, f"pristine_{order['id']}_{os.path.basename(orig_path)}")
                if not os.path.isfile(p_file):
                    try:
                        shutil.copy2(orig_path, p_file)
                    except Exception:
                        pass
                if os.path.isfile(p_file):
                    extra["pristine_path"] = p_file
                    extra["original_file_path"] = orig_path
                    updated = True

    if updated:
        try:
            db.update_order_details(order["id"], {"extra_data": json.dumps(extra)})
            order["extra_data"] = json.dumps(extra)
        except Exception:
            pass

    if storage.is_r2_configured():
        for k in ("pristine_path", "pristine_photo", "pristine_front", "pristine_back"):
            fp = extra.get(k)
            if fp and os.path.isfile(fp):
                try:
                    storage.upload_file_to_r2(fp)
                except Exception:
                    pass


def resolve_studio_source_path(order: dict, extra: dict) -> str:
    """Finds or recovers the source image for a photo studio order."""
    orig_path = order.get("file_path") or ""

    # 1. source_photo in extra
    src_name = extra.get("source_photo") or ""
    if src_name:
        cand = os.path.join(UPLOAD_DIR, src_name)
        if not os.path.isfile(cand) and storage.is_r2_configured():
            storage.ensure_local_from_r2(cand)
        if os.path.isfile(cand):
            return cand

    # 2. pristine_photo in extra
    p_photo = extra.get("pristine_photo") or ""
    if p_photo:
        if not os.path.isfile(p_photo) and storage.is_r2_configured():
            storage.ensure_local_from_r2(p_photo)
        if os.path.isfile(p_photo):
            return p_photo

    # 3. Check for any file on disk matching studio_src_
    clean_id = os.path.splitext(os.path.basename(orig_path))[0].replace("studio_", "")
    for pfx in ("studio_src_", "pristine_studio_src_", "upload_"):
        for ext in (".jpg", ".jpeg", ".png"):
            cand = os.path.join(UPLOAD_DIR, f"{pfx}{clean_id}{ext}")
            if not os.path.isfile(cand) and storage.is_r2_configured():
                storage.ensure_local_from_r2(cand)
            if os.path.isfile(cand):
                return cand

    # 4. If orig_path is already an image
    if orig_path and utils.get_ext(orig_path) in ("jpg", "jpeg", "png"):
        if not os.path.isfile(orig_path) and storage.is_r2_configured():
            storage.ensure_local_from_r2(orig_path)
        if os.path.isfile(orig_path):
            return orig_path

    # 5. If orig_path is PDF, extract embedded image or render page 0
    if orig_path and utils.get_ext(orig_path) == "pdf":
        if not os.path.isfile(orig_path) and storage.is_r2_configured():
            storage.ensure_local_from_r2(orig_path)
        if os.path.isfile(orig_path):
            extracted = os.path.join(UPLOAD_DIR, f"extracted_src_{clean_id}.jpg")
            if os.path.isfile(extracted):
                return extracted
            try:
                import pypdf
                reader = pypdf.PdfReader(orig_path)
                for page in reader.pages:
                    for img_obj in page.images:
                        with open(extracted, "wb") as f:
                            f.write(img_obj.data)
                        return extracted
            except Exception:
                pass
            if utils.PDF_RENDER_AVAILABLE:
                try:
                    img_data = utils.render_pdf_page_preview(orig_path, 0, "color", dpi=150)
                    with open(extracted, "wb") as f:
                        f.write(img_data)
                    return extracted
                except Exception:
                    pass

    return orig_path


@app.get("/admin/order/{order_id}/sub-preview")
async def admin_get_order_sub_preview(order_id: str, request: Request, side: str = "front"):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")

    ensure_order_pristine_backup(order)
    pristine = str(request.query_params.get("pristine", "0")).lower() in ("1", "true")

    extra = {}
    try:
        extra = json.loads(order.get("extra_data") or "{}")
    except Exception:
        pass

    target_path = None
    if extra.get("is_nid") or order.get("paper_size") == "NID" or "nid_" in order["file_path"]:
        token = extra.get("nid_token")
        if not token and "nid_print_" in order["file_path"]:
            token = order["file_path"].split("nid_print_")[1].split(".pdf")[0]
        if token:
            front, back = nid_paths(token)
            if pristine:
                p_nid = os.path.join(UPLOAD_DIR, f"pristine_nid_{token}_b.jpg" if side == "back" else f"pristine_nid_{token}_f.jpg")
                target_path = p_nid if os.path.isfile(p_nid) else (back if side == "back" else front)
            else:
                target_path = back if side == "back" else front
    elif extra.get("is_studio") or order.get("paper_size") == "PHOTO" or "studio_" in order["file_path"]:
        src_name = extra.get("source_photo") or ""
        if pristine and src_name:
            p_photo = os.path.join(UPLOAD_DIR, f"pristine_{src_name}")
            if os.path.isfile(p_photo):
                target_path = p_photo
            else:
                target_path = resolve_studio_source_path(order, extra)
        else:
            target_path = resolve_studio_source_path(order, extra)
    else:
        orig = order["file_path"]
        if pristine:
            p_file = extra.get("pristine_path") or os.path.join(UPLOAD_DIR, f"pristine_{order['id']}_{os.path.basename(orig)}")
            target_path = p_file if os.path.isfile(p_file) else orig
        else:
            target_path = orig

    if not target_path or not os.path.isfile(target_path):
        target_path = order["file_path"]

    ext = utils.get_ext(target_path)
    if ext == "pdf":
        import office_docs
        page_num = max(1, int(request.query_params.get("page", 1) or 1))
        im = office_docs.render_pdf_page_pil(target_path, page_num, dpi=160)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return Response(content=buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    else:
        with Image.open(target_path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            return Response(content=buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/admin/order/{order_id}/save-crop")
async def admin_save_order_crop(order_id: str, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")

    ensure_order_pristine_backup(order)

    body = await request.json()
    image_data = body.get("image_data") or ""
    side = body.get("side") or "front"
    page = int(body.get("page") or 1)

    if not image_data:
        raise HTTPException(status_code=400, detail="No image data provided")
    if "," in image_data:
        image_data = image_data.split(",", 1)[1]

    try:
        raw_bytes = base64.b64decode(image_data)
        im = Image.open(io.BytesIO(raw_bytes))
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image data: {e}")

    extra = {}
    try:
        extra = json.loads(order.get("extra_data") or "{}")
    except Exception:
        pass

    orig_path = order["file_path"]

    if extra.get("is_nid") or order.get("paper_size") == "NID" or "nid_" in orig_path:
        token = extra.get("nid_token")
        if not token and "nid_print_" in orig_path:
            token = orig_path.split("nid_print_")[1].split(".pdf")[0]
        if token:
            front, back = nid_paths(token)
            dest = back if side == "back" else front
            im.save(dest, "JPEG", quality=95)
            fit_mode = extra.get("fit", "cover")
            # Reset rotation for the newly cropped upright card image
            extra["rot_" + side] = 0
            db.update_order_details(order_id, {"extra_data": json.dumps(extra)})
            rot_front = int(extra.get("rot_front", 0))
            rot_back = int(extra.get("rot_back", 0))
            color_mode = order.get("color_mode", "color")
            await asyncio.to_thread(
                utils.save_nid_sheet, front, back, orig_path,
                color_mode=color_mode,
                fit_mode=fit_mode,
                rot_front=rot_front,
                rot_back=rot_back,
                orientation=order.get("orientation", "portrait")
            )
            if storage.is_r2_configured():
                if os.path.isfile(dest):
                    await asyncio.to_thread(storage.upload_file_to_r2, dest)
                if os.path.isfile(orig_path):
                    await asyncio.to_thread(storage.upload_file_to_r2, orig_path)
    elif extra.get("is_studio") or order.get("paper_size") == "PHOTO" or "studio_" in orig_path:
        src_path = resolve_studio_source_path(order, extra)
        if src_path and os.path.isfile(src_path):
            im.save(src_path, "JPEG", quality=95)
            if storage.is_r2_configured():
                await asyncio.to_thread(storage.upload_file_to_r2, src_path)
        sp = studio.sanitize_params(extra.get("studio"))
        p_map = {order_id: (src_path or orig_path)}
        sheets, _info = studio.render_sheets((src_path or orig_path), sp, dpi=300, photos_map=p_map)
        page_count = max(1, len(sheets))
        studio.save_sheet_pdf(sheets, orig_path, 300)
        db.update_order_details(order_id, {"page_count": page_count})
        if storage.is_r2_configured() and os.path.isfile(orig_path):
            await asyncio.to_thread(storage.upload_file_to_r2, orig_path)
    elif order.get("file_type") == "pdf" and utils.get_ext(orig_path) == "pdf":
        import office_docs
        await asyncio.to_thread(office_docs.replace_pdf_page_pil, orig_path, page, im)
        office_docs._clear_nup_caches_for(orig_path)
        if storage.is_r2_configured() and os.path.isfile(orig_path):
            await asyncio.to_thread(storage.upload_file_to_r2, orig_path)
    else:
        fid = uuid.uuid4().hex[:10]
        new_filename = f"cropped_{fid}.jpg"
        new_path = os.path.join(UPLOAD_DIR, new_filename)
        im.save(new_path, "JPEG", quality=95)
        if storage.is_r2_configured() and os.path.isfile(new_path):
            await asyncio.to_thread(storage.upload_file_to_r2, new_path)
        p_file = extra.get("pristine_path")
        orig_rec = extra.get("original_file_path")
        if orig_path != new_path and os.path.isfile(orig_path):
            if orig_path != p_file and orig_path != orig_rec:
                utils.secure_delete_file(orig_path)
        db.update_order_details(order_id, {
            "file_path": new_path,
            "file_type": "image",
            "page_count": 1
        })

    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id})
    return {"success": True, "file_path": new_path if 'new_path' in locals() else orig_path}


@app.post("/admin/order/{order_id}/rotate")
async def admin_rotate_order_file(order_id: str, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")

    ensure_order_pristine_backup(order)

    body = await request.json()
    side = body.get("side") or "front"
    page = int(body.get("page") or 1)
    degrees = int(body.get("degrees") or 90)

    extra = {}
    try:
        extra = json.loads(order.get("extra_data") or "{}")
    except Exception:
        pass

    orig_path = order["file_path"]

    if extra.get("is_nid") or order.get("paper_size") == "NID" or "nid_" in orig_path:
        token = extra.get("nid_token")
        if not token and "nid_print_" in orig_path:
            token = orig_path.split("nid_print_")[1].split(".pdf")[0]
        if token:
            front, back = nid_paths(token)
            target = back if side == "back" else front
            with Image.open(target) as im:
                im = ImageOps.exif_transpose(im)
                rot = im.rotate(-degrees, expand=True)
                rot.save(target, "JPEG", quality=95)
            fit_mode = extra.get("fit", "cover")
            rot_front = int(extra.get("rot_front", 0))
            rot_back = int(extra.get("rot_back", 0))
            color_mode = order.get("color_mode", "color")
            await asyncio.to_thread(
                utils.save_nid_sheet, front, back, orig_path,
                color_mode=color_mode,
                fit_mode=fit_mode,
                rot_front=rot_front,
                rot_back=rot_back,
                orientation=order.get("orientation", "portrait")
            )
            if storage.is_r2_configured() and os.path.isfile(orig_path):
                await asyncio.to_thread(storage.upload_file_to_r2, orig_path)
    elif extra.get("is_studio") or order.get("paper_size") == "PHOTO" or "studio_" in orig_path:
        src_path = resolve_studio_source_path(order, extra)
        if src_path and os.path.isfile(src_path):
            with Image.open(src_path) as im:
                im = ImageOps.exif_transpose(im)
                rot = im.rotate(-degrees, expand=True)
                rot.save(src_path, "JPEG", quality=95)
            if storage.is_r2_configured():
                await asyncio.to_thread(storage.upload_file_to_r2, src_path)
            sp = studio.sanitize_params(extra.get("studio"))
            p_map = {order_id: src_path}
            sheets, _info = studio.render_sheets(src_path, sp, dpi=300, photos_map=p_map)
            page_count = max(1, len(sheets))
            studio.save_sheet_pdf(sheets, orig_path, 300)
            db.update_order_details(order_id, {"page_count": page_count})
            if storage.is_r2_configured() and os.path.isfile(orig_path):
                await asyncio.to_thread(storage.upload_file_to_r2, orig_path)
    elif order.get("file_type") == "pdf" and utils.get_ext(orig_path) == "pdf":
        import pypdf
        import office_docs
        reader = pypdf.PdfReader(orig_path)
        writer = pypdf.PdfWriter()
        for idx, p in enumerate(reader.pages):
            if idx + 1 == page or page == 0:
                p.rotate(degrees)
            writer.add_page(p)
        temp_out = orig_path + ".rot.tmp"
        with open(temp_out, "wb") as f_out:
            writer.write(f_out)
        os.replace(temp_out, orig_path)
        office_docs._clear_nup_caches_for(orig_path)
        if storage.is_r2_configured() and os.path.isfile(orig_path):
            await asyncio.to_thread(storage.upload_file_to_r2, orig_path)
    else:
        with Image.open(orig_path) as im:
            im = ImageOps.exif_transpose(im)
            rot = im.rotate(-degrees, expand=True)
            rot.save(orig_path, "JPEG", quality=95)
        if storage.is_r2_configured() and os.path.isfile(orig_path):
            await asyncio.to_thread(storage.upload_file_to_r2, orig_path)

    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id})
    return {"success": True}


@app.post("/admin/order/{order_id}/camscan")
async def admin_camscan_order_file(order_id: str, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")

    ensure_order_pristine_backup(order)

    body = await request.json()
    mode = clean_choice(body.get("mode"), ("original", "magic_color", "bw_doc", "hd_clear", "grayscale"), "magic_color")
    page = int(body.get("page") or 1)
    all_pages = bool(body.get("all_pages", False))
    side = body.get("side") or "front"
    import image_fx
    import office_docs
    import shutil

    extra = {}
    try:
        extra = json.loads(order.get("extra_data") or "{}")
    except Exception:
        pass

    orig_path = order["file_path"]

    if extra.get("is_nid") or order.get("paper_size") == "NID" or "nid_" in orig_path:
        token = extra.get("nid_token")
        if not token and "nid_print_" in orig_path:
            token = orig_path.split("nid_print_")[1].split(".pdf")[0]
        if token:
            front, back = nid_paths(token)
            p_front = os.path.join(UPLOAD_DIR, f"pristine_nid_{token}_f.jpg")
            p_back = os.path.join(UPLOAD_DIR, f"pristine_nid_{token}_b.jpg")
            targets = [("front", front, p_front), ("back", back, p_back)] if side == "all" else [(side, back if side == "back" else front, p_back if side == "back" else p_front)]
            for s_name, working_target, pristine_target in targets:
                src_to_read = pristine_target if os.path.isfile(pristine_target) else working_target
                if mode == "original":
                    if os.path.isfile(pristine_target) and pristine_target != working_target:
                        shutil.copy2(pristine_target, working_target)
                else:
                    with Image.open(src_to_read) as im:
                        im = ImageOps.exif_transpose(im).convert("RGB")
                        enhanced = image_fx.camscanner_process(im, mode=mode)
                        enhanced.save(working_target, "JPEG", quality=95)
            fit_mode = extra.get("fit", "cover")
            rot_front = int(extra.get("rot_front", 0))
            rot_back = int(extra.get("rot_back", 0))
            color_mode = order.get("color_mode", "color")
            await asyncio.to_thread(
                utils.save_nid_sheet, front, back, orig_path,
                color_mode=color_mode,
                fit_mode=fit_mode,
                rot_front=rot_front,
                rot_back=rot_back,
                orientation=order.get("orientation", "portrait")
            )
            if storage.is_r2_configured() and os.path.isfile(orig_path):
                await asyncio.to_thread(storage.upload_file_to_r2, orig_path)
    elif extra.get("is_studio") or order.get("paper_size") == "PHOTO" or "studio_" in orig_path:
        src_path = resolve_studio_source_path(order, extra)
        if src_path and os.path.isfile(src_path):
            src_name = extra.get("source_photo") or os.path.basename(src_path)
            p_photo = extra.get("pristine_photo") or os.path.join(UPLOAD_DIR, f"pristine_{src_name}")
            src_to_read = p_photo if os.path.isfile(p_photo) else src_path
            if mode == "original":
                if os.path.isfile(p_photo) and p_photo != src_path:
                    shutil.copy2(p_photo, src_path)
            else:
                with Image.open(src_to_read) as im:
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    enhanced = image_fx.camscanner_process(im, mode=mode)
                    enhanced.save(src_path, "JPEG", quality=95)
            if storage.is_r2_configured():
                await asyncio.to_thread(storage.upload_file_to_r2, src_path)
            sp = studio.sanitize_params(extra.get("studio"))
            p_map = {order_id: src_path}
            sheets, _info = studio.render_sheets(src_path, sp, dpi=300, photos_map=p_map)
            page_count = max(1, len(sheets))
            studio.save_sheet_pdf(sheets, orig_path, 300)
            db.update_order_details(order_id, {"page_count": page_count})
            if storage.is_r2_configured() and os.path.isfile(orig_path):
                await asyncio.to_thread(storage.upload_file_to_r2, orig_path)
    elif order.get("file_type") == "pdf" and utils.get_ext(orig_path) == "pdf":
        p_file = extra.get("pristine_path") or os.path.join(UPLOAD_DIR, f"pristine_{order['id']}_{os.path.basename(orig_path)}")
        has_pristine = os.path.isfile(p_file)
        if mode == "original":
            if has_pristine:
                if all_pages:
                    shutil.copy2(p_file, orig_path)
                else:
                    im = await asyncio.to_thread(office_docs.render_pdf_page_pil, p_file, page, 200)
                    await asyncio.to_thread(office_docs.replace_pdf_page_pil, orig_path, page, im)
        else:
            pdf_source = p_file if has_pristine else orig_path
            if all_pages:
                await asyncio.to_thread(office_docs.apply_camscan_to_all_pdf_pages, pdf_source, orig_path, mode)
            else:
                im = await asyncio.to_thread(office_docs.render_pdf_page_pil, pdf_source, page, 200)
                im = await asyncio.to_thread(image_fx.camscanner_process, im, mode)
                await asyncio.to_thread(office_docs.replace_pdf_page_pil, orig_path, page, im)
        office_docs._clear_nup_caches_for(orig_path)
        if storage.is_r2_configured() and os.path.isfile(orig_path):
            await asyncio.to_thread(storage.upload_file_to_r2, orig_path)
    else:
        p_file = extra.get("pristine_path") or os.path.join(UPLOAD_DIR, f"pristine_{order['id']}_{os.path.basename(orig_path)}")
        has_pristine = os.path.isfile(p_file)
        src_to_read = p_file if has_pristine else orig_path
        if mode == "original":
            if has_pristine and p_file != orig_path:
                shutil.copy2(p_file, orig_path)
        else:
            with Image.open(src_to_read) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                enhanced = image_fx.camscanner_process(im, mode=mode)
                enhanced.save(orig_path, "JPEG", quality=95)
        if storage.is_r2_configured() and os.path.isfile(orig_path):
            await asyncio.to_thread(storage.upload_file_to_r2, orig_path)

    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id})
    return {"success": True}


@app.post("/admin/order/{order_id}/revert-original")
async def admin_revert_order_to_original(order_id: str, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")

    ensure_order_pristine_backup(order)
    extra = {}
    try:
        extra = json.loads(order.get("extra_data") or "{}")
    except Exception:
        pass

    orig_path = order["file_path"]
    import shutil

    # 1. NID Card
    if extra.get("is_nid") or order.get("paper_size") == "NID" or "nid_" in orig_path:
        token = extra.get("nid_token")
        if not token and "nid_print_" in orig_path:
            token = orig_path.split("nid_print_")[1].split(".pdf")[0]
        if token:
            front, back = nid_paths(token)
            p_front = os.path.join(UPLOAD_DIR, f"pristine_nid_{token}_f.jpg")
            p_back = os.path.join(UPLOAD_DIR, f"pristine_nid_{token}_b.jpg")
            restored = False
            if os.path.isfile(p_front):
                shutil.copy2(p_front, front)
                restored = True
            if os.path.isfile(p_back):
                shutil.copy2(p_back, back)
                restored = True
            if not restored:
                raise HTTPException(status_code=400, detail="অরিজিনাল NID ফাইল পাওয়া যায়নি")
            extra["rot_front"] = 0
            extra["rot_back"] = 0
            color_mode = order.get("customer_color_mode") or order.get("color_mode", "color")
            fit_mode = extra.get("fit", "cover")
            await asyncio.to_thread(
                utils.save_nid_sheet, front, back, orig_path,
                color_mode=color_mode,
                fit_mode=fit_mode,
                rot_front=0,
                rot_back=0,
                orientation=order.get("orientation", "portrait")
            )
            db.update_order_details(order_id, {
                "color_mode": color_mode,
                "extra_data": json.dumps(extra)
            })
            if storage.is_r2_configured() and os.path.isfile(orig_path):
                await asyncio.to_thread(storage.upload_file_to_r2, orig_path)

    # 2. Photo Studio
    elif extra.get("is_studio") or order.get("paper_size") == "PHOTO" or "studio_" in orig_path:
        src_name = extra.get("source_photo") or ""
        src_path = os.path.join(UPLOAD_DIR, src_name) if src_name else None
        p_photo = os.path.join(UPLOAD_DIR, f"pristine_{src_name}") if src_name else None
        if src_path and p_photo and os.path.isfile(p_photo):
            shutil.copy2(p_photo, src_path)
            sp = studio.sanitize_params(extra.get("studio"))
            p_map = {order_id: src_path}
            sheets, _info = studio.render_sheets(src_path, sp, dpi=300, photos_map=p_map)
            page_count = max(1, len(sheets))
            studio.save_sheet_pdf(sheets, orig_path, 300)
            db.update_order_details(order_id, {"page_count": page_count})
            if storage.is_r2_configured() and os.path.isfile(orig_path):
                await asyncio.to_thread(storage.upload_file_to_r2, orig_path)
        else:
            raise HTTPException(status_code=400, detail="অরিজিনাল স্টুডিও ফটো পাওয়া যায়নি")

    # 3. Document or Image
    else:
        p_file = extra.get("pristine_path") or os.path.join(UPLOAD_DIR, f"pristine_{order['id']}_{os.path.basename(orig_path)}")
        initial_file = extra.get("original_file_path") or orig_path
        if os.path.isfile(p_file):
            shutil.copy2(p_file, initial_file)
            updates = {"file_path": initial_file}
            if initial_file.lower().endswith(".pdf"):
                import office_docs
                office_docs._clear_nup_caches_for(initial_file)
                updates["file_type"] = "pdf"
                try:
                    import pypdf
                    updates["page_count"] = len(pypdf.PdfReader(initial_file).pages)
                except Exception:
                    pass
            else:
                updates["file_type"] = "image"
                updates["page_count"] = 1
            if orig_path != initial_file and os.path.isfile(orig_path):
                utils.secure_delete_file(orig_path)
            db.update_order_details(order_id, updates)
            if storage.is_r2_configured() and os.path.isfile(initial_file):
                await asyncio.to_thread(storage.upload_file_to_r2, initial_file)
        else:
            raise HTTPException(status_code=400, detail="অরিজিনাল ব্যাকআপ ফাইল পাওয়া যায়নি")

    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id})
    return {"success": True, "message": "কাস্টোমারের আসল ডকুমেন্ট সফলভাবে ফিরিয়ে আনা হয়েছে"}

    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id})
    return {"success": True}


@app.post("/admin/order/{order_id}/update-nid")
async def admin_update_order_nid(order_id: str, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")

    body = await request.json()
    fit = clean_choice(body.get("fit"), ("cover", "contain"), "cover")
    color = clean_choice(body.get("color_mode"), ("color", "bw"), order.get("color_mode", "color"))

    extra = {}
    try:
        extra = json.loads(order.get("extra_data") or "{}")
    except Exception:
        pass

    token = extra.get("nid_token")
    if not token and "nid_print_" in order["file_path"]:
        token = order["file_path"].split("nid_print_")[1].split(".pdf")[0]
    front, back = nid_paths(token)
    orig_path = order["file_path"]
    await asyncio.to_thread(
        utils.save_nid_sheet, front, back, orig_path,
        color_mode=color,
        fit_mode=fit,
        rot_front=int(extra.get("rot_front", 0)),
        rot_back=int(extra.get("rot_back", 0)),
        orientation=order.get("orientation", "portrait")
    )
    extra["fit"] = fit
    db.update_order_details(order_id, {
        "color_mode": color,
        "extra_data": json.dumps(extra)
    })
    if storage.is_r2_configured() and os.path.isfile(orig_path):
        await asyncio.to_thread(storage.upload_file_to_r2, orig_path)

    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id})
    return {"success": True}


@app.post("/admin/order/{order_id}/update-studio")
async def admin_update_order_studio(order_id: str, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")

    body = await request.json()
    sp = studio.sanitize_params(body.get("studio"))
    extra = {}
    try:
        extra = json.loads(order.get("extra_data") or "{}")
    except Exception:
        pass

    orig_path = order["file_path"]
    src_path = resolve_studio_source_path(order, extra)
    if not src_path or not os.path.isfile(src_path):
        src_path = orig_path

    p_map = {order_id: src_path}
    sheets, _info = studio.render_sheets(src_path, sp, dpi=300, photos_map=p_map)
    page_count = max(1, len(sheets))
    studio.save_sheet_pdf(sheets, orig_path, 300)

    rate_passport = float(shop.get("rate_passport") or 5.0)
    rate_stamp = float(shop.get("rate_stamp") or 2.0)
    photo_items_cost = 0.0
    for it in sp.get("items", []):
        cnt = int(it.get("count", 0) or 0)
        w = float(it.get("w", 0) or 0)
        h = float(it.get("h", 0) or 0)
        preset = str(it.get("preset", ""))
        if preset == "stamp" or (w <= 25 and h <= 30):
            photo_items_cost += cnt * rate_stamp
        else:
            photo_items_cost += cnt * rate_passport

    copies = int(order.get("copies", 1))
    if sp["paper"] == "4x6":
        sheet_rate = float(shop.get("rate_photo_4x6") or 30.0)
    else:
        sheet_rate = float(shop.get("rate_bw_single") or 3.0) if sp["bw"] else float(shop.get("rate_photo_a4") or 40.0)
    rate = photo_items_cost if photo_items_cost > 0 else sheet_rate
    total_price = rate * copies

    extra["studio"] = sp
    db.update_order_details(order_id, {
        "color_mode": "bw" if sp["bw"] else "color",
        "paper_size": "4x6" if sp["paper"] == "4x6" else ("PHOTO" if order.get("paper_size") == "PHOTO" else "A4"),
        "page_count": page_count,
        "total_price": round(total_price, 2),
        "extra_data": json.dumps(extra)
    })
    if storage.is_r2_configured() and os.path.isfile(orig_path):
        await asyncio.to_thread(storage.upload_file_to_r2, orig_path)

    await manager.notify_admin(shop["id"], {"event": "ORDER_UPDATED", "order_id": order_id})
    return {"success": True, "page_count": page_count, "total_pages": page_count, "total_price": round(total_price, 2), "studio": sp}

@app.get("/admin/order/{order_id}/file")
async def admin_download_order_file(order_id: str, request: Request, inline: int = 0, json_url: int = 0, local: int = 0):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")
    ext = utils.get_ext(order["file_path"])
    mtype = "application/pdf" if ext == "pdf" else ("image/jpeg" if ext in ("jpg", "jpeg") else "image/png")

    # Serve directly from Cloudflare R2 (0 Render egress bandwidth) when configured
    if not local and storage.is_r2_configured():
        r2_url = await asyncio.to_thread(
            storage.get_r2_url_for_local_path,
            order["file_path"],
            order["original_filename"],
            bool(inline),
            mtype,
        )
        if r2_url:
            if json_url:
                return {"url": r2_url, "content_type": mtype, "storage": "cloudflare_r2"}
            return RedirectResponse(url=r2_url, status_code=status.HTTP_302_FOUND)

    if not os.path.exists(order["file_path"]):
        await asyncio.to_thread(storage.ensure_local_from_r2, order["file_path"])
    if not os.path.exists(order["file_path"]):
        raise HTTPException(status_code=404, detail="File has been deleted or not found")
    if json_url:
        return {"url": f"/admin/order/{order_id}/file?inline={inline}&local=1", "content_type": mtype, "storage": "local"}
    return FileResponse(
        order["file_path"],
        filename=order["original_filename"],
        media_type=mtype,
        content_disposition_type="inline" if inline else "attachment"
    )


@app.get("/admin/order/{order_id}/preview")
def admin_preview_order_sheet(
    order_id: str,
    request: Request,
    color: str = "",
    orientation: str = "",
    paper_size: str = "",
    page: int = 1
):
    """Renders a live preview of how an order will be printed inside the Admin Edit/Print modal."""
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")
    path = order["file_path"]
    if not os.path.isfile(path):
        storage.ensure_local_from_r2(path)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found or already printed")

    eff_color = clean_choice(color or order.get("color_mode", "color"), ("bw", "color"), "color")
    eff_orient = clean_choice(orientation or order.get("orientation", "portrait"), ("portrait", "landscape"), "portrait")
    eff_paper = clean_choice(paper_size or order.get("paper_size", "A4"), ("A4", "4x6", "NID", "PHOTO"), "A4")

    ext = utils.get_ext(path)
    if ext == "pdf":
        if not utils.PDF_RENDER_AVAILABLE:
            raise HTTPException(status_code=501, detail="PDF preview renderer unavailable")
        try:
            data = utils.render_pdf_page_preview(path, max(0, page - 1), eff_color, orientation=eff_orient)
            return Response(
                content=data,
                media_type="image/jpeg",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                    "Expires": "0"
                }
            )
        except Exception as e:
            raise HTTPException(status_code=422, detail=str(e))
    else:
        p_mode = "4x6" if eff_paper == "4x6" else "A4"
        data = utils.render_image_preview(path, p_mode, eff_color, orientation=eff_orient)
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )


@app.get("/admin/printers")
async def admin_get_printers(request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"printers": db.get_shop_printers(shop["id"])}


@app.post("/admin/printers/add")
async def admin_add_printer(request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = await request.json()
    pname = str(data.get("printer_name", "")).strip()
    ptype = clean_choice(data.get("printer_type", "all"), ("bw", "color", "all"), "all")
    if not pname:
        raise HTTPException(status_code=400, detail="প্রিন্টারের নাম লিখুন")
    db.add_shop_printer(shop["id"], pname, ptype)
    return {"success": True, "printers": db.get_shop_printers(shop["id"])}


@app.post("/admin/printers/{printer_id}/delete")
async def admin_delete_printer(printer_id: int, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    db.delete_shop_printer(shop["id"], printer_id)
    return {"success": True, "printers": db.get_shop_printers(shop["id"])}


# ── CUSTOMER FLOW (/p/{shop_code}) ──

@app.get("/p/{shop_code}", response_class=HTMLResponse)
async def customer_portal(shop_code: str, request: Request):
    shop = db.get_shop_by_code(shop_code.strip().lower())
    if not shop:
        return HTMLResponse("<h2>দুঃখিত! এই শপ কোডের কোনো দোকান খুঁজে পাওয়া যায়নি।</h2>", status_code=404)
    printers = db.get_shop_printers(shop["id"])
    return templates.TemplateResponse(request=request, name="customer_print.html", context={
        "shop": shop,
        "printers": printers
    })

# ── Upload helpers ──
async def save_upload_limited(file: UploadFile, dest: str, max_bytes: int = MAX_UPLOAD_BYTES) -> int:
    """Streams an upload to disk and aborts if it exceeds max_bytes."""
    size = 0
    try:
        async with aiofiles.open(dest, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail=f"ফাইলের সাইজ সর্বোচ্চ {max_bytes // (1024 * 1024)} MB হতে পারবে।")
                await out.write(chunk)
    except BaseException:
        utils.secure_delete_file(dest)
        raise
    return size


def resolve_upload(file_id: str) -> str:
    """Maps a client supplied file id to a path INSIDE the upload folder (no path traversal)."""
    safe = os.path.basename(str(file_id or ""))
    path = os.path.join(UPLOAD_DIR, safe)
    if not safe or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    return path


NID_TOKEN_RE = re.compile(r"^[0-9a-f]{12}$")


def nid_paths(token: str):
    if not NID_TOKEN_RE.match(token or ""):
        raise HTTPException(status_code=404, detail="NID files not found")
    front = os.path.join(UPLOAD_DIR, f"nid_{token}_f.jpg")
    back = os.path.join(UPLOAD_DIR, f"nid_{token}_b.jpg")
    if not (os.path.isfile(front) and os.path.isfile(back)):
        raise HTTPException(status_code=404, detail="NID files not found")
    return front, back


def clean_choice(value, allowed, default):
    return value if value in allowed else default


def clean_rotation(value) -> int:
    try:
        r = int(value) % 360
    except (TypeError, ValueError):
        return 0
    return r if r in (0, 90, 180, 270) else 0


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """Uploads file temporarily, validates it, converts Office documents if needed, and returns file info."""
    import office_docs
    ext = utils.get_ext(file.filename or "")
    allowed_exts = {"pdf", "jpg", "jpeg", "png", "webp"} | office_docs.OFFICE_EXTENSIONS
    if ext not in allowed_exts:
        raise HTTPException(
            status_code=400,
            detail="শুধুমাত্র PDF, Word (.docx/.doc), Excel (.xlsx/.xls/.csv) অথবা ছবি (JPG, PNG) আপলোড করা যাবে।"
        )

    fid = uuid.uuid4().hex[:10]
    raw_path = os.path.join(UPLOAD_DIR, f"raw_{fid}.{ext}")
    size = await save_upload_limited(file, raw_path)
    orig_safe_name = utils.safe_filename(file.filename, "document")

    is_office = False
    doc_kind = None
    office_orientation = "portrait"

    if ext == "pdf":
        file_id = f"{fid}.pdf"
        save_path = os.path.join(UPLOAD_DIR, file_id)
        os.replace(raw_path, save_path)
        ok, msg = utils.is_valid_pdf(save_path)
        if not ok:
            utils.secure_delete_file(save_path)
            raise HTTPException(status_code=400, detail=msg)
        file_type = "pdf"
    elif ext in office_docs.OFFICE_EXTENSIONS:
        file_id = f"{fid}.pdf"
        save_path = os.path.join(UPLOAD_DIR, file_id)
        state_path = os.path.join(UPLOAD_DIR, f"office_{fid}.json")
        try:
            raw_backup = os.path.join(UPLOAD_DIR, f"raw_{fid}.{ext}")
            shutil.copy2(raw_path, raw_backup)
        except Exception:
            pass
        try:
            info = await asyncio.to_thread(
                office_docs.convert_office_file_to_pdf_and_state,
                raw_path, ext, orig_safe_name, save_path, state_path
            )
            is_office = True
            doc_kind = info.get("doc_kind", "word")
            office_orientation = info.get("orientation", "portrait")
        except Exception as e:
            utils.secure_delete_file(save_path)
            raise HTTPException(status_code=400, detail=f"Word/Excel ফাইলটি প্রসেস করা যায়নি: {e}")
        finally:
            utils.secure_delete_file(raw_path)
        file_type = "pdf"
    else:
        file_id = f"{fid}.jpg"
        save_path = os.path.join(UPLOAD_DIR, file_id)
        try:
            await asyncio.to_thread(utils.normalize_image, raw_path, save_path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        finally:
            utils.secure_delete_file(raw_path)
        file_type = "image"

    pages = utils.get_file_page_count(save_path, file_type)
    return {
        "success": True,
        "file_id": file_id,
        "original_filename": orig_safe_name,
        "file_type": file_type,
        "page_count": pages,
        "file_size": size,
        "is_office": is_office,
        "doc_kind": doc_kind,
        "office_orientation": office_orientation,
        "preview_url": f"/api/preview/{file_id}"
    }


@app.get("/api/office/content/{file_id}")
def get_office_editable_content(file_id: str):
    """Returns editable HTML / spreadsheet rows for an uploaded Word, Excel, or PDF document."""
    import office_docs
    safe_id = os.path.basename(str(file_id or ""))
    src = resolve_upload(safe_id)
    fid = safe_id.rsplit(".", 1)[0]
    state_path = os.path.join(UPLOAD_DIR, f"office_{fid}.json")
    if os.path.isfile(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"success": True, **data}
    if utils.get_ext(src) == "pdf":
        state = office_docs.extract_pdf_editable_state(src, safe_id)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        return {"success": True, **state}
    raise HTTPException(status_code=400, detail="এই ফাইলটি টেক্সট/টেবিল হিসেবে এডিট করা যাবে না")


@app.post("/api/office/save")
async def save_office_editable_content(payload: dict = Body(...)):
    """Saves customer edits to a Word/Excel/PDF document and regenerates the printable PDF."""
    import office_docs
    file_id = os.path.basename(str(payload.get("file_id", "")))
    src = resolve_upload(file_id)
    if utils.get_ext(src) != "pdf":
        raise HTTPException(status_code=400, detail="শুধু ডকুমেন্ট ফাইল এডিট করে সেভ করা যাবে")

    fid = file_id.rsplit(".", 1)[0]
    state_path = os.path.join(UPLOAD_DIR, f"office_{fid}.json")
    doc_kind = str(payload.get("doc_kind", "word"))
    orientation = clean_choice(str(payload.get("orientation", "portrait")), ("portrait", "landscape"), "portrait")
    title = str(payload.get("title", "Document"))

    if doc_kind == "excel" and isinstance(payload.get("sheets"), list):
        sheets = payload["sheets"]
        html_body = office_docs.sheets_to_html(sheets, title=title, orientation=orientation)
    else:
        sheets = None
        html_body = office_docs.sanitize_story_html(str(payload.get("html", "<p>&nbsp;</p>")))

    raw_margins = payload.get("margins")
    margins_tuple = (
        tuple(float(m) for m in raw_margins[:4])
        if isinstance(raw_margins, list) and len(raw_margins) >= 4
        else (36.0, 36.0, 36.0, 36.0)
    )

    state = {
        "doc_kind": doc_kind,
        "title": title,
        "orientation": orientation,
        "margins": list(margins_tuple),
        "html": html_body,
        "sheets": sheets,
    }
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

    pages = await asyncio.to_thread(office_docs.render_html_to_pdf, html_body, src, orientation, margins_tuple)
    _update_orig_backup(src)
    try:
        flag_p = os.path.join(UPLOAD_DIR, f"edited_{fid}.flag")
        with open(flag_p, "w", encoding="utf-8") as flg:
            flg.write("1")
    except Exception:
        pass
    return {
        "success": True,
        "file_id": file_id,
        "page_count": pages,
        "orientation": orientation
    }


@app.post("/api/upload-nid")
async def upload_nid_card(front: UploadFile = File(...), back: UploadFile = File(...)):
    """Stores the two NID / ID card photos (front + back). The A4 sheet is rendered on demand."""
    token = uuid.uuid4().hex[:12]
    front_path = os.path.join(UPLOAD_DIR, f"nid_{token}_f.jpg")
    back_path = os.path.join(UPLOAD_DIR, f"nid_{token}_b.jpg")

    try:
        for upload, dest in ((front, front_path), (back, back_path)):
            ext = utils.get_ext(upload.filename or "") or "jpg"
            if ext not in ["jpg", "jpeg", "png", "webp", "heic"]:
                raise HTTPException(status_code=400, detail="NID এর জন্য শুধু ছবি (JPG, PNG) দিন।")
            raw = os.path.join(UPLOAD_DIR, f"raw_nid_{token}_{os.path.basename(dest)}")
            await save_upload_limited(upload, raw, 20 * 1024 * 1024)
            try:
                await asyncio.to_thread(utils.normalize_image, raw, dest)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            finally:
                utils.secure_delete_file(raw)
    except BaseException:
        utils.secure_delete_file(front_path)
        utils.secure_delete_file(back_path)
        raise

    return {
        "file_id": f"nid_{token}",
        "nid_token": token,
        "original_filename": "NID_Card_Front_Back.pdf",
        "file_type": "image",
        "page_count": 1,
        "is_nid": True,
        "preview_url": f"/api/nid-preview/{token}"
    }


@app.get("/api/nid-preview/{token}")
def nid_preview(token: str, color: str = "color", fit: str = "cover", rf: int = 0, rb: int = 0, w: int = 800, orientation: str = "portrait"):
    """Exact print preview of the A4 sheet that will be printed (front + back at real NID size)."""
    front, back = nid_paths(token)
    data = utils.nid_preview_jpeg(
        front, back, width=max(400, min(w, 1400)),
        color_mode=clean_choice(color, ("color", "bw"), "color"),
        fit_mode=clean_choice(fit, ("cover", "contain"), "cover"),
        rot_front=clean_rotation(rf), rot_back=clean_rotation(rb),
        orientation=clean_choice(orientation, ("portrait", "landscape"), "portrait")
    )
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/preview/{file_id}")
def preview_file(file_id: str, side: str = "front", w: int = 0, page: int = 1):
    """Provides safe preview of the uploaded file, with optional fast resized width w for instant UI previews (including PDF pages)."""
    safe_id = os.path.basename(str(file_id or ""))
    if safe_id.startswith("nid_") and not safe_id.endswith((".jpg", ".jpeg", ".png", ".webp")):
        token = safe_id[4:]
        front, back = nid_paths(token)
        file_path = back if side == "back" else front
    else:
        file_path = resolve_upload(safe_id)
    ext = utils.get_ext(file_path)
    if w and 150 <= w <= 1600:
        if ext in ("jpg", "jpeg", "png", "webp"):
            try:
                with Image.open(file_path) as im:
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    if max(im.size) > w:
                        im.thumbnail((w, w), Image.Resampling.BILINEAR)
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=82)
                    return Response(content=buf.getvalue(), media_type="image/jpeg",
                                    headers={"Cache-Control": "public, max-age=300"})
            except Exception:
                pass
        elif ext == "pdf":
            try:
                import office_docs
                im = office_docs.render_pdf_page_pil(file_path, page_num=page, dpi=150)
                if max(im.size) > w:
                    im.thumbnail((w, w), Image.Resampling.BILINEAR)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=84)
                return Response(content=buf.getvalue(), media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=300"})
            except Exception:
                pass
    media_type = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                  "png": "image/png", "webp": "image/webp"}.get(ext, "application/octet-stream")
    return FileResponse(file_path, media_type=media_type,
                        headers={"Content-Disposition": f"inline; filename={os.path.basename(file_path)}"})


@app.get("/api/preview-info/{file_id}")
def preview_info(file_id: str, nup: int = 1):
    """How many printed sheets the customer will get and whether page images can be rendered."""
    path = resolve_upload(file_id)
    if utils.get_ext(path) == "pdf":
        pages = utils.get_file_page_count(path, "pdf")
        sheets = -(-pages // nup) if nup in (2, 4) else pages
        return {"can_render": utils.PDF_RENDER_AVAILABLE, "sheets": sheets, "pages": pages}
    return {"can_render": True, "sheets": 1, "pages": 1}


def get_image_pos_config(file_id: str) -> dict:
    fid = os.path.splitext(file_id)[0]
    p = os.path.join(UPLOAD_DIR, f"img_pos_{fid}.json")
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"pos_x": 0.5, "pos_y": 0.5, "scale": 1.0, "fit_mode": "contain"}


@app.post("/api/image-position/save")
async def save_image_position(payload: dict = Body(...)):
    file_id = str(payload.get("file_id", ""))
    src = resolve_upload(file_id)
    if utils.get_ext(src) not in ("jpg", "jpeg", "png", "webp"):
        raise HTTPException(status_code=400, detail="শুধু ছবির পজিশন পরিবর্তন করা যাবে")
    fid = os.path.splitext(file_id)[0]
    try:
        pos_x = float(payload.get("pos_x", 0.5))
        pos_y = float(payload.get("pos_y", 0.5))
        scale = float(payload.get("scale", 1.0))
    except (TypeError, ValueError):
        pos_x, pos_y, scale = 0.5, 0.5, 1.0
    fit_mode = str(payload.get("fit_mode", "contain"))

    cfg = {
        "pos_x": max(0.0, min(1.0, pos_x)),
        "pos_y": max(0.0, min(1.0, pos_y)),
        "scale": max(0.1, min(1.0, scale)),
        "fit_mode": "actual" if fit_mode == "actual" else "contain"
    }
    p = os.path.join(UPLOAD_DIR, f"img_pos_{fid}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    return {"success": True, "config": cfg}


@app.get("/api/image-position/get/{file_id}")
async def get_image_position_endpoint(file_id: str):
    resolve_upload(file_id)
    return {"success": True, "config": get_image_pos_config(file_id)}


@app.get("/api/preview-page/{file_id}")
def preview_page(
    file_id: str,
    page: int = 1,
    paper_size: str = "A4",
    nup: int = 1,
    color: str = "color",
    orientation: str = "portrait",
    pos_x: Optional[float] = None,
    pos_y: Optional[float] = None,
    scale: Optional[float] = None,
    fit_mode: Optional[str] = None
):
    orientation = clean_choice(orientation, ("portrait", "landscape"), "portrait")
    """Renders ONE printed sheet exactly as it will come out of the printer."""
    path = resolve_upload(file_id)
    color = clean_choice(color, ("color", "bw"), "color")

    if utils.get_ext(path) == "pdf":
        if not utils.PDF_RENDER_AVAILABLE:
            raise HTTPException(status_code=501, detail="PDF page rendering not available on this server")
        source = path
        if nup in (2, 4):
            cache = os.path.join(UPLOAD_DIR, f"pv_nup{nup}_{os.path.basename(path)}")
            source = utils.get_or_create_nup_preview(path, cache, nup)
        try:
            data = utils.render_pdf_page_preview(source, page - 1, color, orientation=orientation)
        except IndexError:
            raise HTTPException(status_code=404, detail="Page not found")
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Preview failed: {e}")
    else:
        cfg = get_image_pos_config(file_id)
        px = pos_x if pos_x is not None else cfg.get("pos_x", 0.5)
        py = pos_y if pos_y is not None else cfg.get("pos_y", 0.5)
        sc = scale if scale is not None else cfg.get("scale", 1.0)
        fm = fit_mode if fit_mode is not None else cfg.get("fit_mode", "contain")
        data = utils.render_image_preview(
            path,
            clean_choice(paper_size, ("A4", "4x6"), "A4"),
            color,
            orientation=orientation,
            pos_x=px,
            pos_y=py,
            scale=sc,
            fit_mode=fm
        )

    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=120"})


@app.post("/api/studio-preview")
def studio_preview(payload: dict = Body(...)):
    """Live preview of the photo sheet (passport / stamp / custom sizes in rows, multi-photo support)."""
    src = resolve_upload(payload.get("file_id", ""))
    if utils.get_ext(src) not in ("jpg", "jpeg", "png"):
        raise HTTPException(status_code=400, detail="Only images can be used in the photo studio")
    params = studio.sanitize_params(payload.get("params"))

    # Resolve all uploaded photos for multi-photo sheets
    photos_map = {str(payload.get("file_id", "")): src}
    extra_photos = payload.get("photos") or params.get("photos") or []
    for fid in extra_photos:
        try:
            p_src = resolve_upload(str(fid))
            if utils.get_ext(p_src) in ("jpg", "jpeg", "png"):
                photos_map[str(fid)] = p_src
        except Exception:
            pass

    page = max(1, int(payload.get("page", 1) or 1))
    sheets, info = studio.render_sheets(src, params, dpi=100, photos_map=photos_map)
    total_pages = max(1, len(sheets))
    page_idx = max(0, min(total_pages - 1, page - 1))
    sheet = sheets[page_idx]
    info["current_page"] = page_idx + 1
    info["total_pages"] = total_pages

    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=85)
    return Response(content=buf.getvalue(), media_type="image/jpeg",
                    headers={"X-Studio-Info": json.dumps(info), "Cache-Control": "no-store"})


def _ensure_orig_backup(src_path: str) -> str:
    """Ensures an unmodified backup copy of src_path exists so scanner filters can be previewed/re-applied cleanly."""
    import shutil
    orig_path = os.path.join(os.path.dirname(src_path), "orig_" + os.path.basename(src_path))
    if not os.path.isfile(orig_path):
        shutil.copy2(src_path, orig_path)
    return orig_path


def _update_orig_backup(src_path: str):
    """Updates the backup copy after a structural change (crop, rotate, cutout replacement)."""
    import shutil
    orig_path = os.path.join(os.path.dirname(src_path), "orig_" + os.path.basename(src_path))
    if os.path.isfile(src_path):
        shutil.copy2(src_path, orig_path)


@app.post("/api/image/crop")
async def crop_image_endpoint(payload: dict = Body(...)):
    """Crops an uploaded image by normalized box [x, y, width, height] (0..1)."""
    file_id = os.path.basename(str(payload.get("file_id", "")))
    if file_id.startswith("nid_") and not file_id.endswith((".jpg", ".jpeg", ".png", ".webp")):
        side = str(payload.get("side", "front"))
        token = file_id[4:]
        front, back = nid_paths(token)
        src = back if side == "back" else front
    else:
        src = resolve_upload(file_id)

    if utils.get_ext(src) not in ("jpg", "jpeg", "png", "webp"):
        raise HTTPException(status_code=400, detail="Only images can be cropped")

    box = payload.get("box", {})
    x = max(0.0, min(1.0, float(box.get("x", 0))))
    y = max(0.0, min(1.0, float(box.get("y", 0))))
    w = max(0.05, min(1.0 - x, float(box.get("w", 1))))
    h = max(0.05, min(1.0 - y, float(box.get("h", 1))))

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        iw, ih = im.size
        crop_box = (int(x * iw), int(y * ih), int((x + w) * iw), int((y + h) * ih))
        cropped = im.crop(crop_box)
        cropped.save(src, "JPEG", quality=95)
    _update_orig_backup(src)

    return {"success": True, "file_id": file_id}


@app.get("/api/image/camscan-preview")
def camscan_preview_endpoint(file_id: str, mode: str = "magic_color", strength: str = "normal", page: int = 1):
    """Renders a live preview of the CamScanner effect without overwriting the saved file yet (supports images, NID, and PDF pages)."""
    file_id = os.path.basename(str(file_id or ""))
    mode = clean_choice(mode, ("original", "magic_color", "bw_doc", "hd_clear", "grayscale"), "magic_color")
    strength = clean_choice(strength, ("light", "normal", "strong"), "normal")
    import image_fx
    import office_docs

    if file_id.startswith("nid_") and not file_id.endswith((".jpg", ".jpeg", ".png", ".webp")):
        token = file_id[4:]
        front, back = nid_paths(token)
        orig_f = _ensure_orig_backup(front)
        orig_b = _ensure_orig_backup(back)
        with Image.open(orig_f) as im_f, Image.open(orig_b) as im_b:
            im_f = ImageOps.exif_transpose(im_f).convert("RGB")
            im_b = ImageOps.exif_transpose(im_b).convert("RGB")
            im_f.thumbnail((840, 840), Image.Resampling.LANCZOS)
            im_b.thumbnail((840, 840), Image.Resampling.LANCZOS)
            if mode != "original":
                im_f = image_fx.camscanner_process(im_f, mode=mode, strength=strength)
                im_b = image_fx.camscanner_process(im_b, mode=mode, strength=strength)
            pad = 16
            cw = max(im_f.width, im_b.width) + pad * 2
            ch = im_f.height + im_b.height + pad * 3
            canvas = Image.new("RGB", (cw, ch), (241, 245, 249))
            canvas.paste(im_f, ((cw - im_f.width) // 2, pad))
            canvas.paste(im_b, ((cw - im_b.width) // 2, im_f.height + pad * 2))
            buf = io.BytesIO()
            canvas.save(buf, "JPEG", quality=86)
            return Response(content=buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=300"})

    src = resolve_upload(file_id)
    ext = utils.get_ext(src)
    if ext not in ("jpg", "jpeg", "png", "webp", "pdf"):
        raise HTTPException(status_code=400, detail="Only images and PDFs can be scanned/enhanced")
    orig = _ensure_orig_backup(src)
    if ext == "pdf":
        im = office_docs.render_pdf_page_pil(orig, page_num=page, dpi=140)
        im.thumbnail((840, 840), Image.Resampling.BILINEAR)
        if mode != "original":
            im = image_fx.camscanner_process(im, mode=mode, strength=strength)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=84)
        return Response(content=buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=300"})

    with Image.open(orig) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((840, 840), Image.Resampling.BILINEAR)
        if mode != "original":
            im = image_fx.camscanner_process(im, mode=mode, strength=strength)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=84)
        return Response(content=buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=300"})


@app.post("/api/image/camscan")
async def camscan_image_endpoint(payload: dict = Body(...)):
    """Applies CamScanner document flattening & clarity enhancement to an uploaded photo, NID card, or PDF (single page or all pages)."""
    import shutil
    import image_fx
    import office_docs
    file_id = os.path.basename(str(payload.get("file_id", "")))
    mode = clean_choice(str(payload.get("mode", "magic_color")).strip(), ("original", "magic_color", "bw_doc", "hd_clear", "grayscale"), "magic_color")
    strength = clean_choice(str(payload.get("strength", "normal")).strip(), ("light", "normal", "strong"), "normal")
    page_num = max(1, int(payload.get("page", 1) or 1))
    apply_all = bool(payload.get("apply_all", False))

    if file_id.startswith("nid_") and not file_id.endswith((".jpg", ".jpeg", ".png", ".webp")):
        token = file_id[4:]
        front, back = nid_paths(token)
        for p in (front, back):
            orig = _ensure_orig_backup(p)
            if mode == "original":
                shutil.copy2(orig, p)
            else:
                with Image.open(orig) as im:
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    enhanced = image_fx.camscanner_process(im, mode=mode, strength=strength)
                    enhanced.save(p, "JPEG", quality=95)
        return {"success": True, "file_id": file_id, "mode": mode, "strength": strength}

    src = resolve_upload(file_id)
    ext = utils.get_ext(src)
    if ext not in ("jpg", "jpeg", "png", "webp", "pdf"):
        raise HTTPException(status_code=400, detail="Only images and PDFs can be scanned/enhanced")

    orig = _ensure_orig_backup(src)
    if ext == "pdf":
        if apply_all:
            if mode == "original":
                shutil.copy2(orig, src)
                office_docs._clear_nup_caches_for(src)
            else:
                await asyncio.to_thread(office_docs.apply_camscan_to_all_pdf_pages, orig, src, mode, strength)
        else:
            im = await asyncio.to_thread(office_docs.render_pdf_page_pil, orig, page_num, 200)
            if mode != "original":
                im = await asyncio.to_thread(image_fx.camscanner_process, im, mode, 1.3, 1.08, strength)
            await asyncio.to_thread(office_docs.replace_pdf_page_pil, src, page_num, im)
        return {"success": True, "file_id": file_id, "mode": mode, "strength": strength}

    if mode == "original":
        shutil.copy2(orig, src)
    else:
        with Image.open(orig) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            enhanced = image_fx.camscanner_process(im, mode=mode, strength=strength)
            enhanced.save(src, "JPEG", quality=95)

    return {"success": True, "file_id": file_id, "mode": mode, "strength": strength}


@app.post("/api/file/rotate")
async def rotate_file_endpoint(payload: dict = Body(...)):
    """Rotates an image, NID pair, or PDF file by 90 degrees clockwise."""
    file_id = os.path.basename(str(payload.get("file_id", "")))
    if file_id.startswith("nid_") and not file_id.endswith((".jpg", ".jpeg", ".png", ".webp")):
        token = file_id[4:]
        front, back = nid_paths(token)
        for p in (front, back):
            with Image.open(p) as im:
                im = ImageOps.exif_transpose(im)
                rotated = im.rotate(-90, expand=True)
                rotated.save(p, "JPEG", quality=95)
            _update_orig_backup(p)
        return {"success": True, "file_id": file_id}

    src = resolve_upload(file_id)
    ext = utils.get_ext(src)

    if ext in ("jpg", "jpeg", "png", "webp"):
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            rotated = im.rotate(-90, expand=True)
            rotated.save(src, "JPEG" if ext in ("jpg", "jpeg") else "PNG", quality=95)
        _update_orig_backup(src)
    elif ext == "pdf":
        import pypdf
        import office_docs
        reader = pypdf.PdfReader(src)
        writer = pypdf.PdfWriter()
        for p in reader.pages:
            p.rotate(90)
            writer.add_page(p)
        temp_out = src + ".rot.tmp"
        with open(temp_out, "wb") as f_out:
            writer.write(f_out)
        os.replace(temp_out, src)
        _update_orig_backup(src)
        office_docs._clear_nup_caches_for(src)
    else:
        raise HTTPException(status_code=400, detail="Cannot rotate this file format")

    return {"success": True, "file_id": file_id}


@app.post("/api/image/save-crop")
async def save_cropped_image(file_id: str = Form(...), file: UploadFile = File(...), side: str = Form("front"), page: int = Form(1)):
    """Receives pixel-perfect cropped canvas blob from client Cropper.js (supports images, NID, and PDF pages)."""
    import office_docs
    file_id = os.path.basename(file_id)
    if file_id.startswith("nid_") and not file_id.endswith((".jpg", ".jpeg", ".png", ".webp")):
        token = file_id[4:]
        front, back = nid_paths(token)
        dest = back if side == "back" else front
    else:
        dest = resolve_upload(file_id)

    ext = utils.get_ext(dest)
    if ext not in ("jpg", "jpeg", "png", "webp", "pdf"):
        raise HTTPException(status_code=400, detail="Only images and PDFs can be cropped")

    content = await file.read()
    with Image.open(io.BytesIO(content)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if ext == "pdf":
            await asyncio.to_thread(office_docs.replace_pdf_page_pil, dest, int(page or 1), im)
        else:
            im.save(dest, "JPEG", quality=95)
    _update_orig_backup(dest)

    return {"success": True, "file_id": file_id}


@app.post("/api/image/replace-cutout")
async def replace_cutout_endpoint(
    file_id: str = Form(...),
    file: UploadFile = File(...),
    bg_color: str = Form("#ffffff"),
    side: str = Form("front")
):
    """Directly replaces current photo with a cutout PNG downloaded from remove.bg website."""
    file_id = os.path.basename(file_id)
    if file_id.startswith("nid_") and not file_id.endswith((".jpg", ".jpeg", ".png", ".webp")):
        token = file_id[4:]
        front, back = nid_paths(token)
        dest = back if side == "back" else front
    else:
        dest = resolve_upload(file_id)

    if utils.get_ext(dest) not in ("jpg", "jpeg", "png", "webp"):
        raise HTTPException(status_code=400, detail="Only images are supported")

    content = await file.read()
    with Image.open(io.BytesIO(content)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            fill_col = bg_color if (bg_color and bg_color.startswith("#")) else "#ffffff"
            bg = Image.new("RGBA", im.size, fill_col)
            bg.paste(im, (0, 0), im)
            im = bg.convert("RGB")
        else:
            im = im.convert("RGB")
        im.save(dest, "JPEG", quality=95)
    _update_orig_backup(dest)

    return {"success": True, "file_id": file_id}




@app.get("/api/image/detect-corners")
def detect_corners_endpoint(file_id: str, side: str = "front", page: int = 1):
    """Automatically detects the 4 corners of a tilted document, PDF page, or ID card."""
    import image_fx
    import office_docs
    safe_id = os.path.basename(str(file_id or ""))
    if safe_id.startswith("nid_") and not safe_id.endswith((".jpg", ".jpeg", ".png", ".webp")):
        token = safe_id[4:]
        front, back = nid_paths(token)
        src = back if side == "back" else front
    else:
        src = resolve_upload(safe_id)

    if utils.get_ext(src) == "pdf":
        im = office_docs.render_pdf_page_pil(src, page_num=page, dpi=140)
        pts = image_fx.detect_document_corners(im)
        return {"success": True, "points": pts}

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        pts = image_fx.detect_document_corners(im)
    return {"success": True, "points": pts}


@app.post("/api/image/perspective-crop")
async def perspective_crop_endpoint(payload: dict = Body(...)):
    """
    CamScanner-style 4-corner Smart Perspective Crop:
    Takes 4 uneven/tilted corner coordinates and warps them into a straight rectangle (supports images, NID, and PDF pages).
    """
    import image_fx
    import office_docs
    file_id = os.path.basename(str(payload.get("file_id", "")))
    side = str(payload.get("side", "front"))
    page_num = max(1, int(payload.get("page", 1) or 1))
    if file_id.startswith("nid_") and not file_id.endswith((".jpg", ".jpeg", ".png", ".webp")):
        token = file_id[4:]
        front, back = nid_paths(token)
        src = back if side == "back" else front
    else:
        src = resolve_upload(file_id)

    ext = utils.get_ext(src)
    if ext not in ("jpg", "jpeg", "png", "webp", "pdf"):
        raise HTTPException(status_code=400, detail="Only images and PDFs can be perspective-cropped")

    raw_pts = payload.get("points", [])
    if not isinstance(raw_pts, list) or len(raw_pts) < 4:
        raise HTTPException(status_code=400, detail="4 corner points are required")

    norm_pts = [(float(p.get("x", 0.0)), float(p.get("y", 0.0))) for p in raw_pts[:4]]
    ratio_key = str(payload.get("ratio", "auto")).strip().lower()
    ratio_map = {
        "auto": None,
        "free": None,
        "nid": 85.6 / 54.0,
        "passport": 35.0 / 45.0,
        "stamp": 20.0 / 25.0,
        "square": 1.0,
        "a4": 210.0 / 297.0
    }
    target_ratio = ratio_map.get(ratio_key, None)
    enhance_mode = clean_choice(str(payload.get("enhance_mode", "none")).strip(), ("none", "magic_color", "bw_doc", "hd_clear", "grayscale"), "none")

    if ext == "pdf":
        im = await asyncio.to_thread(office_docs.render_pdf_page_pil, src, page_num, 220)
        warped = await asyncio.to_thread(image_fx.perspective_crop_4points, im, norm_pts, target_ratio)
        if enhance_mode != "none":
            warped = await asyncio.to_thread(image_fx.camscanner_process, warped, enhance_mode)
        await asyncio.to_thread(office_docs.replace_pdf_page_pil, src, page_num, warped)
        _update_orig_backup(src)
        return {"success": True, "file_id": file_id}

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        warped = image_fx.perspective_crop_4points(im, norm_pts, target_ratio=target_ratio)
        if enhance_mode != "none":
            warped = image_fx.camscanner_process(warped, mode=enhance_mode)
        warped.save(src, "JPEG", quality=95)
    _update_orig_backup(src)

    return {"success": True, "file_id": file_id}




@app.get("/api/file/check/{file_id}")
def check_file_exists_endpoint(file_id: str):
    """Checks if a previously uploaded file or NID pair still exists on disk (for session restore on refresh)."""
    safe_id = os.path.basename(str(file_id or ""))
    if not safe_id:
        return {"exists": False}
    if safe_id.startswith("nid_") and not safe_id.endswith((".jpg", ".jpeg", ".png", ".webp")):
        token = safe_id[4:]
        if not NID_TOKEN_RE.match(token or ""):
            return {"exists": False}
        front = os.path.join(UPLOAD_DIR, f"nid_{token}_f.jpg")
        back = os.path.join(UPLOAD_DIR, f"nid_{token}_b.jpg")
        return {"exists": bool(os.path.isfile(front) and os.path.isfile(back))}
    path = os.path.join(UPLOAD_DIR, safe_id)
    return {"exists": os.path.isfile(path)}


@app.post("/api/image/perspective-crop-preview")
def perspective_crop_preview_endpoint(payload: dict = Body(...)):
    """
    Renders a live in-memory preview of the 4-corner perspective warp
    without overwriting the file on disk yet (until the user clicks OK).
    """
    import image_fx
    import office_docs
    file_id = os.path.basename(str(payload.get("file_id", "")))
    side = str(payload.get("side", "front"))
    page_num = max(1, int(payload.get("page", 1) or 1))
    if file_id.startswith("nid_") and not file_id.endswith((".jpg", ".jpeg", ".png", ".webp")):
        token = file_id[4:]
        front, back = nid_paths(token)
        src = back if side == "back" else front
    else:
        src = resolve_upload(file_id)

    ext = utils.get_ext(src)
    if ext not in ("jpg", "jpeg", "png", "webp", "pdf"):
        raise HTTPException(status_code=400, detail="Only images and PDFs can be perspective-cropped")

    raw_pts = payload.get("points", [])
    if not isinstance(raw_pts, list) or len(raw_pts) < 4:
        raise HTTPException(status_code=400, detail="4 corner points are required")

    norm_pts = [(float(p.get("x", 0.0)), float(p.get("y", 0.0))) for p in raw_pts[:4]]
    ratio_key = str(payload.get("ratio", "auto")).strip().lower()
    ratio_map = {
        "auto": None,
        "free": None,
        "nid": 85.6 / 54.0,
        "passport": 35.0 / 45.0,
        "stamp": 20.0 / 25.0,
        "square": 1.0,
        "a4": 210.0 / 297.0
    }
    target_ratio = ratio_map.get(ratio_key, None)
    enhance_mode = clean_choice(str(payload.get("enhance_mode", "none")).strip(), ("none", "magic_color", "bw_doc", "hd_clear", "grayscale"), "none")

    if ext == "pdf":
        im = office_docs.render_pdf_page_pil(src, page_num=page_num, dpi=170)
        warped = image_fx.perspective_crop_4points(im, norm_pts, target_ratio=target_ratio, max_out_dim=1000)
        if enhance_mode != "none":
            warped = image_fx.camscanner_process(warped, mode=enhance_mode)
        buf = io.BytesIO()
        warped.save(buf, "JPEG", quality=84)
        return Response(content=buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        warped = image_fx.perspective_crop_4points(im, norm_pts, target_ratio=target_ratio, max_out_dim=1000)
        if enhance_mode != "none":
            warped = image_fx.camscanner_process(warped, mode=enhance_mode)
        buf = io.BytesIO()
        warped.save(buf, "JPEG", quality=84)
        return Response(content=buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/order/create")
async def customer_create_order(request: Request):
    data = await request.json()
    shop_id = int(data.get("shop_id", 0) or 0)
    shop = db.get_shop_by_id(shop_id) if shop_id else None
    if not shop:
        shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")

    file_id = os.path.basename(str(data.get("file_id", "")))
    if not file_id:
        raise HTTPException(status_code=400, detail="Uploaded file not found")

    try:
        copies = max(1, min(int(data.get("copies", 1)), 100))
    except (TypeError, ValueError):
        copies = 1

    paper_size = clean_choice(data.get("paper_size"), ("A4", "4x6", "NID", "PHOTO"), "A4")
    # Customer requested color mode (from client selection)
    if paper_size == "PHOTO" and data.get("studio", {}).get("bw"):
        customer_req_color = "bw"
    else:
        customer_req_color = clean_choice(data.get("color_mode"), ("bw", "color"), "color")

    color_mode = "color"
    duplex = clean_choice(data.get("duplex"), ("single", "double"), "single")
    orientation = clean_choice(data.get("orientation"), ("portrait", "landscape"), "portrait")
    payment_method = clean_choice(data.get("payment_method"), ("counter_cash", "bkash", "nagad"), "counter_cash")
    try:
        pages_per_sheet = int(data.get("pages_per_sheet", 1))
    except (TypeError, ValueError):
        pages_per_sheet = 1
    if pages_per_sheet not in (1, 2, 4):
        pages_per_sheet = 1

    order_id = f"QSP-{secrets.token_hex(3).upper()}"
    original_filename = utils.safe_filename(data.get("original_filename"), "document")
    extra_data_dict = {}

    if paper_size == "NID":
        # Build the final A4 PDF: front + back at the exact 85.6 x 54 mm size
        token = file_id[4:] if file_id.startswith("nid_") else ""
        front, back = nid_paths(token)
        file_path = os.path.join(UPLOAD_DIR, f"nid_print_{token}.pdf")
        fit_mode = clean_choice(data.get("nid_fit"), ("cover", "contain"), "cover")
        rot_front = clean_rotation(data.get("nid_rot_front"))
        rot_back = clean_rotation(data.get("nid_rot_back"))
        await asyncio.to_thread(
            utils.save_nid_sheet, front, back, file_path,
            color_mode="color",
            fit_mode=fit_mode,
            rot_front=rot_front,
            rot_back=rot_back,
            orientation=orientation,
        )
        file_type, page_count = "pdf", 1
        original_filename = "NID_Card_Front_Back.pdf"
        rate = shop["rate_bw_single"] if customer_req_color == "bw" else shop["rate_color_single"]
        total_price = rate * copies
        duplex = "single"
        extra_data_dict = {
            "is_nid": True,
            "nid_token": token,
            "fit": fit_mode,
            "rot_front": rot_front,
            "rot_back": rot_back,
            "customer_color_mode": customer_req_color
        }
        if storage.is_r2_configured():
            if os.path.isfile(front):
                await asyncio.to_thread(storage.upload_file_to_r2, front)
            if os.path.isfile(back):
                await asyncio.to_thread(storage.upload_file_to_r2, back)
    elif paper_size == "PHOTO":
        # Photo Studio: passport / stamp / custom photos in rows on A4 or 4x6 paper
        src = resolve_upload(file_id)
        if utils.get_ext(src) not in ("jpg", "jpeg", "png"):
            raise HTTPException(status_code=400, detail="ফটো স্টুডিওতে শুধু ছবি ব্যবহার করা যাবে।")
        sp = studio.sanitize_params(data.get("studio"))
        sp["bw"] = False
        fid_clean = os.path.splitext(file_id)[0]
        file_path = os.path.join(UPLOAD_DIR, f"studio_{fid_clean}.pdf")

        # Keep a preserved copy of the source photo for admin editing
        src_backup = os.path.join(UPLOAD_DIR, f"studio_src_{fid_clean}.jpg")
        try:
            shutil.copy2(src, src_backup)
        except Exception:
            src_backup = src
        if storage.is_r2_configured() and os.path.isfile(src_backup):
            await asyncio.to_thread(storage.upload_file_to_r2, src_backup)

        photos_map = {file_id: src}
        extra_photos = sp.get("photos") or []
        for fid in extra_photos:
            try:
                p_src = resolve_upload(str(fid))
                if utils.get_ext(p_src) in ("jpg", "jpeg", "png"):
                    photos_map[str(fid)] = p_src
            except Exception:
                pass

        def _build():
            sheets, _info = studio.render_sheets(src, sp, dpi=300, photos_map=photos_map)
            studio.save_sheet_pdf(sheets, file_path, 300)
            return max(1, len(sheets))

        page_count = await asyncio.to_thread(_build)
        file_type = "pdf"
        color_mode = "color"
        duplex = "single"
        original_filename = f"Photo_Sheet_{sp['paper']}.pdf"
        rate_passport = float(shop.get("rate_passport") or 5.0)
        rate_stamp = float(shop.get("rate_stamp") or 2.0)
        photo_items_cost = 0.0
        for it in sp.get("items", []):
            cnt = int(it.get("count", 0) or 0)
            w = float(it.get("w", 0) or 0)
            h = float(it.get("h", 0) or 0)
            preset = str(it.get("preset", ""))
            if preset == "stamp" or (w <= 25 and h <= 30):
                photo_items_cost += cnt * rate_stamp
            else:
                photo_items_cost += cnt * rate_passport

        if sp["paper"] == "4x6":
            sheet_rate = float(shop.get("rate_photo_4x6") or 30.0)
        else:
            sheet_rate = float(shop.get("rate_bw_single") or 3.0) if sp["bw"] else float(shop.get("rate_photo_a4") or 40.0)

        rate = photo_items_cost if photo_items_cost > 0 else sheet_rate
        total_price = rate * copies
        extra_data_dict = {
            "is_studio": True,
            "source_photo": os.path.basename(src_backup),
            "studio": sp,
            "customer_color_mode": customer_req_color
        }
    else:
        file_path = resolve_upload(file_id)
        file_type = "pdf" if utils.get_ext(file_path) == "pdf" else "image"
        page_count = utils.get_file_page_count(file_path, file_type)   # never trust the browser's number

        if paper_size == "4x6":
            if file_type != "image":
                raise HTTPException(status_code=400, detail="৪×৬ পাসপোর্ট শিট শুধু ছবির জন্য।")
            pristine_upload = os.path.join(UPLOAD_DIR, f"pristine_{order_id}_{file_id}")
            try:
                shutil.copy2(file_path, pristine_upload)
                extra_data_dict["pristine_path"] = pristine_upload
                if storage.is_r2_configured():
                    await asyncio.to_thread(storage.upload_file_to_r2, pristine_upload)
            except Exception:
                pass
            sheet_path = os.path.join(UPLOAD_DIR, f"sheet_{os.path.splitext(file_id)[0]}.jpg")
            await asyncio.to_thread(utils.create_4x6_passport_sheet, file_path, sheet_path, 6, "color")
            if sheet_path != file_path and file_path != pristine_upload:
                utils.secure_delete_file(file_path)
            file_path = sheet_path
            original_filename = os.path.splitext(original_filename)[0] + "_4x6.jpg"
            page_count = 1
            total_price = shop["rate_photo_4x6"] * copies
            extra_data_dict["customer_color_mode"] = customer_req_color
        else:
            if file_type == "image":
                cfg = get_image_pos_config(file_id)
                img_pos_payload = data.get("image_pos") or {}
                try:
                    pos_x = float(img_pos_payload.get("pos_x", cfg.get("pos_x", 0.5)))
                    pos_y = float(img_pos_payload.get("pos_y", cfg.get("pos_y", 0.5)))
                    scale = float(img_pos_payload.get("scale", cfg.get("scale", 1.0)))
                except (TypeError, ValueError):
                    pos_x, pos_y, scale = 0.5, 0.5, 1.0
                fit_mode = str(img_pos_payload.get("fit_mode", cfg.get("fit_mode", "contain")))

                pristine_upload = os.path.join(UPLOAD_DIR, f"pristine_{order_id}_{file_id}")
                try:
                    shutil.copy2(file_path, pristine_upload)
                    extra_data_dict["pristine_path"] = pristine_upload
                    if storage.is_r2_configured():
                        await asyncio.to_thread(storage.upload_file_to_r2, pristine_upload)
                except Exception:
                    pass

                a4_path = os.path.join(UPLOAD_DIR, f"a4_{orientation}_{os.path.splitext(file_id)[0]}.jpg")
                await asyncio.to_thread(
                    utils.save_a4_image_sheet,
                    file_path,
                    a4_path,
                    "color",
                    orientation,
                    pos_x=pos_x,
                    pos_y=pos_y,
                    scale=scale,
                    fit_mode=fit_mode
                )
                if a4_path != file_path and file_path != pristine_upload:
                    utils.secure_delete_file(file_path)
                file_path = a4_path
            elif pages_per_sheet in (2, 4) and file_type == "pdf":
                nup_path = os.path.join(UPLOAD_DIR, f"nup{pages_per_sheet}_{file_id}")
                nup_path, new_sheet_count = await asyncio.to_thread(utils.create_nup_pdf, file_path, nup_path, pages_per_sheet)
                if nup_path != file_path:
                    utils.secure_delete_file(file_path)
                file_path = nup_path
                page_count = new_sheet_count

            if customer_req_color == "bw":
                rate = shop["rate_bw_double"] if duplex == "double" else shop["rate_bw_single"]
            else:
                rate = shop["rate_color_double"] if duplex == "double" else shop["rate_color_single"]
            total_price = rate * page_count * copies
            if "customer_color_mode" not in extra_data_dict:
                extra_data_dict["customer_color_mode"] = customer_req_color

    # Sync printable order file to Cloudflare R2 (so downloads/prints use 0 Render egress bandwidth)
    if storage.is_r2_configured() and os.path.isfile(file_path):
        await asyncio.to_thread(storage.upload_file_to_r2, file_path)

    order_data = {
        "id": order_id,
        "shop_id": shop["id"],
        "customer_name": utils.safe_filename(data.get("customer_name"), "Walk-in")[:60],
        "customer_phone": str(data.get("customer_phone", ""))[:20],
        "file_path": file_path,
        "original_filename": original_filename,
        "file_type": file_type,
        "page_count": page_count,
        "copies": copies,
        "color_mode": color_mode,
        "customer_color_mode": customer_req_color,
        "paper_size": paper_size,
        "duplex": duplex,
        "page_range": "all",
        "orientation": orientation,
        "printer_target": str(data.get("printer_target", "") or "").strip()[:100],
        "total_price": round(total_price, 2),
        "payment_method": payment_method,
        "trx_id": re.sub(r"[^A-Za-z0-9]", "", str(data.get("trx_id", "")))[:30],
        "status": "pending_payment",
        "extra_data": json.dumps(extra_data_dict) if extra_data_dict else ""
    }

    db.create_order(order_data)
    try:
        ensure_order_pristine_backup(order_data)
    except Exception:
        pass

    # Broadcast new order to Admin dashboard (never expose the server file path)
    public_order = {k: v for k, v in order_data.items() if k != "file_path"}
    await manager.notify_admin(shop["id"], {"event": "NEW_ORDER", "order": public_order})

    return {"success": True, "order_id": order_id, "total_price": round(total_price, 2), "order": public_order}


@app.post("/api/order/{order_id}/pay")
async def customer_submit_payment(order_id: str, request: Request):
    data = await request.json()
    order = db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
        
    payment_method = data.get("payment_method", "counter_cash")
    trx_id = data.get("trx_id", "")
    
    conn = db.get_db()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE orders SET payment_method = ?, trx_id = ? WHERE id = ?
    """, (payment_method, trx_id, order_id))
    conn.commit()
    conn.close()
    
    msg = "কাউন্টারে ক্যাশ পেমেন্ট রিকোয়েস্ট পাঠানো হয়েছে।" if payment_method == "counter_cash" else f"পেমেন্ট তথ্য ({payment_method.upper()} - {trx_id}) জমা দেওয়া হয়েছে।"
    
    # Notify Admin Dashboard
    await manager.notify_admin(order["shop_id"], {
        "event": "PAYMENT_SUBMITTED",
        "order_id": order_id,
        "payment_method": payment_method,
        "trx_id": trx_id
    })
    
    return {"success": True, "message": msg}


@app.get("/api/orders/status")
async def customer_get_orders_status(ids: str = ""):
    """Returns live status and metadata for a comma-separated list of recent customer order IDs."""
    raw_ids = [oid.strip() for oid in (ids or "").split(",") if oid.strip()][:20]
    if not raw_ids:
        return {"orders": []}
    result = []
    for oid in raw_ids:
        o = db.get_order(oid)
        if o:
            result.append({
                "id": o["id"],
                "shop_id": o["shop_id"],
                "original_filename": o.get("original_filename") or "document",
                "page_count": o.get("page_count") or 1,
                "copies": o.get("copies") or 1,
                "color_mode": o.get("color_mode") or "color",
                "customer_color_mode": o.get("customer_color_mode") or o.get("color_mode") or "color",
                "paper_size": o.get("paper_size") or "A4",
                "total_price": o.get("total_price") or 0,
                "payment_method": o.get("payment_method") or "counter_cash",
                "status": o.get("status") or "pending_payment",
                "created_at": o.get("created_at") or "",
            })
    return {"orders": result}


@app.post("/api/order/{order_id}/cancel")
async def customer_cancel_order(order_id: str, request: Request):
    """Allows customer to cancel/delete their print job if it hasn't printed yet, or delete previous jobs from history."""
    order = db.get_order(order_id)
    if not order:
        return {"success": True, "message": "অর্ডারটি ইতিপূর্বে মুছে ফেলা হয়েছে।"}

    # If the print is in progress ('printing'), customer cannot cancel without admin
    if order["status"] == "printing":
        raise HTTPException(
            status_code=400,
            detail="প্রিন্টারে ফাইলটি প্রিন্ট হচ্ছে! এখন বাতিল করা সম্ভব নয়। অনুগ্রহ করে কাউন্টারে কথা বলুন।"
        )

    file_path = order.get("file_path")
    if file_path:
        utils.secure_delete_file(file_path)

    db.mark_order_file_purged(order_id)

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    
    delete_record = body.get("delete_record", False)
    if delete_record or order["status"] in ("completed", "cancelled"):
        db.delete_order_record(order_id, order["shop_id"])
    else:
        db.update_order_status(order_id, "cancelled")

    await manager.notify_order(order_id, {
        "event": "STATUS_CHANGE",
        "status": "cancelled",
        "message": "কাস্টমার কর্তৃক অর্ডারটি বাতিল ও ডিলিট করা হয়েছে।"
    })
    await manager.notify_admin(order["shop_id"], {
        "event": "ORDER_UPDATED",
        "order_id": order_id
    })

    return {"success": True, "message": "অর্ডারটি সফলভাবে বাতিল ও ডিলিট করা হয়েছে।"}


@app.post("/api/orders/purge-customer-history")
async def customer_purge_history(request: Request):
    """Allows customer to delete all their listed previous order records and files."""
    body = await request.json()
    ids = body.get("ids") or []
    deleted_count = 0
    for oid in ids:
        order = db.get_order(oid)
        if not order:
            continue
        if order["status"] == "printing":
            continue
        file_path = order.get("file_path")
        if file_path:
            utils.secure_delete_file(file_path)
        db.delete_order_record(oid, order["shop_id"])
        deleted_count += 1
        await manager.notify_admin(order["shop_id"], {"event": "ORDER_UPDATED", "order_id": oid})
    return {"success": True, "deleted_count": deleted_count}

# ── AGENT (WINDOWS DESKTOP) ENDPOINTS ──

@app.get("/download/{order_id}")
async def download_print_file(order_id: str, key: str):
    """Allows authenticated shop agent to download the file for printing (directly from Cloudflare R2 when configured)."""
    shop = db.get_shop_by_api_key(key)
    if not shop:
        raise HTTPException(status_code=401, detail="Invalid API Key")
        
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="File not found")
        
    dl_name = order["original_filename"]
    stored_ext = os.path.splitext(order["file_path"])[1].lower()
    if stored_ext and os.path.splitext(dl_name)[1].lower() != stored_ext:
        dl_name = os.path.splitext(dl_name)[0] + stored_ext
    mtype = "application/pdf" if stored_ext == ".pdf" else ("image/jpeg" if stored_ext in (".jpg", ".jpeg") else "image/png")

    if storage.is_r2_configured():
        r2_url = await asyncio.to_thread(
            storage.get_r2_url_for_local_path,
            order["file_path"],
            dl_name,
            False,
            mtype,
        )
        if r2_url:
            return RedirectResponse(url=r2_url, status_code=status.HTTP_302_FOUND)

    if not os.path.exists(order["file_path"]):
        await asyncio.to_thread(storage.ensure_local_from_r2, order["file_path"])
    if not os.path.exists(order["file_path"]):
        raise HTTPException(status_code=404, detail="File has already been securely removed")

    # If unedited original Office document exists, serve it directly so Windows agent prints via native Office COM (100% fidelity)
    base_fid = os.path.splitext(os.path.basename(order["file_path"]))[0]
    edited_flag = os.path.join(UPLOAD_DIR, f"edited_{base_fid}.flag")
    if not os.path.isfile(edited_flag):
        for raw_ext in ("docx", "doc", "rtf", "xlsx", "xls", "pptx", "ppt"):
            raw_cand = os.path.join(UPLOAD_DIR, f"raw_{base_fid}.{raw_ext}")
            if os.path.isfile(raw_cand):
                raw_dl_name = os.path.splitext(dl_name)[0] + f".{raw_ext}"
                raw_mtype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if raw_ext == "docx" else "application/octet-stream"
                return FileResponse(raw_cand, filename=raw_dl_name, media_type=raw_mtype)
        
    return FileResponse(order["file_path"], filename=dl_name, media_type=mtype)

@app.post("/api/agent/order/{order_id}/printed")
async def agent_mark_printed(order_id: str, request: Request):
    data = await request.json()
    api_key = data.get("api_key")
    shop = db.get_shop_by_api_key(api_key)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    order = db.get_order(order_id)
    if not order or order["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Order not found")
        
    now = _now_iso()
    db.update_order_status(order_id, "completed", printed_at=now)
    
    # Notify customer live that print is done (file auto-deletes after 30 mins)
    await manager.notify_order(order_id, {
        "event": "STATUS_CHANGE",
        "status": "completed",
        "message": "প্রিন্ট সম্পন্ন হয়েছে! ৩০ মিনিট পর আপনার ফাইলটি সার্ভার থেকে স্বয়ংক্রিয়ভাবে মুছে যাবে।"
    })
    
    # Notify admin
    await manager.notify_admin(shop["id"], {
        "event": "ORDER_UPDATED",
        "order_id": order_id,
        "status": "completed"
    })
    
    return {"success": True}

# ── FILE TRANSFER (customer <-> shop) ──

def _transfer_dict(t, request: Request) -> dict:
    d = {
        "id": t["id"],
        "direction": t["direction"],
        "filename": t["original_filename"],
        "size": t["file_size"],
        "size_text": utils.human_size(t["file_size"]),
        "sender_name": t["sender_name"] or "",
        "note": t["note"] or "",
        "one_time": bool(t["one_time"]),
        "download_count": t["download_count"],
        "created_at": t["created_at"],
        "expires_at": t["expires_at"],
    }
    if t["direction"] == "to_customer":
        d["url"] = f"{base_url(request)}/t/{t['id']}"
        d["qr_url"] = f"/t/{t['id']}/qr.png"
    return d


def _new_transfer_id() -> str:
    while True:
        tid = secrets.token_hex(4).upper()
        if not db.get_transfer(tid):
            return tid


def _transfer_expired(t) -> bool:
    return t["expires_at"] < _now_iso()


async def _store_transfer(shop_id: int, direction: str, file: UploadFile, sender_name: str, note: str, one_time: bool):
    filename = utils.safe_filename(file.filename, "file")
    ext = utils.get_ext(filename)
    if ext in utils.BLOCKED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="নিরাপত্তার কারণে এই ধরনের ফাইল (exe, bat, apk ইত্যাদি) পাঠানো যাবে না।")
    if db.count_transfers(shop_id, direction) >= TRANSFER_MAX_PER_DIRECTION:
        raise HTTPException(status_code=429, detail="ফাইল বক্স এখন পূর্ণ। কিছুক্ষণ পর আবার চেষ্টা করুন।")

    tid = _new_transfer_id()
    stored_path = os.path.join(TRANSFER_DIR, f"{tid}.dat")
    size = await save_upload_limited(file, stored_path, MAX_UPLOAD_BYTES)
    if size == 0:
        utils.secure_delete_file(stored_path)
        raise HTTPException(status_code=400, detail="ফাঁকা ফাইল পাঠানো যাবে না।")

    if storage.is_r2_configured():
        mtype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        await asyncio.to_thread(storage.upload_file_to_r2, stored_path, None, mtype)

    now = datetime.now(timezone.utc)
    record = {
        "id": tid, "shop_id": shop_id, "direction": direction,
        "original_filename": filename, "stored_path": stored_path, "file_size": size,
        "sender_name": utils.safe_filename(sender_name, "")[:60],
        "note": re.sub(r"[<>]", "", note or "")[:300],
        "one_time": one_time,
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(hours=TRANSFER_TTL_HOURS)).isoformat(timespec="seconds"),
    }
    db.create_transfer(record)
    return db.get_transfer(tid)


@app.post("/api/transfer/send/{shop_code}")
async def customer_send_file(
    shop_code: str, request: Request,
    file: UploadFile = File(...),
    sender_name: str = Form(""),
    note: str = Form("")
):
    """Customer -> Shop: any file (docs, zip, photos...) lands in the shopkeeper's inbox."""
    shop = db.get_shop_by_code(shop_code.strip().lower())
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")
    t = await _store_transfer(shop["id"], "to_shop", file, sender_name, note, False)
    await manager.notify_admin(shop["id"], {"event": "NEW_TRANSFER", "transfer": _transfer_dict(t, request)})
    return {"success": True, "transfer_id": t["id"], "expires_hours": TRANSFER_TTL_HOURS}


@app.get("/admin/transfers")
async def admin_list_transfers(request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    rows = [t for t in db.list_shop_transfers(shop["id"]) if not _transfer_expired(t)]
    return {"transfers": [_transfer_dict(t, request) for t in rows]}


@app.post("/admin/transfer/upload")
async def admin_upload_transfer(
    request: Request,
    file: UploadFile = File(...),
    note: str = Form(""),
    one_time: Optional[int] = Form(0)
):
    """Shop -> Customer: creates a share link + QR code the customer can open on their phone."""
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    t = await _store_transfer(shop["id"], "to_customer", file, shop["name"], note, bool(one_time))
    return {"success": True, "transfer": _transfer_dict(t, request)}


@app.get("/admin/transfer/{transfer_id}/view")
async def admin_view_transfer(transfer_id: str, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    t = db.get_transfer(transfer_id.upper())
    if not t or t["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="File not found or expired")
    if not os.path.isfile(t["stored_path"]):
        await asyncio.to_thread(storage.ensure_local_from_r2, t["stored_path"])
    if not os.path.isfile(t["stored_path"]):
        raise HTTPException(status_code=404, detail="File not found or expired")

    orig_name = t["original_filename"]
    ext = utils.get_ext(orig_name).lower()
    mtype = mimetypes.guess_type(orig_name)[0] or "application/octet-stream"

    # If it is an Office document (Word, Excel, PPT), convert to PDF for inline preview
    import office_docs
    if ext in office_docs.OFFICE_EXTENSIONS:
        pdf_cache = os.path.join(TRANSFER_DIR, f"{t['id']}_preview.pdf")
        if not os.path.isfile(pdf_cache):
            state_cache = os.path.join(TRANSFER_DIR, f"{t['id']}_state.json")
            tmp_office_path = os.path.join(TRANSFER_DIR, f"{t['id']}_raw.{ext}")
            try:
                if not os.path.isfile(tmp_office_path):
                    shutil.copy2(t["stored_path"], tmp_office_path)
                await asyncio.to_thread(
                    office_docs.convert_office_file_to_pdf_and_state,
                    tmp_office_path, ext, orig_name, pdf_cache, state_cache
                )
            except Exception as e:
                logger.error(f"Failed to convert office transfer {t['id']} for preview: {e}")
            finally:
                utils.secure_delete_file(tmp_office_path)
        if os.path.isfile(pdf_cache):
            return FileResponse(
                pdf_cache,
                media_type="application/pdf",
                content_disposition_type="inline"
            )
        raise HTTPException(
            status_code=400,
            detail="এই Office ফাইলটির প্রিভিউ তৈরি করা সম্ভব হয়নি। ফাইলটি দেখতে ডাউনলোড করে নিন।"
        )

    if storage.is_r2_configured():
        r2_url = await asyncio.to_thread(
            storage.get_r2_url_for_local_path, t["stored_path"], orig_name, True, mtype
        )
        if r2_url:
            return RedirectResponse(url=r2_url, status_code=status.HTTP_302_FOUND)

    safe_exts = {"jpg", "jpeg", "png", "webp", "gif", "pdf", "txt", "csv", "log", "json"}
    if ext in {"jpg", "jpeg"}:
        mtype = "image/jpeg"
    elif ext == "png":
        mtype = "image/png"
    elif ext == "webp":
        mtype = "image/webp"
    elif ext == "gif":
        mtype = "image/gif"
    elif ext == "pdf":
        mtype = "application/pdf"
    elif ext in {"txt", "csv", "log", "json"}:
        mtype = "text/plain; charset=utf-8"
    else:
        # Strictly forbid inline rendering of dangerous/unknown files (e.g. HTML, SVG)
        return FileResponse(
            t["stored_path"],
            filename=orig_name,
            media_type="application/octet-stream",
            content_disposition_type="attachment"
        )

    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox"
    }
    return FileResponse(
        t["stored_path"],
        media_type=mtype,
        content_disposition_type="inline",
        headers=headers
    )


@app.get("/admin/transfer/{transfer_id}/download")
async def admin_download_transfer(transfer_id: str, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    t = db.get_transfer(transfer_id.upper())
    if not t or t["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="File not found or expired")
    mtype = mimetypes.guess_type(t["original_filename"])[0] or "application/octet-stream"
    if storage.is_r2_configured():
        r2_url = await asyncio.to_thread(
            storage.get_r2_url_for_local_path, t["stored_path"], t["original_filename"], False, mtype
        )
        if r2_url:
            db.increment_transfer_download(t["id"])
            return RedirectResponse(url=r2_url, status_code=status.HTTP_302_FOUND)
    if not os.path.isfile(t["stored_path"]):
        await asyncio.to_thread(storage.ensure_local_from_r2, t["stored_path"])
    if not os.path.isfile(t["stored_path"]):
        raise HTTPException(status_code=404, detail="File not found or expired")
    db.increment_transfer_download(t["id"])
    return FileResponse(
        t["stored_path"], filename=t["original_filename"],
        media_type=mtype
    )


@app.post("/admin/transfer/{transfer_id}/delete")
async def admin_delete_transfer(transfer_id: str, request: Request):
    shop = get_current_shop(request)
    if not shop:
        raise HTTPException(status_code=401, detail="Unauthorized")
    t = db.get_transfer(transfer_id.upper())
    if not t or t["shop_id"] != shop["id"]:
        raise HTTPException(status_code=404, detail="Not found")
    utils.secure_delete_file(t["stored_path"])
    db.delete_transfer_row(t["id"])
    return {"success": True}


def _public_transfer_or_404(transfer_id: str):
    t = db.get_transfer(transfer_id.strip().upper())
    if not t or t["direction"] != "to_customer" or _transfer_expired(t):
        raise HTTPException(status_code=404, detail="Link expired or file not found")
    if not os.path.isfile(t["stored_path"]) and not storage.r2_object_exists(storage.path_to_r2_key(t["stored_path"])):
        raise HTTPException(status_code=404, detail="Link expired or file not found")
    return t


@app.get("/t/{transfer_id}", response_class=HTMLResponse)
async def public_transfer_page(transfer_id: str, request: Request):
    try:
        t = _public_transfer_or_404(transfer_id)
    except HTTPException:
        return HTMLResponse(
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<body style='font-family:sans-serif;text-align:center;padding:48px 16px'>"
            "<h2>দুঃখিত! লিংকটির মেয়াদ শেষ হয়েছে বা ফাইলটি পাওয়া যায়নি।</h2>"
            "<p>দোকানদারের কাছ থেকে নতুন লিংক চেয়ে নিন।</p></body>", status_code=404)
    shop = db.get_shop_by_id(t["shop_id"])
    return templates.TemplateResponse(request=request, name="transfer_download.html", context={
        "t": _transfer_dict(t, request), "shop": shop
    })


@app.get("/t/{transfer_id}/download")
async def public_transfer_download(transfer_id: str):
    t = _public_transfer_or_404(transfer_id)
    db.increment_transfer_download(t["id"])
    mtype = mimetypes.guess_type(t["original_filename"])[0] or "application/octet-stream"
    if not t["one_time"] and storage.is_r2_configured():
        r2_url = await asyncio.to_thread(
            storage.get_r2_url_for_local_path, t["stored_path"], t["original_filename"], False, mtype
        )
        if r2_url:
            return RedirectResponse(url=r2_url, status_code=status.HTTP_302_FOUND)
    if not os.path.isfile(t["stored_path"]):
        await asyncio.to_thread(storage.ensure_local_from_r2, t["stored_path"])
    background = None
    if t["one_time"]:
        def _burn():
            utils.secure_delete_file(t["stored_path"])
            db.delete_transfer_row(t["id"])
        background = BackgroundTask(_burn)
    return FileResponse(
        t["stored_path"], filename=t["original_filename"],
        media_type=mtype,
        background=background
    )


@app.get("/t/{transfer_id}/qr.png")
async def public_transfer_qr(transfer_id: str, request: Request):
    t = _public_transfer_or_404(transfer_id)
    png = utils.generate_shop_qr(f"{base_url(request)}/t/{t['id']}")
    return Response(content=png, media_type="image/png")


# ── QR FILE TRANSFER ROOMS (phone <-> PC, any file type, no login) ──
ROOM_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_room_create_log: Dict[str, List[float]] = {}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown"))


def _new_room_id() -> str:
    while True:
        rid = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(8))
        if not db.get_room(rid):
            return rid


def _room_or_404(room_id: str):
    room = db.get_room((room_id or "").strip().upper())
    if not room or room["expires_at"] < _now_iso():
        raise HTTPException(status_code=404, detail="Room expired or not found")
    return room


def _room_files_payload(room) -> dict:
    files = db.list_room_files(room["id"])
    return {
        "room_id": room["id"],
        "expires_at": room["expires_at"],
        "max_files": ROOM_MAX_FILES,
        "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "files": [{
            "id": f["id"], "name": f["original_filename"], "size": f["file_size"],
            "size_text": utils.human_size(f["file_size"]), "uploader": f["uploader"] or "",
            "created_at": f["created_at"],
        } for f in files],
    }


@app.api_route("/ping", methods=["GET", "HEAD"])
async def ping():
    """Tiny keep-alive endpoint for cron-job.org / UptimeRobot (a few bytes, no database)."""
    return Response(content="ok", media_type="text/plain", headers={"Cache-Control": "no-store"})


@app.get("/api/storage-status")
@app.get("/api/r2/health")
def storage_status():
    """Diagnostic endpoint to verify whether Cloudflare R2 is configured and reachable."""
    import storage
    configured = storage.is_r2_configured()
    bucket = storage.R2_BUCKET_NAME if configured else None
    account_prefix = (storage.R2_ACCOUNT_ID[:6] + "...") if storage.R2_ACCOUNT_ID else None
    client_ok = False
    error = None
    if configured:
        try:
            client = storage.get_r2_client()
            if client:
                client.head_bucket(Bucket=storage.R2_BUCKET_NAME)
                client_ok = True
        except Exception as e:
            error = str(e)
    return {
        "r2_configured": configured,
        "bucket_name": bucket,
        "account_prefix": account_prefix,
        "connected": client_ok,
        "error": error
    }


@app.get("/transfer", response_class=HTMLResponse)
async def transfer_landing(request: Request):
    return templates.TemplateResponse(request=request, name="transfer_room.html", context={"room": None})


@app.post("/transfer/new")
async def transfer_new_room(request: Request):
    ip = _client_ip(request)
    now = datetime.now().timestamp()
    recent = [t for t in _room_create_log.get(ip, []) if now - t < 3600]
    if len(recent) >= 20:
        raise HTTPException(status_code=429, detail="অনেক বেশি সেশন তৈরি হয়েছে, কিছুক্ষণ পর চেষ্টা করুন।")
    recent.append(now)
    _room_create_log[ip] = recent

    rid = _new_room_id()
    created = datetime.now(timezone.utc)
    db.create_room(rid, created.isoformat(timespec="seconds"),
                   (created + timedelta(hours=ROOM_TTL_HOURS)).isoformat(timespec="seconds"))
    return RedirectResponse(url=f"/r/{rid}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/r/{room_id}", response_class=HTMLResponse)
async def transfer_room_page(room_id: str, request: Request):
    try:
        room = _room_or_404(room_id)
    except HTTPException:
        return HTMLResponse(
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<body style='font-family:sans-serif;text-align:center;padding:48px 16px'>"
            "<h2>এই ট্রান্সফার সেশনের মেয়াদ শেষ হয়েছে বা কোডটি ভুল।</h2>"
            "<p><a href='/transfer' style='color:#db2777;font-weight:bold'>নতুন সেশন শুরু করুন</a></p></body>",
            status_code=404)
    return templates.TemplateResponse(request=request, name="transfer_room.html", context={
        "room": {"id": room["id"], "url": f"{base_url(request)}/r/{room['id']}", "expires_at": room["expires_at"]}
    })


@app.get("/r/{room_id}/qr.png")
async def transfer_room_qr(room_id: str, request: Request):
    room = _room_or_404(room_id)
    return Response(content=utils.generate_shop_qr(f"{base_url(request)}/r/{room['id']}"), media_type="image/png")


@app.get("/api/room/{room_id}/files")
async def room_files(room_id: str):
    return _room_files_payload(_room_or_404(room_id))


@app.post("/api/room/{room_id}/upload")
async def room_upload(room_id: str, file: UploadFile = File(...), device: str = Form("")):
    room = _room_or_404(room_id)
    existing = db.list_room_files(room["id"])
    if len(existing) >= ROOM_MAX_FILES:
        raise HTTPException(status_code=429, detail=f"একটি সেশনে সর্বোচ্চ {ROOM_MAX_FILES}টি ফাইল রাখা যায়। কিছু ফাইল মুছুন।")

    fid = secrets.token_hex(6)
    stored = os.path.join(ROOM_DIR, f"{fid}.dat")
    size = await save_upload_limited(file, stored, MAX_UPLOAD_BYTES)
    if size == 0:
        utils.secure_delete_file(stored)
        raise HTTPException(status_code=400, detail="ফাঁকা ফাইল পাঠানো যাবে না।")
    if sum(f["file_size"] for f in existing) + size > ROOM_MAX_TOTAL_BYTES:
        utils.secure_delete_file(stored)
        raise HTTPException(status_code=413, detail=f"সেশনের মোট সাইজ {ROOM_MAX_TOTAL_BYTES // (1024 * 1024)} MB এর বেশি হতে পারবে না।")

    orig_name = utils.safe_filename(file.filename, "file")
    if storage.is_r2_configured():
        await asyncio.to_thread(storage.upload_file_to_r2, stored, None, "application/octet-stream")

    db.add_room_file({
        "id": fid, "room_id": room["id"],
        "original_filename": orig_name,
        "stored_path": stored, "file_size": size,
        "uploader": utils.safe_filename(device, "")[:30],
        "created_at": _now_iso(),
    })
    await manager.notify_room(room["id"], {"event": "ROOM_UPDATED"})
    return {"success": True, "file_id": fid}


def _room_file_or_404(room_id: str, file_id: str):
    room = _room_or_404(room_id)
    f = db.get_room_file(file_id)
    if not f or f["room_id"] != room["id"]:
        raise HTTPException(status_code=404, detail="File not found")
    if not os.path.isfile(f["stored_path"]) and not storage.r2_object_exists(storage.path_to_r2_key(f["stored_path"])):
        raise HTTPException(status_code=404, detail="File not found")
    return room, f


@app.get("/api/room/{room_id}/file/{file_id}")
async def room_download(room_id: str, file_id: str):
    _, f = _room_file_or_404(room_id, file_id)
    if storage.is_r2_configured():
        r2_url = await asyncio.to_thread(
            storage.get_r2_url_for_local_path, f["stored_path"], f["original_filename"], False, "application/octet-stream"
        )
        if r2_url:
            return RedirectResponse(url=r2_url, status_code=status.HTTP_302_FOUND)
    if not os.path.isfile(f["stored_path"]):
        await asyncio.to_thread(storage.ensure_local_from_r2, f["stored_path"])
    # always as attachment – an uploaded .html can never run inside our site
    return FileResponse(f["stored_path"], filename=f["original_filename"], media_type="application/octet-stream")


@app.post("/api/room/{room_id}/file/{file_id}/delete")
async def room_delete_file(room_id: str, file_id: str):
    room, f = _room_file_or_404(room_id, file_id)
    utils.secure_delete_file(f["stored_path"])
    db.delete_room_file(f["id"])
    await manager.notify_room(room["id"], {"event": "ROOM_UPDATED"})
    return {"success": True}


@app.get("/api/room/{room_id}/zip")
async def room_zip(room_id: str):
    import zipfile
    room = _room_or_404(room_id)
    all_files = db.list_room_files(room["id"])
    files = []
    for f in all_files:
        if os.path.isfile(f["stored_path"]) or await asyncio.to_thread(storage.ensure_local_from_r2, f["stored_path"]):
            files.append(f)
    if not files:
        raise HTTPException(status_code=404, detail="No files")
    tmp = os.path.join(ROOM_DIR, f"zip_{secrets.token_hex(6)}.tmp")
    used = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            name, n = f["original_filename"], 1
            while name in used:
                stem, dot, ext = f["original_filename"].rpartition(".")
                name = f"{stem or ext} ({n}){dot}{ext if stem else ''}"
                n += 1
            used.add(name)
            z.write(f["stored_path"], name)
    return FileResponse(tmp, filename=f"files_{room['id']}.zip", media_type="application/zip",
                        background=BackgroundTask(utils.secure_delete_file, tmp))


@app.post("/api/room/{room_id}/close")
async def room_close(room_id: str):
    room = _room_or_404(room_id)
    for f in db.list_room_files(room["id"]):
        utils.secure_delete_file(f["stored_path"])
    db.delete_room(room["id"])
    await manager.notify_room(room["id"], {"event": "ROOM_CLOSED"})
    return {"success": True}


@app.websocket("/ws/room/{room_id}")
async def websocket_room(websocket: WebSocket, room_id: str):
    rid = (room_id or "").strip().upper()
    room = db.get_room(rid)
    if not room or room["expires_at"] < _now_iso():
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await manager.connect_room(rid, websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                # WebRTC Signaling relay (P2P direct file transfer between Mobile<->Mobile, PC<->PC, Mobile<->PC)
                if str(msg.get("type", "")).startswith("webrtc_"):
                    for ws in list(manager.room_connections.get(rid, [])):
                        if ws != websocket:
                            try:
                                await ws.send_text(raw)
                            except Exception:
                                pass
            except Exception:
                pass
    except WebSocketDisconnect:
        manager.disconnect_room(rid, websocket)
    except Exception:
        manager.disconnect_room(rid, websocket)


# ── QR CODE GENERATOR ROUTE ──

@app.get("/qr/{shop_code}.png")
async def get_shop_qr_png(shop_code: str, request: Request):
    shop = db.get_shop_by_code(shop_code.strip().lower())
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")
        
    target_url = f"{base_url(request)}/p/{shop['shop_code']}"
    
    qr_bytes = utils.generate_shop_qr(target_url)
    return Response(content=qr_bytes, media_type="image/png")

# ── RESUME MAKER ROUTE ──

@app.get("/resume", response_class=HTMLResponse)
async def resume_maker(request: Request):
    return templates.TemplateResponse(request=request, name="resume_maker.html", context={})

# ── WEBSOCKETS ──

@app.websocket("/ws/admin/{shop_id}")
async def websocket_admin(websocket: WebSocket, shop_id: int):
    if unsign_value(websocket.cookies.get("shop_session")) != str(shop_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await manager.connect_admin(shop_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_admin(shop_id, websocket)

@app.websocket("/ws/agent/{api_key}")
async def websocket_agent(websocket: WebSocket, api_key: str):
    shop = db.get_shop_by_api_key(api_key)
    if not shop:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
        
    shop_id = shop["id"]
    await manager.connect_agent(shop_id, websocket)
    await manager.notify_admin(shop_id, {"event": "AGENT_STATUS", "online": True})
    print(f"[+] Agent connected for shop {shop['name']} ({shop['shop_code']})")
    
    try:
        while True:
            data = await websocket.receive_json()
            event = data.get("event")
            if event == "REGISTER_PRINTERS":
                p_list = data.get("printers", [])
                if isinstance(p_list, list):
                    db.sync_agent_printers(shop_id, p_list)
                    await manager.notify_admin(shop_id, {"event": "PRINTERS_SYNCED", "printers": db.get_shop_printers(shop_id)})
            elif event == "PRINT_COMPLETED":
                order_id = data.get("order_id")
                order = db.get_order(order_id)
                if not order or order["shop_id"] != shop_id:
                    continue
                db.update_order_status(order_id, "completed", printed_at=_now_iso())
                await manager.notify_order(order_id, {
                    "event": "STATUS_CHANGE",
                    "status": "completed",
                    "message": "প্রিন্ট সম্পন্ন হয়েছে! ৩০ মিনিট পর ফাইলটি স্বয়ংক্রিয়ভাবে মুছে যাবে।"
                })
                await manager.notify_admin(shop_id, {"event": "ORDER_UPDATED", "order_id": order_id})
            elif event == "PRINT_FAILED":
                order_id = data.get("order_id")
                reason = data.get("reason", "প্রিন্টার সংযোগ পাওয়া যায়নি")
                order = db.get_order(order_id)
                if order and order["shop_id"] == shop_id:
                    db.update_order_status(order_id, "paid")
                    await manager.notify_admin(shop_id, {"event": "PRINT_FAILED", "order_id": order_id, "reason": reason})
    except WebSocketDisconnect:
        manager.disconnect_agent(shop_id, websocket)
        await manager.notify_admin(shop_id, {"event": "AGENT_STATUS", "online": False})
        print(f"[-] Agent disconnected for shop {shop['name']}")

@app.websocket("/ws/order/{order_id}")
async def websocket_order(websocket: WebSocket, order_id: str):
    await manager.connect_order(order_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_order(order_id, websocket)


@app.websocket("/ws/superadmin")
async def websocket_superadmin(websocket: WebSocket):
    if unsign_value(websocket.cookies.get("superadmin_session")) != "superadmin":
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await manager.connect_superadmin(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_superadmin(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
