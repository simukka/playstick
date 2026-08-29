"""Offscreen smoke test: construct the whole window and pump the event loop
briefly. Not part of the unittest suite (it needs PySide6); run by hand from the
admin venv:

    QT_QPA_PLATFORM=offscreen python src/admin/smoke_test.py

It runs against a scratch database and a scratch share, so it can be run on a
machine with neither -- and so that it can never touch the real ones. The
database is the thing worth being careful about: it holds every metadata
correction anybody has made, and unlike the stick's it cannot be rebuilt from
anywhere.
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

    # A scratch share, so the window does not pop the settings dialog, and a
    # scratch database beside it -- see the module docstring.
    tmp = tempfile.mkdtemp(prefix="playstick-smoke-")
    os.environ["PLAYSTICK_ADMIN_DB"] = os.path.join(tmp, "admin.db")
    nas = os.path.join(tmp, "nas")
    os.makedirs(nas)
    s = settings_mod.Settings()
    s.nas_root = nas
    s.staging_dir = os.path.join(tmp, "staging")

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
