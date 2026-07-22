from __future__ import annotations

from pathlib import Path

import fitz
from PIL import Image

from .models import NormalizedRect


class PdfService:
    PREVIEW_DPI = 180
    MAX_DETAIL_DPI = 300

    def render_first_page(self, path: Path, dpi: int = PREVIEW_DPI) -> Image.Image:
        with fitz.open(path) as document:
            if document.page_count != 1:
                raise ValueError(f"首版仅支持单页 PDF，当前文件有 {document.page_count} 页")
            page = document[0]
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

    def render_region(
        self,
        path: Path,
        rect: NormalizedRect,
        rotation: int = 0,
        dpi: int = MAX_DETAIL_DPI,
    ) -> Image.Image:
        """Render one visible region without allocating a high-resolution full page.

        ``rect`` uses the normalized coordinates currently visible in the UI,
        including the user's additional 90-degree rotations. PyMuPDF receives
        coordinates in the page's original displayed orientation, so the
        rectangle is mapped back before rendering and the resulting patch is
        rotated into the UI orientation afterwards.
        """

        display_rect = rect.clamped()
        if display_rect.width <= 0 or display_rect.height <= 0:
            raise ValueError("高清预览区域为空")
        source_rect = self.unrotate_normalized_rect(display_rect, rotation)
        dpi = min(max(int(dpi), self.PREVIEW_DPI), self.MAX_DETAIL_DPI)

        with fitz.open(path) as document:
            if document.page_count != 1:
                raise ValueError(f"首版仅支持单页 PDF，当前文件有 {document.page_count} 页")
            page = document[0]
            page_rect = page.rect
            clip = fitz.Rect(
                page_rect.x0 + source_rect.x * page_rect.width,
                page_rect.y0 + source_rect.y * page_rect.height,
                page_rect.x0 + (source_rect.x + source_rect.width) * page_rect.width,
                page_rect.y0 + (source_rect.y + source_rect.height) * page_rect.height,
            )
            pixmap = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        return self.rotate(image, rotation)

    @staticmethod
    def unrotate_normalized_rect(rect: NormalizedRect, degrees_ccw: int) -> NormalizedRect:
        """Map a rectangle from the rotated preview back to the base page."""

        rect = rect.clamped()
        rotation = degrees_ccw % 360
        if rotation == 0:
            return rect
        if rotation == 90:
            return NormalizedRect(
                1 - rect.y - rect.height,
                rect.x,
                rect.height,
                rect.width,
            ).clamped()
        if rotation == 180:
            return NormalizedRect(
                1 - rect.x - rect.width,
                1 - rect.y - rect.height,
                rect.width,
                rect.height,
            ).clamped()
        if rotation == 270:
            return NormalizedRect(
                rect.y,
                1 - rect.x - rect.width,
                rect.height,
                rect.width,
            ).clamped()
        raise ValueError("预览旋转角度必须是 90° 的倍数")

    @staticmethod
    def rotate(image: Image.Image, degrees_ccw: int) -> Image.Image:
        degrees_ccw %= 360
        if degrees_ccw == 0:
            return image.copy()
        return image.rotate(degrees_ccw, expand=True, fillcolor="white")

    @staticmethod
    def crop(image: Image.Image, rect: NormalizedRect, padding: int = 8) -> Image.Image:
        rect = rect.clamped()
        left = max(0, round(rect.x * image.width) - padding)
        top = max(0, round(rect.y * image.height) - padding)
        right = min(image.width, round((rect.x + rect.width) * image.width) + padding)
        bottom = min(image.height, round((rect.y + rect.height) * image.height) + padding)
        if right <= left or bottom <= top:
            raise ValueError("识别框面积过小")
        return image.crop((left, top, right, bottom))
