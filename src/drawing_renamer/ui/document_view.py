from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QPixmap, QTransform, QWheelEvent
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)
from PIL import Image

from ..models import FieldKind, NormalizedRect


COLORS = {
    FieldKind.MATERIAL: QColor("#2478ff"),
    FieldKind.NAME: QColor("#16a36a"),
    FieldKind.PROCESS: QColor("#f08a24"),
}
BOX_PEN_WIDTH = 1.0


def box_pen(color: QColor) -> QPen:
    pen = QPen(color)
    pen.setWidthF(BOX_PEN_WIDTH)
    pen.setCosmetic(True)
    return pen


def pil_to_pixmap(image: Image.Image) -> QPixmap:
    rgb = image.convert("RGB")
    raw = rgb.tobytes("raw", "RGB")
    qimage = QImage(raw, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimage.copy())


class RoiItem(QGraphicsRectItem):
    HANDLE_SIZE = 18.0

    def __init__(self, kind: FieldKind, rect: QRectF, bounds: QRectF) -> None:
        super().__init__(rect)
        self.kind = kind
        self.bounds = bounds
        self.resizing = False
        self.resize_origin = QPointF()
        self.original_rect = QRectF()
        self.setFlags(
            QGraphicsRectItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsRectItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        color = COLORS[kind]
        self.setPen(box_pen(color))
        fill = QColor(color)
        fill.setAlpha(42)
        self.setBrush(fill)
        # Keep labels in screen coordinates so they remain crisp and the same
        # readable size while the drawing is zoomed in or out.
        self.label_background = QGraphicsRectItem(self)
        self.label_background.setBrush(color)
        self.label_background.setPen(QPen(Qt.PenStyle.NoPen))
        self.label_background.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations,
            True,
        )
        self.label_background.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

        self.label_item = QGraphicsSimpleTextItem(kind.label, self.label_background)
        self.label_item.setBrush(Qt.GlobalColor.white)
        self.label_item.setPen(QPen(Qt.PenStyle.NoPen))
        font = self.label_item.font()
        font.setBold(True)
        font.setPixelSize(14)
        self.label_item.setFont(font)
        self.label_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

        text_bounds = self.label_item.boundingRect()
        label_width = text_bounds.width() + 12
        label_height = text_bounds.height() + 6
        self.label_background.setRect(0, -label_height, label_width, label_height)
        self.label_item.setPos(6, -label_height + 3)
        self._position_label()

    def _position_label(self) -> None:
        self.label_background.setPos(self.rect().left(), self.rect().top())

    def _in_handle(self, point: QPointF) -> bool:
        rect = self.rect()
        return (
            rect.right() - self.HANDLE_SIZE <= point.x() <= rect.right() + self.HANDLE_SIZE
            and rect.bottom() - self.HANDLE_SIZE <= point.y() <= rect.bottom() + self.HANDLE_SIZE
        )

    def hoverMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.setCursor(Qt.CursorShape.SizeFDiagCursor if self._in_handle(event.pos()) else Qt.CursorShape.SizeAllCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton and self._in_handle(event.pos()):
            self.resizing = True
            self.resize_origin = event.scenePos()
            self.original_rect = QRectF(self.rect())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.resizing:
            delta = event.scenePos() - self.resize_origin
            rect = QRectF(self.original_rect)
            rect.setWidth(max(30.0, rect.width() + delta.x()))
            rect.setHeight(max(22.0, rect.height() + delta.y()))
            rect = rect.intersected(self.bounds.translated(-self.pos()))
            self.setRect(rect)
            self._position_label()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        was_resizing = self.resizing
        self.resizing = False
        super().mouseReleaseEvent(event)
        view = self.scene().views()[0] if self.scene() and self.scene().views() else None
        if isinstance(view, DocumentGraphicsView):
            view.keep_item_in_bounds(self)
            view.boxFinished.emit(self.kind)
        if was_resizing:
            event.accept()

    def itemChange(self, change, value):  # type: ignore[no-untyped-def]
        if change == QGraphicsRectItem.GraphicsItemChange.ItemPositionChange:
            new_pos = value
            rect = self.rect().translated(new_pos)
            dx = 0.0
            dy = 0.0
            if rect.left() < self.bounds.left():
                dx = self.bounds.left() - rect.left()
            elif rect.right() > self.bounds.right():
                dx = self.bounds.right() - rect.right()
            if rect.top() < self.bounds.top():
                dy = self.bounds.top() - rect.top()
            elif rect.bottom() > self.bounds.bottom():
                dy = self.bounds.bottom() - rect.bottom()
            return QPointF(new_pos.x() + dx, new_pos.y() + dy)
        return super().itemChange(change, value)

    def scene_rect(self) -> QRectF:
        return self.rect().translated(self.pos())


class DocumentGraphicsView(QGraphicsView):
    boxesChanged = Signal()
    boxFinished = Signal(object)
    highResolutionRequested = Signal(object, int)
    highResolutionCancelled = Signal()

    DETAIL_DEBOUNCE_MS = 180
    DETAIL_ZOOM_THRESHOLD = 1.02

    def __init__(self, parent=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor("#24282f"))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.pixmap_item: QGraphicsPixmapItem | None = None
        self.high_res_item: QGraphicsPixmapItem | None = None
        self.roi_items: dict[FieldKind, RoiItem] = {}
        self.preview_dpi = 180
        self.max_detail_dpi = 300
        self.active_kind: FieldKind | None = None
        self._drawing = False
        self._start = QPointF()
        self._draft: QGraphicsRectItem | None = None
        self._last_detail_request: tuple[float, float, float, float, int] | None = None
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(self.DETAIL_DEBOUNCE_MS)
        self._detail_timer.timeout.connect(self._request_visible_detail)
        self.horizontalScrollBar().valueChanged.connect(self._schedule_visible_detail)
        self.verticalScrollBar().valueChanged.connect(self._schedule_visible_detail)

    @property
    def image_rect(self) -> QRectF:
        return self.pixmap_item.boundingRect() if self.pixmap_item else QRectF()

    def set_image(
        self,
        image: Image.Image,
        boxes: dict[FieldKind, NormalizedRect] | None = None,
        preview_dpi: int = 180,
        max_detail_dpi: int = 300,
    ) -> None:
        self._detail_timer.stop()
        self.scene().clear()
        self.high_res_item = None
        self.roi_items.clear()
        self.preview_dpi = preview_dpi
        self.max_detail_dpi = max(max_detail_dpi, preview_dpi)
        self._last_detail_request = None
        pixmap = pil_to_pixmap(image)
        self.pixmap_item = self.scene().addPixmap(pixmap)
        self.pixmap_item.setZValue(-10)
        self.pixmap_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.scene().setSceneRect(self.pixmap_item.boundingRect())
        for kind, rect in (boxes or {}).items():
            self.add_box(kind, rect)
        self.fit_to_window()

    def set_high_resolution_region(self, image: Image.Image, normalized: NormalizedRect) -> None:
        """Overlay a crisp visible-region render without changing scene coordinates."""

        if not self.pixmap_item:
            return
        self._remove_high_resolution_item()
        bounds = self.image_rect
        normalized = normalized.clamped()
        target = QRectF(
            normalized.x * bounds.width(),
            normalized.y * bounds.height(),
            normalized.width * bounds.width(),
            normalized.height * bounds.height(),
        ).intersected(bounds)
        if target.isEmpty() or image.width <= 0 or image.height <= 0:
            return
        item = self.scene().addPixmap(pil_to_pixmap(image))
        item.setPos(target.left(), target.top())
        item.setTransform(
            QTransform.fromScale(
                target.width() / image.width,
                target.height() / image.height,
            )
        )
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        item.setZValue(-5)
        self.high_res_item = item

    def clear_high_resolution(self, notify: bool = True) -> None:
        had_detail = self.high_res_item is not None or self._last_detail_request is not None
        self._detail_timer.stop()
        self._remove_high_resolution_item()
        self._last_detail_request = None
        if notify and had_detail:
            self.highResolutionCancelled.emit()

    def _remove_high_resolution_item(self) -> None:
        if self.high_res_item is not None:
            if self.high_res_item.scene() is self.scene():
                self.scene().removeItem(self.high_res_item)
            self.high_res_item = None

    def set_active_kind(self, kind: FieldKind | None) -> None:
        self.active_kind = kind
        self.setDragMode(
            QGraphicsView.DragMode.NoDrag if kind is not None else QGraphicsView.DragMode.ScrollHandDrag
        )
        self.viewport().setCursor(Qt.CursorShape.CrossCursor if kind else Qt.CursorShape.OpenHandCursor)

    def add_box(self, kind: FieldKind, normalized: NormalizedRect) -> None:
        if not self.pixmap_item:
            return
        if kind in self.roi_items:
            self.scene().removeItem(self.roi_items.pop(kind))
        bounds = self.image_rect
        rect = QRectF(
            normalized.x * bounds.width(),
            normalized.y * bounds.height(),
            normalized.width * bounds.width(),
            normalized.height * bounds.height(),
        ).intersected(bounds)
        if rect.width() < 10 or rect.height() < 10:
            return
        item = RoiItem(kind, rect, bounds)
        item.setZValue(5)
        self.scene().addItem(item)
        self.roi_items[kind] = item

    def remove_box(self, kind: FieldKind) -> None:
        item = self.roi_items.pop(kind, None)
        if item:
            self.scene().removeItem(item)
            self.boxesChanged.emit()

    def clear_boxes(self) -> None:
        for item in list(self.roi_items.values()):
            self.scene().removeItem(item)
        self.roi_items.clear()
        self.boxesChanged.emit()

    def normalized_boxes(self) -> dict[FieldKind, NormalizedRect]:
        bounds = self.image_rect
        if bounds.isEmpty():
            return {}
        result: dict[FieldKind, NormalizedRect] = {}
        for kind, item in self.roi_items.items():
            rect = item.scene_rect()
            result[kind] = NormalizedRect(
                rect.x() / bounds.width(),
                rect.y() / bounds.height(),
                rect.width() / bounds.width(),
                rect.height() / bounds.height(),
            ).clamped()
        return result

    def keep_item_in_bounds(self, item: RoiItem) -> None:
        item.setPos(item.pos())
        self.boxesChanged.emit()

    def fit_to_window(self) -> None:
        if self.pixmap_item:
            self.fitInView(self.image_rect, Qt.AspectRatioMode.KeepAspectRatio)
            self._schedule_visible_detail()

    def zoom_in(self) -> None:
        self.scale(1.2, 1.2)
        self._schedule_visible_detail()

    def zoom_out(self) -> None:
        self.scale(1 / 1.2, 1 / 1.2)
        self._schedule_visible_detail()

    def wheelEvent(self, event: QWheelEvent) -> None:
        self.scale(1.15 if event.angleDelta().y() > 0 else 1 / 1.15, 1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
        self._schedule_visible_detail()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._schedule_visible_detail()

    def _schedule_visible_detail(self) -> None:
        if self.pixmap_item is not None:
            self._detail_timer.start()

    def _request_visible_detail(self) -> None:
        if self.pixmap_item is None:
            return
        view_scale = max(abs(self.transform().m11()), abs(self.transform().m22()))
        if view_scale <= self.DETAIL_ZOOM_THRESHOLD:
            self.clear_high_resolution()
            return

        target_dpi = min(
            self.max_detail_dpi,
            int(math.ceil((self.preview_dpi * view_scale) / 30.0) * 30),
        )
        if target_dpi <= self.preview_dpi:
            self.clear_high_resolution()
            return

        bounds = self.image_rect
        visible = self.mapToScene(self.viewport().rect()).boundingRect().intersected(bounds)
        if visible.isEmpty():
            return
        margin_x = visible.width() * 0.12
        margin_y = visible.height() * 0.12
        visible = visible.adjusted(-margin_x, -margin_y, margin_x, margin_y).intersected(bounds)
        normalized = NormalizedRect(
            visible.x() / bounds.width(),
            visible.y() / bounds.height(),
            visible.width() / bounds.width(),
            visible.height() / bounds.height(),
        ).clamped()
        request_key = (
            round(normalized.x, 4),
            round(normalized.y, 4),
            round(normalized.width, 4),
            round(normalized.height, 4),
            target_dpi,
        )
        if request_key == self._last_detail_request:
            return
        self._last_detail_request = request_key
        self.highResolutionRequested.emit(normalized, target_dpi)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.active_kind is not None and event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            clicked = self.scene().itemAt(scene_pos, self.transform())
            if clicked in (self.pixmap_item, self.high_res_item) or clicked is None:
                self._drawing = True
                self._start = self._bounded(scene_pos)
                color = COLORS[self.active_kind]
                self._draft = self.scene().addRect(QRectF(self._start, self._start), box_pen(color))
                self._draft.setZValue(8)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drawing and self._draft:
            current = self._bounded(self.mapToScene(event.position().toPoint()))
            self._draft.setRect(QRectF(self._start, current).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drawing and self._draft and self.active_kind:
            rect = self._draft.rect().intersected(self.image_rect)
            self.scene().removeItem(self._draft)
            self._draft = None
            self._drawing = False
            if rect.width() >= 20 and rect.height() >= 15:
                bounds = self.image_rect
                normalized = NormalizedRect(
                    rect.x() / bounds.width(),
                    rect.y() / bounds.height(),
                    rect.width() / bounds.width(),
                    rect.height() / bounds.height(),
                )
                kind = self.active_kind
                self.add_box(kind, normalized)
                self.boxesChanged.emit()
                self.boxFinished.emit(kind)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _bounded(self, point: QPointF) -> QPointF:
        bounds = self.image_rect
        return QPointF(
            min(max(point.x(), bounds.left()), bounds.right()),
            min(max(point.y(), bounds.top()), bounds.bottom()),
        )
