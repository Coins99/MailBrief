"""Read and drive the brief MainWindow shows in its workspace."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from mailbrief.ui.brief_detail import BriefDetailPane
from mailbrief.ui.main_window import MainWindow


def detail(window: MainWindow) -> BriefDetailPane:
    return window.workspace.detail


def select_item(window: MainWindow, message_key: str) -> None:
    """Select the email whose ``message_key`` is given; its detail is shown."""
    view = window.workspace.brief_list
    for number, row in enumerate(view.brief_model.rows()):
        if row.item is not None and row.item.message_key == message_key:
            view.setCurrentIndex(view.model().index(number, 0))
            return
    raise AssertionError(f"No email {message_key!r} in the brief.")


def shown_text(window: MainWindow) -> str:
    """Everything the brief shows, one line each: the heading's title, the meta's and the
    coverage's full accessible names, every list row, then the detail's labels and buttons
    in order (button text without its ``&`` escaping)."""
    workspace = window.workspace
    lines: list[str] = []
    if not workspace.heading.isHidden():
        lines += [workspace.heading.title.text(), workspace.heading.meta.accessibleName()]
    lines.append(workspace.coverage.accessibleName())
    model = workspace.brief_list.model()
    for number in range(model.rowCount()):
        lines.append(str(model.index(number, 0).data(Qt.ItemDataRole.AccessibleTextRole)))
    content = workspace.detail.widget()
    if content is not None:
        # Depth first, in creation order: the order the detail lays them out.
        for widget in content.findChildren(QWidget):
            if isinstance(widget, QPushButton):
                lines.append(widget.text().replace("&&", "&"))
            elif isinstance(widget, QLabel) and widget.text():
                lines.append(widget.text())
    return "\n".join(lines)
