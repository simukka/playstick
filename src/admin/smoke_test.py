"""Offscreen smoke test: construct the whole window and pump the event loop
briefly. Not part of the unittest suite (it needs PySide6); run by hand from the
admin venv:

    QT_QPA_PLATFORM=offscreen python src/admin/smoke_test.py

It runs against a scratch database and a scratch share, so it can be run on a
machine with neither -- and so that it can never touch the real ones. The
database is the thing worth being careful about: it holds every metadata
correction anybody has made, and unlike the stick's it cannot be rebuilt from
anywhere.

The SETTINGS are scratch too, and that is not optional. They are QSettings,
keyed by organisation and application name, so a smoke run that set the share
on an ordinary Settings() wrote the scratch path into the real admin.conf --
and the next `make admin` scanned a temp directory, which the database rightly
refused as the wrong share. QSettings.setPath points the whole process at the
scratch directory before anything opens a settings file.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QCoreApplication, QSettings, QTimer
from PySide6.QtWidgets import QApplication

from playstick_admin import log as log_setup
from playstick_admin import settings as settings_mod
from playstick_admin import theme


def main():
    # The same logging the app installs, so what this prints looks like what
    # `make admin` prints -- and so that a warning raised while the window is
    # being built is a line with a timestamp rather than a bare sentence from
    # logging's last resort.
    log_setup.setup(sys.argv)
    log_setup.install_qt_handler()
    QCoreApplication.setOrganizationName(settings_mod.ORG)
    QCoreApplication.setApplicationName(settings_mod.APP)
    app = QApplication(sys.argv)
    theme.apply(app)

    # A scratch share, so the window does not pop the settings dialog, and a
    # scratch database beside it -- see the module docstring.
    tmp = tempfile.mkdtemp(prefix="playstick-smoke-")
    os.environ["PLAYSTICK_ADMIN_DB"] = os.path.join(tmp, "admin.db")
    # And the rest of what the app writes under its own name -- site.yml for
    # actl among it -- so a smoke run cannot replace the real one.
    os.environ["XDG_STATE_HOME"] = os.path.join(tmp, "state")
    os.environ["XDG_CACHE_HOME"] = os.path.join(tmp, "cache")
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, os.path.join(tmp, "config"))
    nas = os.path.join(tmp, "nas")
    os.makedirs(nas)
    s = settings_mod.Settings()
    s.nas_root = nas

    from playstick_admin.app import MainWindow
    window = MainWindow()
    window.show()

    # Exercise every page, through the sidebar the way a click would, then quit.
    for i in range(window._pages.count()):
        window._nav.button(i).click()
        assert window._pages.currentIndex() == i, i
        app.processEvents()

    QTimer.singleShot(200, app.quit)
    code = app.exec()
    print("smoke ok: %d pages, exit %d" % (window._pages.count(), code))
    return 0


if __name__ == "__main__":
    sys.exit(main())
