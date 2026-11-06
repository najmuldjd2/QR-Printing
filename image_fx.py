"""
Enhanced Image Processing Engine:
1. CamScanner-style enhancement (Magic Color, B&W Document, Clean White Flattening)
2. Advanced Background Removal & Replacement (Remove.bg style)
3. Formal Suit/Dress overlay for passport photos
4. Individual Crop & Perspective adjustments
"""
import io
import os
import re
from typing import Optional, Tuple, Dict, List
import numpy as np
from PIL import Image, ImageOps, ImageEnhance, ImageFilter, ImageDraw

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SUITS_DIR = os.path.join(STATIC_DIR, "suits")

def camscanner_process(
    img: Image.Image,
    mode: str = "magic_color",
    contrast: float = 1.3,
    brightness: float = 1.08,
    strength: str = "normal"
) -> Image.Image:
    """
    Pro CamScanner document & ID card enhancement algorithm:
    - Eliminates shadow and uneven yellow/dark background (pure #FFFFFF white paper clipping)
    - Deepens faint ink/text while preserving color saturation and sharp edges
    - Supports strength: 'light' | 'normal' | 'strong'
    - Supports mode: 'magic_color' | 'bw_doc' | 'hd_clear' | 'grayscale' | 'original'
    """
    img = img.convert("RGB")
    w, h = img.size

    if mode == "original":
        return img

    # Fast illumination estimation: downscale 4x before GaussianBlur, then upscale back
    gray = ImageOps.grayscale(img)
    gray_arr = np.array(gray, dtype=np.float32)
    small_w, small_h = max(32, w // 4), max(32, h // 4)
    gray_small = gray.resize((small_w, small_h), Image.Resampling.BILINEAR)
    small_radius = max(5, min(small_w, small_h) // 18)
    bg_small = gray_small.filter(ImageFilter.GaussianBlur(radius=small_radius))
    bg = np.array(bg_small.resize((w, h), Image.Resampling.BILINEAR), dtype=np.float32)

    if mode == "magic_color":
        # Normalize uneven lighting
        flattened = np.clip((gray_arr / (bg + 1.0)) * 255.0, 0.0, 255.0)
        # White-point & black-point stretch (clips shadow haze to pure 255 white & deepens ink)
        if strength == "strong":
            black_pt, white_pt = 68.0, 218.0
        elif strength == "light":
            black_pt, white_pt = 28.0, 238.0
        else:
            black_pt, white_pt = 48.0, 228.0

        stretched = np.clip((flattened - black_pt) * (255.0 / max(10.0, white_pt - black_pt)), 0.0, 255.0)
        arr = np.array(img, dtype=np.float32)
        norm_factor = (stretched / (gray_arr + 1.0))[:, :, np.newaxis]
        enhanced = np.clip(arr * norm_factor, 0.0, 255.0)

        # Ensure paper background areas (where stretched >= 250) become clean pure white
        paper_alpha = np.clip((stretched - 235.0) / 20.0, 0.0, 1.0)[:, :, np.newaxis]
        enhanced = enhanced * (1.0 - paper_alpha) + 255.0 * paper_alpha

        res = Image.fromarray(enhanced.astype(np.uint8), mode="RGB")
        res = ImageEnhance.Color(res).enhance(1.22 if strength != "light" else 1.12)
        res = res.filter(ImageFilter.UnsharpMask(radius=1.4, percent=155, threshold=3))
        return res

    elif mode == "bw_doc":
        # Adaptive local ratio thresholding with anti-aliased transition (pure white paper + crisp black text)
        ratio = gray_arr / (bg + 1.0)
        if strength == "strong":
            low_t, high_t = 0.78, 0.92
        elif strength == "light":
            low_t, high_t = 0.66, 0.85
        else:
            low_t, high_t = 0.72, 0.89

        bw_float = np.clip((ratio - low_t) / max(0.05, high_t - low_t) * 255.0, 0.0, 255.0).astype(np.uint8)
        res = Image.fromarray(bw_float, mode="L")
        res = res.filter(ImageFilter.UnsharpMask(radius=1.2, percent=170, threshold=4))
        return res.convert("RGB")

    elif mode == "hd_clear":
        # Gentle illumination balance + high sharpness (ideal for ID cards with photos & colorful certificates)
        flattened = np.clip((gray_arr / (bg + 1.0)) * 255.0, 0.0, 255.0)
        blend_lum = 0.55 * gray_arr + 0.45 * np.clip((flattened - 20.0) * (255.0 / 225.0), 0.0, 255.0)
        arr = np.array(img, dtype=np.float32)
        norm_factor = (blend_lum / (gray_arr + 1.0))[:, :, np.newaxis]
        enhanced = np.clip(arr * norm_factor, 0.0, 255.0).astype(np.uint8)
        res = Image.fromarray(enhanced, mode="RGB")
        res = ImageOps.autocontrast(res, cutoff=(1, 1))
        res = ImageEnhance.Color(res).enhance(1.15)
        res = res.filter(ImageFilter.UnsharpMask(radius=1.3, percent=145, threshold=2))
        return res

    elif mode == "grayscale":
        flattened = np.clip((gray_arr / (bg + 1.0)) * 255.0, 0.0, 255.0)
        blend = (0.4 * gray_arr + 0.6 * flattened).astype(np.uint8)
        res = Image.fromarray(blend, mode="L")
        res = ImageOps.autocontrast(res, cutoff=(2, 2))
        res = res.filter(ImageFilter.UnsharpMask(radius=1.3, percent=140, threshold=3))
        return res.convert("RGB")

    return img


def advanced_remove_background(img: Image.Image, tol: float = 35.0) -> Tuple[Image.Image, Image.Image]:
    """
    Remove.bg style foreground segmentation:
    Returns (rgba_image_with_transparent_bg, alpha_mask)
    """
    small = img.copy()
    small.thumbnail((600, 600), Image.Resampling.BILINEAR)
    arr = np.asarray(small.convert("RGB"), dtype=np.float32)
    h, w, _ = arr.shape

    # Sample borders & corners as background
    t = max(2, int(0.04 * min(h, w)))
    side_h = max(t, int(h * 0.75))
    border = np.concatenate([
        arr[:t].reshape(-1, 3),
        arr[:side_h, :t].reshape(-1, 3),
        arr[:side_h, -t:].reshape(-1, 3)
    ])
    bg_median = np.median(border, axis=0)
    bg_std = np.std(border, axis=0) + 10.0

    # Color distance
    dist = np.sqrt(np.sum(((arr - bg_median) / bg_std) ** 2, axis=2))
    bg_seed = dist < (tol / 18.0)

    # Flood grow from top/sides
    reach = np.zeros_like(bg_seed)
    reach[0, :] = bg_seed[0, :]
    reach[:side_h, 0] = bg_seed[:side_h, 0]
    reach[:side_h, -1] = bg_seed[:side_h, -1]

    for i in range(1200):
        grown = reach.copy()
        grown[1:] |= reach[:-1]
        grown[:-1] |= reach[1:]
        grown[:, 1:] |= reach[:, :-1]
        grown[:, :-1] |= reach[:, 1:]
        grown &= bg_seed
        if i % 8 == 7 and np.array_equal(grown, reach):
            reach = grown
            break
        reach = grown

    fg_mask = (~reach).astype(np.uint8) * 255
    mask_im = Image.fromarray(fg_mask, mode="L").filter(ImageFilter.GaussianBlur(1.2))
    mask_full = mask_im.resize(img.size, Image.Resampling.BICUBIC)

    rgba = img.convert("RGBA")
    rgba.putalpha(mask_full)
    return rgba, mask_full


def apply_dress_overlay(
    photo: Image.Image,
    suit_id: str,
    suit_zoom: float = 1.0,
    suit_ox: float = 0.0,
    suit_oy: float = 0.0
) -> Image.Image:
    """
    Overlays a formal suit/coat/blazer onto a passport photo at exact neck/shoulder alignment.
    suit_id: 'suit1', 'suit2', 'suit3', 'suit4'
    """
    svg_path = os.path.join(SUITS_DIR, f"{suit_id}.svg")
    if not os.path.exists(svg_path):
        return photo

    # We can rasterize SVG or load PNG/Pillow drawings
    # Since Cairo/librsvg may not be on Windows, we convert/render via Pillow directly:
    try:
        suit_img = render_suit_pillow(suit_id, photo.size)
    except Exception as e:
        print(f"[Dress] Error rendering suit: {e}")
        return photo

    # Scale and translate suit
    pw, ph = photo.size
    sw = int(pw * 1.05 * suit_zoom)
    sh = int(ph * 0.9 * suit_zoom)
    suit_resized = suit_img.resize((max(10, sw), max(10, sh)), Image.Resampling.LANCZOS)

    # Base position: anchor at bottom center
    sx = int((pw - sw) / 2 + suit_ox * (pw / 2))
    sy = int(ph * 0.45 + suit_oy * (ph / 2))

    res = photo.copy().convert("RGBA")
    res.paste(suit_resized, (sx, sy), suit_resized)
    return res.convert("RGB")


def render_suit_pillow(suit_id: str, target_size: Tuple[int, int]) -> Image.Image:
    """Procedurally renders high-quality crisp suits directly with Pillow (independent of system fonts/cairo)."""
    w, h = 400, 400
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)

    if suit_id == "suit2":  # Navy Blue Suit + Gold Tie
        # Shirt
        draw.polygon([(150, 110), (250, 110), (215, 280), (185, 280)], fill="#e0f2fe", outline="#93c5fd")
        draw.polygon([(150, 110), (185, 150), (160, 180)], fill="#f0f9ff", outline="#bae6fd")
        draw.polygon([(250, 110), (215, 150), (240, 180)], fill="#f0f9ff", outline="#bae6fd")
        # Tie
        draw.polygon([(190, 140), (210, 140), (216, 165), (184, 165)], fill="#f59e0b")
        draw.polygon([(186, 165), (214, 165), (220, 340), (200, 375), (180, 340)], fill="#d97706", outline="#78350f")
        # Coat
        draw.polygon([(150, 110), (40, 165), (10, 400), (390, 400), (360, 165), (250, 110), (225, 230), (200, 280), (175, 230)], fill="#1e3a8a")
        # Lapels
        draw.polygon([(150, 110), (90, 180), (165, 225), (180, 270), (200, 280), (190, 240), (160, 170)], fill="#1e40af", outline="#172554")
        draw.polygon([(250, 110), (310, 180), (235, 225), (220, 270), (200, 280), (210, 240), (240, 170)], fill="#1e40af", outline="#172554")
    elif suit_id == "suit3":  # Formal Crisp White Shirt
        draw.polygon([(140, 105), (45, 170), (15, 400), (385, 400), (355, 170), (260, 105), (225, 230), (175, 230)], fill="#ffffff", outline="#cbd5e1")
        draw.rectangle([(188, 160), (212, 400)], fill="#f8fafc", outline="#cbd5e1")
        for cy in (205, 265, 325, 385):
            draw.ellipse([(196, cy - 4), (204, cy + 4)], fill="#ffffff", outline="#94a3b8")
        draw.polygon([(140, 105), (190, 165), (175, 185), (130, 120)], fill="#ffffff", outline="#cbd5e1")
        draw.polygon([(260, 105), (210, 165), (225, 185), (270, 120)], fill="#ffffff", outline="#cbd5e1")
    elif suit_id == "suit4":  # Charcoal Executive Blazer
        draw.polygon([(145, 115), (255, 115), (210, 260), (190, 260)], fill="#f8fafc", outline="#e2e8f0")
        draw.polygon([(145, 115), (45, 165), (20, 400), (380, 400), (355, 165), (255, 115), (220, 240), (180, 240)], fill="#1e293b")
        draw.polygon([(145, 115), (95, 185), (170, 225), (185, 280), (200, 290), (190, 245), (160, 175)], fill="#334155", outline="#0f172a")
        draw.polygon([(255, 115), (305, 185), (230, 225), (215, 280), (200, 290), (210, 245), (240, 175)], fill="#334155", outline="#0f172a")
    else:  # suit1: Classic Black Tuxedo + Red Tie
        draw.polygon([(150, 110), (250, 110), (215, 280), (185, 280)], fill="#f8fafc", outline="#cbd5e1")
        draw.polygon([(150, 110), (185, 150), (160, 180)], fill="#ffffff", outline="#cbd5e1")
        draw.polygon([(250, 110), (215, 150), (240, 180)], fill="#ffffff", outline="#cbd5e1")
        draw.polygon([(190, 140), (210, 140), (216, 165), (184, 165)], fill="#b91c1c")
        draw.polygon([(186, 165), (214, 165), (220, 340), (200, 375), (180, 340)], fill="#991b1b", outline="#450a0a")
        draw.polygon([(150, 110), (40, 165), (10, 400), (390, 400), (360, 165), (250, 110), (225, 230), (200, 280), (175, 230)], fill="#0f172a")
        draw.polygon([(150, 110), (90, 180), (165, 225), (180, 270), (200, 280), (190, 240), (160, 170)], fill="#1e293b", outline="#020617")
        draw.polygon([(250, 110), (310, 180), (235, 225), (220, 270), (200, 280), (210, 240), (240, 170)], fill="#1e293b", outline="#020617")
        draw.rectangle([(75, 230), (110, 234)], fill="#020617")
        draw.polygon([(80, 230), (90, 218), (100, 230)], fill="#ffffff")

    return im


def order_quad_points(pts: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Orders 4 points into [Top-Left, Top-Right, Bottom-Right, Bottom-Left]."""
    pts_sorted_y = sorted(pts, key=lambda p: (p[1], p[0]))
    top_two = sorted(pts_sorted_y[:2], key=lambda p: p[0])
    bot_two = sorted(pts_sorted_y[2:], key=lambda p: p[0])
    tl, tr = top_two[0], top_two[1]
    bl, br = bot_two[0], bot_two[1]
    return [tl, tr, br, bl]


def perspective_crop_4points(
    img: Image.Image,
    norm_points: List[Tuple[float, float]],
    target_ratio: Optional[float] = None,
    max_out_dim: Optional[int] = None
) -> Image.Image:
    """
    CamScanner-style 4-point perspective warp (homography transform):
    Takes 4 normalized corner coordinates (0..1) of an uneven/skewed document or card
    and warps the quadrilateral into a flat, straight rectangle.
    """
    w, h = img.size
    pixel_pts = [
        (max(0.0, min(float(w), float(px) * w)), max(0.0, min(float(h), float(py) * h)))
        for px, py in norm_points[:4]
    ]
    tl, tr, br, bl = order_quad_points(pixel_pts)

    w_top = float(np.hypot(tr[0] - tl[0], tr[1] - tl[1]))
    w_bot = float(np.hypot(br[0] - bl[0], br[1] - bl[1]))
    h_left = float(np.hypot(bl[0] - tl[0], bl[1] - tl[1]))
    h_right = float(np.hypot(br[0] - tr[0], br[1] - tr[1]))

    out_w = max(24, int(round(max(w_top, w_bot))))
    out_h = max(24, int(round(max(h_left, h_right))))

    if target_ratio and target_ratio > 0:
        out_h = max(24, int(round(out_w / target_ratio)))

    if max_out_dim and max(out_w, out_h) > max_out_dim:
        scale = float(max_out_dim) / float(max(out_w, out_h))
        out_w = max(24, int(round(out_w * scale)))
        out_h = max(24, int(round(out_h * scale)))

    dst = [
        (0.0, 0.0),
        (float(out_w), 0.0),
        (float(out_w), float(out_h)),
        (0.0, float(out_h))
    ]
    src = [tl, tr, br, bl]

    matrix = []
    rhs = []
    for (X, Y), (x, y) in zip(dst, src):
        matrix.append([X, Y, 1.0, 0.0, 0.0, 0.0, -X * x, -Y * x])
        matrix.append([0.0, 0.0, 0.0, X, Y, 1.0, -X * y, -Y * y])
        rhs.append(x)
        rhs.append(y)

    coeffs = np.linalg.solve(np.array(matrix, dtype=np.float64), np.array(rhs, dtype=np.float64)).tolist()
    resample = Image.Resampling.BILINEAR if max_out_dim else Image.Resampling.BICUBIC
    warped = img.transform((out_w, out_h), Image.Transform.PERSPECTIVE, coeffs, resample)
    return warped


def _keep_largest_central_component(mask: np.ndarray) -> np.ndarray:
    """
    Filters a binary foreground mask to keep only the primary connected document/card component
    near the center of the frame, completely eliminating stray table dust, fingers, or border noise.
    """
    from collections import deque
    h, w = mask.shape
    step = max(2, min(h, w) // 56)
    small = mask[::step, ::step]
    sh, sw = small.shape
    visited = np.zeros_like(small, dtype=bool)
    best_comp = []
    best_score = -1.0
    cy, cx = sh * 0.5, sw * 0.5

    ys, xs = np.where(small)
    for y0, x0 in zip(ys, xs):
        if visited[y0, x0]:
            continue
        comp = []
        q = deque([(int(y0), int(x0))])
        visited[y0, x0] = True
        while q:
            cy_i, cx_i = q.popleft()
            comp.append((cy_i, cx_i))
            if cy_i > 0 and not visited[cy_i - 1, cx_i] and small[cy_i - 1, cx_i]:
                visited[cy_i - 1, cx_i] = True
                q.append((cy_i - 1, cx_i))
            if cy_i + 1 < sh and not visited[cy_i + 1, cx_i] and small[cy_i + 1, cx_i]:
                visited[cy_i + 1, cx_i] = True
                q.append((cy_i + 1, cx_i))
            if cx_i > 0 and not visited[cy_i, cx_i - 1] and small[cy_i, cx_i - 1]:
                visited[cy_i, cx_i - 1] = True
                q.append((cy_i, cx_i - 1))
            if cx_i + 1 < sw and not visited[cy_i, cx_i + 1] and small[cy_i, cx_i + 1]:
                visited[cy_i, cx_i + 1] = True
                q.append((cy_i, cx_i + 1))
        if len(comp) < 6:
            continue
        arr_c = np.array(comp, dtype=np.float32)
        mean_y, mean_x = float(np.mean(arr_c[:, 0])), float(np.mean(arr_c[:, 1]))
        dist_center = float(np.hypot((mean_x - cx) / sw, (mean_y - cy) / sh))
        score = len(comp) * max(0.25, 1.15 - dist_center * 1.4)
        if score > best_score:
            best_score = score
            best_comp = comp

    if not best_comp:
        return mask

    clean_small = np.zeros_like(small, dtype=np.uint8)
    for y_i, x_i in best_comp:
        clean_small[y_i, x_i] = 255
    pil_s = Image.fromarray(clean_small, mode="L").filter(ImageFilter.MaxFilter(3))
    full_gate = np.asarray(pil_s.resize((w, h), Image.Resampling.NEAREST)) > 127
    return mask & full_gate


def _fit_line_robust(u: np.ndarray, v: np.ndarray):
    """Two-pass outlier-trimmed linear regression v = m * u + c."""
    if len(u) < 12:
        return None
    m, c = np.polyfit(u, v, 1)
    res = np.abs(v - (m * u + c))
    inliers = res <= max(1.4, float(np.percentile(res, 72)))
    if int(np.sum(inliers)) >= 8:
        m, c = np.polyfit(u[inliers], v[inliers], 1)
        res = np.abs(v[inliers] - (m * u[inliers] + c))
    return (float(m), float(c), float(np.median(res)))


def _intersect_hv(line_h, line_v, sw: float, sh: float):
    """Intersects horizontal edge line (y = m_h*x + c_h) with vertical edge line (x = m_v*y + c_v)."""
    m_h, c_h, _ = line_h
    m_v, c_v, _ = line_v
    denom = 1.0 - m_h * m_v
    if abs(denom) < 1e-6:
        return None
    y = (m_h * c_v + c_h) / denom
    x = m_v * y + c_v
    return (float(x) / float(sw - 1), float(y) / float(sh - 1))


def detect_document_corners(img: Image.Image) -> List[Dict[str, float]]:
    """
    High-precision 4-stage document & ID card corner detector:
    1. Multi-strip border color modeling & Mahalanobis segmentation (with Sobel fallback for white-on-white).
    2. Morphological denoising + Largest Central Connected Component isolation (eliminates stray background noise).
    3. True extreme contour vertex localization (never pulls corners inward).
    4. Gradient-snapped 4-edge robust line fitting & intersection (eliminates drop shadows & recovers rounded NID corners).
    Returns 4 normalized points [{"x": .., "y": ..}, ...] in [TL, TR, BR, BL] order.
    """
    default_pts = [
        {"x": 0.07, "y": 0.07},
        {"x": 0.93, "y": 0.07},
        {"x": 0.93, "y": 0.93},
        {"x": 0.07, "y": 0.93}
    ]
    try:
        small = img.copy().convert("RGB")
        small.thumbnail((360, 360), Image.Resampling.BILINEAR)
        arr = np.asarray(small, dtype=np.float32)
        sh, sw, _ = arr.shape
        if sh < 30 or sw < 30:
            return default_pts

        # 1. Sample 4 border strips; discard 1 outlier strip if the document touches one edge of the frame
        t = max(2, int(0.04 * min(sh, sw)))
        strips = [
            arr[:t, :, :].reshape(-1, 3),
            arr[-t:, :, :].reshape(-1, 3),
            arr[:, :t, :].reshape(-1, 3),
            arr[:, -t:, :].reshape(-1, 3)
        ]
        strip_meds = np.array([np.median(s, axis=0) for s in strips])
        global_med = np.median(strip_meds, axis=0)
        strip_dists = np.linalg.norm(strip_meds - global_med, axis=1)
        best_indices = np.argsort(strip_dists)[:3]
        border = np.concatenate([strips[i] for i in best_indices], axis=0)

        bg_med = np.median(border, axis=0)
        bg_std = np.std(border, axis=0) + 12.0

        dist = np.sqrt(np.sum(((arr - bg_med) / bg_std) ** 2, axis=2))
        mask = dist > 1.85

        # Morphological opening (3x3) to remove single-pixel grain
        mask_img = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
        mask_img = mask_img.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
        mask = np.asarray(mask_img) > 127

        # Keep only the primary central connected component
        mask = _keep_largest_central_component(mask)

        # Compute directional image gradients for edge snapping & white-on-white fallback
        gray = np.mean(arr, axis=2)
        gx = np.abs(np.gradient(gray, axis=1))
        gy = np.abs(np.gradient(gray, axis=0))

        fg_ratio = float(np.mean(mask))
        if fg_ratio < 0.07 or fg_ratio > 0.95:
            # Fallback for white paper on white table: use closed strong Sobel edge contour
            grad = np.hypot(gx, gy)
            g_mask = grad > max(10.0, float(np.percentile(grad, 88)))
            g_mask[:2, :] = g_mask[-2:, :] = g_mask[:, :2] = g_mask[:, -2:] = False
            g_pil = Image.fromarray((g_mask.astype(np.uint8) * 255), mode="L")
            g_pil = g_pil.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
            mask = _keep_largest_central_component(np.asarray(g_pil) > 127)
            fg_ratio = float(np.mean(mask))
            if fg_ratio < 0.05 or fg_ratio > 0.95:
                return default_pts

        ys, xs = np.where(mask)
        if len(xs) < 50:
            return default_pts

        nx = xs.astype(np.float32) / float(sw - 1)
        ny = ys.astype(np.float32) / float(sh - 1)

        # 3. Exact extreme vertices along diagonal axes (x+y and x-y)
        s = nx + ny
        d = nx - ny

        idx_tl = int(np.argmin(s))
        idx_br = int(np.argmax(s))
        idx_tr = int(np.argmax(d))
        idx_bl = int(np.argmin(d))

        v_tl = (float(nx[idx_tl]), float(ny[idx_tl]))
        v_tr = (float(nx[idx_tr]), float(ny[idx_tr]))
        v_br = (float(nx[idx_br]), float(ny[idx_br]))
        v_bl = (float(nx[idx_bl]), float(ny[idx_bl]))

        pts = [v_tl, v_tr, v_br, v_bl]

        # 4. Gradient-snapped 4-edge line intersection refinement (recovers rounded ID card corners & removes shadow bleed)
        x_left_est = max(v_tl[0], v_bl[0])
        x_right_est = min(v_tr[0], v_br[0])
        y_top_est = max(v_tl[1], v_tr[1])
        y_bot_est = min(v_bl[1], v_br[1])

        if (x_right_est - x_left_est) > 0.18 and (y_bot_est - y_top_est) > 0.18:
            x_lo = int((x_left_est * 0.72 + x_right_est * 0.28) * (sw - 1))
            x_hi = int((x_left_est * 0.28 + x_right_est * 0.72) * (sw - 1))
            y_lo = int((y_top_est * 0.72 + y_bot_est * 0.28) * (sh - 1))
            y_hi = int((y_top_est * 0.28 + y_bot_est * 0.72) * (sh - 1))

            top_x, top_y, bot_x, bot_y = [], [], [], []
            for x in range(max(2, x_lo), min(sw - 2, x_hi)):
                col = np.where(mask[:, x])[0]
                if len(col) > 4:
                    rt, rb = int(col[0]), int(col[-1])
                    # Snap within [-2, +5] inward to sharpest vertical gradient (ignores soft drop shadow)
                    t_lo, t_hi = max(1, rt - 2), min(sh - 2, rt + 5)
                    b_lo, b_hi = max(1, rb - 5), min(sh - 2, rb + 2)
                    st = t_lo + int(np.argmax(gy[t_lo:t_hi + 1, x]))
                    sb = b_lo + int(np.argmax(gy[b_lo:b_hi + 1, x]))
                    top_x.append(x); top_y.append(st)
                    bot_x.append(x); bot_y.append(sb)

            left_y, left_x, right_y, right_x = [], [], [], []
            for y in range(max(2, y_lo), min(sh - 2, y_hi)):
                row = np.where(mask[y, :])[0]
                if len(row) > 4:
                    rl, rr = int(row[0]), int(row[-1])
                    l_lo, l_hi = max(1, rl - 2), min(sw - 2, rl + 5)
                    r_lo, r_hi = max(1, rr - 5), min(sw - 2, rr + 2)
                    sl = l_lo + int(np.argmax(gx[y, l_lo:l_hi + 1]))
                    sr = r_lo + int(np.argmax(gx[y, r_lo:r_hi + 1]))
                    left_y.append(y); left_x.append(sl)
                    right_y.append(y); right_x.append(sr)

            lt = _fit_line_robust(np.array(top_x, dtype=np.float32), np.array(top_y, dtype=np.float32))
            lb = _fit_line_robust(np.array(bot_x, dtype=np.float32), np.array(bot_y, dtype=np.float32))
            ll = _fit_line_robust(np.array(left_y, dtype=np.float32), np.array(left_x, dtype=np.float32))
            lr = _fit_line_robust(np.array(right_y, dtype=np.float32), np.array(right_x, dtype=np.float32))

            if lt and lb and ll and lr and max(lt[2], lb[2], ll[2], lr[2]) <= 2.2:
                cand_corners = [
                    _intersect_hv(lt, ll, sw, sh),
                    _intersect_hv(lt, lr, sw, sh),
                    _intersect_hv(lb, lr, sw, sh),
                    _intersect_hv(lb, ll, sw, sh)
                ]
                refined = []
                for v_pt, c_pt in zip(pts, cand_corners):
                    if c_pt is not None and np.hypot(c_pt[0] - v_pt[0], c_pt[1] - v_pt[1]) <= 0.065:
                        refined.append(c_pt)
                    else:
                        refined.append(v_pt)
                pts = refined

        pts = [
            (float(np.clip(p[0], 0.015, 0.985)), float(np.clip(p[1], 0.015, 0.985)))
            for p in pts
        ]
        ordered = order_quad_points(pts)
        return [{"x": round(p[0], 4), "y": round(p[1], 4)} for p in ordered]
    except Exception:
        return default_pts
