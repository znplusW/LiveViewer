from collections import OrderedDict
from PySide6.QtCore import QAbstractListModel, Qt, QSize, QRect, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QFont
from PySide6.QtWidgets import QStyledItemDelegate, QStyle
from .ui_theme import tokens_for


OVERVIEW_DEFAULT_WIDTH = 204
OVERVIEW_MIN_WIDTH = 120
OVERVIEW_MAX_WIDTH = 480


def overview_tile_size(width):
    return QSize(width, round((width - 20) * 2 / 3) + 43)


class CatalogModel(QAbstractListModel):
    thumbnail_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.thumbnails = OrderedDict()
        self.pending = set()
        self.failures = set()

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return item.image_path.name
        if role == Qt.ItemDataRole.UserRole:
            return item
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(item.image_path) + ('\n' + item.note if item.note else '')
        if role == Qt.ItemDataRole.DecorationRole:
            key = str(item.image_path)
            if key in self.thumbnails:
                self.thumbnails.move_to_end(key)
                return self.thumbnails[key]
            if key not in self.pending and key not in self.failures:
                self.pending.add(key)
                self.thumbnail_requested.emit(item.image_path)
        return None

    def set_items(self, items):
        self.beginResetModel()
        self.items = list(items)
        self.endResetModel()

    def put_thumbnail(self, path, image):
        key = str(path)
        self.pending.discard(key)
        if image is None or image.isNull():
            self.failures.add(key)
        else:
            self.thumbnails[key] = QPixmap.fromImage(image)
            while len(self.thumbnails) > 384:
                self.thumbnails.popitem(last=False)
        for row, item in enumerate(self.items):
            if item.image_path == path:
                ix = self.index(row)
                self.dataChanged.emit(ix, ix, [Qt.ItemDataRole.DecorationRole])


class ThumbnailDelegate(QStyledItemDelegate):
    def __init__(self, parent=None, large=False):
        super().__init__(parent)
        self.large = large
        self.thumbnail_width = OVERVIEW_DEFAULT_WIDTH
        self.thumbnail_height = 116

    def set_thumbnail_width(self, width):
        self.thumbnail_width = width

    def set_thumbnail_height(self, height):
        height = max(64, int(height))
        changed = self.thumbnail_height != height
        self.thumbnail_height = height
        return changed

    def sizeHint(self, option, index):
        return (overview_tile_size(self.thumbnail_width) if self.large else
                QSize(round(self.thumbnail_height * 144 / 116), self.thumbnail_height))

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        colors = tokens_for(option.widget or self.parent())
        rect = option.rect.adjusted(5, 5, -5, -5)
        painter.setPen(QPen(QColor(colors['thumbnail_selected_border' if selected else 'thumbnail_border']), 2 if selected else 1))
        painter.setBrush(QColor(colors['thumbnail_selected' if selected else 'thumbnail']))
        painter.drawRoundedRect(rect, 8, 8)
        image_rect = rect.adjusted(5, 5, -5, -29)
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            scaled = pixmap.scaled(image_rect.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            x = image_rect.x() + (image_rect.width() - scaled.width()) // 2
            y = image_rect.y() + (image_rect.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            painter.setPen(QColor(colors['muted']))
            painter.drawText(image_rect, Qt.AlignmentFlag.AlignCenter, '· · ·')
        item = index.data(Qt.ItemDataRole.UserRole)
        if item.is_live:
            badge = QRect(image_rect.x()+4, image_rect.y()+4, 47, 20)
            painter.fillRect(badge, QColor(colors['badge_background']))
            painter.setPen(QColor(colors['badge_text']))
            font = QFont(option.font)
            font.setPointSize(8)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, '◉ LIVE')
        if item.pending or item.uncertain:
            painter.setPen(QColor(colors['thumbnail_selected_border']))
            painter.drawText(image_rect.adjusted(0, 0, -4, -2), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, '…' if item.pending else '!')
        painter.setFont(option.font)
        painter.setPen(QColor(colors['thumbnail_selected_text' if selected else 'thumbnail_text']))
        title = option.fontMetrics.elidedText(item.image_path.name, Qt.TextElideMode.ElideMiddle, rect.width()-12)
        painter.drawText(rect.adjusted(6, rect.height()-24, -6, -4), Qt.AlignmentFlag.AlignCenter, title)
        painter.restore()
