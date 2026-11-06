import base64
import csv
import html as _html
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageOps

try:
    import pymupdf as _fitz  # type: ignore
except Exception:
    try:
        import fitz as _fitz  # type: ignore
    except Exception:
        _fitz = None

WORD_EXTENSIONS = {"docx", "doc", "rtf", "txt"}
EXCEL_EXTENSIONS = {"xlsx", "xls", "csv"}
PPT_EXTENSIONS = {"pptx", "ppt"}
OFFICE_EXTENSIONS = WORD_EXTENSIONS | EXCEL_EXTENSIONS | PPT_EXTENSIONS

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
WP_NS = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _clear_nup_caches_for(pdf_path: str) -> None:
    folder = os.path.dirname(pdf_path)
    base = os.path.basename(pdf_path)
    for nup in (2, 4):
        cpath = os.path.join(folder, f"pv_nup{nup}_{base}")
        if os.path.isfile(cpath):
            try:
                os.remove(cpath)
            except OSError:
                pass


def render_pdf_page_pil(pdf_path: str, page_num: int = 1, dpi: int = 200) -> Image.Image:
    """Renders a 1-based page of a PDF file to a PIL RGB Image."""
    if _fitz is None:
        raise RuntimeError("PDF renderer not available")
    doc = _fitz.open(pdf_path)
    try:
        if len(doc) == 0:
            raise ValueError("Empty PDF")
        idx = max(0, min(len(doc) - 1, int(page_num or 1) - 1))
        pix = doc[idx].get_pixmap(dpi=dpi, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def replace_pdf_page_pil(pdf_path: str, page_num: int, img: Image.Image) -> None:
    """Replaces a single 1-based page inside an existing PDF with the given PIL Image."""
    if _fitz is None:
        raise RuntimeError("PDF renderer not available")
    img = ImageOps.exif_transpose(img).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=93)
    img_bytes = buf.getvalue()

    doc = _fitz.open(pdf_path)
    try:
        idx = max(0, min(len(doc) - 1, int(page_num or 1) - 1))
        old_rect = doc[idx].rect
        orig_w, orig_h = float(old_rect.width) or 595.0, float(old_rect.height) or 842.0
        if (img.width > img.height * 1.15 and orig_h > orig_w) or (img.height > img.width * 1.15 and orig_w > orig_h):
            pw, ph = orig_h, orig_w
        else:
            pw, ph = orig_w, orig_h

        doc.delete_page(idx)
        new_page = doc.new_page(pno=idx, width=pw, height=ph)
        margin = 14.0
        avail_w = max(50.0, pw - margin * 2)
        avail_h = max(50.0, ph - margin * 2)
        scale = min(avail_w / max(1, img.width), avail_h / max(1, img.height))
        dw = img.width * scale
        dh = img.height * scale
        x0 = (pw - dw) * 0.5
        y0 = (ph - dh) * 0.5
        target_rect = _fitz.Rect(x0, y0, x0 + dw, y0 + dh)
        new_page.insert_image(target_rect, stream=img_bytes)

        tmp_path = pdf_path + ".page.tmp"
        doc.save(tmp_path, garbage=2, deflate=True)
    finally:
        doc.close()

    os.replace(tmp_path, pdf_path)
    _clear_nup_caches_for(pdf_path)


def apply_camscan_to_all_pdf_pages(orig_pdf_path: str, dest_pdf_path: str, mode: str, strength: str = "normal") -> None:
    """Applies CamScanner processing to all pages of a PDF (up to 40 pages)."""
    import image_fx
    if _fitz is None:
        raise RuntimeError("PDF renderer not available")
    src_doc = _fitz.open(orig_pdf_path)
    out_doc = _fitz.open()
    try:
        total = min(len(src_doc), 40)
        for idx in range(total):
            page = src_doc[idx]
            rect = page.rect
            pw, ph = float(rect.width) or 595.0, float(rect.height) or 842.0
            pix = page.get_pixmap(dpi=180, alpha=False)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            if mode != "original":
                im = image_fx.camscanner_process(im, mode=mode, strength=strength)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
            new_p = out_doc.new_page(width=pw, height=ph)
            new_p.insert_image(_fitz.Rect(0, 0, pw, ph), stream=buf.getvalue())
        tmp_path = dest_pdf_path + ".allscan.tmp"
        out_doc.save(tmp_path, garbage=2, deflate=True)
    finally:
        src_doc.close()
        out_doc.close()

    os.replace(tmp_path, dest_pdf_path)
    _clear_nup_caches_for(dest_pdf_path)


# ── Tier 1: Native Office / LibreOffice Exact PDF Export (When Installed) ──

def _try_native_office_to_pdf(raw_path: str, ext: str, pdf_out_path: str) -> bool:
    """
    Attempts 100% pixel-exact conversion using Microsoft Office COM (in-process win32com or PowerShell)
    or headless LibreOffice (Linux/Windows). Returns True if pdf_out_path was successfully created.
    """
    abs_in = os.path.abspath(raw_path)
    abs_out = os.path.abspath(pdf_out_path)
    out_dir = os.path.dirname(abs_out)

    # 1. On Windows, try direct in-process Microsoft Office COM automation first (fastest & most reliable)
    if sys.platform == "win32" and ext in ("doc", "docx", "rtf", "xls", "xlsx", "ppt", "pptx"):
        try:
            import win32com.client
            import pythoncom
            pythoncom.CoInitialize()
            try:
                if ext in ("doc", "docx", "rtf"):
                    word = win32com.client.DispatchEx("Word.Application")
                    word.Visible = False
                    word.DisplayAlerts = 0
                    try:
                        doc = word.Documents.Open(abs_in, False, True)
                        doc.ExportAsFixedFormat(abs_out, 17)  # 17 = wdExportFormatPDF
                        doc.Close(0)
                        if os.path.isfile(abs_out) and os.path.getsize(abs_out) > 200:
                            return True
                    finally:
                        word.Quit()
                elif ext in ("xls", "xlsx"):
                    excel = win32com.client.DispatchEx("Excel.Application")
                    excel.Visible = False
                    excel.DisplayAlerts = False
                    try:
                        wb = excel.Workbooks.Open(abs_in, 0, True)
                        for ws in wb.Worksheets:
                            try:
                                ws.PageSetup.PrintGridlines = True
                            except Exception:
                                pass
                        wb.ExportAsFixedFormat(0, abs_out)
                        wb.Close(False)
                        if os.path.isfile(abs_out) and os.path.getsize(abs_out) > 200:
                            return True
                    finally:
                        excel.Quit()
                elif ext in ("ppt", "pptx"):
                    ppt = win32com.client.DispatchEx("PowerPoint.Application")
                    try:
                        pr = ppt.Presentations.Open(abs_in, -1, 0, 0)
                        pr.SaveAs(abs_out, 32)  # 32 = ppSaveAsPDF
                        pr.Close()
                        if os.path.isfile(abs_out) and os.path.getsize(abs_out) > 200:
                            return True
                    finally:
                        ppt.Quit()
            finally:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
        except Exception:
            pass

    # 2. Try LibreOffice / soffice if available (Linux or Windows without Office)
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        try:
            subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", out_dir, abs_in],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=35,
                check=False,
            )
            expected_pdf = os.path.join(out_dir, os.path.splitext(os.path.basename(abs_in))[0] + ".pdf")
            if os.path.isfile(expected_pdf) and os.path.getsize(expected_pdf) > 200:
                os.replace(expected_pdf, abs_out)
                return True
        except Exception:
            pass

    # 3. Fallback on Windows: PowerShell COM automation
    if sys.platform == "win32" and ext in ("doc", "docx", "rtf", "xls", "xlsx", "ppt", "pptx"):
        ps_in = abs_in.replace("'", "''")
        ps_out = abs_out.replace("'", "''")
        if ext in ("doc", "docx", "rtf"):
            ps_script = (
                f"$w = New-Object -ComObject Word.Application; $w.Visible = $false; $w.DisplayAlerts = 0; "
                f"try {{ $d = $w.Documents.Open('{ps_in}', $false, $true); $d.ExportAsFixedFormat('{ps_out}', 17); $d.Close(0) }} "
                f"finally {{ $w.Quit() }}"
            )
        elif ext in ("xls", "xlsx"):
            ps_script = (
                f"$e = New-Object -ComObject Excel.Application; $e.Visible = $false; $e.DisplayAlerts = $false; "
                f"try {{ $wb = $e.Workbooks.Open('{ps_in}', 0, $true); "
                f"foreach ($s in $wb.Worksheets) {{ $s.PageSetup.PrintGridlines = $true }}; "
                f"$wb.ExportAsFixedFormat(0, '{ps_out}'); $wb.Close($false) }} "
                f"finally {{ $e.Quit() }}"
            )
        else:
            ps_script = (
                f"$p = New-Object -ComObject PowerPoint.Application; "
                f"try {{ $pr = $p.Presentations.Open('{ps_in}', -1, 0, 0); $pr.SaveAs('{ps_out}', 32); $pr.Close() }} "
                f"finally {{ $p.Quit() }}"
            )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            if os.path.isfile(abs_out) and os.path.getsize(abs_out) > 200:
                return True
        except Exception:
            pass

    return False


# ── Tier 2: High-Fidelity DOCX / DOC / RTF / TXT Parser (Preserves Tables, Borders, Colors, Widths) ──

