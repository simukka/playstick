"""Entry point: `python -m playstick_admin` or the `playstick-admin` script."""

import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from . import settings as settings_mod


def main(argv=None):
    QCoreApplication.setOrganizationName(settings_mod.ORG)
    QCoreApplication.setApplicationName(settings_mod.APP)
    app = QApplication(argv if argv is not None else sys.argv)
    # Imported here so a headless import of this module (e.g. in a test) does
    # not require the widgets to be constructed.
    from .app import MainWindow
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
