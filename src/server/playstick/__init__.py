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

The control that does matter is that no filesystem path crosses the HTTP
boundary on any route a phone uses. The client sends an opaque id which indexes
a table this process reads out of its own database; there is no endpoint a
child's phone can reach that takes a path.

There is one endpoint that takes paths, and it is worth knowing about rather
than being surprised by. /api/admin/films is how the desktop admin app puts the
library on this device -- see db.py for why the library lives here now -- and a
film is a path by nature. It cannot be otherwise: there is no index on the
share left to read them from, because the share is read-only and the machine
that prepares films is not this one. What contains it is that the paths are
library-RELATIVE, that every one is resolved and proved to sit under the
library root before it becomes a row AND again before anything opens it, and
that this is the only route on the server that accepts one at all.

There is exactly one endpoint that streams file bytes, and it used to say there
were none. /api/audio is what lets several people watch one silent projector
and each hear their own language in their own headphones -- see PHONE_AUDIO
below for why that is the only sound this appliance has. It is narrower than it
needs to be on purpose: the route is a regex matched against the whole path,
the id must be exactly the sixteen lowercase hex characters the library table
is keyed by, and the track is a small integer that indexes a list whose paths
were already proved to sit under the library root by Library._under_root. The
only files it can name are the ones playstick-prep.py wrote. No string a phone
sends is ever joined onto a filesystem path.

THE SECOND ENDPOINT THAT REACHES OFF THIS BOX

/api/nas/wake sends a wake-on-LAN packet, which is the only thing this daemon
does that touches a machine other than itself. It is narrow in the same way and
for the same reason as the audio route: it takes no body at all. There is one
NAS, its address and MAC are settings, and no string a client sends reaches the
socket -- a client cannot name a host to wake any more than it can name a file
to play. What it can do is cause a packet to be sent, so the watcher rate-limits
that on its own account rather than trusting six phones to agree.

WHERE THE LIBRARY COMES FROM

A SQLite database at /var/lib/playstick/playstick.db, and nowhere else. The
film table, every metadata edit, every poster and every resume position are
rows in it, put there by the desktop admin app over the routes above.

This used to be a JSON index the preparer wrote onto the share, re-read every
five minutes, with a recursive walk of that share as the fallback. Both are
gone, and db.py's header says why at length -- the short version is that the
share is mounted read-only, so every correction anybody made from the admin
view was lost at the next restart with one line in the journal about it.

The consequence to know before debugging it: a device whose database is empty
serves an empty grid, and waiting does not fill it. That is honest rather than
broken. The sync is one button in the admin app.

CONFIGURATION is one file -- /etc/playstick/settings.toml, named by
PLAYSTICK_SETTINGS, which is the only environment variable this package reads.
Configuration and state are deliberately different things in different places:
Ansible owns the first and rewrites it on every run, and this daemon owns the
second and Ansible never touches it. See config.py for what the file holds and
group_vars/all.yml for why each value is what it is.
"""
