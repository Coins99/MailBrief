"""Main MailBrief window."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow


class MainWindow(QMainWindow):
    """Placeholder shell for the future connected workflow."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MailBrief")
        self.resize(900, 600)

        placeholder = QLabel("MailBrief MVP foundation is ready.")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCentralWidget(placeholder)
