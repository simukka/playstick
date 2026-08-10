#!/usr/bin/env python3
"""Entry point for the movie player daemon. The code is in the playstick
package next to this file; see its __init__.py for what the daemon does and
why it arbitrates the display itself.

This stays a separate file rather than becoming playstick/__main__.py because
the systemd unit and the dev container both exec a path, and a path is a
simpler contract than "python3 -m" plus a working directory.
"""

import os
import sys

# The package sits beside this script in a checkout and under PLAYSTICK_LIB on
# the device. Preferring the sibling is what lets the repository be run
# straight out of a clone, which is how the dev container and every ad-hoc
# test invocation reach it.
_here = os.path.dirname(os.path.abspath(__file__))
_lib = (_here if os.path.isdir(os.path.join(_here, "playstick"))
        else os.environ.get("PLAYSTICK_LIB", "/usr/local/lib"))
if _lib not in sys.path:
    sys.path.insert(0, _lib)

from playstick.main import main   # noqa: E402 - the path above has to come first

if __name__ == "__main__":
    main()
