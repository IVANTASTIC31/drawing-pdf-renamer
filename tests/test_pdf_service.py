from __future__ import annotations

import fitz
import pytest

from drawing_renamer.models import NormalizedRect
from drawing_renamer.pdf_service import PdfService


def test_unrotate_normalized_rect_for_quarter_turns() -> None:
    service = PdfService()
    displayed = NormalizedRect(0.10, 0.20, 0.30, 0.40)

    def values(rect: NormalizedRect) -> tuple[float, float, float, float]:
        return rect.x, rect.y, rect.width, rect.height

    assert values(service.unrotate_normalized_rect(displayed, 90)) == pytest.approx((0.40, 0.10, 0.40, 0.30))
    assert values(service.unrotate_normalized_rect(displayed, 180)) == pytest.approx((0.60, 0.40, 0.30, 0.40))
    assert values(service.unrotate_normalized_rect(displayed, 270)) == pytest.approx((0.20, 0.60, 0.40, 0.30))
    with pytest.raises(ValueError):
        service.unrotate_normalized_rect(displayed, 45)


def test_render_region_allocates_only_the_requested_area(tmp_path) -> None:
    path = tmp_path / "region.pdf"
    document = fitz.open()
    page = document.new_page(width=200, height=100)
    page.draw_rect(fitz.Rect(0, 0, 100, 100), color=(0, 0, 0), fill=(1, 0, 0))
    page.draw_rect(fitz.Rect(100, 0, 200, 100), color=(0, 0, 0), fill=(0, 0, 1))
    document.save(path)
    document.close()

    image = PdfService().render_region(
        path,
        NormalizedRect(0.5, 0.0, 0.5, 1.0),
        dpi=300,
    )

    assert 415 <= image.width <= 418
    assert 415 <= image.height <= 418
    red, green, blue = image.getpixel((image.width // 2, image.height // 2))
    assert blue > 240 and red < 20 and green < 20


def test_render_region_respects_preview_rotation(tmp_path) -> None:
    path = tmp_path / "rotated-region.pdf"
    document = fitz.open()
    document.new_page(width=200, height=100)
    document.save(path)
    document.close()

    image = PdfService().render_region(
        path,
        NormalizedRect(0.0, 0.0, 0.5, 1.0),
        rotation=90,
        dpi=300,
    )

    assert image.height > image.width * 3


def test_multi_page_pdf_can_render_each_page_and_region(tmp_path) -> None:
    path = tmp_path / "multi-page.pdf"
    document = fitz.open()
    first = document.new_page(width=200, height=100)
    first.draw_rect(first.rect, color=(1, 0, 0), fill=(1, 0, 0))
    second = document.new_page(width=200, height=100)
    second.draw_rect(second.rect, color=(0, 0, 1), fill=(0, 0, 1))
    document.save(path)
    document.close()

    service = PdfService()
    assert service.page_count(path) == 2
    first_image = service.render_page(path, 0)
    second_image = service.render_page(path, 1)
    second_region = service.render_region(
        path,
        NormalizedRect(0.25, 0.25, 0.5, 0.5),
        page_index=1,
    )

    first_pixel = first_image.getpixel((first_image.width // 2, first_image.height // 2))
    second_pixel = second_image.getpixel((second_image.width // 2, second_image.height // 2))
    region_pixel = second_region.getpixel((second_region.width // 2, second_region.height // 2))
    assert first_pixel[0] > 240 and first_pixel[2] < 20
    assert second_pixel[2] > 240 and second_pixel[0] < 20
    assert region_pixel[2] > 240 and region_pixel[0] < 20

    with pytest.raises(ValueError, match="页码超出范围"):
        service.render_page(path, 2)