def _clean_hex_color(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    v = val.strip().lstrip("#")
    if v.lower() == "auto" or len(v) != 6:
        return None
    if all(c in "0123456789abcdefABCDEF" for c in v):
        return f"#{v}"
    return None


def _parse_docx_numbering(zf: zipfile.ZipFile) -> Any:
    """
    Parses word/numbering.xml and returns a stateful closure `get_prefix(numId, ilvl)`
    that accurately resolves multilevel auto-numbering (e.g. '1. ', '2. ', 'a. ', 'i. ', '• ').
    """
    if "word/numbering.xml" not in zf.namelist():
        return lambda numId, ilvl: ""
    try:
        num_root = ET.fromstring(zf.read("word/numbering.xml"))
    except Exception:
        return lambda numId, ilvl: ""

    num_to_abstract: Dict[str, str] = {}
    for num in num_root.findall(f"{W_NS}num"):
        num_id = num.attrib.get(f"{W_NS}numId")
        ab_el = num.find(f"{W_NS}abstractNumId")
        if num_id and ab_el is not None:
            num_to_abstract[num_id] = ab_el.attrib.get(f"{W_NS}val", "")

    levels: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for ab in num_root.findall(f"{W_NS}abstractNum"):
        ab_id = ab.attrib.get(f"{W_NS}abstractNumId", "")
        for lvl in ab.findall(f"{W_NS}lvl"):
            ilvl_str = lvl.attrib.get(f"{W_NS}ilvl", "0")
            numFmt_el = lvl.find(f"{W_NS}numFmt")
            lvlText_el = lvl.find(f"{W_NS}lvlText")
            start_el = lvl.find(f"{W_NS}start")

            numFmt = numFmt_el.attrib.get(f"{W_NS}val", "decimal") if numFmt_el is not None else "decimal"
            lvlText = lvlText_el.attrib.get(f"{W_NS}val", "%1.") if lvlText_el is not None else "%1."
            try:
                start = int(start_el.attrib.get(f"{W_NS}val", "1") or 1) if start_el is not None else 1
            except Exception:
                start = 1
            levels[(ab_id, int(ilvl_str))] = {
                "numFmt": numFmt,
                "lvlText": lvlText,
                "start": start
            }

    counters: Dict[Tuple[str, int], int] = {}

    def format_num_val(val: int, fmt: str) -> str:
        fmt = (fmt or "").lower()
        if fmt == "decimal":
            return str(val)
        if fmt == "lowerletter":
            return chr(ord('a') + max(0, val - 1) % 26)
        if fmt == "upperletter":
            return chr(ord('A') + max(0, val - 1) % 26)
        if fmt == "lowerroman":
            romans = [(10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
            res = ""
            v = max(1, val)
            for n, r in romans:
                while v >= n:
                    res += r
                    v -= n
            return res or "i"
        if fmt == "upperroman":
            romans = [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
            res = ""
            v = max(1, val)
            for n, r in romans:
                while v >= n:
                    res += r
                    v -= n
            return res or "I"
        if fmt == "decimalzero":
            return f"{val:02d}"
        if fmt in ("bullet", "none"):
            return "•"
        return str(val)

    def get_numbering_prefix(numId_str: Optional[str], ilvl_str: Optional[str]) -> str:
        if not numId_str or numId_str == "0":
            return ""
        ab_id = num_to_abstract.get(numId_str)
        if not ab_id:
            return ""
        try:
            ilvl = int(ilvl_str or 0)
        except Exception:
            ilvl = 0

        lvl_info = levels.get((ab_id, ilvl))
        if not lvl_info:
            return ""

        key = (numId_str, ilvl)
        if key not in counters:
            counters[key] = lvl_info["start"]
        else:
            counters[key] += 1

        curr_val = counters[key]
        fmt = lvl_info["numFmt"].lower()
        tmpl = lvl_info["lvlText"]

        if fmt in ("bullet", "none"):
            return "• "

        formatted = tmpl
        for lvl_idx in range(9):
            placeholder = f"%{lvl_idx + 1}"
            if placeholder in formatted:
                sub_val = counters.get((numId_str, lvl_idx), 1) if lvl_idx == ilvl else 1
                sub_info = levels.get((ab_id, lvl_idx), lvl_info)
                formatted = formatted.replace(placeholder, format_num_val(sub_val, sub_info["numFmt"]))

        return (formatted + " ").strip() + " "

    return get_numbering_prefix


def _parse_docx_to_html_and_meta(path: str) -> Tuple[str, str, Tuple[float, float, float, float]]:
    """
    Extracts paragraphs, headings, font sizes, colors, shading, tables (with exact gridCol widths,
    colspan, rowspan, cell background colors, and borders), embedded images, auto-numbering,
    margins, and line-spacing from a .docx file.
    Returns (html_body, orientation, margins_pt).
    """
    parts: List[str] = []
    orientation = "portrait"
    margins_pt: Tuple[float, float, float, float] = (54.0, 54.0, 54.0, 54.0)
    try:
        with zipfile.ZipFile(path, "r") as zf:
            get_numbering_prefix = _parse_docx_numbering(zf)
            style_num_map: Dict[str, Tuple[str, str]] = {}
            if "word/styles.xml" in zf.namelist():
                try:
                    s_root = ET.fromstring(zf.read("word/styles.xml"))
                    for style in s_root.findall(f"{W_NS}style"):
                        s_id = style.attrib.get(f"{W_NS}styleId", "").lower()
                        pPr_s = style.find(f"{W_NS}pPr")
                        if pPr_s is not None:
                            numPr_s = pPr_s.find(f"{W_NS}numPr")
                            if numPr_s is not None:
                                il_el = numPr_s.find(f"{W_NS}ilvl")
                                nid_el = numPr_s.find(f"{W_NS}numId")
                                il = il_el.attrib.get(f"{W_NS}val", "0") if il_el is not None else "0"
                                nid = nid_el.attrib.get(f"{W_NS}val", "0") if nid_el is not None else "0"
                                style_num_map[s_id] = (nid, il)
                except Exception:
                    pass

            rels_map: Dict[str, str] = {}
            if "word/_rels/document.xml.rels" in zf.namelist():
                try:
                    rel_root = ET.fromstring(zf.read("word/_rels/document.xml.rels"))
                    for rel in rel_root:
                        rid = rel.attrib.get("Id", "")
                        target = rel.attrib.get("Target", "")
                        if rid and target:
                            rels_map[rid] = target
                except Exception:
                    pass

            xml_bytes = zf.read("word/document.xml")
            root = ET.fromstring(xml_bytes)
            body = root.find(f"{W_NS}body")
            if body is None:
                body = root

            # Check section page orientation (<w:pgSz w:orient="landscape"/>)
            for pgSz in body.iter(f"{W_NS}pgSz"):
                orient_attr = pgSz.attrib.get(f"{W_NS}orient", "").lower()
                w_attr = int(pgSz.attrib.get(f"{W_NS}w", "11906") or 11906)
                h_attr = int(pgSz.attrib.get(f"{W_NS}h", "16838") or 16838)
                if orient_attr == "landscape" or w_attr > h_attr:
                    orientation = "landscape"

            # Check section page margins (<w:pgMar>) in twips (1 pt = 20 twips)
            for pgMar in body.iter(f"{W_NS}pgMar"):
                try:
                    t_pt = int(pgMar.attrib.get(f"{W_NS}top", "1080") or 1080) / 20.0
                    r_pt = int(pgMar.attrib.get(f"{W_NS}right", "1080") or 1080) / 20.0
                    b_pt = int(pgMar.attrib.get(f"{W_NS}bottom", "1080") or 1080) / 20.0
                    l_pt = int(pgMar.attrib.get(f"{W_NS}left", "1080") or 1080) / 20.0
                    margins_pt = (
                        max(18.0, min(108.0, t_pt)),
                        max(18.0, min(108.0, r_pt)),
                        max(18.0, min(108.0, b_pt)),
                        max(18.0, min(108.0, l_pt)),
                    )
                except Exception:
                    pass

            def render_paragraph(p_el: ET.Element, in_table: bool = False) -> str:
                align = ""
                style_val = ""
                is_list_item = False
                num_prefix = ""
                p_bg = None
                p_styles: List[str] = []
                if in_table:
                    p_styles.append("margin:2px 0")
                else:
                    p_styles.append("margin-top:2px; margin-bottom:3px")

                pPr = p_el.find(f"{W_NS}pPr")
                if pPr is not None:
                    jc = pPr.find(f"{W_NS}jc")
                    if jc is not None:
                        val = jc.attrib.get(f"{W_NS}val", "")
                        if val in ("center", "right", "both", "distribute"):
                            align = "justify" if val in ("both", "distribute") else val
                            p_styles.append(f"text-align:{align}")
                    pStyle = pPr.find(f"{W_NS}pStyle")
                    if pStyle is not None:
                        style_val = pStyle.attrib.get(f"{W_NS}val", "").lower()

                    # Auto-numbering resolver
                    numPr = pPr.find(f"{W_NS}numPr")
                    if numPr is not None:
                        ilvl_el = numPr.find(f"{W_NS}ilvl")
                        numId_el = numPr.find(f"{W_NS}numId")
                        ilvl_val = ilvl_el.attrib.get(f"{W_NS}val", "0") if ilvl_el is not None else "0"
                        numId_val = numId_el.attrib.get(f"{W_NS}val", "0") if numId_el is not None else "0"
                        num_prefix = get_numbering_prefix(numId_val, ilvl_val)
                        if not num_prefix:
                            is_list_item = True
                    elif style_val and style_val in style_num_map:
                        nid, il = style_num_map[style_val]
                        num_prefix = get_numbering_prefix(nid, il)
                        if not num_prefix:
                            is_list_item = True

                    # Paragraph spacing & line-height
                    spacing_el = pPr.find(f"{W_NS}spacing")
                    if spacing_el is not None:
                        line_val = spacing_el.attrib.get(f"{W_NS}line")
                        line_rule = spacing_el.attrib.get(f"{W_NS}lineRule", "auto")
                        if line_val:
                            try:
                                if line_rule == "auto":
                                    lh = float(line_val) / 240.0
                                    if 0.8 <= lh <= 2.8:
                                        p_styles.append(f"line-height:{lh:.2f}")
                                else:
                                    lh_pt = float(line_val) / 20.0
                                    if 8.0 <= lh_pt <= 48.0:
                                        p_styles.append(f"line-height:{lh_pt:.1f}pt")
                            except Exception:
                                pass
                        b_val = spacing_el.attrib.get(f"{W_NS}before")
                        a_val = spacing_el.attrib.get(f"{W_NS}after")
                        if b_val and not in_table:
                            try:
                                b_pt = max(0.0, min(72.0, float(b_val) / 20.0))
                                p_styles.append(f"margin-top:{b_pt:.1f}pt")
                            except Exception:
                                pass
                        if a_val and not in_table:
                            try:
                                a_pt = max(0.0, min(72.0, float(a_val) / 20.0))
                                p_styles.append(f"margin-bottom:{a_pt:.1f}pt")
                            except Exception:
                                pass

                    # Paragraph indentation
                    ind_el = pPr.find(f"{W_NS}ind")
                    if ind_el is not None:
                        l_val = ind_el.attrib.get(f"{W_NS}left")
                        if l_val:
                            try:
                                l_pt = max(0.0, min(240.0, float(l_val) / 20.0))
                                p_styles.append(f"padding-left:{l_pt:.1f}pt")
                            except Exception:
                                pass
                        f_val = ind_el.attrib.get(f"{W_NS}firstLine")
                        h_val = ind_el.attrib.get(f"{W_NS}hanging")
                        if f_val:
                            try:
                                f_pt = max(0.0, min(140.0, float(f_val) / 20.0))
                                p_styles.append(f"text-indent:{f_pt:.1f}pt")
                            except Exception:
                                pass
                        elif h_val:
                            try:
                                h_pt = max(0.0, min(140.0, float(h_val) / 20.0))
                                p_styles.append(f"text-indent:-{h_pt:.1f}pt")
                            except Exception:
                                pass

                    if pPr.find(f"{W_NS}pageBreakBefore") is not None:
                        p_styles.append("page-break-before:always; break-before:page")

                    shd = pPr.find(f"{W_NS}shd")
                    if shd is not None:
                        p_bg = _clean_hex_color(shd.attrib.get(f"{W_NS}fill"))
                        if p_bg:
                            p_styles.append(f"background-color:{p_bg}; padding:2px 4px")

                runs_html: List[str] = []
                for child in p_el:
                    # Handle hyperlinks and direct runs
                    run_nodes = [child] if child.tag == f"{W_NS}r" else list(child.iter(f"{W_NS}r"))
                    for r_node in run_nodes:
                        rPr = r_node.find(f"{W_NS}rPr")
                        is_b = rPr is not None and (rPr.find(f"{W_NS}b") is not None or rPr.find(f"{W_NS}bCs") is not None)
                        is_i = rPr is not None and (rPr.find(f"{W_NS}i") is not None or rPr.find(f"{W_NS}iCs") is not None)
                        is_u = rPr is not None and rPr.find(f"{W_NS}u") is not None
                        r_color = None
                        r_size_pt = None
                        if rPr is not None:
                            col_el = rPr.find(f"{W_NS}color")
                            if col_el is not None:
                                r_color = _clean_hex_color(col_el.attrib.get(f"{W_NS}val"))
                            sz_el = rPr.find(f"{W_NS}sz")
                            if sz_el is not None:
                                try:
                                    half_pts = int(sz_el.attrib.get(f"{W_NS}val", "0") or 0)
                                    if 10 <= half_pts <= 144:
                                        r_size_pt = half_pts / 2.0
                                except Exception:
                                    pass

                        texts: List[str] = []
                        for sub in r_node:
                            if sub.tag == f"{W_NS}t" and sub.text:
                                texts.append(_html.escape(sub.text))
                            elif sub.tag == f"{W_NS}tab":
                                texts.append('<span style="display:inline-block; min-width:32pt;">&nbsp;&nbsp;&nbsp;&nbsp;</span>')
                            elif sub.tag in (f"{W_NS}br", f"{W_NS}cr"):
                                if sub.attrib.get(f"{W_NS}type") == "page":
                                    texts.append('<div style="page-break-before:always; break-before:page; height:0; line-height:0;"></div>')
                                else:
                                    texts.append("<br>")

                        for blip in r_node.iter(f"{A_NS}blip"):
                            embed_id = blip.attrib.get(f"{R_NS}embed", "")
                            target = rels_map.get(embed_id, "")
                            if target:
                                media_path = "word/" + target.lstrip("/")
                                if media_path in zf.namelist():
                                    try:
                                        raw_img = zf.read(media_path)
                                        if len(raw_img) < 4 * 1024 * 1024:
                                            b64 = base64.b64encode(raw_img).decode("ascii")
                                            ext_m = media_path.rsplit(".", 1)[-1].lower()
                                            mime = "image/png" if ext_m == "png" else "image/jpeg"
                                            # Determine image size from wp:extent if present (1 pt = 12700 EMUs)
                                            img_w_style = "max-width:100%; max-height:260px;"
                                            for ext_el in r_node.iter(f"{WP_NS}extent"):
                                                cx_emu = int(ext_el.attrib.get("cx", "0") or 0)
                                                cy_emu = int(ext_el.attrib.get("cy", "0") or 0)
                                                if cx_emu > 0 and cy_emu > 0:
                                                    w_pt = min(480, max(24, int(cx_emu / 12700)))
                                                    h_pt = min(620, max(24, int(cy_emu / 12700)))
                                                    img_w_style = f"width:{w_pt}pt; height:{h_pt}pt;"
                                                    break
                                            texts.append(f'<img src="data:{mime};base64,{b64}" style="{img_w_style} display:inline-block; margin:2px 0;">')
                                    except Exception:
                                        pass

                        chunk = "".join(texts)
                        if not chunk:
                            continue
                        span_styles: List[str] = []
                        if r_color:
                            span_styles.append(f"color:{r_color}")
                        if r_size_pt:
                            span_styles.append(f"font-size:{r_size_pt:.1f}pt")
                        if span_styles:
                            chunk = f'<span style="{";".join(span_styles)}">{chunk}</span>'
                        if is_u:
                            chunk = f"<u>{chunk}</u>"
                        if is_i:
                            chunk = f"<i>{chunk}</i>"
                        if is_b:
                            chunk = f"<b>{chunk}</b>"
                        runs_html.append(chunk)

                inner = "".join(runs_html).strip()
                if num_prefix:
                    inner = f'<span style="display:inline-block; min-width:18pt; font-weight:600;">{num_prefix}</span>' + (f'&nbsp;{inner}' if inner else '')
                elif is_list_item:
                    inner = '<span style="display:inline-block; min-width:14pt; font-weight:bold;">•</span>' + (f'&nbsp;{inner}' if inner else '')

                style_attr = f' style="{"; ".join(p_styles)};"' if p_styles else ""

                if not inner:
                    return "" if in_table else "<p style='margin:2px 0;'>&nbsp;</p>"
                if "heading1" in style_val or style_val == "title":
                    return f"<h2{style_attr}>{inner}</h2>"
                if "heading2" in style_val or "heading3" in style_val:
                    return f"<h3{style_attr}>{inner}</h3>"
                return f"<p{style_attr}>{inner}</p>"

            page_w_pt = 841.9 if orientation == "landscape" else 595.3
            avail_w_pt = max(200.0, page_w_pt - margins_pt[1] - margins_pt[3])

            def render_table(tbl_el: ET.Element) -> str:
                # 1. Parse table properties <w:tblPr> (width, alignment)
                tblPr = tbl_el.find(f"{W_NS}tblPr")
                tbl_align = "left"
                specified_tbl_w_pt: Optional[float] = None
                if tblPr is not None:
                    jc = tblPr.find(f"{W_NS}jc")
                    if jc is not None:
                        tbl_align = jc.attrib.get(f"{W_NS}val", "left").lower()
                    tblW = tblPr.find(f"{W_NS}tblW")
                    if tblW is not None:
                        w_type = tblW.attrib.get(f"{W_NS}type", "auto").lower()
                        try:
                            w_val = float(tblW.attrib.get(f"{W_NS}w", "0") or 0)
                            if w_type == "dxa" and w_val > 0:
                                specified_tbl_w_pt = w_val / 20.0
                            elif w_type == "pct" and w_val > 0:
                                specified_tbl_w_pt = (w_val / 5000.0) * avail_w_pt
                        except Exception:
                            pass

                # 2. Extract column widths from w:tcW and w:tblGrid/w:gridCol
                rows_el = tbl_el.findall(f"{W_NS}tr")
                cell_widths_by_col: Dict[int, float] = {}
                for tr in rows_el:
                    c_idx = 0
                    for tc in tr.findall(f"{W_NS}tc"):
                        c_span = 1
                        tcPr = tc.find(f"{W_NS}tcPr")
                        if tcPr is not None:
                            gs = tcPr.find(f"{W_NS}gridSpan")
                            if gs is not None:
                                try:
                                    c_span = max(1, int(gs.attrib.get(f"{W_NS}val", "1") or 1))
                                except Exception:
                                    c_span = 1
                            tcW = tcPr.find(f"{W_NS}tcW")
                            if tcW is not None:
                                try:
                                    w_type = tcW.attrib.get(f"{W_NS}type", "dxa").lower()
                                    w_val = float(tcW.attrib.get(f"{W_NS}w", "0") or 0)
                                    if w_val > 50 and w_type in ("dxa", ""):
                                        if c_span == 1:
                                            cell_widths_by_col[c_idx] = max(cell_widths_by_col.get(c_idx, 0.0), w_val / 20.0)
                                except Exception:
                                    pass
                        c_idx += c_span

                grid_widths_pt: List[float] = []
                tblGrid = tbl_el.find(f"{W_NS}tblGrid")
                if tblGrid is not None:
                    for gc in tblGrid.findall(f"{W_NS}gridCol"):
                        try:
                            grid_widths_pt.append(max(5.0, float(gc.attrib.get(f"{W_NS}w", "1000") or 1000) / 20.0))
                        except Exception:
                            grid_widths_pt.append(50.0)

                num_cols = max(len(grid_widths_pt), max(cell_widths_by_col.keys(), default=-1) + 1)

                has_valid_tc_widths = False
                if len(cell_widths_by_col) == num_cols and num_cols > 1:
                    vals = list(cell_widths_by_col.values())
                    if (max(vals) - min(vals)) > 2.0:
                        has_valid_tc_widths = True

                has_valid_grid_widths = False
                if len(grid_widths_pt) == num_cols and num_cols > 1:
                    if (max(grid_widths_pt) - min(grid_widths_pt)) > 2.0:
                        has_valid_grid_widths = True

                if has_valid_tc_widths:
                    chosen_pts = [cell_widths_by_col.get(i, 50.0) for i in range(num_cols)]
                elif has_valid_grid_widths:
                    chosen_pts = grid_widths_pt
                elif len(cell_widths_by_col) == num_cols and num_cols > 0:
                    chosen_pts = [cell_widths_by_col.get(i, 50.0) for i in range(num_cols)]
                elif len(grid_widths_pt) == num_cols and num_cols > 0:
                    chosen_pts = grid_widths_pt
                else:
                    col_char_counts: List[int] = [0] * max(1, num_cols)
                    for tr in rows_el:
                        c_cur = 0
                        for tc in tr.findall(f"{W_NS}tc"):
                            c_span = 1
                            tcPr = tc.find(f"{W_NS}tcPr")
                            if tcPr is not None:
                                gs = tcPr.find(f"{W_NS}gridSpan")
                                if gs is not None:
                                    try:
                                        c_span = max(1, int(gs.attrib.get(f"{W_NS}val", "1") or 1))
                                    except Exception:
                                        c_span = 1
                            t_len = sum(len(t.text or "") for t in tc.iter(f"{W_NS}t"))
                            if c_cur < len(col_char_counts) and c_span == 1:
                                col_char_counts[c_cur] = max(col_char_counts[c_cur], t_len)
                            c_cur += c_span

                    weights: List[float] = []
                    for idx, ch_cnt in enumerate(col_char_counts):
                        if idx == 0 and ch_cnt <= 6:
                            w = 32.0 + ch_cnt * 1.5
                        elif ch_cnt == 0:
                            w = 35.0
                        else:
                            w = max(40.0, min(380.0, ch_cnt * 7.0 + 25.0))
                        weights.append(w)
                    chosen_pts = weights

                total_grid_pt = sum(chosen_pts) or 1.0
                # Preserve original Word table width: do not force small tables to 100% width!
                if specified_tbl_w_pt and 50 < specified_tbl_w_pt <= avail_w_pt:
                    target_w_pt = specified_tbl_w_pt
                    table_is_full_width = (specified_tbl_w_pt >= avail_w_pt * 0.92)
                elif total_grid_pt >= avail_w_pt * 0.92:
                    target_w_pt = avail_w_pt
                    table_is_full_width = True
                elif total_grid_pt > 50:
                    target_w_pt = min(total_grid_pt, avail_w_pt)
                    table_is_full_width = False
                else:
                    target_w_pt = avail_w_pt
                    table_is_full_width = True

                scale = target_w_pt / total_grid_pt
                final_col_pts = [max(16.0, round(w * scale, 1)) for w in chosen_pts]
                diff = target_w_pt - sum(final_col_pts)
                if final_col_pts:
                    final_col_pts[-1] = round(final_col_pts[-1] + diff, 1)

                actual_tbl_w_pt = sum(final_col_pts) if final_col_pts else target_w_pt

                # Calculate individual cell content widths for Story (Story adds 10.8pt padding + 1pt border)
                # By providing explicit content width per cell, Story renders EXACT Word column dimensions!
                padding_and_border_pt = 11.8
                cell_content_pts = [max(10.0, round(w - padding_and_border_pt, 1)) for w in final_col_pts]

                # 3. Pre-scan vertical merges (w:vMerge) across rows to prevent column shifts
                vmerge_info: Dict[Tuple[int, int], Dict[str, Any]] = {}
                active_vmerges: Dict[int, Tuple[int, int]] = {}

                for r_i, tr in enumerate(rows_el):
                    col_cursor = 0
                    for tc in tr.findall(f"{W_NS}tc"):
                        tcPr = tc.find(f"{W_NS}tcPr")
                        colspan = 1
                        is_vmerge_start = False
                        is_vmerge_cont = False
                        if tcPr is not None:
                            gs = tcPr.find(f"{W_NS}gridSpan")
                            if gs is not None:
                                try:
                                    colspan = max(1, int(gs.attrib.get(f"{W_NS}val", "1") or 1))
                                except Exception:
                                    colspan = 1
                            vMerge = tcPr.find(f"{W_NS}vMerge")
                            if vMerge is not None:
                                val = vMerge.attrib.get(f"{W_NS}val", "").lower()
                                if val == "restart":
                                    is_vmerge_start = True
                                else:
                                    is_vmerge_cont = True

                        if is_vmerge_start:
                            active_vmerges[col_cursor] = (r_i, col_cursor)
                            vmerge_info[(r_i, col_cursor)] = {"action": "render", "rowspan": 1}
                        elif is_vmerge_cont and col_cursor in active_vmerges:
                            starter = active_vmerges[col_cursor]
                            if starter in vmerge_info:
                                vmerge_info[starter]["rowspan"] += 1
                            vmerge_info[(r_i, col_cursor)] = {"action": "skip"}
                        else:
                            active_vmerges.pop(col_cursor, None)
                            vmerge_info[(r_i, col_cursor)] = {"action": "render", "rowspan": 1}

                        col_cursor += colspan

                first_col_is_sl = False
                t_rows: List[str] = []
                for r_i, tr in enumerate(rows_el):
                    trPr = tr.find(f"{W_NS}trPr")
                    row_h_pt: Optional[float] = None
                    if trPr is not None:
                        trHeight = trPr.find(f"{W_NS}trHeight")
                        if trHeight is not None:
                            try:
                                h_val = float(trHeight.attrib.get(f"{W_NS}val", "0") or 0)
                                if h_val > 0:
                                    row_h_pt = max(10.0, h_val / 20.0)
                            except Exception:
                                pass

                    cells: List[str] = []
                    col_cursor = 0
                    for tc in tr.findall(f"{W_NS}tc"):
                        tcPr = tc.find(f"{W_NS}tcPr")
                        colspan = 1
                        cell_bg = None
                        v_align = "top"
                        if tcPr is not None:
                            gs = tcPr.find(f"{W_NS}gridSpan")
                            if gs is not None:
                                try:
                                    colspan = max(1, int(gs.attrib.get(f"{W_NS}val", "1") or 1))
                                except Exception:
                                    colspan = 1
                            shd = tcPr.find(f"{W_NS}shd")
                            if shd is not None:
                                cell_bg = _clean_hex_color(shd.attrib.get(f"{W_NS}fill"))
                            vAl = tcPr.find(f"{W_NS}vAlign")
                            if vAl is not None:
                                vv = vAl.attrib.get(f"{W_NS}val", "").lower()
                                if vv in ("center", "bottom"):
                                    v_align = "middle" if vv == "center" else "bottom"

                        vm = vmerge_info.get((r_i, col_cursor), {"action": "render", "rowspan": 1})
                        if vm.get("action") == "skip":
                            col_cursor += colspan
                            continue

                        rowspan = vm.get("rowspan", 1)

                        w_pt_style = ""
                        if final_col_pts and col_cursor < len(final_col_pts):
                            span_w = sum(final_col_pts[col_cursor : col_cursor + colspan])
                            w_pt_style = f"width:{span_w:.1f}pt; max-width:{span_w:.1f}pt;"

                        curr_col = col_cursor
                        col_cursor += colspan

                        cell_blocks: List[str] = []
                        for sub_el in tc:
                            if sub_el.tag == f"{W_NS}p":
                                p_h = render_paragraph(sub_el, in_table=True)
                                if p_h:
                                    cell_blocks.append(p_h)
                            elif sub_el.tag == f"{W_NS}tbl":
                                cell_blocks.append(render_table(sub_el))

                        raw_cell_text = "".join(t.text or "" for t in tc.iter(f"{W_NS}t")).strip()
                        if r_i == 0 and curr_col == 0:
                            hdr_clean = re.sub(r"[^\w#]", "", raw_cell_text.lower())
                            if any(k in hdr_clean for k in ("sl", "serial", "\u0995\u09cd\u09b0\u09ae\u09bf\u0995", "\u0995\u09cd\u09b0", "#")) or hdr_clean in ("no", "num", "number", "slno"):
                                first_col_is_sl = True

                        cell_inner = "".join(cell_blocks)
                        if (not cell_inner or cell_inner == "&nbsp;") and curr_col == 0 and r_i >= 1 and first_col_is_sl:
                            cell_inner = f"<p style='margin:1px 0; font-weight:600;'>{r_i}.</p>"
                        elif not cell_inner:
                            cell_inner = "&nbsp;"

                        w_pt_style = ""
                        if cell_content_pts and curr_col < len(cell_content_pts):
                            span_w = sum(cell_content_pts[curr_col : curr_col + colspan]) + (colspan - 1) * padding_and_border_pt
                            w_pt_style = f"width:{span_w:.1f}pt;"

                        c_styles = [
                            "border:1px solid #1e293b",
                            "padding:2.5pt 5.4pt",
                            f"vertical-align:{v_align}",
                            "box-sizing:border-box",
                            "line-height:1.15",
                            "word-break:break-word",
                            "overflow-wrap:break-word",
                        ]
                        if w_pt_style:
                            c_styles.append(w_pt_style)
                        if row_h_pt:
                            c_styles.append(f"height:{row_h_pt:.1f}pt")
                        if cell_bg:
                            c_styles.append(f"background-color:{cell_bg}")

                        attrs = []
                        if colspan > 1:
                            attrs.append(f'colspan="{colspan}"')
                        if rowspan > 1:
                            attrs.append(f'rowspan="{rowspan}"')
                        attrs.append(f'style="{"; ".join(c_styles)};"')
                        cells.append(f'<td {" ".join(attrs)}>{cell_inner}</td>')

                    if cells:
                        row_style = f' style="height:{row_h_pt:.1f}pt;"' if row_h_pt else ""
                        t_rows.append(f"<tr{row_style}>" + "".join(cells) + "</tr>")

                if not t_rows:
                    return ""

                colgroup_html = ""
                if final_col_pts:
                    total_pts = sum(final_col_pts) or 1.0
                    col_pcts = [round((w / total_pts) * 100.0, 2) for w in final_col_pts]
                    diff_pct = 100.0 - sum(col_pcts)
                    col_pcts[-1] = round(col_pcts[-1] + diff_pct, 2)
                    colgroup_html = "<colgroup>" + "".join(f'<col style="width:{pct:.2f}%;">' for pct in col_pcts) + "</colgroup>"

                margin_css = "margin:6px 0;"
                if tbl_align == "center":
                    margin_css = "margin:6px auto;"
                elif tbl_align == "right":
                    margin_css = "margin:6px 0 6px auto;"

                tbl_w_style = "width:100%;" if table_is_full_width else f"width:{actual_tbl_w_pt:.1f}pt; max-width:100%;"
                table_css = f"border-collapse:collapse; {tbl_w_style} {margin_css} font-size:10pt;"
                return f'<table border="1" cellspacing="0" cellpadding="4" style="{table_css}">{"".join(t_rows)}</table>'
            for elem in body:
                if elem.tag == f"{W_NS}p":
                    parts.append(render_paragraph(elem, in_table=False))
                elif elem.tag == f"{W_NS}tbl":
                    tbl_html = render_table(elem)
                    if tbl_html:
                        parts.append(tbl_html)
    except Exception as e:
        parts.append(f"<p>ডকুমেন্ট লোড হয়েছে ({_html.escape(str(e))})</p>")

    return ("\n".join(parts) if parts else "<p>&nbsp;</p>"), orientation, margins_pt


# ── PowerPoint (.pptx / .ppt) Coordinate-Accurate Slide Renderer ──

def _render_pptx_to_pdf_and_html(raw_path: str, pdf_out_path: str) -> Tuple[str, int]:
    """
    Renders a .pptx presentation directly onto landscape slide pages in PyMuPDF,
    preserving exact shape/text/table/image positions, colors, and slide dimensions,
    and also returns an HTML summary for the editor.
    """
    if _fitz is None:
        raise RuntimeError("PyMuPDF is required")

    slide_html_blocks: List[str] = []
    out_doc = _fitz.open()

    try:
        with zipfile.ZipFile(raw_path, "r") as zf:
            # 1. Read slide size from ppt/presentation.xml (in EMUs; 1 pt = 12700 EMUs)
            sld_w_pt, sld_h_pt = 720.0, 405.0  # Default 16:9 widescreen
            if "ppt/presentation.xml" in zf.namelist():
                try:
                    p_root = ET.fromstring(zf.read("ppt/presentation.xml"))
                    sldSz = p_root.find(f"{P_NS}sldSz")
                    if sldSz is not None:
                        cx = int(sldSz.attrib.get("cx", "9144000") or 9144000)
                        cy = int(sldSz.attrib.get("cy", "5143500") or 5143500)
                        sld_w_pt = max(300.0, cx / 12700.0)
                        sld_h_pt = max(200.0, cy / 12700.0)
                except Exception:
                    pass

            slide_files = sorted(
                [n for n in zf.namelist() if re.match(r"^ppt/slides/slide\d+\.xml$", n)],
                key=lambda s: int(re.search(r"slide(\d+)\.xml", s).group(1))  # type: ignore
            )

            for s_idx, sf in enumerate(slide_files):
                s_num = s_idx + 1
                rels_path = f"ppt/slides/_rels/{os.path.basename(sf)}.rels"
                rels_map: Dict[str, str] = {}
                if rels_path in zf.namelist():
                    try:
                        r_root = ET.fromstring(zf.read(rels_path))
                        for rel in r_root:
                            rid = rel.attrib.get("Id", "")
                            target = rel.attrib.get("Target", "")
                            if rid and target:
                                rels_map[rid] = target
                    except Exception:
                        pass

                s_root = ET.fromstring(zf.read(sf))
                page = out_doc.new_page(width=sld_w_pt, height=sld_h_pt)
                # Subtle slide border
                page.draw_rect(_fitz.Rect(1, 1, sld_w_pt - 1, sld_h_pt - 1), color=(0.82, 0.85, 0.88), width=1)

                slide_texts_html: List[str] = [f"<h3>🎞️ Slide {s_num}</h3>"]

                # Helper to extract xfrm box (x, y, w, h) in points
                def get_xfrm_rect(el: ET.Element) -> Optional[_fitz.Rect]:
                    for xfrm in el.iter(f"{A_NS}xfrm"):
                        off = xfrm.find(f"{A_NS}off")
                        ext = xfrm.find(f"{A_NS}ext")
                        if off is not None and ext is not None:
                            try:
                                x = int(off.attrib.get("x", "0") or 0) / 12700.0
                                y = int(off.attrib.get("y", "0") or 0) / 12700.0
                                w = int(ext.attrib.get("cx", "0") or 0) / 12700.0
                                h = int(ext.attrib.get("cy", "0") or 0) / 12700.0
                                if w > 2 and h > 2:
                                    return _fitz.Rect(x, y, x + w, y + h)
                            except Exception:
                                pass
                    return None

                spTree = s_root.find(f".//{P_NS}spTree")
                if spTree is None:
                    spTree = s_root

                for child in spTree:
                    # 1. Pictures (<p:pic>)
                    if child.tag == f"{P_NS}pic":
                        rect = get_xfrm_rect(child)
                        for blip in child.iter(f"{A_NS}blip"):
                            rid = blip.attrib.get(f"{R_NS}embed", "")
                            target = rels_map.get(rid, "")
                            if target:
                                norm_target = os.path.normpath(os.path.join("ppt/slides", target)).replace("\\", "/")
                                if norm_target in zf.namelist():
                                    try:
                                        img_bytes = zf.read(norm_target)
                                        target_r = rect or _fitz.Rect(40, 40, sld_w_pt - 40, sld_h_pt - 40)
                                        page.insert_image(target_r, stream=img_bytes)
                                    except Exception:
                                        pass

                    # 2. Shapes / Textboxes (<p:sp>)
                    elif child.tag == f"{P_NS}sp":
                        rect = get_xfrm_rect(child) or _fitz.Rect(36, 36, sld_w_pt - 36, sld_h_pt - 36)
                        # Check shape solidFill
                        spPr = child.find(f"{P_NS}spPr")
                        if spPr is not None:
                            sf_el = spPr.find(f"{A_NS}solidFill")
                            if sf_el is not None:
                                srgb = sf_el.find(f"{A_NS}srgbClr")
                                if srgb is not None:
                                    hx = srgb.attrib.get("val", "")
                                    if len(hx) == 6:
                                        try:
                                            r_c = int(hx[0:2], 16) / 255.0
                                            g_c = int(hx[2:4], 16) / 255.0
                                            b_c = int(hx[4:6], 16) / 255.0
                                            page.draw_rect(rect, color=None, fill=(r_c, g_c, b_c))
                                        except Exception:
                                            pass

                        p_htmls: List[str] = []
                        for a_p in child.iter(f"{A_NS}p"):
                            align = "left"
                            pPr = a_p.find(f"{A_NS}pPr")
                            if pPr is not None:
                                algn = pPr.attrib.get("algn", "")
                                if algn == "ctr":
                                    align = "center"
                                elif algn == "r":
                                    align = "right"
                            r_chunks: List[str] = []
                            for a_r in a_p.findall(f"{A_NS}r"):
                                rPr = a_r.find(f"{A_NS}rPr")
                                a_t = a_r.find(f"{A_NS}t")
                                if a_t is None or not a_t.text:
                                    continue
                                txt = _html.escape(a_t.text)
                                sz_pt = 14.0
                                is_b = False
                                col_hex = "#0f172a"
                                if rPr is not None:
                                    if rPr.attrib.get("b") == "1":
                                        is_b = True
                                    try:
                                        sz_val = int(rPr.attrib.get("sz", "0") or 0)
                                        if sz_val >= 600:
                                            sz_pt = min(44.0, sz_val / 100.0)
                                    except Exception:
                                        pass
                                    srgb = rPr.find(f".//{A_NS}srgbClr")
                                    if srgb is not None and len(srgb.attrib.get("val", "")) == 6:
                                        col_hex = "#" + srgb.attrib["val"]
                                chunk = f'<span style="font-size:{sz_pt:.1f}pt; color:{col_hex};">{txt}</span>'
                                if is_b:
                                    chunk = f"<b>{chunk}</b>"
                                r_chunks.append(chunk)
                            if r_chunks:
                                line_html = "".join(r_chunks)
                                p_htmls.append(f'<p style="margin:2px 0; text-align:{align};">{line_html}</p>')
                                slide_texts_html.append(f'<p style="margin:2px 0;">{line_html}</p>')

                        if p_htmls:
                            box_html = "<div style='font-family:sans-serif;'>" + "".join(p_htmls) + "</div>"
                            try:
                                page.insert_htmlbox(rect, box_html)
                            except Exception:
                                pass

                    # 3. Tables inside slide (<p:graphicFrame> containing <a:tbl>)
                    elif child.tag == f"{P_NS}graphicFrame":
                        rect = get_xfrm_rect(child) or _fitz.Rect(36, 80, sld_w_pt - 36, sld_h_pt - 40)
                        for a_tbl in child.iter(f"{A_NS}tbl"):
                            # Extract column widths in EMU (1 pt = 12700 EMU)
                            raw_col_pts: List[float] = []
                            a_grid = a_tbl.find(f"{A_NS}tblGrid")
                            if a_grid is not None:
                                for gc in a_grid.findall(f"{A_NS}gridCol"):
                                    try:
                                        raw_col_pts.append(max(15.0, float(gc.attrib.get("w", "1270000")) / 12700.0))
                                    except Exception:
                                        raw_col_pts.append(60.0)

                            frame_w = float(rect.width) if rect else 500.0
                            tot_c_w = sum(raw_col_pts) if raw_col_pts else frame_w
                            if tot_c_w > 0 and raw_col_pts:
                                scale = frame_w / tot_c_w
                                final_ppt_cols = [round(w * scale, 1) for w in raw_col_pts]
                            else:
                                final_ppt_cols = raw_col_pts

                            t_rows: List[str] = []
                            for a_tr in a_tbl.findall(f"{A_NS}tr"):
                                tr_h_pt = None
                                try:
                                    h_emu = float(a_tr.attrib.get("h", "0") or 0)
                                    if h_emu > 0:
                                        tr_h_pt = max(10.0, h_emu / 12700.0)
                                except Exception:
                                    pass

                                cells: List[str] = []
                                for c_idx, a_tc in enumerate(a_tr.findall(f"{A_NS}tc")):
                                    cell_txts = [
                                        _html.escape(t.text)
                                        for t in a_tc.iter(f"{A_NS}t")
                                        if t.text
                                    ]
                                    c_inner = " ".join(cell_txts) or "&nbsp;"
                                    w_st = f"width:{final_ppt_cols[c_idx]:.1f}pt;" if c_idx < len(final_ppt_cols) else ""
                                    h_st = f"height:{tr_h_pt:.1f}pt;" if tr_h_pt else ""
                                    st_list = ["border:1px solid #334155", "padding:4px 6px", "box-sizing:border-box"]
                                    if w_st:
                                        st_list.append(w_st)
                                    if h_st:
                                        st_list.append(h_st)
                                    cells.append(f'<td style="{"; ".join(st_list)}">{c_inner}</td>')
                                if cells:
                                    row_st = f' style="height:{tr_h_pt:.1f}pt;"' if tr_h_pt else ""
                                    t_rows.append(f"<tr{row_st}>" + "".join(cells) + "</tr>")
                            if t_rows:
                                colgroup_str = ""
                                if final_ppt_cols:
                                    colgroup_str = "<colgroup>" + "".join(f'<col style="width:{w:.1f}pt;">' for w in final_ppt_cols) + "</colgroup>"
                                tbl_h = (
                                    f'<table border="1" cellspacing="0" cellpadding="4" style="border-collapse:collapse; width:{frame_w:.1f}pt; font-size:11pt;">'
                                    + colgroup_str
                                    + "".join(t_rows)
                                    + "</table>"
                                )
                                slide_texts_html.append(tbl_h)
                                try:
                                    page.insert_htmlbox(rect, tbl_h)
                                except Exception:
                                    pass

                slide_html_blocks.append("\n".join(slide_texts_html))

        if len(out_doc) == 0:
            out_doc.new_page(width=720, height=405)
        tmp_path = pdf_out_path + ".pptx.tmp"
        out_doc.save(tmp_path, garbage=2, deflate=True)
        total_pages = len(out_doc)
    finally:
        out_doc.close()

    os.replace(tmp_path, pdf_out_path)
    _clear_nup_caches_for(pdf_out_path)
    return ("\n<hr>\n".join(slide_html_blocks) if slide_html_blocks else "<p>&nbsp;</p>"), total_pages


def _parse_legacy_text_or_doc(path: str, ext: str) -> str:
    """Extracts clean readable paragraphs from .txt, .rtf, or legacy .doc/.ppt files."""
    with open(path, "rb") as f:
        raw = f.read()

    if ext == "txt":
        for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                text = ""
        lines = [_html.escape(line) or "&nbsp;" for line in text.splitlines()]
        return "\n".join(f"<p style='margin:3px 0;'>{ln}</p>" for ln in lines[:2000])

    if ext == "rtf":
        text = raw.decode("latin-1", errors="ignore")
        text = re.sub(r"\\par\b", "\n", text)
        text = re.sub(r"\\[a-zA-Z]+\d*\s?", "", text)
        text = re.sub(r"[{}]", "", text)
        lines = [_html.escape(ln.strip()) for ln in text.splitlines() if ln.strip()]
        return "\n".join(f"<p style='margin:4px 0;'>{ln}</p>" for ln in lines[:1500]) or "<p>&nbsp;</p>"

    chunks: List[str] = []
    try:
        u16 = raw.decode("utf-16le", errors="ignore")
        for m in re.findall(r"[\u0980-\u09FF\x20-\x7E\r\n\t]{12,}", u16):
            cleaned = m.strip()
            if len(cleaned) >= 12 and not cleaned.startswith(("Root Entry", "WordDocument", "Microsoft", "PowerPoint")):
                chunks.append(cleaned)
    except Exception:
        pass
    if not chunks:
        asc = raw.decode("latin-1", errors="ignore")
        for m in re.findall(r"[\x20-\x7E\r\n\t]{16,}", asc):
            cleaned = m.strip()
            if len(cleaned) >= 16 and "Microsoft" not in cleaned and "Normal.dot" not in cleaned:
                chunks.append(cleaned)

    out_ps: List[str] = []
    for ch in chunks[:400]:
        for ln in ch.splitlines():
            ln_s = ln.strip()
            if ln_s:
                out_ps.append(f"<p style='margin:4px 0;'>{_html.escape(ln_s)}</p>")
    return "\n".join(out_ps) if out_ps else "<p>&nbsp;</p>"


# ── XLSX / XLS / CSV Parser ─────────────────────────────────────────────────

def _col_letters_to_index(col_str: str) -> int:
    idx = 0
    for ch in col_str:
        if "A" <= ch <= "Z":
            idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(0, idx - 1)


def _parse_xlsx_zip_fallback(path: str) -> List[Dict[str, Any]]:
    """Pure-stdlib XML parser for .xlsx workbooks in case openpyxl fails or is unavailable."""
    sheets_out: List[Dict[str, Any]] = []
    with zipfile.ZipFile(path, "r") as zf:
        shared_strings: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in ss_root.findall(f"{S_NS}si"):
                t_parts = [t.text or "" for t in si.iter(f"{S_NS}t")]
                shared_strings.append("".join(t_parts))

        sheet_files = sorted(
            [n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
        )
        for s_idx, sf in enumerate(sheet_files):
            s_root = ET.fromstring(zf.read(sf))
            grid: Dict[Tuple[int, int], str] = {}
            max_r, max_c = 0, 0
            for row_el in s_root.iter(f"{S_NS}row"):
                r_num = int(row_el.attrib.get("r", "1") or 1) - 1
                for c_el in row_el.findall(f"{S_NS}c"):
                    ref = c_el.attrib.get("r", "A1")
                    col_part = "".join(ch for ch in ref if ch.isalpha())
                    c_num = _col_letters_to_index(col_part or "A")
                    ctype = c_el.attrib.get("t", "")
                    v_el = c_el.find(f"{S_NS}v")
                    is_el = c_el.find(f"{S_NS}is")
                    val = ""
                    if ctype == "s" and v_el is not None and v_el.text:
                        s_i = int(v_el.text)
                        if 0 <= s_i < len(shared_strings):
                            val = shared_strings[s_i]
                    elif ctype == "inlineStr" and is_el is not None:
                        val = "".join(t.text or "" for t in is_el.iter(f"{S_NS}t"))
                    elif v_el is not None and v_el.text is not None:
                        val = v_el.text
                    if val != "":
                        grid[(r_num, c_num)] = str(val)
                        max_r = max(max_r, r_num + 1)
                        max_c = max(max_c, c_num + 1)

            max_r = min(max_r, 500)
            max_c = min(max(max_c, 1), 40)
            rows: List[List[str]] = []
            for r in range(max_r):
                rows.append([grid.get((r, c), "") for c in range(max_c)])
            if rows:
                sheets_out.append({"name": f"Sheet {s_idx + 1}", "rows": rows})
    return sheets_out


def _parse_excel_to_sheets(path: str, ext: str) -> List[Dict[str, Any]]:
    """Parses .xlsx, .xls, or .csv into [{"name": ..., "rows": [[...], ...]}]."""
    if ext == "csv":
        with open(path, "rb") as f:
            raw = f.read()
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                text = ""
        reader = csv.reader(io.StringIO(text))
        rows = [[str(cell) for cell in row[:40]] for row in list(reader)[:500]]
        return [{"name": "CSV Data", "rows": rows or [[""]]}]

    if ext == "xlsx":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path, data_only=True)
            sheets_out: List[Dict[str, Any]] = []
            for name in wb.sheetnames:
                ws = wb[name]
                raw_rows: List[List[str]] = []
                max_non_empty_col = 0
                for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
                    if r_idx >= 500:
                        break
                    str_row = ["" if v is None else str(v) for v in row[:40]]
                    for c_i, val in enumerate(str_row):
                        if val.strip() != "":
                            max_non_empty_col = max(max_non_empty_col, c_i + 1)
                    raw_rows.append(str_row)
                while raw_rows and all(c.strip() == "" for c in raw_rows[-1]):
                    raw_rows.pop()
                if max_non_empty_col > 0 and raw_rows:
                    trimmed = [r[:max_non_empty_col] for r in raw_rows]

                    # Extract column widths
                    col_widths_pt: List[float] = []
                    for col_i in range(1, max_non_empty_col + 1):
                        col_letter = openpyxl.utils.get_column_letter(col_i)
                        dim = ws.column_dimensions.get(col_letter)
                        w = dim.width if (dim and dim.width) else None
                        if w:
                            col_widths_pt.append(max(25.0, float(w) * 5.6 + 10.0))
                        else:
                            # Infer from column text content length
                            max_len = max((len(r[col_i - 1]) for r in trimmed if col_i - 1 < len(r)), default=5)
                            col_widths_pt.append(max(35.0, min(240.0, max_len * 6.5 + 18.0)))

                    # Extract row heights
                    row_heights_pt: List[Optional[float]] = []
                    for r_i in range(1, len(trimmed) + 1):
                        r_dim = ws.row_dimensions.get(r_i)
                        h = r_dim.height if (r_dim and r_dim.height) else None
                        row_heights_pt.append(float(h) if h and float(h) > 0 else None)

                    # Extract merged cell ranges
                    merged_ranges = []
                    for m in ws.merged_cells.ranges:
                        if m.min_row <= len(trimmed) and m.min_col <= max_non_empty_col:
                            merged_ranges.append({
                                "min_r": m.min_row - 1,
                                "max_r": min(len(trimmed) - 1, m.max_row - 1),
                                "min_c": m.min_col - 1,
                                "max_c": min(max_non_empty_col - 1, m.max_col - 1),
                            })

                    sheets_out.append({
                        "name": name,
                        "rows": trimmed,
                        "col_widths": col_widths_pt,
                        "row_heights": row_heights_pt,
                        "merged_ranges": merged_ranges
                    })
            if sheets_out:
                return sheets_out
        except Exception:
            pass
        try:
            fallback = _parse_xlsx_zip_fallback(path)
            if fallback:
                return fallback
        except Exception:
            pass

    try:
        import xlrd  # type: ignore
        book = xlrd.open_workbook(path)
        sheets_out = []
        for s in book.sheets():
            rows = []
            for r in range(min(s.nrows, 500)):
                rows.append([str(s.cell_value(r, c)) for c in range(min(s.ncols, 40))])
            if rows:
                sheets_out.append({"name": s.name, "rows": rows})
        if sheets_out:
            return sheets_out
    except Exception:
        pass

    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", errors="ignore")
    if "\t" in text:
        rows = [[c.strip() for c in ln.split("\t")[:30]] for ln in text.splitlines()[:400] if ln.strip()]
        if rows:
            return [{"name": "Sheet 1", "rows": rows}]

    return [{"name": "Sheet 1", "rows": [["ক্রমিক", "বিবরণ", "পরিমাণ", "মূল্য"], ["১", "", "", ""]]}]


def sheets_to_html(sheets: List[Dict[str, Any]], title: str = "", orientation: str = "portrait") -> str:
    """Converts spreadsheet rows into clean, exact-dimension HTML tables with proportional column widths and row heights."""
    avail_w = 778.0 if orientation == "landscape" else 531.0
    blocks: List[str] = []
    for s_idx, sheet in enumerate(sheets):
        s_name = _html.escape(str(sheet.get("name") or f"Sheet {s_idx + 1}"))
        rows = sheet.get("rows") or []
        if not rows:
            continue
        if len(sheets) > 1:
            blocks.append(f'<h3 style="margin:10px 0 4px 0; color:#1e293b; font-size:11pt;">📊 {s_name}</h3>')

        num_cols = max((len(r) for r in rows), default=1)
        raw_cols = list(sheet.get("col_widths") or [])
        if len(raw_cols) < num_cols:
            for c_i in range(len(raw_cols), num_cols):
                max_len = max((len(str(r[c_i])) for r in rows if c_i < len(r)), default=5)
                raw_cols.append(max(35.0, min(250.0, max_len * 6.5 + 18.0)))
        raw_cols = raw_cols[:num_cols]

        total_w = sum(raw_cols) if raw_cols else avail_w
        if total_w > avail_w:
            scale = avail_w / total_w
            final_cols = [max(20.0, round(w * scale, 1)) for w in raw_cols]
        else:
            final_cols = [round(w, 1) for w in raw_cols]
        tbl_w = sum(final_cols)

        row_heights = list(sheet.get("row_heights") or [])

        # Process merged cells
        merged_lookup: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for m in sheet.get("merged_ranges") or []:
            min_r, max_r = m["min_r"], m["max_r"]
            min_c, max_c = m["min_c"], m["max_c"]
            for r in range(min_r, max_r + 1):
                for c in range(min_c, max_c + 1):
                    if r == min_r and c == min_c:
                        merged_lookup[(r, c)] = {
                            "action": "render",
                            "rowspan": max_r - min_r + 1,
                            "colspan": max_c - min_c + 1
                        }
                    else:
                        merged_lookup[(r, c)] = {"action": "skip"}

        t_rows: List[str] = []
        for r_i, row in enumerate(rows):
            cells: List[str] = []
            is_header = (r_i == 0 and len(rows) > 1)
            bg_style = "background-color:#f1f5f9; font-weight:bold;" if is_header else ""
            r_h = row_heights[r_i] if r_i < len(row_heights) else None
            h_style = f"height:{r_h:.1f}pt;" if r_h and r_h > 0 else ""

            for c_i, cell in enumerate(row):
                m_info = merged_lookup.get((r_i, c_i), {"action": "render", "rowspan": 1, "colspan": 1})
                if m_info["action"] == "skip":
                    continue

                rspan = m_info.get("rowspan", 1)
                cspan = m_info.get("colspan", 1)

                val = _html.escape(str(cell if cell is not None else "")) or "&nbsp;"
                w_pt = sum(final_cols[c_i : c_i + cspan]) if c_i < len(final_cols) else 50.0
                cell_styles = [
                    "border:1px solid #334155",
                    "padding:4px 6px",
                    "vertical-align:middle",
                    f"width:{w_pt:.1f}pt",
                    "box-sizing:border-box"
                ]
                if bg_style:
                    cell_styles.append(bg_style)
                if h_style:
                    cell_styles.append(h_style)

                attrs = [f'style="{"; ".join(cell_styles)}"']
                if rspan > 1:
                    attrs.append(f'rowspan="{rspan}"')
                if cspan > 1:
                    attrs.append(f'colspan="{cspan}"')

                cells.append(f'<td {" ".join(attrs)}>{val}</td>')

            tr_style_attr = f' style="{h_style}"' if h_style else ""
            t_rows.append(f"<tr{tr_style_attr}>" + "".join(cells) + "</tr>")

        colgroup = "<colgroup>" + "".join(f'<col style="width:{w:.1f}pt;">' for w in final_cols) + "</colgroup>"
        blocks.append(
            f'<table border="1" cellspacing="0" cellpadding="4" style="border-collapse:collapse; width:{tbl_w:.1f}pt; max-width:100%; font-size:9.5pt; margin-bottom:12px;">'
            + colgroup
            + "".join(t_rows)
            + "</table>"
        )
    return "\n".join(blocks) if blocks else "<p>&nbsp;</p>"


def _rgb_match_to_hex(m):
    r = int(m.group(1))
    g = int(m.group(2))
    b = int(m.group(3))
    return f"#{r:02x}{g:02x}{b:02x}"


def sanitize_story_html(html: str) -> str:
    """
    Sanitizes HTML before feeding to PyMuPDF Story:
    1. Converts all rgb(r,g,b) and rgba(r,g,b,a) color declarations to #rrggbb hex,
       because PyMuPDF Story's CSS parser drops border declarations containing rgb/rgba!
    2. Ensures all <table> elements have border-collapse: collapse; max-width: 100%;
    3. Ensures all <td> and <th> elements preserve crisp borders (border: 1px solid #1e293b)
       and removes any accidental 'border:none' or 'border:0' from editor operations.
    """
    if not html:
        return html

    # 1. Convert rgb(...) and rgba(...) to #rrggbb hex colors
    html = re.sub(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*[\d.]+)?\s*\)', _rgb_match_to_hex, html, flags=re.IGNORECASE)

    # 2. Table styling
    def clean_table(m):
        attrs = m.group(1)
        if 'border-collapse' not in attrs:
            if 'style="' in attrs:
                attrs = re.sub(r'style="([^"]*)"', r'style="border-collapse:collapse; max-width:100%; \1"', attrs)
            else:
                attrs += ' style="border-collapse:collapse; max-width:100%;"'
        elif 'max-width' not in attrs:
            attrs = re.sub(r'style="([^"]*)"', r'style="max-width:100%; \1"', attrs)
        return f'<table {attrs}>'

    html = re.sub(r'<table\s+([^>]+)>', clean_table, html, flags=re.IGNORECASE)

    # 3. Clean td/th cells (including bare <td> and <th>)
    def clean_cell(m):
        tag = m.group(1)
        attrs = m.group(2) or ""
        attrs = re.sub(r'border\s*:\s*(none|0|transparent)[^;]*;?', '', attrs, flags=re.IGNORECASE)
        if 'border:' not in attrs and 'border-width:' not in attrs and 'border-top:' not in attrs:
            if 'style="' in attrs:
                attrs = re.sub(r'style="([^"]*)"', r'style="border:1px solid #1e293b; \1"', attrs)
            else:
                attrs += ' style="border:1px solid #1e293b;"'
        return f'<{tag} {attrs}>'

    html = re.sub(r'<(td|th)(?:\s+([^>]*))?>', clean_cell, html, flags=re.IGNORECASE)
    return html


def render_html_to_pdf(
    html_body: str,
    pdf_out_path: str,
    orientation: str = "portrait",
    margins: Tuple[float, float, float, float] = (36.0, 36.0, 36.0, 36.0)
) -> int:
    """
    Renders rich HTML (Word paragraphs, headings, bordered tables, Excel grids, Bengali + English Unicode)
    into a clean multi-page A4 PDF using PyMuPDF Story. Returns total page count.
    """
    if _fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required for Office document rendering")

    html_body = sanitize_story_html(html_body)
    top_m, right_m, bottom_m, left_m = margins

    full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {{
    size: A4 {"landscape" if orientation == "landscape" else "portrait"};
    margin: {top_m:.1f}pt {right_m:.1f}pt {bottom_m:.1f}pt {left_m:.1f}pt;
  }}
  body {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
    word-break: break-word;
    overflow-wrap: break-word;
    font-family: "Calibri", "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, "Kalpurush", "SolaimanLipi", "Nirmala UI", Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.18;
    color: #000000;
  }}
  h1, h2, h3 {{
    color: #000000;
    margin-top: 6px;
    margin-bottom: 4px;
    line-height: 1.2;
  }}
  p {{
    margin-top: 0;
    margin-bottom: 3.5pt;
    line-height: 1.18;
  }}
  table {{
    border-collapse: collapse;
    margin-top: 4pt;
    margin-bottom: 6pt;
  }}
  td, th {{
    border: 1px solid #1e293b;
    padding: 2.5pt 5.4pt;
    line-height: 1.15;
  }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""

    story = _fitz.Story(html=full_html)
    buf = io.BytesIO()
    writer = _fitz.DocumentWriter(buf)
    base_rect = _fitz.paper_rect("a4")
    if orientation == "landscape":
        mediabox = _fitz.Rect(0, 0, base_rect.height, base_rect.width)
    else:
        mediabox = base_rect
    where = _fitz.Rect(left_m, top_m, mediabox.width - right_m, mediabox.height - bottom_m)

    more = 1
    page_guard = 0
    while more and page_guard < 120:
        page_guard += 1
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()

    pdf_bytes = buf.getvalue()
    tmp_path = pdf_out_path + ".office.tmp"
    with open(tmp_path, "wb") as f:
        f.write(pdf_bytes)
    os.replace(tmp_path, pdf_out_path)
    _clear_nup_caches_for(pdf_out_path)

    with _fitz.open(pdf_out_path) as doc:
        return max(1, len(doc))


def convert_office_file_to_pdf_and_state(
    raw_path: str,
    ext: str,
    orig_filename: str,
    pdf_out_path: str,
    state_json_path: str
) -> Dict[str, Any]:
    """
    Converts an uploaded Word (.docx, .doc, .rtf, .txt), Excel (.xlsx, .xls, .csv), or PowerPoint (.pptx, .ppt)
    file into a high-fidelity printable PDF (preserving original tables, borders, slides, and layout)
    AND saves an editable JSON state file.
    """
    ext = ext.lower().lstrip(".")

    # 1. Try 100% native Office / LibreOffice PDF export first if available on the host OS
    native_ok = _try_native_office_to_pdf(raw_path, ext, pdf_out_path)

    if ext in EXCEL_EXTENSIONS:
        doc_kind = "excel"
        sheets = _parse_excel_to_sheets(raw_path, ext)
        max_cols = max((len(r) for s in sheets for r in s.get("rows", [])), default=4)
        orientation = "landscape" if max_cols >= 8 else "portrait"
        html_body = sheets_to_html(sheets, title=orig_filename, orientation=orientation)
        if not native_ok:
            pages = render_html_to_pdf(html_body, pdf_out_path, orientation=orientation)
        else:
            with _fitz.open(pdf_out_path) as d:
                pages = max(1, len(d))

    elif ext in PPT_EXTENSIONS:
        doc_kind = "ppt"
        sheets = None
        orientation = "landscape"
        if ext == "pptx":
            if not native_ok:
                html_body, pages = _render_pptx_to_pdf_and_html(raw_path, pdf_out_path)
            else:
                # Still extract slide text/tables for the editor state
                tmp_pdf = pdf_out_path + ".slides_meta.tmp"
                try:
                    html_body, _ = _render_pptx_to_pdf_and_html(raw_path, tmp_pdf)
                finally:
                    if os.path.isfile(tmp_pdf):
                        try:
                            os.remove(tmp_pdf)
                        except OSError:
                            pass
                with _fitz.open(pdf_out_path) as d:
                    pages = max(1, len(d))
        else:
            html_body = _parse_legacy_text_or_doc(raw_path, ext)
            if not native_ok:
                pages = render_html_to_pdf(html_body, pdf_out_path, orientation="landscape")
            else:
                with _fitz.open(pdf_out_path) as d:
                    pages = max(1, len(d))

    else:
        doc_kind = "word"
        sheets = None
        docx_margins = (36.0, 36.0, 36.0, 36.0)
        if ext == "docx":
            html_body, orientation, docx_margins = _parse_docx_to_html_and_meta(raw_path)
        else:
            html_body = _parse_legacy_text_or_doc(raw_path, ext)
            orientation = "portrait"

        # IMPORTANT: Do NOT use _fitz.open(raw_path, filetype="docx") because MuPDF's built-in docx loader
        # strips <w:tbl> tables into plain text. Instead, use native Office/LibreOffice or our full-table Story renderer!
        if not native_ok:
            pages = render_html_to_pdf(html_body, pdf_out_path, orientation=orientation, margins=docx_margins)
        else:
            with _fitz.open(pdf_out_path) as d:
                pages = max(1, len(d))

    state = {
        "doc_kind": doc_kind,
        "source_ext": ext,
        "title": orig_filename,
        "orientation": orientation,
        "margins": list(docx_margins) if "docx_margins" in locals() and docx_margins else [36.0, 36.0, 36.0, 36.0],
        "html": html_body,
        "sheets": sheets,
    }
    with open(state_json_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

    return {
        "doc_kind": doc_kind,
        "page_count": pages,
        "orientation": orientation,
    }


def extract_pdf_editable_state(pdf_path: str, orig_filename: str = "Document.pdf") -> Dict[str, Any]:
    """If a user wants to edit text from a regular PDF, extracts its paragraphs into an editable Word-like HTML state."""
    if _fitz is None:
        return {"doc_kind": "word", "title": orig_filename, "orientation": "portrait", "html": "<p>&nbsp;</p>", "sheets": None}
    ps: List[str] = []
    with _fitz.open(pdf_path) as doc:
        for p_idx in range(min(len(doc), 30)):
            txt = doc[p_idx].get_text("text").strip()
            if not txt:
                continue
            if p_idx > 0:
                ps.append(f'<hr><p style="color:#64748b; font-size:9pt;">— পৃষ্ঠা {p_idx + 1} —</p>')
            for para in txt.split("\n\n"):
                lines = [_html.escape(ln.strip()) for ln in para.splitlines() if ln.strip()]
                if lines:
                    ps.append("<p style='margin:4px 0;'>" + "<br>".join(lines) + "</p>")
    return {
        "doc_kind": "word",
        "title": orig_filename,
        "orientation": "portrait",
        "html": "\n".join(ps) if ps else "<p>নতুন লেখা যোগ করুন...</p>",
        "sheets": None,
    }
