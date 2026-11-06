import os
import sys
import time
import json
import asyncio
import tempfile
import urllib.request
import ssl
import re
import shutil
import subprocess
import websockets

# Ensure UTF-8 output on Windows consoles to prevent cp1252 UnicodeEncodeError
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(AGENT_DIR, "agent_config.json")

DEFAULT_CONFIG = {
    "server_url": "https://qr-print-bd.onrender.com",
    "printer_name": "",
    "sumatra_path": ""
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(DEFAULT_CONFIG)

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
    except Exception as e:
        print(f"[!] Config save error: {e}")

def list_windows_printers():
    """Returns a list of installed Windows printer names."""
    printers = []
    if sys.platform != "win32":
        return printers
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        for (_flags, _desc, name, _comment) in win32print.EnumPrinters(flags):
            if name and name not in printers:
                printers.append(name)
        return printers
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", "Get-Printer | Select-Object -ExpandProperty Name"],
            timeout=10, text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            name = line.strip()
            if name and name not in printers:
                printers.append(name)
    except Exception:
        pass
    return printers

def resolve_matching_printer(requested_name: str, installed_list: list) -> str:
    """Matches a requested printer name (or partial name) against installed Windows printers."""
    req = (requested_name or "").strip()
    if not req or not installed_list:
        return ""
    for p in installed_list:
        if p.lower() == req.lower():
            return p
    for p in installed_list:
        if req.lower() in p.lower() or p.lower() in req.lower():
            return p
    return ""

def find_sumatra(config: dict):
    candidates = [
        config.get("sumatra_path", ""),
        os.path.join(AGENT_DIR, "SumatraPDF.exe"),
        os.path.join(AGENT_DIR, "SumatraPDF-portable.exe"),
        r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
        r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
        shutil.which("SumatraPDF") or "",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None

def print_with_sumatra(sumatra: str, file_path: str, copies: int, printer_name: str = "", orientation: str = "portrait") -> bool:
    settings_items = ["shrink", f"{max(1, copies)}x"]
    if orientation == "landscape":
        settings_items.append("landscape")
    elif orientation == "portrait":
        settings_items.append("portrait")
    settings = ",".join(settings_items)
    cmd = [sumatra]
    cmd += ["-print-to", printer_name] if printer_name else ["-print-to-default"]
    cmd += ["-print-settings", settings, "-silent", file_path]
    print(f"[PRINT] SumatraPDF ({orientation}, no scaling, {copies} copy): {os.path.basename(file_path)}")
    result = subprocess.run(cmd, timeout=180)
    return result.returncode == 0

def print_pil_images_gdi(pil_images: list, printer_name: str, copies: int, orientation: str = "portrait", doc_name: str = "QR_Se_Print") -> bool:
    """
    Native Windows GDI printing using win32print + win32ui + PIL.ImageWin.Dib.
    Works on any Windows printer without needing external PDF/Image viewer apps!
    """
    import win32print
    import win32ui
    import win32con
    from PIL import ImageWin, ImageOps

    target_printer = printer_name or win32print.GetDefaultPrinter()
    print(f"[GDI PRINT] Sending {len(pil_images)} page(s) x {copies} copy to printer: '{target_printer}' ({orientation})")

    hDC = win32ui.CreateDC()
    hDC.CreatePrinterDC(target_printer)
    printable_w = hDC.GetDeviceCaps(win32con.HORZRES)
    printable_h = hDC.GetDeviceCaps(win32con.VERTRES)

    hDC.StartDoc(doc_name)
    try:
        for _c in range(copies):
            for img in pil_images:
                page_im = ImageOps.exif_transpose(img).convert("RGB")
                # Rotate image to match paper aspect if needed
                printer_is_landscape = printable_w > printable_h
                want_landscape = (orientation == "landscape")
                if want_landscape != printer_is_landscape:
                    # Rotate 90 degrees so Landscape content fits upright on the physical sheet
                    if (want_landscape and page_im.width > page_im.height) or (not want_landscape and page_im.height > page_im.width):
                        page_im = page_im.rotate(90, expand=True)

                hDC.StartPage()
                iw, ih = page_im.size
                scale = min(printable_w / float(iw), printable_h / float(ih))
                dst_w = int(iw * scale)
                dst_h = int(ih * scale)
                x0 = (printable_w - dst_w) // 2
                y0 = (printable_h - dst_h) // 2

                dib = ImageWin.Dib(page_im)
                dib.draw(hDC.GetHandleOutput(), (x0, y0, x0 + dst_w, y0 + dst_h))
                hDC.EndPage()
    finally:
        hDC.EndDoc()
        hDC.DeleteDC()
    return True

def print_file_windows(file_path: str, copies: int = 1, config: dict = None) -> bool:
    """
    Multi-layer Windows printing engine:
    1) SumatraPDF (if installed)
    2) Native Windows GDI Spooler via win32ui + PIL + PyMuPDF (prints PDFs & images directly to printer)
    3) Built-in Windows mspaint.exe /pt for images
    4) ShellExecute / PowerShell fallback
    """
    config = config or {}
    installed = list_windows_printers()
    raw_printer = (config.get("printer_name") or "").strip()
    printer_name = resolve_matching_printer(raw_printer, installed)
    if raw_printer and not printer_name:
        print(f"[i] Requested printer '{raw_printer}' not found locally; using Windows Default Printer.")
    copies = max(1, int(copies or 1))
    orientation = (config.get("orientation") or "portrait").strip()

    try:
        if sys.platform != "win32":
            print(f"[*] Simulating {copies} print(s) for '{file_path}' (Non-Windows platform)")
            time.sleep(1)
            return True

        ext = os.path.splitext(file_path)[1].lower()

        # Layer 0: Direct Microsoft Word COM printing (100% Microsoft 365 Fidelity)
        if ext in (".doc", ".docx", ".rtf"):
            try:
                import win32com.client
                import pythoncom
                pythoncom.CoInitialize()
                word = win32com.client.DispatchEx("Word.Application")
                word.Visible = False
                word.DisplayAlerts = 0
                if printer_name:
                    try:
                        word.ActivePrinter = printer_name
                    except Exception:
                        pass
                doc = word.Documents.Open(os.path.abspath(file_path), False, True)
                for _ in range(copies):
                    doc.PrintOut(False)
                doc.Close(0)
                word.Quit()
                pythoncom.CoUninitialize()
                print(f"[V] Printed directly via Microsoft Word COM (100% Native Fidelity)!")
                return True
            except Exception as word_err:
                print(f"[i] Microsoft Word COM direct print note: {word_err}")

        # Layer 1: SumatraPDF
        sumatra = find_sumatra(config)
        if sumatra and ext in (".pdf", ".jpg", ".jpeg", ".png"):
            if print_with_sumatra(sumatra, file_path, copies, printer_name, orientation):
                return True

        # Layer 2: Native Windows GDI printing (win32ui + PIL + PyMuPDF)
        try:
            from PIL import Image
            if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
                with Image.open(file_path) as im:
                    im.load()
                    if print_pil_images_gdi([im], printer_name, copies, orientation, os.path.basename(file_path)):
                        return True
            elif ext == ".pdf":
                try:
                    import pymupdf as fitz
                except ImportError:
                    import fitz
                doc = fitz.open(file_path)
                pages_pil = []
                for page in doc:
                    pix = page.get_pixmap(dpi=200, alpha=False)
                    pages_pil.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
                doc.close()
                if pages_pil and print_pil_images_gdi(pages_pil, printer_name, copies, orientation, os.path.basename(file_path)):
                    return True
        except Exception as gdi_err:
            print(f"[i] GDI direct print fallback note: {gdi_err}")

        # Layer 3: Built-in mspaint.exe for images (present on all Windows 10/11 machines)
        if ext in (".jpg", ".jpeg", ".png", ".bmp"):
            mspaint = shutil.which("mspaint") or r"C:\Windows\System32\mspaint.exe"
            if os.path.isfile(mspaint):
                for n in range(copies):
                    cmd = [mspaint, "/pt", file_path, printer_name] if printer_name else [mspaint, "/p", file_path]
                    print(f"[MSPAINT] ({n + 1}/{copies}) Printing image via mspaint...")
                    subprocess.run(cmd, timeout=60)
                return True

        # Layer 4: ShellExecute / os.startfile fallback
        for n in range(copies):
            try:
                import win32api
                import win32print
                target_printer = printer_name or win32print.GetDefaultPrinter()
                print(f"[SHELL PRINT] ({n + 1}/{copies}) {target_printer}: {os.path.basename(file_path)}")
                win32api.ShellExecute(
                    0,
                    "printto" if printer_name else "print",
                    file_path,
                    f'"{target_printer}"' if printer_name else None,
                    ".",
                    0
                )
            except Exception:
                print(f"[STARTFILE PRINT] ({n + 1}/{copies}) default printer: {os.path.basename(file_path)}")
                os.startfile(file_path, "print")
            time.sleep(6)
        return True
    except Exception as e:
        print(f"[!] Print error: {e}")
        return False

async def handle_print_job(job_data: dict, server_url: str, api_key: str, websocket, config: dict = None, ssl_context=None):
    order_id = job_data["order_id"]
    download_rel_url = job_data["download_url"]
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", job_data.get("filename", "print_doc")) or "print_doc"
    copies = int(job_data.get("copies", 1) or 1)
    target_printer = (job_data.get("printer_name") or "").strip()

    full_url = f"{server_url.rstrip('/')}{download_rel_url}?key={api_key}"
    print(f"\n========================================================")
    print(f"[+] NEW PRINT ORDER: Order #{order_id} ({filename}) x{copies} {f'-> Printer: {target_printer}' if target_printer else ''}")
    print(f"========================================================")

    temp_dir = tempfile.gettempdir()
    local_file_path = os.path.join(temp_dir, f"qsp_{order_id}_{filename}")

    try:
        print(f"[1/3] Downloading file from cloud...")
        req = urllib.request.Request(full_url, headers={"User-Agent": "QRSePrint-Agent/2.0"})
        with urllib.request.urlopen(req, context=ssl_context, timeout=60) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if not os.path.splitext(local_file_path)[1]:
                if "pdf" in ctype:
                    local_file_path += ".pdf"
                else:
                    local_file_path += ".jpg"
            with open(local_file_path, "wb") as out_f:
                shutil.copyfileobj(resp, out_f)

        print(f"[✓] Download complete ({os.path.getsize(local_file_path)} bytes)")

        print(f"[2/3] Sending to printer...")
        effective_config = dict(config or {})
        effective_config["orientation"] = job_data.get("orientation", "portrait")
        if target_printer:
            effective_config["printer_name"] = target_printer
        success = await asyncio.to_thread(print_file_windows, local_file_path, copies, effective_config)

        if success:
            print(f"[✓] Print successful!")
            await websocket.send(json.dumps({
                "event": "PRINT_COMPLETED",
                "order_id": order_id
            }))
        else:
            print(f"[!] Print failed.")
            await websocket.send(json.dumps({
                "event": "PRINT_FAILED",
                "order_id": order_id,
                "reason": "প্রিন্টারে কমান্ড পাঠানো যায়নি"
            }))

    except Exception as e:
        print(f"[!] Processing error: {e}")
        try:
            await websocket.send(json.dumps({
                "event": "PRINT_FAILED",
                "order_id": order_id,
                "reason": str(e)
            }))
        except Exception:
            pass
    finally:
        try:
            if os.path.exists(local_file_path):
                os.remove(local_file_path)
                print(f"[PRIVACY SAFE] Local temp file permanently deleted.")
        except Exception as err:
            print(f"[!] File delete error: {err}")

def ask_api_key(server_url: str, retry: bool = False) -> str:
    print(f"\n[*] Target Server: {server_url}")
    print("    Open your Admin Dashboard (/admin) and copy your Shop API Key.")
    while True:
        key = input(">> Enter or paste your Shop API Key and press Enter: " if not retry
                    else ">> Enter the correct Shop API Key: ").strip().strip('"').strip("'")
        if key:
            return key
        print("[!] API Key cannot be empty. Please try again.")

async def run_agent():
    print("=" * 60)
    print("      QR Se Print - Windows Desktop Auto-Print Agent       ")
    print("=" * 60)

    config = load_config()
    server_url = config.get("server_url", "https://qr-printing.onrender.com").rstrip("/")

    if config.pop("api_key", None):
        save_config(config)

    installed_printers = list_windows_printers()
    if installed_printers:
        print(f"[*] Detected Windows Printers ({len(installed_printers)}): {', '.join(installed_printers)}")

    api_key = ask_api_key(server_url)

    ws_protocol = "wss" if server_url.startswith("https") else "ws"
    host_port = server_url.split("://")[-1]

    ssl_context = None
    if ws_protocol == "wss":
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    print(f"[*] Server URL: {server_url}")
    print(f"[*] API Key   : {api_key[:8]}...{api_key[-4:] if len(api_key)>12 else ''}")
    print(f"[*] Connecting to server...")

    retry_delay = 3
    while True:
        ws_url = f"{ws_protocol}://{host_port}/ws/agent/{api_key}"
        try:
            async with websockets.connect(ws_url, ssl=ssl_context, ping_interval=20, ping_timeout=20) as ws:
                print(f"\n[🟢 ONLINE] Connected successfully to {server_url}!")
                if installed_printers:
                    await ws.send(json.dumps({
                        "event": "REGISTER_PRINTERS",
                        "printers": installed_printers
                    }))
                print(f"[*] Ready and waiting for incoming print jobs...\n")
                retry_delay = 3

                while True:
                    raw_msg = await ws.recv()
                    data = json.loads(raw_msg)
                    if data.get("event") == "PRINT_JOB":
                        await handle_print_job(data.get("data", {}), server_url, api_key, ws, config, ssl_context)

        except Exception as e:
            err_msg = str(e)
            if "403" in err_msg or "1008" in err_msg or "rejected" in err_msg:
                print("\n[ERROR] Server rejected this API Key!")
                api_key = ask_api_key(server_url, retry=True)
                print("[OK] Reconnecting...")
                continue
            elif "ConnectionRefused" in err_msg or "WinError 10061" in err_msg:
                print(f"\r[OFFLINE] Server not reachable. Retrying in {retry_delay}s...", end="")
            else:
                print(f"\r[OFFLINE] Connection issue ({err_msg}). Retrying in {retry_delay}s...", end="")

            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay + 2, 20)

if __name__ == "__main__":
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        print("\n[!] Agent stopped by user.")
