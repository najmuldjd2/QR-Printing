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
        w = _num(it.get("w"), 10, 205, 0)
        h = _num(it.get("h"), 10, 290, 0)
        count = int(_num(it.get("count"), 0, 60, 0))
        if w and h and count > 0:
            item_entry = {
                "w": w,
                "h": h,
                "preset": str(it.get("preset") or ""),
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

    raw_fill = raw.get("fill")
    fill = bool(raw_fill is True or str(raw_fill).lower() in ("true", "1", "autofill"))

    return {
        "paper": paper,
        "photos": photos,
        "items": items,
        "fill": fill,
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
def load_working(path: Union[str, Image.Image], max_side: int) -> Image.Image:
    if isinstance(path, Image.Image):
        im = path.copy().convert("RGB")
    else:
        with Image.open(path) as raw:
            im = ImageOps.exif_transpose(raw).convert("RGB")
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
def pack_single_page(items: List[Tuple[int, float, float]], page_w: float, page_h: float, margin: float, gap: float):
    """
    Packs as many items as possible on a single page.
    items: [(item_index, w_mm, h_mm)] — tallest first.
    Returns (placements, unplaced_items).
    """
    avail_w = page_w - 2 * margin
    bottom = page_h - margin
    rows: List[Dict] = []
    unplaced_items: List[Tuple[int, float, float]] = []

    for item in sorted(items, key=lambda t: -t[2]):
        idx, w, h = item
        if w > avail_w or h > bottom - margin:
            unplaced_items.append(item)
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
            unplaced_items.append(item)

    placements = []
    for row in rows:
        x = margin + (avail_w - row["w"]) / 2
        for idx, w, h in row["items"]:
            placements.append((idx, x, row["y"], w, h))
            x += w + gap
    return placements, unplaced_items


def pack(items: List[Tuple[int, float, float]], page_w: float, page_h: float, margin: float, gap: float):
    """
    Backwards-compatible single-page pack.
    Returns (placements, unplaced_indices).
    """
    placements, unplaced_items = pack_single_page(items, page_w, page_h, margin, gap)
    return placements, [it[0] for it in unplaced_items]


def pack_all_pages(items: List[Tuple[int, float, float]], page_w: float, page_h: float, margin: float, gap: float, max_pages: int = 30) -> List[List[Tuple[int, float, float, float, float]]]:
    """
    Packs all items across as many pages as necessary.
    Returns a list of placement lists, one per page: [ [placements_page_1], [placements_page_2], ... ]
    """
    pages_placements = []
    remaining = list(items)
    while remaining and len(pages_placements) < max_pages:
        placements, unplaced_items = pack_single_page(remaining, page_w, page_h, margin, gap)
        if not placements:
            # Remaining items cannot fit on the paper dimensions at all
            break
        pages_placements.append(placements)
        if len(unplaced_items) == len(remaining):
            # No progress made
            break
        remaining = unplaced_items
    return pages_placements


def render_sheets(
    src_path: str,
    params: Dict,
    dpi: int = 300,
    photos_map: Dict[str, str] = None,
    photos_list: List[str] = None
) -> Tuple[List[Image.Image], Dict]:
    """
    Renders all required sheet pages so that NO photo size or copy is dropped.
    Returns (list_of_sheet_images, info).
    """
    p = sanitize_params(params)
    page_w, page_h, margin, gap = PAPERS[p["paper"]]
    mm = lambda v: int(round(v / 25.4 * dpi))

    is_autofill = bool(p.get("fill")) and len(p.get("items", [])) == 1
    items = p["items"][:1] if is_autofill else p["items"]
    expanded = []
    for i, it in enumerate(items):
        n = 400 if is_autofill else it.get("count", 1)
        expanded += [(i, it["w"], it["h"])] * n

    if is_autofill:
        pl, _ = pack_single_page(expanded, page_w, page_h, margin, gap)
        pages_placements = [pl] if pl else []
    else:
        pages_placements = pack_all_pages(expanded, page_w, page_h, margin, gap)

    # 4x6 photo paper: try both orientations if single page
    if p["paper"] == "4x6" and len(pages_placements) <= 1:
        alt_pages = pack_all_pages(expanded, page_h, page_w, margin, gap)
        if alt_pages:
            tot_alt = sum(len(pl) for pl in alt_pages)
            tot_cur = sum(len(pl) for pl in pages_placements)
            if tot_alt >= tot_cur:
                pages_placements = alt_pages
                page_w, page_h = page_h, page_w

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

        cache_key = id(target_path) if isinstance(target_path, Image.Image) else str(target_path)
        if cache_key in processed_photos:
            return processed_photos[cache_key]

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
        processed_photos[cache_key] = im
        return im

    # Cache key: (item_idx, w_px, h_px) so different items with same size retain independent photos/crops!
    cache: Dict[Tuple[int, int, int], Image.Image] = {}
    line = max(1, dpi // 150)
    sheets: List[Image.Image] = []

    if not pages_placements:
        empty_sheet = Image.new("RGB", (mm(page_w), mm(page_h)), "white")
        sheets.append(empty_sheet)
    else:
        for page_placements in pages_placements:
            sheet = Image.new("RGB", (mm(page_w), mm(page_h)), "white")
            draw = ImageDraw.Draw(sheet)

            for idx, x, y, w, h in page_placements:
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

            sheets.append(sheet)

    placed = [0] * len(items)
    for page_placements in pages_placements:
        for idx, *_ in page_placements:
            placed[idx] += 1

    info = {
        "placed": placed,
        "requested": [None if p["fill"] else it["count"] for it in items],
        "total": sum(placed),
        "total_pages": len(sheets),
        "orientation": "landscape" if page_w > page_h else "portrait",
    }
    return sheets, info


def render_sheet(
    src_path: str,
    params: Dict,
    dpi: int = 300,
    photos_map: Dict[str, str] = None,
    photos_list: List[str] = None,
    page: int = 1
) -> Tuple[Image.Image, Dict]:
    """
    Returns (sheet image for specified page, info).
    """
    sheets, info = render_sheets(src_path, params, dpi=dpi, photos_map=photos_map, photos_list=photos_list)
    p_idx = max(0, min(len(sheets) - 1, int(page or 1) - 1))
    sheet = sheets[p_idx] if sheets else Image.new("RGB", (100, 100), "white")
    info["current_page"] = p_idx + 1
    info["total_pages"] = len(sheets)
    return sheet, info


def save_sheet_pdf(sheet_or_sheets, out_path: str, dpi: int = 300) -> str:
    """
    Saves a single sheet Image or list of sheet Images as a single-page or multi-page PDF.
    """
    if isinstance(sheet_or_sheets, (list, tuple)):
        sheets = list(sheet_or_sheets)
    else:
        sheets = [sheet_or_sheets]
    if not sheets:
        return out_path
    if len(sheets) == 1:
        sheets[0].save(out_path, "PDF", resolution=float(dpi))
    else:
        sheets[0].save(out_path, "PDF", resolution=float(dpi), save_all=True, append_images=sheets[1:])
    return out_path


save_sheets_pdf = save_sheet_pdf
