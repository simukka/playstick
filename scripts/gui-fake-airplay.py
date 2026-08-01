#!/usr/bin/env python3
"""Hold an established TCP connection on UxPlay's port, inside the GUI dev
container, so the AirPlay interlock can be exercised without an iPhone.

    docker compose exec gui fake-airplay      # ^C to release

The daemon never asks "is UxPlay running" -- there is no UxPlay here and there
would be no answer. It asks `ss` for an established connection with a source
port of PLAYSTICK_AIRPLAY_PORT, because uxplay-kms holds the DRM node from
service start to shutdown and so can never distinguish idle from mirroring.
That question has the same answer whatever is on the other end of the socket,
which is the whole reason this file can be eleven lines of stdlib.

While it runs: the grid greys out and says somebody is using the projector,
and starting a film is refused with 409. Note the two-second delay before the
refusal -- airplay_confirmed() samples twice a second apart, because iOS opens
brief connections to :7000 merely from having the AirPlay picker on screen.
"""

import socket
import sys
import time

port = int(sys.argv[1]) if len(sys.argv) > 1 else 7000

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(1)

client = socket.create_connection(("127.0.0.1", port))
conn, _ = server.accept()

print("holding a connection to :%d -- the UI should refuse to play a film."
      % port)
print("^C to release it.")
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    print("")
