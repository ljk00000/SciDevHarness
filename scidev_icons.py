"""Small, theme-aware vector icons for the desktop workbench."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def _icon_paths(name: str) -> tuple[QPainterPath, ...]:
    if name == "explorer":
        folder = QPainterPath()
        folder.moveTo(3.5, 7.25)
        folder.lineTo(8.5, 7.25)
        folder.lineTo(10.25, 9.25)
        folder.lineTo(20.5, 9.25)
        folder.lineTo(19.25, 19.5)
        folder.lineTo(4.25, 19.5)
        folder.closeSubpath()
        return (folder,)

    if name == "search":
        handle = QPainterPath()
        handle.moveTo(14.6, 14.6)
        handle.lineTo(20.1, 20.1)
        return (handle,)

    if name == "git":
        trunk = QPainterPath()
        trunk.moveTo(7, 5)
        trunk.lineTo(7, 19)
        branch = QPainterPath()
        branch.moveTo(7, 9)
        branch.cubicTo(7, 14, 17, 9, 17, 15)
        return (trunk, branch)

    raise ValueError(f"unknown activity icon: {name}")


@lru_cache(maxsize=32)
def _render_icon(name: str, color_name: str) -> QPixmap:
    pixmap = QPixmap(48, 48)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    pen = QPen(QColor(color_name), 1.75, Qt.PenStyle.SolidLine)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if name == "search":
        painter.drawEllipse(QRectF(3.9, 3.9, 11.5, 11.5))
    elif name == "git":
        painter.drawEllipse(QRectF(5.2, 3.2, 3.6, 3.6))
        painter.drawEllipse(QRectF(5.2, 17.2, 3.6, 3.6))
        painter.drawEllipse(QRectF(15.2, 13.2, 3.6, 3.6))

    for path in _icon_paths(name):
        painter.drawPath(path)
    painter.end()
    return pixmap


@lru_cache(maxsize=24)
def activity_icon(name: str, muted_color: str, accent_color: str) -> QIcon:
    """Create crisp normal/hover/selected states without relying on system fonts."""
    icon = QIcon()
    icon.addPixmap(_render_icon(name, muted_color), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(_render_icon(name, accent_color), QIcon.Mode.Active, QIcon.State.Off)
    icon.addPixmap(_render_icon(name, accent_color), QIcon.Mode.Normal, QIcon.State.On)
    icon.addPixmap(_render_icon(name, accent_color), QIcon.Mode.Active, QIcon.State.On)
    return icon
