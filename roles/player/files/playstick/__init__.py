#!/usr/bin/env python3
"""Movie player for the UxPlay appliance: a phone-sized web UI on the LAN that
plays files from the NAS share straight to a DRM plane, with no compositor.

WHY THE DAEMON ARBITRATES THE DISPLAY INSTEAD OF SYSTEMD

Exactly one process may hold DRM master, and uxplay-kms.service takes it when
the *service* starts, not when a client connects -- see the header of
uxplay-idle-clock.py, where the same fact is what forces the idle clock to
write pixels rather than text. So mpv cannot share the card with UxPlay: the
AirPlay receiver has to be stopped for the length of a film and started again
afterwards.

That could be expressed as Conflicts= on an mpv unit, and it was written that
way first. It does not work, because systemd will happily stop uxplay-kms for
the conflict and then has no reason to ever start it again -- a film that ends
at 21:30 leaves the projector with no AirPlay receiver until somebody notices.
Restoring it is a decision, and decisions live here.

One consequence worth knowing rather than debugging: while a film is playing
the device disappears from the AirPlay list entirely, because UxPlay is what
publishes the mDNS record. It comes back a few seconds after the film stops.

The interlock in the other direction is a check, not a race: a film will not
start while a client holds a TCP connection to UxPlay's port. Mirroring wins,
because somebody is standing there holding a phone.

WHAT RUNS AS ROOT AND WHY

All of it. mpv needs to become DRM master, and the arbitration above needs to
start and stop a system unit. The alternative -- a dedicated user in video and
render plus a polkit rule for two systemctl verbs -- is more moving parts than
the thing it protects on a single-purpose appliance with one service on it.
The unit is confined with ProtectSystem=strict instead.

The control that does matter is that no filesystem path ever crosses the HTTP
boundary. The client sends an opaque id which indexes a table this process
built from the index or by walking the share; there is no endpoint that takes
a path.

There is exactly one endpoint that streams file bytes, and it used to say there
were none. /api/audio is what lets several people watch one silent projector
and each hear their own language in their own headphones -- see PHONE_AUDIO
below for why that is the only sound this appliance has. It is narrower than it
needs to be on purpose: the route is a regex matched against the whole path,
the id must be exactly the sixteen lowercase hex characters the library table
is keyed by, and the track is a small integer that indexes a list whose paths
were already proved to sit under the library root by Library._under_root. The
only files it can name are the ones playstick-prep.py wrote. No string a client
sends is ever joined onto a filesystem path.

Configuration is entirely environmental -- see playstick-web.service.
"""
