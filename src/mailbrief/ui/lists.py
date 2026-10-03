"""List widgets shared by the window's panels and dialogs."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QListWidget


class ActivatingList(QListWidget):
    """Return and Enter activate the current row exactly once, on every platform.

    Qt's item views differ by platform: on macOS they only try to edit an item on Return,
    and elsewhere they activate it and then let the key reach the dialog's default button,
    which would act a second time. Here the key activates the row and is consumed.
    """

    def keyPressEvent(self, event: QKeyEvent) -> None:
        item = self.currentItem()
        if item is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.itemActivated.emit(item)
            event.accept()
            return
        super().keyPressEvent(event)
