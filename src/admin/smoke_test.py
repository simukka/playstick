"""Offscreen smoke test: construct the whole window and pump the event loop
briefly. Not part of the unittest suite (it needs PySide6); run by hand from the
admin venv:

    QT_QPA_PLATFORM=offscreen python src/admin/smoke_test.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtWidgets import QApplication

from playstick_admin import settings as settings_mod


def main():
    QCoreApplication.setOrganizationName(settings_mod.ORG)
    QCoreApplication.setApplicationName(settings_mod.APP)
    app = QApplication(sys.argv)

    # Seed a source library so the window does not pop the settings dialog.
    tmp = tempfile.mkdtemp()
    s = settings_mod.Settings()
    s.source_library = tmp

    from playstick_admin.app import MainWindow
    window = MainWindow()
    window.show()

    # Exercise every tab, then quit.
    for i in range(window._tabs.count()):
        window._tabs.setCurrentIndex(i)
        app.processEvents()

    QTimer.singleShot(200, app.quit)
    code = app.exec()
    print("smoke ok: %d tabs, exit %d" % (window._tabs.count(), code))
    return 0


if __name__ == "__main__":
    sys.exit(main())
