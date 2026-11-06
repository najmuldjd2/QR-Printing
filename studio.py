import os
"""
Photo Studio engine: passport / stamp / visa / custom-size photos laid out in neat rows on
A4 (or 4x6 inch) paper, with brightness, contrast, black&white, zoom/position and
background colour change.

All sizes are in millimetres; the sheet is rendered at the requested DPI
(100 DPI for the live preview, 300 DPI for the file that is printed).
"""
import re
from typing import Dict, List, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

# paper -> (width_mm, height_mm, margin_mm, gap_mm)
PAPERS = {
    "A4": (210.0, 297.0, 8.0, 2.0),
    "4x6": (152.4, 101.6, 3.0, 1.5),   # 6 x 4 inch (landscape photo paper)
}
MAX_ITEMS = 12
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _num(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def sanitize_params(raw: Dict) -> Dict:
    """Clamps every value that comes from the browser."""
    raw = raw or {}
    paper = raw.get("paper") if raw.get("paper") in PAPERS else "A4"

    global_zoom = _num(raw.get("zoom"), 1.0, 3.0, 1.0)
    global_ox = _num(raw.get("ox"), -1.0, 1.0, 0.0)
    global_oy = _num(raw.get("oy"), -1.0, 1.0, 0.0)

    items = []
    for it in (raw.get("items") or [])[:MAX_ITEMS]:
        w = _num(it.get("w"), 10, 190, 0)
        h = _num(it.get("h"), 10, 280, 0)
        count = int(_num(it.get("count"), 0, 60, 0))
        if w and h and count > 0:
            item_entry = {
                "w": w,
                "h": h,
                "count": count,
                "file_id": str(it.get("file_id") or "").strip() or None,
                "photo_idx": int(_num(it.get("photo_idx"), 0, 20, 0)),
                "zoom": _num(it.get("zoom"), 1.0, 3.0, global_zoom),
                "ox": _num(it.get("ox"), -1.0, 1.0, global_ox),
                "oy": _num(it.get("oy"), -1.0, 1.0, global_oy),
                "bw": bool(it.get("bw")) if "bw" in it else None,
            }
            items.append(item_entry)
    if not items:
        items = [{
            "w": 45.0,
            "h": 55.0,
            "count": 6,
            "file_id": None,
            "photo_idx": 0,
            "zoom": global_zoom,
            "ox": global_ox,
            "oy": global_oy,
            "bw": None,
        }]
    photos = [str(x).strip() for x in (raw.get("photos") or []) if str(x).strip()]

    bg = raw.get("bg") or "none"
    if bg != "none" and not HEX_RE.match(str(bg)):
        bg = "none"

    suit = str(raw.get("suit") or "none").strip()
    if suit not in ("none", "suit1", "suit2", "suit3", "suit4"):
        suit = "none"

    camscan = str(raw.get("camscan") or "none").strip()
    if camscan not in ("none", "magic_color", "bw_doc", "grayscale"):
        camscan = "none"

    return {
        "paper": paper,
        "photos": photos,
        "items": items,
        "fill": bool(raw.get("fill")),
        "brightness": _num(raw.get("brightness"), -60, 60, 0),
        "contrast": _num(raw.get("contrast"), -60, 60, 0),
        "bw": bool(raw.get("bw")),
        "border": raw.get("border", True) is not False,
        "bg": bg,
        "tol": _num(raw.get("tol"), 10, 100, 45),
        "zoom": _num(raw.get("zoom"), 1.0, 3.0, 1.0),
        "ox": _num(raw.get("ox"), -1.0, 1.0, 0.0),
        "oy": _num(raw.get("oy"), -1.0, 1.0, 0.0),
        "suit": suit,
        "suit_zoom": _num(raw.get("suit_zoom"), 0.5, 2.0, 1.0),
        "suit_ox": _num(raw.get("suit_ox"), -1.0, 1.0, 0.0),
        "suit_oy": _num(raw.get("suit_oy"), -1.0, 1.0, 0.0),
        "camscan": camscan,
    }


# ── image operations ────────────────────────────────────────────────────────
def load_working(path: str, max_side: int) -> Image.Image:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return im


def _background_mask(img: Image.Image, tol: float) -> Image.Image:
    """
    Finds the plain background: colour of the image border (top + sides) is taken as the
    background colour, then every similar pixel that is CONNECTED to the border is background.
    Works best with plain / light walls. Returns an 'L' mask (255 = background).
    """
    small = img.copy()
    small.thumbnail((480, 480), Image.Resampling.BILINEAR)
    a = np.asarray(small, dtype=np.float32)
    h, w, _ = a.shape
    t = max(2, int(0.03 * min(h, w)))
    side_h = max(t, int(h * 0.7))
    border = np.concatenate([a[:t].reshape(-1, 3), a[:side_h, :t].reshape(-1, 3), a[:side_h, -t:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    similar = np.sqrt(((a - bg) ** 2).sum(axis=2)) < tol

    reach = np.zeros_like(similar)
    reach[0, :] = similar[0, :]
    reach[:side_h, 0] = similar[:side_h, 0]
    reach[:side_h, -1] = similar[:side_h, -1]
    for i in range(1500):
        grown = reach.copy()
        grown[1:] |= reach[:-1]
        grown[:-1] |= reach[1:]
        grown[:, 1:] |= reach[:, :-1]
        grown[:, :-1] |= reach[:, 1:]
        grown &= similar
        if i % 8 == 7 and np.array_equal(grown, reach):
            reach = grown
            break
        reach = grown

    mask = Image.fromarray((reach * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(1.2))
    return mask.resize(img.size, Image.Resampling.BICUBIC)


def replace_background(img: Image.Image, hex_color: str, tol: float) -> Image.Image:
    mask = _background_mask(img, tol)
    solid = Image.new("RGB", img.size, hex_color)
    return Image.composite(solid, img, mask)


def adjust(img: Image.Image, brightness: float, contrast: float, bw: bool) -> Image.Image:
    if brightness:
        img = ImageEnhance.Brightness(img).enhance(1 + brightness / 100.0)
    if contrast:
        img = ImageEnhance.Contrast(img).enhance(1 + contrast / 100.0)
    if bw:
        img = ImageOps.grayscale(img).convert("RGB")
    return img


def crop_frame(img: Image.Image, w_px: int, h_px: int, zoom: float, ox: float, oy: float) -> Image.Image:
    """Cover-crops img to the frame aspect ratio, then applies zoom and position (ox, oy in -1..1)."""
    iw, ih = img.size
    target = w_px / h_px
    if iw / ih > target:
        bh, bw = ih, ih * target
    else:
        bw, bh = iw, iw / target
    bw, bh = bw / zoom, bh / zoom
    cx = iw / 2 + ox * (iw - bw) / 2
    cy = ih / 2 + oy * (ih - bh) / 2
    left = min(max(cx - bw / 2, 0), iw - bw)
    top = min(max(cy - bh / 2, 0), ih - bh)
    box = (int(left), int(top), int(left + bw), int(top + bh))
    return img.crop(box).resize((w_px, h_px), Image.Resampling.LANCZOS)


# ── layout (shelf packing in rows) ──────────────────────────────────────────
def pack(items: List[Tuple[int, float, float]], page_w: float, page_h: float, margin: float, gap: float):
    """
    items: [(item_index, w_mm, h_mm)] — tallest first, laid out left→right in rows,
    every row centred. Returns (placements, unplaced) where placements = [(item_index, x, y, w, h)].
    """
    avail_w = page_w - 2 * margin
    bottom = page_h - margin
    rows: List[Dict] = []
    unplaced = []

    for idx, w, h in sorted(items, key=lambda t: -t[2]):
        if w > avail_w or h > bottom - margin:
            unplaced.append(idx)
            continue
        row = rows[-1] if rows else None
        if row and row["w"] + gap + w <= avail_w and h <= row["h"] + 1e-6:
            row["items"].append((idx, w, h))
            row["w"] += gap + w
            continue
        y = margin if not rows else rows[-1]["y"] + rows[-1]["h"] + gap
        if y + h <= bottom + 1e-6:
            rows.append({"y": y, "h": h, "w": w, "items": [(idx, w, h)]})
        else:
            unplaced.append(idx)

    placements = []
    for row in rows:
        x = margin + (avail_w - row["w"]) / 2
        for idx, w, h in row["items"]:
            placements.append((idx, x, row["y"], w, h))
            x += w + gap
    return placements, unplaced


def render_sheet(
    src_path: str,
    params: Dict,
    dpi: int = 300,
    photos_map: Dict[str, str] = None,
    photos_list: List[str] = None
) -> Tuple[Image.Image, Dict]:
    """
    Returns (sheet image, info) where info tells how many photos of each size fit.
    Supports multi-photo layouts and independent per-row zoom & crop.
    """
    p = sanitize_params(params) if "paper" not in params or "fill" not in params else params
    page_w, page_h, margin, gap = PAPERS[p["paper"]]
    mm = lambda v: int(round(v / 25.4 * dpi))

    items = p["items"][:1] if p["fill"] else p["items"]
    expanded = []
    for i, it in enumerate(items):
        n = 400 if p["fill"] else it["count"]
        expanded += [(i, it["w"], it["h"])] * n
    placements, _ = pack(expanded, page_w, page_h, margin, gap)

    # 4x6 photo paper: try the page both ways and keep the one that holds more photos
    if p["paper"] == "4x6":
        alt, _ = pack(expanded, page_h, page_w, margin, gap)
        if len(alt) >= len(placements):
            placements, page_w, page_h = alt, page_h, page_w

    import image_fx
    max_side = max(700, int(dpi * 8))

    # Pre-process base photos for items
    processed_photos: Dict[str, Image.Image] = {}

    def get_base_photo_for_item(it_cfg: Dict) -> Image.Image:
        target_path = src_path
        fid = it_cfg.get("file_id")
        pidx = it_cfg.get("photo_idx", 0)

        if photos_map and fid and fid in photos_map and os.path.isfile(photos_map[fid]):
            target_path = photos_map[fid]
        elif photos_list and 0 <= pidx < len(photos_list) and os.path.isfile(photos_list[pidx]):
            target_path = photos_list[pidx]

        if target_path in processed_photos:
            return processed_photos[target_path]

        im = load_working(target_path, max_side)

        # 1. CamScanner
        if p.get("camscan") and p["camscan"] != "none":
            im = image_fx.camscanner_process(im, mode=p["camscan"])

        # 2. Advanced Remove.bg style Background Replacement
        if p["bg"] != "none":
            rgba, mask = image_fx.advanced_remove_background(im, p["tol"])
            solid = Image.new("RGB", im.size, p["bg"])
            im = Image.composite(im, solid, mask)

        # 3. Formal Suit / Dress Overlay
        if p.get("suit") and p["suit"] != "none":
            im = image_fx.apply_dress_overlay(
                im, p["suit"],
                suit_zoom=p.get("suit_zoom", 1.0),
                suit_ox=p.get("suit_ox", 0.0),
                suit_oy=p.get("suit_oy", 0.0)
            )

        # 4. Color, Brightness & Contrast adjustment (sheet-level default)
        im = adjust(im, p["brightness"], p["contrast"], p["bw"])
        processed_photos[target_path] = im
        return im

    sheet = Image.new("RGB", (mm(page_w), mm(page_h)), "white")
    draw = ImageDraw.Draw(sheet)
    # Cache key: (item_idx, w_px, h_px) so different items with same size retain independent photos/crops!
    cache: Dict[Tuple[int, int, int], Image.Image] = {}
    line = max(1, dpi // 150)

    for idx, x, y, w, h in placements:
        it_cfg = items[idx]
        w_px, h_px = mm(w), mm(h)
        cache_key = (idx, w_px, h_px)

        if cache_key not in cache:
            base_photo = get_base_photo_for_item(it_cfg)
            item_zoom = it_cfg.get("zoom", p["zoom"])
            item_ox = it_cfg.get("ox", p["ox"])
            item_oy = it_cfg.get("oy", p["oy"])

            cropped = crop_frame(base_photo, w_px, h_px, item_zoom, item_ox, item_oy)

            # Check if this item has row-level B&W override
            if it_cfg.get("bw") is True and not p["bw"]:
                cropped = ImageOps.grayscale(cropped).convert("RGB")

            cache[cache_key] = cropped

        px, py = mm(x), mm(y)
        sheet.paste(cache[cache_key], (px, py))
        if p["border"]:
            draw.rectangle([px, py, px + w_px - 1, py + h_px - 1], outline="#9ca3af", width=line)

    placed = [0] * len(items)
    for idx, *_ in placements:
        placed[idx] += 1
    info = {
        "placed": placed,
        "requested": [None if p["fill"] else it["count"] for it in items],
        "total": len(placements),
        "orientation": "landscape" if page_w > page_h else "portrait",
    }
    return sheet, info


def save_sheet_pdf(sheet: Image.Image, out_path: str, dpi: int = 300) -> str:
    sheet.save(out_path, "PDF", resolution=float(dpi))
    return out_path
