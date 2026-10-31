"""Contact sheet renderer: ~1600px JPG, 4x3, letterboxed (no centre crop),
numbered cells, text labels for flags/usage.

Handover §9.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .paths import Home


@dataclass
class SheetCell:
    idx: int
    image_path: Path | None = None
    kind: str = "photo"
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    license_id: str | None = None
    flags: list[str] = field(default_factory=list)
    used_in: list[str] = field(default_factory=list)
    title: str = ""


def _draw_dashed_rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], color: tuple[int, int, int], width: int = 2) -> None:
    x0, y0, x1, y1 = box
    dash_len = 6
    # Top and bottom
    for x in range(x0, x1, dash_len * 2):
        draw.line([(x, y0), (min(x + dash_len, x1), y0)], fill=color, width=width)
        draw.line([(x, y1), (min(x + dash_len, x1), y1)], fill=color, width=width)
    # Left and right
    for y in range(y0, y1, dash_len * 2):
        draw.line([(x0, y), (x0, min(y + dash_len, y1))], fill=color, width=width)
        draw.line([(x1, y), (x1, min(y + dash_len, y1))], fill=color, width=width)


def render_contact_sheet(
    cells: list[SheetCell],
    output_path: Path,
    cols: int = 4,
    rows: int = 3,
    sheet_width: int = 1600,
) -> Path:
    """Render a 4x3 contact sheet image and save as JPEG."""
    cell_w = sheet_width // cols
    cell_h = int(cell_w * 0.75)  # 4:3 aspect for cells
    sheet_height = cell_h * rows

    sheet = Image.new("RGB", (sheet_width, sheet_height), color=(18, 18, 18))
    draw = ImageDraw.Draw(sheet)

    font = ImageFont.load_default()

    for i, cell in enumerate(cells[: cols * rows]):
        r = i // cols
        c = i % cols
        x0 = c * cell_w
        y0 = r * cell_h
        x1 = x0 + cell_w
        y1 = y0 + cell_h

        # Cell border / background
        draw.rectangle([x0 + 2, y0 + 2, x1 - 2, y1 - 2], fill=(28, 28, 28), outline=(45, 45, 45))

        # Thumbnail rendering (letterbox inside cell)
        inner_pad = 8
        bottom_bar_h = 24
        thumb_box_w = cell_w - (inner_pad * 2)
        thumb_box_h = cell_h - (inner_pad * 2) - bottom_bar_h

        loaded_img = None
        if cell.image_path and cell.image_path.is_file():
            try:
                with Image.open(cell.image_path) as src:
                    # Convert to RGB if needed
                    src_rgb = src.convert("RGB")
                    src_w, src_h = src_rgb.size
                    scale = min(thumb_box_w / src_w, thumb_box_h / src_h)
                    target_w = max(1, int(src_w * scale))
                    target_h = max(1, int(src_h * scale))
                    loaded_img = src_rgb.resize((target_w, target_h), Image.Resampling.BILINEAR)
            except Exception:
                loaded_img = None

        if loaded_img:
            tx = x0 + inner_pad + (thumb_box_w - loaded_img.width) // 2
            ty = y0 + inner_pad + (thumb_box_h - loaded_img.height) // 2
            sheet.paste(loaded_img, (tx, ty))
        else:
            # Placeholder text
            placeholder_text = f"[{cell.kind.upper()}]\n{cell.title[:25]}"
            draw.text((x0 + inner_pad + 10, y0 + inner_pad + 30), placeholder_text, fill=(140, 140, 140), font=font)

        # Flag warning border (thick yellow)
        if cell.flags:
            flag_str = f"⚠ {', '.join(cell.flags)}"
            draw.rectangle([x0 + 4, y0 + 4, x1 - 4, y1 - 4], outline=(230, 180, 0), width=3)
            draw.rectangle([x0 + 6, y0 + 6, x0 + 10 + len(flag_str) * 7, y0 + 22], fill=(230, 180, 0))
            draw.text((x0 + 8, y0 + 8), flag_str, fill=(0, 0, 0), font=font)

        # Used indicator (dashed border)
        elif cell.used_in:
            used_str = f"used: {cell.used_in[0]}"
            _draw_dashed_rect(draw, (x0 + 4, y0 + 4, x1 - 4, y1 - 4), color=(100, 160, 240), width=2)
            draw.rectangle([x0 + 6, y0 + 6, x0 + 10 + len(used_str) * 7, y0 + 22], fill=(40, 70, 120))
            draw.text((x0 + 8, y0 + 8), used_str, fill=(255, 255, 255), font=font)

        # Top-left number badge (black background, white bold text)
        idx_str = f" #{cell.idx} "
        badge_w = max(24, len(idx_str) * 7)
        badge_y = y0 + 6 if (not cell.flags and not cell.used_in) else y0 + 26
        draw.rectangle([x0 + 6, badge_y, x0 + 6 + badge_w, badge_y + 16], fill=(0, 0, 0), outline=(255, 255, 255))
        draw.text((x0 + 8, badge_y + 2), idx_str, fill=(255, 255, 255), font=font)

        # Bottom info bar
        res_str = f"{cell.width}x{cell.height}" if cell.width and cell.height else ""
        dur_str = f"{int(cell.duration)}s" if cell.duration else ""
        lic_str = cell.license_id or ""
        info_parts = [p for p in (cell.kind, res_str, dur_str, lic_str) if p]
        info_line = " • ".join(info_parts)

        bar_y0 = y1 - bottom_bar_h - 4
        bar_y1 = y1 - 4
        draw.rectangle([x0 + 4, bar_y0, x1 - 4, bar_y1], fill=(15, 15, 15))
        draw.text((x0 + 8, bar_y0 + 5), info_line[:40], fill=(210, 210, 210), font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, "JPEG", quality=85)
    return output_path
