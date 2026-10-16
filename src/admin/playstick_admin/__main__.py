"""Entry point: `python -m playstick_admin` or the `playstick-admin` script.

Logging is set up before anything else, and before the QApplication, so that a
failure while the window is being built has somewhere to go. `make admin` runs
this on a terminal and everything the app does afterwards arrives there -- see
playstick_admin.log for what is written and how to turn the volume up.
"""

import logging
import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from . import log as log_setup
from . import paths
from . import settings as settings_mod

LOG = logging.getLogger(__name__)


def main(argv=None):
    argv = argv if argv is not None else sys.argv
    level = log_setup.setup(argv)
    log_setup.install_hooks()
    log_setup.install_qt_handler()

    QCoreApplication.setOrganizationName(settings_mod.ORG)
    QCoreApplication.setApplicationName(settings_mod.APP)
    app = QApplication(argv)
    # Imported here so a headless import of this module (e.g. in a test) does
    # not require the widgets to be constructed.
    from .app import MainWindow

    # Said once, at the top, because every question that starts "why is it
    # doing that" is answered by one of these three and none of them is
    # visible in the window.
    LOG.info("playstick admin starting; logging at %s",
             logging.getLevelName(level))
    LOG.info("library: %s", paths.db_path())

    window = MainWindow()
    window.show()
    code = app.exec()
    LOG.info("closing (exit %d)", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
