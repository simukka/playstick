#!/usr/bin/env python3
"""Command-line entry point for the library preparer.

The code moved to src/prep/playstick_prep.py so the Qt admin app can import it
as a module and drive it directly -- a hyphen in a filename is the right name
for a command and the wrong one for `import`. This shim keeps the documented
invocation working:

    ./scripts/playstick-prep.py --library /mnt/nas/video
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src", "prep"))

import playstick_prep

if __name__ == "__main__":
    try:
        sys.exit(playstick_prep.main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
