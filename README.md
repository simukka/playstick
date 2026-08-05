![playstick](logo.svg)

**Old tech meets modern software.**

Playstick turns an **ASUS VivoStick TS10** into a compact, dedicated AirPlay mirroring receiver 
that plugs directly into a projector's HDMI port.

It provisions and measures a minimal Ubuntu Server installation running [`UxPlay`](https://github.com/simukka/UxPlay), 
with video rendered directly through the Intel integrated graphics stack **without an X server or desktop environment**.

* The device is managed entirely over SSH from a separate control machine.
* Plays movies from a NAS (simple enough for a child to use).
* Web based movie library.
* 

## Motivation

I discovered UxPlay while looking for a way to mirror an Apple device to a Linux machine. 
Not long afterwards, I found an ASUS VivoStick TS10 listed on Finn and offered 400 NOK for it.

A few days later, it arrived. I installed Ubuntu Server (deliberately avoiding a desktop distribution 
or other graphics-heavy environment) with the goal of turning this tiny, ageing PC into a 
single-purpose AirPlay receiver.

Playstick documents and automates that process: provisioning the device, configuring direct 
graphics output, and measuring how well the hardware performs in its new role.

## The hardware, accurately

`lspci` reports the GPU as `Atom/Celeron/Pentium x5-E8000/J3xxx/N3xxx Integrated Graphics`, which
is ambiguous — PCI ID `8086:22b0` is shared between Braswell and Cherry Trail. The TS10 is a
VivoStick, so this is **Cherry Trail**:

| | |
|---|---|
| SoC | Intel Atom x5-**Z8350**, 4 × 1.44 GHz Airmont |
| GPU | Gen8 LP, 12 EU |
| RAM | 2 GB LPDDR3 |
| Storage | 32 GB eMMC |
| Ports | 1 × USB 3.0, 1 × USB 2.0, micro-USB power, 3.5 mm combo |
| Network | SDIO Wi-Fi on board — but this unit runs on a **USB Ethernet** adapter |

Three consequences shape everything here:

1. **Memory bandwidth is the real constraint, not CPU.** This is the single most important
   thing measured on this box, and it was not the assumption the project started with — see
   [Results](#results-so-far). The display engine, CPU and GPU all share one modest LPDDR3
   subsystem, and saturating it produces `CPU pipe C FIFO underrun` in dmesg and visible
   corruption. `drm_force_mode` therefore pins the output; it is `1280x720@60` on this projector.
2. **Cherry Trail is i965-only.** `intel-media-driver` (iHD) does not cover Gen8 LP, so
   `LIBVA_DRIVER_NAME=i965` is pinned everywhere to stop libva autodetection wandering off.
3. **2 GB RAM / 32 GB eMMC.** No desktop, no build toolchain — hence the archive UxPlay package
   rather than a source build, and zram rather than a swapfile.

> **A founding assumption that turned out to be wrong.** This project began from "Airmont at
> 1.44 GHz cannot sustain 1080p30 H.264 in software, so VA-API is a hard requirement." The
> decode-only measurements support it. The end-to-end ones do not: once frames have to reach a
> display, software decode outperformed both hardware decoders, because the VA path pays an
> uncached readback that software decode never performs. The `graphics` role still asserts that
> hardware VLD exists — it is a useful signal that the driver stack is healthy — but it is no
> longer true that the design depends on using it.

Verified present in Ubuntu 26.04 "Resolute": `uxplay 1.73.2-1`, `gstreamer1.0-vaapi 1.26.8-2`, and
the i965 driver in two mutually exclusive builds — `i965-va-driver 2.4.1+dfsg1-2build1` (universe,
DFSG-free) and `i965-va-driver-shaders 2.4.1-2build1` (multiverse, with the non-free shader
binaries). They `Conflict`; exactly one may be installed. `i965_nonfree_shaders` picks, and
defaults to the full shader set because this SoC has no performance to spare.

**Audio is deliberately out of scope.** UxPlay runs with `-a`. The Cherry Trail LPE/SST audio
stack is the least reliable part of this platform, and skipping it removes the largest single
source of failure. If audio is wanted later, a USB audio adapter sidesteps the SoC path entirely.

## Layout

```
ansible.cfg              inventory path, pipelining, longer timeouts
inventory.yml            single host: vivostick, and nothing site-specific
host_vars/vivostick/     local.yml  <- YOUR address, login, NAS. gitignored
                         vault.yml  <- secrets. gitignored AND encrypted
group_vars/all.yml       every tunable lives here
site.yml                 base -> trim -> graphics -> uxplay -> idle -> nas -> player -> probe
fetch-results.yml        run the probes, pull output into results/
Dockerfile               the Ansible control node
Dockerfile.gui           the web UI on a dev machine, no hardware needed
scripts/playstick-prep.py prepare the movie library on the DEV machine
scripts/projector-probe.py talk to the projector by hand, ON THE DEVICE, before
             trusting the daemon to do it
scripts/make-testclip.sh build H.264 test clips on the CONTROL machine
scripts/gui-*            entrypoint and AirPlay stub for Dockerfile.gui
roles/
  base/      apt components, uxplay service account, zram
  trim/      strip services/packages the kiosk does not need
  graphics/  i965 VA-API stack, modetest, the hardware-decode assert gate
  uxplay/    uxplay + avahi + cage/seatd, both units, one of them started, tty1
  idle/      the framebuffer clock shown when nothing is mirroring
  nas/       read-only CIFS automount for the movie library
  player/    mpv + the web UI a child drives it from, the display arbiter,
             and the RS-232 control that switches the projector on and off
  probe/     measurement scripts and test clips
docs/        install.md — every command, in order, including the vault
             matrix-narrative.md — how this was measured, and what fooled me
results/     probe output fetched back (gitignored)
```

Only `ansible.builtin` modules are used — a plain `ansible-core` install is enough, no collections.

## Control node in Docker

Debian 12 ships `ansible-core 2.14`; the image pins something current and keeps the control
machine clean. It carries the whole control-side toolchain — ansible, ansible-lint, ffmpeg for the
test clips, iperf3 for the throughput probe — so Docker is the only host requirement.

```bash
./actl                                        # build on first run, then syntax-check
./actl 'ansible -m ping vivostick'
./actl 'ansible-playbook site.yml --check --diff'
./actl ./scripts/make-testclip.sh
./actl bash                                   # poke around
docker compose up iperf                       # iperf3 -s on host networking
docker compose up gui                         # the web UI at :8080, no device
```

That last one is a different image with a different job — `Dockerfile.gui` runs
what Ansible would have installed rather than installing it. See
[The UI, on a development machine](#the-ui-on-a-development-machine).

`actl` wraps `docker compose run`, exporting `HOST_UID`/`HOST_GID` so anything written into the
mounted repo — `results/`, generated clips — comes back owned by you rather than by root. (Not
`UID`: bash makes that readonly, so exporting it aborts the wrapper.)

**SSH.** `~/.ssh` is mounted **read-only** so the container can never rewrite your keys or
`known_hosts`, and `$SSH_AUTH_SOCK` is forwarded so a passphrase never enters the container. The
tradeoff is that an unknown host key cannot be recorded from inside — SSH to the device once from
the host first. `ansible.cfg` keeps `host_key_checking = True`; that is the point.

If key auth to the device is not set up yet, ansible fails with
`Permission denied (publickey,password)`. Fix it on the **host**, once:

```bash
ssh-copy-id <user>@<host>              # the ansible_user and ansible_host from local.yml
```

Or, if you would rather stay on passwords, `sshpass` is in the image and
`./actl 'ansible-playbook site.yml --ask-pass --ask-become-pass'` works.

**sudo-rs.** Ubuntu 26.04 makes `sudo-rs` the default `sudo` through
`update-alternatives`. It does not honour the custom prompt ansible passes with `-p`, so ansible
never matches the prompt and `become` dies after 60 s with:

```
Timeout (62s) waiting for privilege escalation prompt
```

Classic sudo is still installed at `/usr/bin/sudo.ws`, so `group_vars/all.yml` sets
`ansible_become_exe: /usr/bin/sudo.ws`. Nothing on the device changes — the alternatives link and
sudo-rs itself are left alone. Drop that variable if you ever repoint the alternative, or if
sudo-rs gains prompt support.

## Usage

The short version is below. Every command in order, including the vault, is in
**[docs/install.md](docs/install.md)**.

```bash
# 0. control machine: a test clip (skip if using ./actl for everything)
./scripts/make-testclip.sh             # needs ffmpeg; writes to roles/probe/files/

# 1. point the inventory at the stick. local.yml is gitignored: your address,
#    login and NAS live there, not in the committed files.
cp host_vars/vivostick/local.yml.example host_vars/vivostick/local.yml
$EDITOR host_vars/vivostick/local.yml  # ansible_host, ansible_user, nas_*
ansible -m ping vivostick

# 2. provision. -K because sudo on the device wants a password.
ansible-playbook site.yml --check --diff -K
ansible-playbook site.yml -K

# 3. drm_force_mode needs a reboot before it takes effect
ssh <user>@<host> sudo reboot

# 4. find out what this box can actually do
iperf3 -s &                            # on the control machine
ansible-playbook fetch-results.yml -K -e probe_iperf_server=<x1-ip>
ansible-playbook fetch-results.yml -K -e run_matrix=true   # ~20 min, owns the display
```

`site.yml` enables and starts one UxPlay unit — `uxplay_output_path`, which defaults to `kms`.
It used to enable neither, on the grounds that which one wins is a probe result rather than a
guess; the sweep has since been run, and a device that comes up as an AirPlay receiver by itself
is worth more than that reservation. Set `uxplay_output_path: none` to get the old behaviour
back.

Step 3 is not optional on first provision: `drm_force_mode` writes the kernel cmdline, and until
the box reboots you are still measuring the old display mode.

The clips total ~114 MB and are copied to the device by the `probe` role — a few seconds over the
USB Ethernet link. Later runs reuse whatever is already in `/usr/local/share/uxplay-probe`.
Shorten them with `DURATION=30 ./scripts/make-testclip.sh` if you want faster sweeps.

## Choosing the output path

Two units are installed, `Conflicts=` each other, and only one should ever be enabled.
`uxplay_output_path` picks which — `kms`, `cage`, or `none` for neither — and the `uxplay` role
enables and starts that one while stopping *and disabling* the other. Disabling matters: an
enabled loser comes back at the next boot and races the winner for the card.

**`uxplay-kms.service`** — pure DRM/KMS, no compositor at all. The default.

UxPlay's GStreamer pipeline ends in `kmssink`, which writes to a DRM plane on the i915 node. It
becomes DRM master simply by being the first process to open the card: i915's fbdev console
emulation lives in the kernel and holds no master, so the only real contender is a getty painting
tty1 — which is why the unit conflicts with `getty@tty1` and the `base` role masks it. tty2 stays
as a rescue console.

**The DRM node is not `card0`.** On this box simpledrm claims `card0` as the EFI framebuffer at
boot, i915 then binds and lands on `card1`, and a stale `by-path` symlink to the dead `card0` is
left behind. The numbering is a boot-order artifact and is not stable, so the `graphics` role
discovers the node — preferring `/dev/dri/by-path/pci-0000:00:02.0-card` and falling back to
whichever card reports `i915` as its driver — and feeds it to both units. Override with
`drm_card_path` if needed.

**Do not set `driver-name=i915` on `kmssink`.** It looks like the obvious way to pin the device
and it is actively harmful: unprivileged it gives `Could not open DRM module i915`, and as root it
opens something that is not KMS-capable, so `kmssink` fails at `start()` with `driver does not
provide mode settings configuration`. Bare `kmssink` auto-detects correctly. `force-modesetting=true`
also fails on this driver — measured separately, all 6 attempts. Both are omitted from the unit.

Connectors present: `card1-HDMI-A-1` (the projector) and `card1-DP-1`.

**`uxplay-cage.service`** — UxPlay under `cage`, a single-window wlroots kiosk compositor. Still
no X. Costs ~30 MB and one more moving part, and buys seat/DRM-master handling plus EDID re-read
on projector hotplug.

Switch paths:

```bash
./actl 'ansible-playbook site.yml -K -e uxplay_output_path=cage'   # try it
$EDITOR group_vars/all.yml                                         # keep it
```

Expect the hotplug behaviour to be the deciding factor rather than raw throughput: a projector
that gets unplugged between sessions is the normal case, and wlroots handles that re-read where
bare `kmssink` may not.

Two things follow the choice automatically, and both would be silent bugs if they did not.
`player_airplay_unit` is derived from it, so the film player stops and restarts whichever
receiver is actually running rather than a hardcoded one. And the `idle` role refuses to run at
all on the cage path — see [Limitations](#limitations) below.

## Idle screen

Between sessions the projector would otherwise show the tail of the boot log and a blinking
cursor — `claim_tty1` masks `getty@tty1`, so once `multi-user.target` is reached nothing paints
tty1 at all. `uxplay-idle.service` puts a large clock and the advertised AirPlay name there
instead, from boot onwards.

### It draws pixels, and that took two attempts

The first implementation printed block digits to `/dev/tty1`. The reasoning was that the
framebuffer console holds no DRM master, so the kernel would suspend fbcon while `kmssink` owned
the card and restore it afterwards — no arbitration code needed. Supporting it was `uxplay -h`:
`-nc  Do NOT Close video window when client stops mirroring`, implying the default releases the
display between sessions.

**It froze on screen at 18:04.** Two things were wrong:

1. UxPlay opens the DRM device inside `gst_kms_sink_start`, during service startup, *before any
   client connects*. `fuser` confirms it: `/dev/dri/card1: uxplay` with nothing mirroring. `-nc`
   governs an X/Wayland window; with `kmssink` there is no window.
2. Whoever opens the device first becomes DRM master, and the fbdev helper calls
   `drm_master_internal_acquire()` before pushing console updates to the screen. That fails while
   another master exists. So the clock kept writing to tty1, the console text buffer kept
   updating, and none of it ever reached the framebuffer.

The fix came from what the failure *didn't* break: the stale image survived a whole mirroring
session and reappeared afterwards, so the framebuffer's contents were still live — only the
console-to-framebuffer path was blocked. i915's fbdev emulation maps the real scanout buffer with
no shadow, so writing to `/dev/fb0` skips that path entirely. Confirmed before rewriting anything:

```bash
# with uxplay-kms running, nothing mirroring
head -c 200000 /dev/zero | tr '\0' '\377' > /dev/fb0   # white bar appears
```

`uxplay-idle-clock.py` therefore composes a full frame and writes it to `/dev/fb0`. While a client
mirrors, the CRTC scans out UxPlay's buffer and those writes are invisible; when the session ends
the CRTC returns to the fbdev buffer and the clock is there, current. Nothing is coordinated
between the two services, and they deliberately do not `Conflict`.

### How it draws

Glyphs are parsed from a PSF console font by the clock itself, which means **fbcon's font
validation no longer applies** — the 32x16 face this kernel refuses to load with `setfont`
(`Unable to load such font with such kernel version`, i.e. `KDFONTOP` returning `EINVAL` on a
font that is not 8 px wide) renders perfectly here. Everything is greyscale, so the framebuffer's
channel order never has to be worked out; geometry comes from
`/sys/class/graphics/fb0/{virtual_size,bits_per_pixel,stride}`.

The clock is scaled to fit — 90% of width or 55% of height, whichever binds first. On 1280×720
with the 16×32 face that is scale 12, giving 192×384 px digits. The subtitle steps down until it
fits, so a long hostname shrinks rather than running off the edge. Composing a frame costs a few
milliseconds and happens once a minute, when `HH:MM` changes.

Blanking after `idle_blank_minutes` (30) needs to know when a session ends. It asks `ss` for an
established connection to UxPlay's port — deliberately *not* "is the DRM node open", because
`uxplay-kms` holds that from startup to shutdown and so can never distinguish idle from
mirroring. The kernel's own 10-minute console blanker is switched off (`setterm --blank 0`) so it
cannot black the framebuffer out from under the clock; `--powersave off` keeps the HDMI signal
alive while black, since DPMS-off makes a projector report "no signal" and re-sync on wake.

### Two UxPlay flags are load-bearing

Both concern whether UxPlay hands the screen back, and both are enforced:

| | |
|---|---|
| `uxplay_nofreeze: true` → `-nofreeze` | Without it UxPlay "leaves a frozen screen in place after reset", so the CRTC keeps scanning out its buffer after the `-reset` timeout and the clock never returns. |
| `-nc` rejected in `uxplay_extra_args` | Same hazard on the clean-stop path. The `idle` role asserts it is absent. `-nc no` is fine. |

### Limitations

**The cage path cannot have this.** `uxplay-cage.service` `Conflicts=uxplay-idle.service`. A
compositor owns the CRTC for its whole lifetime and never returns it to the fbdev buffer, so the
clock would be permanently invisible. An idle screen for that path would have to be a Wayland
client.

Now that the receiver is started rather than left for an operator, the `idle` role asserts the
combination away: `uxplay_output_path: cage` with `idle_enabled: true` fails the play and names
the line to change. The dangerous half is not the invisible clock — `Conflicts=` runs both ways,
so starting the clock afterwards would *stop the receiver*, trading a working projector for a
screen nobody can see.

**Stop it during probe sweeps.** `uxplay-probe-matrix.sh` runs pipelines on VT1 via `openvt`;
`systemctl stop uxplay-idle` first removes the variable.

```bash
systemctl disable --now uxplay-idle.service    # rollback
```

## Movies from the NAS

The second thing this box does: play files off a read-only CIFS share directly, with no
compositor and no phone in the loop, driven from a web page anyone on the LAN can open.
The page is deliberately small enough for a child — a grid of posters, tap one, then a
large play/pause and a large stop. There is no seek bar; a control that can lose your
place is a control that produces tears.

```bash
# host_vars/vivostick/local.yml -- gitignored, so your LAN stays yours
nas_server: "10.0.1.5"
nas_share: "movies"      # the exported share NAME. A Synology's /volume1/movies
                         # is exported as //nas/movies -- the last component alone.

# host_vars/vivostick/vault.yml -- encrypted, and that path is load-bearing
nas_username: "kyle"
nas_password: "…"
```

Gitignored is not encrypted, so the password is in the vault rather than beside the
address. Both are `host_vars`, and *that* path is the load-bearing part: `vivostick` is a
*host*, so `group_vars/vivostick/` is silently never read, and `group_vars/all/` silently
shadows `group_vars/all.yml`. `vault.yml` also overrides `local.yml` where both define
the same variable — lexical load order — so keep each variable in one file. The full
command sequence is in [docs/install.md](docs/install.md#4-secrets-the-vault).

### Preparing the library first

`scripts/playstick-prep.py` runs on the **developer machine** and does everything the
stick would otherwise have to do badly, or could not do at all:

```bash
./scripts/playstick-prep.py --library /mnt/nas/video
```

It walks the share, probes every file, and writes `playstick-library.json` next to the
films. The daemon reads that instead of walking, and everything in it was decided on a
machine with cores to spare:

| it does | because on the stick |
|---|---|
| **verifies** each file decodes, and that the ending is actually there | a partial download plays fine for 40 minutes and then stops, in the dark, at bedtime |
| **de-duplicates** — same film, best copy wins | the grid otherwise shows *Ponyo* three times and a child picks the 480p one |
| **transcodes** anything that is not already 8-bit H.264 at ≤720p | HEVC, 10-bit and 4K do not play at all; software H.264 at 720p is measured at 29 fps with nothing to spare |
| **extracts posters** — sidecar, embedded cover, or a frame | the daemon does this with mpv, one at a time, only while nothing is playing, over CIFS. A hundred films is an afternoon |
| **extracts subtitles** to UTF-8 SRT | the share is mounted read-only, so nothing can be written beside the film |
| **collects rating and genre** from `.nfo` files and container tags | there is no metadata database on a 2 GB appliance |
| **matches against TMDb** on title, year *and* runtime, and refuses rather than guesses | the shelf is what a child picks from, and a wrong film on it is worse than a plain one |

Artifacts go in `<library>/.playstick/`, which the daemon's fallback walk skips — so a
half-prepared library shows the originals rather than each film twice. Nothing under the
library is ever modified or deleted; losing duplicates are reported, and only moved if
you pass `--duplicates-dir`.

The index is a preference, not a requirement. Missing, unparseable or written by a newer
version, it logs one line and walks the share as before.

```bash
--verify full            # decode every frame, not just the first and last seconds
--transcode never        # index and collect metadata, encode nothing
--dry-run                # say what would happen
--tmdb-key <key>         # ratings, genres and posters from themoviedb.org.
                         # OFF by default: it sends your film titles to a third party
--refresh-posters        # re-download every TMDb poster instead of keeping what
                         # is on disk. Run once after upgrading — see below
```

#### A TMDb match has to be earned

The first run of this against a real library published **Red Hook Summer** — its title,
its poster, its plot and its rating — for `Hook (1991)/Hook.1991.720p.BRrip.x264.YIFY.mp4`.
Thirty-three of a hundred and seventeen entries were somebody else's film. Nothing about
it looked like a failure: every step succeeded and returned a plausible answer.

The cause was a chain, and the first link is worth knowing about if you have YIFY rips:
**that file's only date is `creation_time: 2012-10-08`, the moment it was muxed.** It was
read as a release year, so TMDb was asked for a 1991 film released in 2012, and the top
hit for `query=Hook&year=2012` is Red Hook Summer. 69 of 547 films were dated by their
muxer this way. `creation_time` is no longer read, a container tag can no longer overwrite
a year the filename gave, and the year is taken from the folder when the file has none
(`Die.Hard.1988…/die_hard.mkv` had no year at all).

The second link is that the search result was simply believed. It now has to survive three
independent checks — the title, compared after normalising accents, punctuation and
articles; the year; and **the runtime**, which is the one signal that owes nothing to
anybody's spelling and would have caught this on its own: 121 minutes of Red Hook Summer
against 142 minutes of Hook. Popularity breaks ties and nothing else, since sorting by
popularity is what caused this. Below the bar, the run says so and moves on:

```
TMDb: 112 matched, 5 not confident enough to use
  no match: Contact (1997)/Contac.1997.720p.x264.YIFY.mkv -- best was …
```

An unmatched film keeps its filename title and year and gets a frame for a poster, which
is what an unmatched film has always looked like. That is deliberate: a wrong film on a
shelf a child picks from is worse than a plain one. The usual cause is a typo in the
filename — `Contac` above — and renaming the file fixes it.

Two notes for a library prepared by an earlier version. The state cache stores the derived
title and year, so entries written under the old rules are re-probed automatically; it
costs one `ffprobe` per film and no re-encoding. Posters are not so lucky — a JPEG
downloaded for the wrong film is indistinguishable from a right one here — so pass
`--refresh-posters` once.

Then `http://vivostick.local/` from any phone on the LAN — the `player` role
advertises the UI over the avahi daemon that is already running for AirPlay, so nobody
has to know the stick's IP address, and `player_port: 80` means there is nothing to
remember after the hostname either. The daemon runs as root for reasons that predate the
port (mpv needs DRM master), so binding below 1024 costs nothing here.

### Narrowing the shelf

Prep collects a year, a score and a genre list for every film, and until now the page threw
all three away — the grid was every film the share held, in one order, and at a couple of
hundred posters that is a wall to scroll past rather than a shelf to pick from. A funnel
button next to the sound icon opens a second sheet:

| | |
|---|---|
| **Order** | A to Z, newest first, oldest first |
| **Kind** | one row per genre the library actually has, each carrying the count that choosing it would leave |
| **Score** | any, 6+, 7+, 8+ — the 0–10 number from the `.nfo`, the container tag or TMDb |
| **Headphones** | only the films a phone can hear |

**A to Z is the order the server already sends, and the page does not re-sort it.** Prep
files by a normalised title so *The Fifth Element* sits under F the way it would on a shelf,
and the daemon keeps that order verbatim; a page that sorted on `title` instead would file
every *The* together and quietly disagree with its own default. `sort_title` is on the wire
for exactly this, and falls back to the title for a library nobody has prepped.

A film with **no year is last in both year orders**, not oldest. It is unknown, and burying
it under "oldest first" is a lie the grid would tell silently. Same reasoning for a missing
score: it drops out only once a threshold is actually asked for.

"Ready for headphones" is keyed on **extracted audio tracks, not the index's `prepared`
flag**. That flag is set only where prep had to transcode, so a film it judged already
stick-friendly reads false while playing perfectly on every phone in the room — the property
somebody in the room can notice is whether there is a soundtrack to send them, and that is
`audio_langs`.

The filtering is in the page, not in the daemon. `/api/library` is one small payload the page
already holds in full, so a filter is an array operation over it; the alternative — query
parameters — would have this process parsing attacker-shaped strings, when its whole design
is that clients send opaque ids and small integers.

Two consequences of a child being the user. A choice is remembered across reloads
(`ps.lib.*` in `localStorage`), so the grid they left is the grid they come back to — which
means a filter that hides everything must never be a dead end: the active filters show as a
chip above the grid that clears on tap, an empty grid says *"Nothing matches what you
picked"* with a full-width way out, and a genre that has left the library since it was
chosen un-picks itself on the next scan. And a library nobody has prepped has no genres, no
years and no scores, so those sections are taken away rather than rendered empty, and the
sheet says why.

One latent bug fell out of this. `refreshThumbs()` used to swap posters by matching the
grid's `<img>` list against the server's item list *by position*, which held only while the
grid was the whole library in the order it arrived. Tiles are now looked up by id.

### AirPlay is unavailable while a film plays

This is a property of the hardware, not a shortcut, and it is the same fact that forced
the idle clock to draw pixels: **there is one CRTC and one DRM master, and `uxplay-kms`
takes the card when the service starts, not when a client connects.** mpv therefore cannot
share it. The receiver has to be stopped for the length of a film, and while it is stopped
the stick does not appear in the iOS AirPlay list at all, because UxPlay is what publishes
the mDNS record. It comes back a few seconds after the film ends.

The interlock in the other direction is a check rather than a race: `playstick-web` refuses
to start a film while an established TCP connection to UxPlay's port says somebody is
mirroring. Mirroring wins, because somebody is standing there holding a phone. The check is
debounced over two samples a second apart — iOS opens brief connections to `:7000` merely
from having the AirPlay picker on screen, and a single sample would refuse to play a film
because someone across the room glanced at a menu.

### Why the daemon arbitrates instead of `Conflicts=`

`Conflicts=uxplay-kms.service` on an mpv unit was the first design. systemd will happily
stop the receiver for the conflict and then has no reason to ever start it again — a film
that ends at 21:30 leaves the projector with no AirPlay until somebody notices. Restoring
it is a decision, so it lives in the daemon, which records what it took the display from
before it stops anything. If the daemon is killed mid-film, `Restart=always` brings it back
and it reads that record on startup; `KillMode=control-group` means mpv died with it, and
`ExecStopPost=` covers a clean stop. `/run` is tmpfs, so a reboot clears the lot, which is
correct — a boot starts the receiver itself.

The idle clock needs no arbitration, for the reason it never did: it holds no DRM master,
its writes land in the fbdev buffer, and they are invisible while anything else scans out.
It gains exactly one thing, `IDLE_BUSY_FILE`, so the blank countdown does not run out
partway through a film and leave the clock blanked when it comes back.

### What is unproven, and how to settle it

**`player_vo` and `player_hwdec` ship at `drm` / `no`** — software decode into a dumb
buffer, the closest structural analogue of the pipeline that measured 29.19 fps / 0.00%
drop for UxPlay. That looks like it contradicts the VA-API result above and it does not:
that result was a *GStreamer* readback stall, and mpv's `--vo=gpu --gpu-context=drm
--hwdec=vaapi` path exports the VA surface as a dmabuf and samples it on the GPU, so the
stall is structurally absent. Hardware decode very likely wins here. It is an expectation,
not a measurement, and nothing on this box has ever run GL — so the default is the
configuration with the fewest unproven parts until the sweep says otherwise:

```bash
./actl 'ansible-playbook fetch-results.yml -e run_player_probe=true'
```

Watch the `hwdec_used` column: a `vaapi` run that reports `vaapi-copy` fell back to the
readback path, and its numbers are not measuring the hypothesis.

`--drm-mode` is pinned to `drm_force_mode` and that is load-bearing. `kmssink` never
modesets, so UxPlay inherits whatever the kernel cmdline set; **mpv picks its own mode**,
and left at `preferred` it would choose this projector's EDID-preferred `1920x1080i@60` —
silently undoing `drm_force_mode` and putting a deinterlacer back in the path.

**Audio is off and films play silently.** `player_audio: false`, mpv runs `--ao=null`. On
Cherry Trail, HDMI audio does not come out of an HDA codec at `hw:0,3` the way it does on
desktop Intel — it goes through the i915-created `hdmi-lpe-audio` device, i.e. the same LPE
block `uxplay_suppress_audio` exists to avoid. Nothing on this box has ever produced a
sample. The facts probe now reports the ALSA cards, their ELD state and whether the LPE
module is bound; read that, then set `player_audio: true` and `player_audio_device`. If the
platform is as unreliable as this README warns elsewhere, a USB audio adapter sidesteps the
SoC path entirely and `player_audio_device` is the only line that changes.

**H.265 is the real content risk, not 1080p.** If `vainfo` shows no `VAProfileHEVCMain :
VAEntrypointVLD`, software HEVC on 4×1.44 GHz Airmont will not save it. The facts probe
prints the decodable profiles.

### Turning the projector on

Until this existed, a child who picked a poster got mpv drawing to a plane nobody could
see: the film ran, correctly, on a screen that was switched off, until an adult found the
remote. `playstick-web` now talks to the projector over RS-232C, so picking a poster is the
whole of what it takes to watch a film — and, more importantly, so the lamp goes out again
when the room empties.

**It is off by default and that is not timidity.** With `player_projector_model` unset the
daemon builds a `NullProjector`: every step below is a no-op, the film starts exactly as it
did before, and an appliance with no serial cable — or the development GUI, which never has
one — behaves identically. Setting a model is the only thing that turns any of it on.

```
                             QPW  -> 000, standby
  Waking the projector up…   PON
  Waiting for the lamp…      ....... 10 s of documented deafness .......
                             QPW  -> 000 ... 000 ... 001, lit
  Pointing it at the movie…  IIS:HD3   then QIN to check it landed
  Making room on the screen… systemctl stop uxplay-kms.service
  Starting the movie…        mpv
```

`POST /api/play` now returns as soon as the film is **accepted** rather than when mpv is
running, because a cold PT-AE4000 takes the better part of a minute to answer `QPW` with
`001` and no browser holds a request open that long. The work happens on a thread and the
page watches `/api/status`, which is the mechanism it already used for everything else.

That is also what makes the wait bearable. A child who taps a poster and sees nothing for
forty seconds cannot tell a warming lamp from a broken appliance, and the second guess is
the one they act on — they press things, or they fetch somebody, or they give up. So each
step names itself, the preparing view shows the poster of the film they picked so they can
see the right one is coming, the bar is deliberately indeterminate (there is no honest
percentage; a bar that crept to nine tenths and stopped would be a lie they can see
through), and there is always a **Never mind** button.

**A projector that cannot be reached never stops a film.** Every serial fault in the
sequence is logged, reported to the page in one sentence, and stepped over. This is the
same judgement `library.py` makes about a corrupt index, and it matters more here: if the
lamp will not strike, the likely explanations are that somebody already switched the
projector on by hand or that a cable is loose, and in the first case the film is exactly
what was wanted while in the second an adult standing in the room can fix in two seconds
something this daemon cannot fix at all. Refusing to play would help nobody. Unplug the
adapter mid-provision and the only difference is a banner.

#### The lamp goes out after thirty minutes

A keeper thread ticks every fifteen seconds. The clock is reset by **a film playing or
being prepared, or a confirmed AirPlay session** — and deliberately not by a phone with the
page open. The page polls every three seconds, so counting that would mean the projector
stays lit until every browser tab in the house is closed, and one phone left in a pocket
would keep a lamp burning all night.

The same tick can switch the projector **on** for a mirroring session, and the two AirPlay
questions it asks are deliberately different:

| direction | check | why |
|---|---|---|
| keep the lamp lit | `airplay_active()`, one `ss` sample | a false positive only postpones a power-off, which costs nothing |
| strike a cold lamp | `airplay_confirmed()`, sustained across `player_projector_airplay_wake_ticks` | iOS opens brief connections to `:7000` merely from having the AirPlay picker on screen |

Without that asymmetry, somebody glancing at an AirPlay menu across the room would light a
lamp in an empty one. Two ticks is about thirty seconds of sustained connection: a glance
does not survive it, a session does. `player_projector_airplay_wake_ticks` is the number to
raise if the projector ever switches itself on unbidden, and
`player_projector_wake_on_airplay: false` turns the direction off entirely.

The input cannot be selected at wake time — the projector is deaf for ten seconds after
`PON` and refuses `IIS` until the lamp is up — so a later tick does it once `QPW` says the
lamp is lit.

#### Prove the cable before you trust any of this

**Run `scripts/projector-probe.py` on the device first.** It is not a formality; it is the
step that decides whether this feature can work at all.

```console
$ sudo ./projector-probe.py status          # the one that proves the cable
$ sudo ./projector-probe.py --verbose status
$ sudo ./projector-probe.py on              # times the warm-up
$ sudo ./projector-probe.py cycle           # times the cool-down too
```

It shares no code with the daemon on purpose. The driver is written to degrade quietly, and
quiet degradation is precisely the wrong behaviour for the question being asked, which is
*did any bytes come back at all*. Here every frame is printed in both directions and silence
is a headline.

Two things it settles, neither of which software can:

1. **Whether the adapter is the right kind.** The one in use reports USB ID `0403:6015` —
   the FTDI FT230X/FT231X, sold both as a real RS-232 cable with a MAX3232 on board and as
   a bare 3.3 V TTL breakout. The projector wants ±12 V and cannot hear the second kind.
   A moulded D-sub 9 on the projector end is the good sign; a bare header is the bad one.
2. **Whether the protocol is right.** The command strings come from the PT-AE4000 manual
   (TQBJ0313, pp. 42–44) by way of a Rust implementation in a sibling repository that had
   only ever been tested against mocks. `tests/test_projector_protocol.py` ports that
   crate's vectors byte for byte — two implementations written from the same manual agreeing
   on the same bytes is worth more than either agreeing with itself — but agreeing with the
   manual is not the same as agreeing with the projector.

The manual is also ambiguous between models: the PT-AE3000U has two component inputs
(`CP1`/`CP2`) where the PT-AE4000 has a computer input (`RG1`). Both code sets are offered
and the projector answers `ER401` for whichever it lacks, which the sequence steps over.

Also worth knowing: the port is straight-through to a PC (pin 2 TXD, 3 RXD, 5 GND), so a
null-modem cable — identical from the outside — will not work; in standby the projector
accepts nothing but `PON`; and no `DeviceAllow=` is needed in the unit, because it already
runs as root and `ProtectSystem=full` does not touch `/dev`.

#### Seeing it without a projector

`docker compose up gui` runs a `fake` projector made of arithmetic, with a three-second
warm-up and a two-minute idle timeout, so the preparing view can be looked at and its
wording argued about on a laptop. It honours the two rules the sequence is built around —
standby accepts nothing but `PON`, and the lamp is not lit the instant `PON` returns — and
ignores everything else, because the rest would only be scenery. Set
`PLAYSTICK_GUI_PROJECTOR=` (empty) to get the `NullProjector` instead, which is the path
that must never stop a film playing.

#### Adding another projector

A file in `roles/player/files/playstick/projector/` and a line in its `MODELS`. Nothing
else in the daemon names a model, an input code or a baud rate. `base.py` is the whole
interface — `power_state`, `power_on`, `power_off`, `set_input`, `current_input` — and
`serial_io.py` is reusable by anything that frames commands between two bytes.

### Collecting sync telemetry from a phone

Headphone audio that breaks up for a few milliseconds every second or two only
reproduces on a real phone over real Wi-Fi — where there is no console to read
and nothing to attach a profiler to. So the phone measures and the stick keeps
the record: open the page with **`?debug`**, and every status poll carries the
listener's own numbers in an `X-Playstick-Sync` request header, which the daemon
writes to the journal next to what mpv believed at the same instant.

```bash
ssh vivostick 'journalctl -u playstick-web -f' | grep sync          # watch live
ssh vivostick 'journalctl -u playstick-web --since "1 hour ago" -o short-iso' \
  > sync.log                                                        # keep a film
```

One line per phone per second, and one field per thing that could be wrong:

```
sync 192.168.1.42 playing pos=1421.83 buf=0 v=1;id=8f2c;t=612.4;st=play;hid=0;
ct=1421.79;rs=4;nb=1;ahead=48.2;amin=47.9;err=-38;errp=-41;rate=-712;drift=-680;
step=0.2;ns=8;rtt=24;trim=0;w=1;dw=140;sk=0;wt=0;bf=0;lag=22;ls=0
```

Everything before `v=1` is the daemon's own view. After it: `id` distinguishes
phones (and reloads), `t` is seconds since that page loaded, `ct` the element's
`currentTime`, `ahead`/`amin` seconds of buffer now and at its low-water mark,
`err`/`errp` sound-minus-picture in ms and its signed peak, `rate`/`drift` the
correction and the crystal estimate in ppm, `w`/`dw` writes to `playbackRate`
and the largest of them, `wt` `waiting`/`stalled` events, `bf` polls where mpv
reported paused-for-cache, and `lag`/`ls` the worst shortfall in the element's
own clock and how many exceeded 30 ms. The full legend is in the docstring of
`Handler._log_sync`.

**Counts and peaks describe the interval since the previous line, not the film.**
The correction loop runs at 250 ms and the poll at 1 s, so a line that sampled
rather than accumulated would miss three quarters of what happened — which is
most of the point, since the fault is shorter than either.

### Reading a capture

Ten minutes of one phone is six hundred lines, which is past the point of
reading them. `scripts/sync-log-to-csv.py` flattens the journal into a table,
one column per field, and prints a digest first so a wasted capture is obvious
before anything gets plotted:

```bash
./scripts/sync-log-to-csv.py --summary sync.log > sync.csv
```
```
3 telemetry lines from 2 phone(s)
  phone                   lines     span   play  stalls  worst lag  min buf  rate writes   gaps
  10.0.1.237/d32b8e         612     611s    598      31     310 ms     0.4s          842      4
  10.0.1.99/aa11cc            8       8s      0       0          -        -            0      0
```

It reads whichever journalctl format it is handed (`-o short-iso`, `-o json`,
`-o cat`, or the default) and ignores everything that is not telemetry, so a
whole unfiltered journal can be piped in. `--id` narrows to one page load and
`--playing` drops the lines from a phone that was not listening.

Three columns are added to the ones the phone sends:

| column | |
| --- | --- |
| `dt` | seconds since that phone's previous line — **divide the counters by it.** The poll backs off to 5 s behind a locked screen, so a pocketed phone otherwise looks calm when it is not |
| `gap` | 1 when a poll was skipped before this line: do not read a trend across it |
| `ctpos` | `ct - pos` in ms, the element's clock against mpv's. A coarse cross-check on `err`, biased by the RTT and offset corrections `err` includes — good for catching an `err` that looks healthy because the page's own clock model has drifted |

### Adjusting the controller from the phone

`?debug` also puts a **Playback parameters** section in the sound sheet, one
row per constant the sync loop runs on — seek threshold, samples before
placing, proportional and integral gain, write deadband, rate clamp, error
smoothing, offset slew, correction interval, stall threshold. Minus and plus,
applied immediately, on the phone that is hearing the problem.

This exists because the loop between "change a number" and "hear whether it
helped" was an Ansible run, a service restart and a reload — and two of these
constants have already been set wrong from the armchair. `SEEK_LIMIT` below the
cost of the seek it triggers, `RATE_EPS` below the noise it was meant to
reject: neither is findable without a real device making a real sound.

Values are shown in the same units the telemetry uses — ppm and milliseconds —
so a number read off a capture can be typed straight back in. Each row names
the constant and its shipped value, so `git grep` still finds the thing you
just changed.

Three rules, all deliberate:

- **`?debug` only.** A value tuned during one film would otherwise sit in that
  phone's `localStorage` for good, invisibly, and the next listener would be
  debugging a build that exists nowhere.
- **This phone only.** Nothing is sent to the daemon and no other listener is
  affected. Six people can hold six different tunings at once, which is the
  cheapest A/B this system will ever offer.
- **Every telemetry line records them**, in a `tun` field listing whatever is
  not the shipped value (`tun=sl:350,re:300`). Empty on a stock build. Without
  it a capture taken mid-experiment is a capture of an unknown build.

**Reset to shipped** puts everything back. Nothing survives a reload without
`?debug`, so the way out of a tuning that made things worse is to drop the
query string.

### Plotting a capture

```bash
./scripts/sync-log-plot.py sync.csv -o sync.html     # or pipe the journal straight in
```

A standalone HTML file — inline SVG, no libraries, no network — with every
metric stacked against one time axis. Hovering anywhere reports every field at
that instant, which is what an SVG `<title>` is for and why there is no
JavaScript in it.

**One axis, because none of these faults is visible in a single series.** A
dropout is a *coincidence*: the element lost time and the rate was written, or
it lost time and the buffer collapsed, or it lost time and neither. Two
separate plots make that a guess. The panels are, in order: sync error with the
±45/−125 ms perception band drawn in, element clock loss against the 30 ms
stall threshold, per-interval counts (stalls, rate writes, seeks,
`waiting`/`stalled`, mpv buffering), `playbackRate` against its clamp, buffer
headroom, and rtt/`ns`/`dt`. A state strip along the top says whether the phone
was even playing, and stretches where a poll was skipped are shaded — a spike
in a count there may only mean the interval was five seconds instead of one.

`--id` picks a page load when several are in the capture (one page is one
phone; two of them share no clock), and `--start`/`--end` clip to a window in
seconds. Y ranges are set by Tukey fences, so a 13-second startup error does
not flatten the next four minutes; anything outside is drawn on the panel edge
and the panel says so.

Three readings settle which of the two candidate causes it is:

| what the log shows | what it means |
| --- | --- |
| `ahead`/`amin` collapsing toward zero | the daemon's pacing or the radio is starving the element |
| `w`/`dw` moving with the dropouts | this page is re-arming the render pipeline; on iOS `playbackRate` lands on `AVPlayer.rate` |
| `lag`/`ls` at zero through an audible break | neither — the clock never stopped, and the interruption is below anything the page can observe |

`lag` sits around 16–21 ms even when nothing is wrong: iOS reports `currentTime`
on decoded-frame boundaries, 21.3 ms for AAC-LC, so a reader sampling at 4 Hz
sees a staircase. That is why `ls` only counts shortfalls past 30 ms.

Nothing is logged unless a client sends the header, and only the page with
`?debug` does — there is no server-side switch, because the alternative to
logging this is not logging less, it is having no way to see what a phone in
another room was doing. The value is a header from an unauthenticated LAN
client, so it is filtered to `[A-Za-z0-9=;:.,+_-]` (newline and `%` are not in
that set), truncated to 400 characters, passed to the log call as an argument
rather than interpolated, and capped at 20 lines a second across all clients —
triple the design load of six phones, and a bound on what a client stuck in a
retry loop can do to a journal sharing 32 GB of eMMC with everything else.

### What the first capture found

68 seconds of an iPhone over Wi-Fi, 2026-08-02, and it settled the question in
the table above on the second row. Buffer headroom ran **80–296 s and never
dipped**; mpv never reported paused-for-cache; the element never fired
`waiting`/`stalled` on a line where it lost time. Not starvation, and not the
daemon's pacing. What it was:

| | stalled | clean |
| --- | --- | --- |
| wrote `playbackRate` in that second | **7** | 1 |
| didn't | 1 (the startup seek) | **58** |

Each write cost about 43 ms — two AAC-LC frames, which is what a re-armed
`AVPlayer` discards — and up to three ticks in one second, so 110 ms of audio
gone. `ctpos`, which compares mpv's clock to the element's without involving
the page's own model at all, confirmed the loss on every one.

The writes were not the disease. `rate` sat at the **+20000 ppm clamp on 64 of
67 playing lines**, because the element had been placed **1.01 s behind the
film** at the top of the playback and `RATE_LIMIT` can only walk that out at 2%.
The command leaves the clamp when `|err| < RATE_LIMIT/KP = 133 ms`; the `|err|`
on all seven writing lines was 97–132 ms. So: error shrinks to ~100 ms, command
comes off the clamp, write, 40–110 ms lost, error back to ~200 ms, re-saturate.
A limit cycle, and the audio stayed 100–250 ms behind the picture — past the
125 ms where a listener sees it — for the whole capture without converging.

The second was inherited at `t=24.1`, from `ns=1`: **the element was placed off
a single offset sample.** The max filter in `sndSample()` is what rejects a
sample that arrived late, and with one sample there is nothing to reject, so
the daemon's cached position — stale, because mpv had only just started — went
straight into the target. Two changes, both in `playstick-ui.html`:

- **`SEEK_SAMPLES = 3`.** Nothing is placed until the offset window has a
  population. The element is not started before then either, so a film opens
  with up to two seconds of silence rather than with a second of error that
  takes a minute to come out. A phone coming out of a pocket is the one case
  that keeps playing through a thin window — `visibilitychange` empties it on
  purpose while keeping the clock ratio, and silencing that phone every time
  somebody glanced at the time would be its own bug.
- **`SEEK_LIMIT` 1.0 → 0.25 s.** An error takes `err/RATE_LIMIT` to nudge out,
  so the old limit meant fifty seconds pinned at the clamp with the integrator
  frozen. 0.25 caps that at twelve, and stays above the worst standing error
  ever measured (216 ms) so that a loop which somehow re-saturates degrades
  into nudging rather than into a cut every few seconds.

Replaying the capture's opening against the page's real clock model: the old
constants plant the element 1180 ms late, the new ones land it within 10 ms.

### The page notices when it has been replaced

Deploying used to change nothing on any phone in the house. `/` is served
`no-store`, so a browser that *asks* for the page always gets the current one —
but after the first visit none of them ask again. The page polls `/api/status`
forever and never navigates, so a tab opened last month keeps running last
month's JavaScript until somebody thinks to pull down and refresh. Children do
not refresh, and the adults do not think to.

So the daemon stamps each copy on the way out and reports the same value on
every poll:

```console
$ curl -s stick.local/ | grep 'var BUILD'
var BUILD = "c20e48476c19";
$ curl -s stick.local/api/status | python3 -m json.tool | grep build
    "build": "c20e48476c19",
```

A page whose own stamp no longer matches reloads itself, normally within three
seconds of the deploy finishing.

**The stamp is a hash of `ui.html`, and every alternative was worse.** A version
number is something somebody has to remember to raise. The daemon's start time
would order every phone in the house to reload after a power cut, for a page
that had not changed by a byte — and `copy` gives the file a new mtime on every
playbook run, most of which have nothing to do with the page, so a timestamp is
the same mistake with extra steps. Hashing the daemon as well would be wrong in
the other direction: this payload is additive by house rule, an older page
against a newer daemon is a supported combination, and reloading for it would
be interrupting people to deliver nothing.

**It waits for a moment when there is nothing to lose.** A reload is a page that
forgets which film it is following, throws away the clock offset the headphone
sync spent a minute measuring, and drops the audio element out of somebody's
ears. So a mismatch found while a film is playing, paused, or a lamp is warming
is held until the state goes idle. In practice the wait never happens — the
deploy restarts the daemon, which stops the film — but a deploy run while
somebody was watching should not be the thing that ends it.

The hash is computed from the file on disk and cached against its mtime and
size, which costs one `stat` per poll and gets the dev container the same
behaviour for free: edit `roles/player/files/playstick-ui.html`, and every
browser pointed at `docker compose up gui` reloads itself. That is the shipped
mechanism, exercised every time anybody touches the page.

**The same stamp busts the posters and the soundtracks.** There is no third
asset to worry about — no CDN, no web fonts, no external images, one file — but
reloading the page does not empty a browser's image or media cache, and those
two are the only things this page fetches that a browser is allowed to keep. A
prepared poster is held for a day, an extracted frame for a year under
`immutable`, and a soundtrack for an hour. So every URL for one carries the
build:

```
/api/thumb/0123456789abcdef?v=c20e48476c19
/api/audio/0123456789abcdef/0?v=c20e48476c19
```

It goes in the **query**, and that is the whole reason it is safe. Every route
matches against the parsed path, so nothing on the daemon reads this — no new
value crosses the boundary, and those routes still accept exactly what they
accepted before: an opaque sixteen-hex id and a small integer. The
[no-authentication](#no-authentication) argument is untouched.

The cost is one deploy's worth of re-downloading: after a playbook run every
phone pulls the posters in its grid again, and a listener who reconnects pulls
their soundtrack again rather than resuming from the hour-long cache entry.
That is the trade being made deliberately — a poster or a track cached for a
year that the current release no longer produces is a fault nobody can see and
nobody can clear, and `Ctrl-Shift-R` is not a thing you can ask a child for.

### No authentication

`ufw` is purged by explicit decision, so port 80 is open to the LAN and anyone on it can
start a film — consistent with UxPlay next door accepting unauthenticated mirroring.
`player_allow_networks` rejects clients outside RFC1918 at the handler, which keeps a
misconfigured router from publishing the UI to the internet and is not a defence against
anybody already on your Wi-Fi. The control that does matter is that no filesystem path ever
crosses the HTTP boundary: the page addresses films by an opaque id that indexes a table
the daemon built from the index or by walking the share, every resolved path is re-checked
for containment before it reaches mpv, and no endpoint takes a path.

Exactly one endpoint streams file bytes, and this sentence used to say that none did.
`/api/audio` is what lets several people watch one silent projector and each hear their own
language in their own headphones. It is narrowed to the point of being dull: the route is a
regex matched against the whole path, the id must be exactly the sixteen lowercase hex
characters the library table is keyed by, and the track is a small integer indexing a list
whose paths were already proved to sit under the library root when the index was read. The
only files it can name are ones `playstick-prep.py` wrote.

```bash
systemctl disable --now playstick-web.service   # rollback
systemctl disable --now srv-movies.automount    # and the share
```

### The UI, on a development machine

`Dockerfile.gui` runs the player without a VivoStick, a projector or a NAS in
the loop, so the page can be iterated on at a desk — and looked at on a phone,
which is what it is for.

```bash
docker compose up gui                            # http://localhost:8080/ -> :80 inside
PLAYSTICK_GUI_LIBRARY=~/Videos docker compose up gui   # your own films
docker compose down -v                           # reset the library and posters
```

The container serves on port 80 exactly as the device does; only the *published* port is
8080, because a host port below 1024 is refused under rootless Docker. Override with
`PLAYSTICK_GUI_PORT`.

The daemon and the page are bind-mounted from `roles/player/files/`, and the
daemon re-reads `ui.html` on every request: edit the HTML, reload the browser.
Python changes need `docker compose restart gui`.

The daemon itself is the `playstick/` package next to `playstick-web.py`, which
is only an entry point — it prefers a `playstick/` sitting beside it, so a
clone runs without being installed:

| module | what is in it |
| --- | --- |
| `__init__.py` | why the daemon arbitrates the display itself, and what never crosses the HTTP boundary |
| `config.py` | every environment variable, read once |
| `airplay.py` | the UxPlay interlock: one sample, and the debounced version |
| `library.py` | `Library`, plus the title cleaning and sidecar search |
| `thumbs.py` | `Thumbs`, and the placeholder for films without a poster |
| `player.py` | `Player` and `Busy` — mpv, and the master clock phones sync to |
| `http.py` | `Handler` — every route |
| `main.py` | build the workers, hand them to the handler, serve |

With no library mounted the
entrypoint generates one out of lavfi test patterns, named the way a real
collection is — most of what the library code does is undo those names, and a
test library of `movie1.mkv` would never exercise it. Two of the seven files
are filtered on purpose, one by the skip regex and one by `player_scan_depth`.

**What it tests faithfully.** mpv is real, so the library scan, `clean_title`,
the poster pipeline, the play/pause/stop state machine, the progress bar and
the AirPlay interlock behave exactly as they do on the device. The interlock is
the interesting one, and it needs no iPhone — the daemon only ever asks `ss`
whether something holds an established connection to UxPlay's port:

```bash
docker compose exec gui fake-airplay    # ^C to release
```

The grid greys out, and a play request is refused with 409. Expect the refusal
to take two seconds: `airplay_confirmed()` samples twice, a second apart.

**What it cannot tell you: anything about the display**, which is most of what
makes this project hard. A container has no DRM node, no tty1, no VA-API and no
systemd, so `--vo=drm`, `--drm-mode`, the DRM-master arbitration against
`uxplay-kms.service` and every number under [Results](#results-so-far) stay
device-only questions. mpv runs `--vo=null` here: the film is decoded and paced
in real time and nothing is drawn. Two deviations from the unit are deliberate
and worth knowing before trusting what you see — `PLAYSTICK_MIN_SIZE_MB=0`,
because the generated clips are a few MB against the device's 100 MB floor, and
`PLAYSTICK_AUDIO=1`, because the page hides its volume controls otherwise and
they would never be looked at. Set it to `0` to see the layout as it ships.

### Tests

```bash
python3 -m unittest discover -s tests            # ~310 tests, about 11 seconds
./actl 'python3 -m unittest discover -s tests'   # the same, in the control node
```

No dependencies and no device: `unittest` from the standard library, a real
`ThreadingHTTPServer` on a loopback port the kernel picks, and fakes in place of
the three workers behind the handler. What is exercised is the wire — status
lines, headers, Range arithmetic, and when each piece of a paced body arrives.

Two things to know before adding a test, both in `tests/support.py`:
configuration is read from the environment once at import of
`playstick.config`, so tests reach the package through the names that module
re-exports rather than importing it themselves; and because `http.py` binds its
constants with `from .config import ...`, overriding one for a single test means
patching it in the *handler's* namespace, which is what `patched()` does.

The audio route has its own file. Most of what is in it is there because the
client is iOS Safari: `Range: bytes=0-1` answered with a 200 rather than a 206
makes it refuse the resource outright, and it will pull a progressive file as
fast as the socket allows across the one radio that is also reading the film off
the NAS — hence the pacing, and hence a test that asserts the delivery
granularity `AUDIO_CHUNK` implies rather than only its value.

`tests/test_sync_csv.py` covers the telemetry-to-CSV script above, and needs
none of that harness. A field quietly landing in the wrong column there would
not look like a failure — it would look like an answer.

`tests/test_prep_metadata.py` is the same argument about the library index, and
it exists because that failure really happened: every case in it is a real file
from a real shelf, under its own name, with the runtime `ffprobe` measured and
the result set TMDb actually returned. It holds the mux date out of the year,
the release string out of the title, and — the one that matters — asserts that
being asked the wrong question is survivable: fed the recorded `year=2012`
response for *Hook*, the lookup returns nothing rather than Red Hook Summer. It
also diffs `clean_title()` against the daemon's copy, because the comment saying
those two are kept byte-for-byte in step was, until now, only a comment.

`tests/test_prep_media.py` is the same argument again, one level down: what prep
*names* the encode it made. That one also really happened —
`033fa22cc64e9f97-f1-the-movie.mp4` and `033fa22cc64e9f97-f1.mp4` side by side,
the same film twice, because the name used to carry a slug of the title and the
title is re-derived on every run. It is the quietest class of bug this tool has.
Nothing returns an error, nothing looks wrong on the projector, and the only
symptom is a share filling up at twice the rate it should. So the file holds the
naming contract from both ends: one encode per film, named for the id alone, and
everything else that shares that id — another film's encode, a poster, a
subtitle, a half-written `.part`, a `--force` run that failed — left exactly
where it was.

The page has its own, kept separate because they need node:

```bash
./tests/js/run.sh          # uses local node, or node:22-alpine if there is none
```

`tests/js/page.js` loads the real `playstick-ui.html` script under a stub DOM
where **time is a variable the driver holds**, which is the only way a
controller is testable at all. `clock.js` replays the opening of the 2026-08-02
capture — the old constants plant the element 1180 ms behind the film, the
current ones land it within 10 ms — and holds the placement rules and the
steady-state write rate. `telemetry.js` is mostly negatives: a stall detector
that counts a seek, a deliberate slowdown or iOS's frame quantisation as a
dropout does not report a fault, it manufactures one, in the log the next fix
gets argued from. `tune.js` covers the debug sheet's parameter controls,
including the taps going through the handlers the page really attached.
`library.js` is the grid's filters, and holds the things that decide what a
child can see: A to Z is byte-identical to the unfiltered order the server sent,
a film with no year is last in *both* year orders, no combination can leave the
grid empty without a way back out, and a poster arriving mid-scan lands on its
own tile rather than on whichever one shares its index. `preparing.js` is the
view a child watches while a lamp warms up, `admin.js` the curator's editor, and
`build.js` the reload after a deploy — mostly the moments it must *not* pick,
since a reload drops the audio element out of somebody's ears.

## Results so far

Four sweeps. The first three were taken on a 2560×1440 monitor at its EDID-preferred mode — not on
the projector this device is for — so every row carried a 1080p→1440p rescale. **`matrix-20260731-153753`
is the one that counts**: 78 runs at the projector's native `1280x720@60`, where the 720p rows are
pixel-exact with no scaling anywhere in the chain.

Zero `FIFO underrun` messages across the whole sweep. At 74250 kHz / ~297 MB/s the bandwidth
problem that started this investigation is closed.

### Which columns to trust

Frame metrics reproduce across independent sweeps to within about 1%. `cpu_pct` does not —
identical config (1080p `vaapih264dec` → `fakesink`) measured 18.0% in one sweep and **3.5%** in
the next. It is sampled system-wide from `/proc/stat` over the run window, so 100% means all four
cores, and it is meaningless on short or failed rows. Conclusions rest on fps and drop rate.

### Software decode wins end to end

Only three to-screen rows in 78 reached `ok`, and all three are `avdec_h264`:

| clip | pipeline | fps | drop | CPU |
|---|---|---|---|---|
| 720p | `avdec_h264` → videoconvert → kmssink | **29.19** | **0.00%** | 40.1 |
| 720p | `avdec_h264` → videoconvert → kmssink `skip-vsync` | **29.21** | **0.00%** | 41.2 |
| 1080p | `avdec_h264` → videoconvert → kmssink `skip-vsync` | 28.55 | 0.51% | 49.8 |

Best hardware-decode rows, same glue and sink:

| 720p | `vah264dec` | 19.76 | 0.33% | 17.5 |
|---|---|---|---|---|
| 720p | `vaapih264dec` | 19.58 | 0.00% | 17.5 |
| 1080p | `vaapih264dec` | 13.39 | 16.58% | 18.3 |

This retires the assumption the project was founded on. It is not a rescaling artefact: the 720p
rows have no scale in them at all.

**Why hardware decode loses.** `vaapih264dec` into `fakesink` is 29.59 fps at **11.4% CPU** — the
cheapest number in the matrix. Decoding is not the problem; delivery is. Convert the to-screen
rows to milliseconds per frame and fit against pixel count:

```
vaapih264dec   720p 51.1 ms   1080p 74.7 ms   ->  20.5 ms/Mpx  +  32.2 ms fixed
vah264dec      720p 50.6 ms   1080p 87.9 ms   ->  32.4 ms/Mpx  +  20.7 ms fixed
```

A per-frame cost of **20–32 ms that does not scale with resolution**, against a 33.3 ms budget,
while CPU sits at 17.5% of four cores. Idle and failing is the signature of a stall, not of
compute. No `memory:VAMemory` or `memory:DMABuf` feature appears in any negotiated caps, so
zero-copy never happens and every frame is read back out of GPU memory over an uncached mapping.
`avdec_h264` decodes into cacheable system memory, never reads back, costs 2.5× the CPU and wins.

The `19.58 fps / 0.00% drop` row is worth keeping as a teaching case: `fpsdisplaysink` counted
1197 frames and dropped none, because 603 never reached it. `drop_pct` alone is blind to that,
which is why the status rule also requires `fps ≥ 0.95 × nominal`.

### Sinks: two dead ends, one untestable

| sink | result |
|---|---|
| `kms-default` / `kms-novsync` | the only working paths, and only with `videoconvert` |
| `kms-modeset` (`force-modesetting=true`) | **0 of 24**. Broken on this driver |
| `waylandsink` | **0 of 18** — a harness artefact, see below |
| glue `none` | **0 of 24**, `not-negotiated`. kmssink always needs a converter |

`kms-modeset` is not academic: UxPlay's `-fs` sets exactly that property, and it is why
`uxplay-kms.service` shipped a command line that could not start. The sweep found the bug before
the service ever ran.

`vapostproc` as glue is a trap — 21 of its 24 rows land between 45% and 52% drop, including with
`avdec_h264`, which produces no VA surfaces at all. It uploads system memory to a VA surface and
pulls it straight back, manufacturing the very stall described above.

### `skip-vsync=true` is mostly a workaround

| config | default | `skip-vsync` | |
|---|---|---|---|
| `vah264dec` 720p | 6.76 | 19.76 | 2.9× |
| `vaapih264dec` 720p | 7.26 | 19.58 | 2.7× |
| `avdec_h264` 1080p | 27.35 | 28.55 | +4% |
| `avdec_h264` 720p | 29.19 | 29.21 | — |

Large for hardware decode, nil for the configuration that ships. Earlier sweeps recorded this as
"a large win"; at a native 60 Hz mode that reading does not survive, and tearing is not worth 4%
on a path we no longer take. **Deliberately not applied to the unit.**

### Still open

- **`waylandsink` cannot be judged from this data.** The probe runs pipelines bare under `openvt`
  with no compositor for it to connect to, so 0 of 18 says nothing about the cage path. The column
  should either wrap in `cage` or be dropped — as it stands it reads like evidence and is not.
- **The 720p/1080p CPU inversion, third reproduction.** `avdec_h264` → `fakesink` costs 65.6% at
  720p against 46.6% at 1080p, both at full rate. Not thermal (`core_throttle_count = 0`), and not
  the display mode — it has now appeared at 1440p, 1080p30 and 720p60. The 1080p clip carries more
  bits per frame *and* more pixels, so the obvious explanations point the wrong way. The one left
  standing: `make-testclip.sh` does not produce a pixel-proportional pair (6 Mbps at 720p is
  **0.217 bits/px** against 10 Mbps at 1080p's **0.161**), and IDCT/deblocking scale with non-zero
  coefficients rather than pixel count. Cheap to test — re-encode 720p at 4.4 Mbps and re-run the
  two `fakesink` rows.

A first-person write-up of how this was measured — including several ways the harness fooled me
before it produced anything trustworthy — is in
[docs/matrix-narrative.md](docs/matrix-narrative.md).

## Reading the matrix

`uxplay-probe-matrix.sh` sweeps decoder × sink × resolution and writes a CSV:

| dimension | values |
|---|---|
| decoder | `avdec_h264` (software baseline), `vah264dec`, `vaapih264dec` |
| sink | `fakesink` (ceiling), `kms-default`, `kms-modeset`, `kms-novsync`, `waylandsink` (under cage) |
| clip | 1280×720@30, 1920×1080@30 |

Narrow a sweep with `-e probe_decoders=…` and `-e probe_sinks=…` (comma-separated sink labels).
The `kms-*` variants isolate **one property each**, so a failure points at a specific cause rather
than at a bundle — that is how `driver-name` and `force-modesetting` were separated.

Everything runs with `sync=true` against a clip at native rate, so a combination that cannot keep
up shows as **dropped frames** — the same failure mode AirPlay mirroring would have — rather than
as a slow batch job. Pipelines run on a real VT via `openvt`, because over SSH there is no console
at all (`fgconsole` says so) and neither DRM master nor a seat can be acquired without one.

The `glue` column records what GStreamer needed between decoder and sink. Empty is the good
answer: DMABuf straight into `kmssink` with no copy. A `vapostproc` or `videoconvert` there means
negotiation refused the zero-copy path and you are paying for a full-frame download.

**How `status` is decided.** A row is `ok` only if drop rate < 5% **and** sustained fps ≥ 95% of
nominal. Both conditions are needed, and getting this wrong three times is most of the story of
this repo:

- exit code — `openvt` does not propagate the child's, so every row read as an error;
- drop rate alone — a row at 5 fps discarding half its frames was reported `ok`, which also
  stopped the glue loop before it reached the variant that worked;
- drop rate alone again — `fpsdisplaysink` counts only frames it *received* and threw away, so a
  run can reach EOS reporting `dropped: 0` having shown 1427 of 1800 frames.

`rc` is still recorded, but nothing is judged by it.

## Trimming the install

With 1537 MB usable RAM and four 1.44 GHz cores, anything running in the background is competing
with the decoder. The `trim` role strips the server install back toward what a kiosk needs. Every
entry was chosen from what is actually installed and running on this device, not from a generic
debloat list.

**Purged** (109 packages including dependencies): `apport`, `bolt`, `fwupd`, `kdump-tools`,
`landscape-common`, `modemmanager`, `multipath-tools`, `networkd-dispatcher`, `open-iscsi`,
`packagekit`, `plymouth`, `rsyslog`, `snapd`, `software-properties-common`, `udisks2`, `ufw`,
`upower`.

**Modules blacklisted:** `spi_intel_platform`, `spi_intel`. These fail to bind on this firmware
and log two errors onto a console that happens to be the projector:

```
intel-spi intel-spi: unsupported C0DEN: 0xc
intel-spi intel-spi: probe with driver intel-spi failed with error -22
```

`spi-intel` drives the BIOS SPI flash. Nothing here needs it, it cannot work on this hardware, and
a driver capable of writing firmware is not one to leave loaded on a kiosk. Both `blacklist` *and*
`install … /bin/true` are written, because blacklist alone does not stop a modalias-triggered load.

**`kdump-tools` deserves its own note.** It brings `/etc/default/grub.d/kdump-tools.cfg`, which is
where `crashkernel=2G-4G:320M,…` on the kernel cmdline comes from. On this box that reserves
*nothing* — `/proc/iomem` shows `00000000-00000000 : Crash kernel`, because total RAM falls below
the 2 G floor of its own range list — yet the service runs anyway, servicing a crash kernel that
cannot exist. Purging the package is what removes the cmdline; editing `/etc/default/grub` would
not, since the snippet re-adds it. Takes `crash`, `kexec-tools` and `makedumpfile` with it.

**Boot console noise is deliberately left alone** (`trim_quiet_boot: false`). Tempting on a device
whose display is the output, but the message that cracked the performance investigation open was
`i915 … *ERROR* CPU pipe C FIFO underrun`, read straight off tty1. Hiding that class of message
costs more than a tidy boot screen is worth while anything is still being measured.

**Disabled but kept:** periodic timers (`apt-daily`, `motd-news`, `man-db`, `dpkg-db-backup`,
`e2scrub_all`) and `unattended-upgrades`. `fstrim.timer` is deliberately left alone — TRIM is worth
having on eMMC.

**Kept on purpose:** `thermald` (a passively cooled stick doing sustained video decode wants
thermal management), `chrony` (pairing and TLS care about time), `lvm2`, `dbus`, `avahi-daemon`.

Three safety properties, because this role removes things from a device reachable only over SSH:

- A `trim_protected_packages` list is asserted against the purge list before anything runs. `lvm2`
  is on it — root is `/dev/mapper/ubuntu--vg-ubuntu--lv`, so removing it would leave an unbootable
  box.
- **cloud-init is disabled, never purged.** Purging it can take `/etc/netplan/50-cloud-init.yaml`
  with it. That file is how this headless device gets on the network.
- The purge was verified with `apt-get --simulate ... --autoremove` against the real machine
  before being enabled; the audit confirmed no critical package is dragged out by autoremove.

`rsyslog` going away means journald is the only log sink, so it gets capped at
`trim_journal_max_use` (64 M) rather than being left to grow into the eMMC.

Set `trim_enabled: false` to skip the whole role.

### Two consequences worth accepting deliberately

**No firewall.** `ufw` is purged by explicit decision. UxPlay listens on 7000–7002 (TCP and UDP)
plus 5353/udp, and nothing filters them — the device is only as protected as the LAN it sits on.

**Patching is manual.** `unattended-upgrades` is disabled because an unattended apt run on this CPU
would visibly disrupt a live mirroring session. That trade buys smooth playback and costs automatic
security updates; run `apt update && apt full-upgrade` yourself periodically.

## Measured on the device

From `uxplay-probe-facts.sh`, 2026-07-31:

| | |
|---|---|
| VA-API driver | `Intel i965 driver for Intel(R) CherryView - 2.4.1`, VA-API 1.23 |
| H.264 decode | `VAProfileH264High : VAEntrypointVLD` — hardware decode confirmed |
| Elements | `vah264dec`, `vaapih264dec`, `vapostproc`, `kmssink`, `waylandsink` all present |
| Display | HDMI-A-1 connected, connector id 120, EDID good — see [On `drm_force_mode`](#on-drm_force_mode) |
| Network | `enx6c6e070fd5e1` — **USB Ethernet**, 107 Mbps sustained to the control machine |
| Display server | no Xorg, no compositor |

Two things this settled:

**The network risk is closed.** The stick is on a USB Ethernet adapter, not its SDIO Wi-Fi, and
sustains 107 Mbps — four to ten times what 1080p mirroring needs.

**VA plugins need render access to register.** `gst-inspect-1.0 vah264dec` and `vainfo` report
*nothing at all* when run by a user outside the `render` group, which is indistinguishable from a
failed install. `base_login_gpu_access` puts the login user in `video`/`render` to close that trap.
Run the probes as root regardless.

AirPlay discovery is mDNS, so the iOS device must be on the same L2 segment.

## Rollback

```bash
systemctl disable --now uxplay-kms.service uxplay-cage.service
systemctl disable --now uxplay-idle.service   # stop the console clock
systemctl unmask --now getty@tty1.service     # restore the local console
```

The first line is undone by the next `site.yml` run, which is the point of `uxplay_output_path`:
set it to `none` if the receiver should stay off across provisions.

`uxplay-idle.service` sets `TTYReset=yes`, so stopping it hands tty1 back in a usable state. The
console font and blanking timeout it changed are not restored — `setfont` with no argument and
`setterm --blank 10` do that, or just reboot.

## Tunables

All in `group_vars/all.yml`.

| variable | default | what it does |
|---|---|---|
| `drm_force_mode` | `1280x720@60` | Forces the HDMI mode via kernel cmdline. **Reboot required.** |
| `drm_force_connector` | `HDMI-A-1` | Which connector the above applies to |
| `drm_card_path` | *(auto)* | i915 DRM node; discovered, not assumed to be `card0` |
| `uxplay_output_path` | `kms` | Which receiver is enabled and started: `kms`, `cage` or `none` |
| `uxplay_decoder` | `avdec_h264` | Software. See [Results](#results-so-far) — measured, not a compromise |
| `uxplay_request_size` | *(derived)* | `-s`, the resolution asked of the client. Follows `drm_force_mode` |
| `uxplay_request_fps` | `30` | Frame rate asked of the client; not the scanout rate |
| `uxplay_extra_args` | `""` | Appended verbatim to both units. `-nc` is rejected — see [Idle screen](#idle-screen) |
| `uxplay_nofreeze` | `true` | `-nofreeze`, so the display is released when a session resets |
| `idle_enabled` | `true` | The framebuffer clock shown when nothing is mirroring |
| `idle_blank_minutes` | `30` | Black the screen after this long idle; `0` never blanks |
| `idle_fonts` | *(list)* | PSF files the clock renders glyphs from; first readable wins |
| `idle_time_format` | `%H:%M` | strftime format for the clock |
| `idle_clock_scale` | `0` | `0` fits the clock to the framebuffer; set a number to pin it |
| `idle_subtitle` | `""` | Second line; empty derives `name@hostname` from the UxPlay config |
| `uxplay_advert_name` / `uxplay_port` | `Projector` / `7000` | mDNS name, and ports n..n+2 |
| `i965_nonfree_shaders` | `true` | Picks the multiverse driver build; the two `Conflict` |
| `libva_driver` | `i965` | Pinned; iHD does not cover Gen8 LP |
| `base_login_gpu_access` | `true` | Login user into `video`/`render` so `vainfo` works unsudoed |
| `enable_zram` / `claim_tty1` | `true` / `true` | zram swap; mask `getty@tty1` for kmssink |
| `trim_enabled` | `true` | The whole trim role |
| `probe_iperf_server` | `""` | Control-machine address for the throughput test |
| `nas_server` / `nas_share` | `""` / `""` | The CIFS movie library. Set these in `host_vars/vivostick/local.yml`, not here. Both empty skips the role entirely. `nas_share` is the share **name**, not the server-side path |
| `nas_mount_point` | `/srv/movies` | Where the share appears; the unit names are derived from it |
| `nas_username` / `nas_password` | `""` | Vault these. Empty username mounts as guest |
| `player_vo` / `player_hwdec` | `drm` / `no` | Unproven pair — see [Movies from the NAS](#movies-from-the-nas) |
| `player_audio` | `false` | `--ao=null`. **Films play silently** until HDMI audio is probed |
| `player_enabled` / `player_port` | `true` / `80` | The web UI. Below 1024 needs `player_user: root`, which is the default |
| `player_index_file` | `<library>/playstick-library.json` | The index `playstick-prep.py` writes. Present means the daemon reads it instead of walking the share; `""` ignores one that is there |
| `player_subtitles` | `true` | Hand mpv the subtitles the prep tool extracted. They are passed as `--sub-file` because the share is read-only and `--sub-auto` cannot reach them |
| `player_airplay_unit` | *(derived)* | The unit the player stops to take DRM master. Follows `uxplay_output_path` |
| `player_projector_model` | `""` | `pt-ae4000`, `pt-ae3000u`, or empty for no projector. **Run `scripts/projector-probe.py` before setting it** — see [Turning the projector on](#turning-the-projector-on) |
| `player_projector_device` | *(auto)* | Serial port; discovered under `/dev/serial/by-id`, not assumed to be `/dev/ttyUSB0` |
| `player_projector_input` | `HD3` | The `IIS:` parameter. Empty leaves the input alone |
| `player_projector_idle_minutes` | `30` | Lamp off after this long with no film and no mirroring; `0` never |
| `player_projector_wake_on_airplay` | `true` | Whether mirroring may strike the lamp as well as a film |
| `player_projector_airplay_wake_ticks` | `2` | Consecutive confirmed ticks first. Raise this if the projector ever switches itself on unbidden |

The rest live in `roles/nas/defaults/main.yml` and `roles/player/defaults/main.yml`, the way
`idle_*` and `trim_*` already do.

### On `drm_force_mode`

This is the one tunable set from a measured failure rather than a preference. On the 1440p monitor
used for development the kernel logs `i915 0000:00:02.0: [drm] *ERROR* CPU pipe C FIFO underrun` at
the preferred 2560×1440 — the display engine's FIFO running dry mid-scanline, which corrupts the
picture regardless of what fps the pipeline reports. Scanout bandwidth tracks the pixel clock.

The real target is now attached: a **Panasonic PT-AE4000** projector (EDID monitor name `AE-4000`,
2009, 1080p LCD). Its 17 modes, abridged, with scanout cost at 4 B/px:

| mode | pixel clock | ~scanout @ 4 B/px | |
|---|---|---|---|
| 1920×1080**i** @ 60.00 | 74250 kHz | ~297 MB/s | **EDID preferred** |
| 1920×1080 @ 60.00 | 148500 kHz | ~594 MB/s | |
| 1920×1080 @ 50.00 | 148500 kHz | ~594 MB/s | |
| **1920×1080 @ 24.00** | **74250 kHz** | **~297 MB/s** | **default** |
| 1920×1080 @ 23.98 | 74176 kHz | ~297 MB/s | |
| 1280×720 @ 60.00 | 74250 kHz | ~297 MB/s | |

Three things this changed:

**The preferred mode is interlaced.** Both detailed timings in the base EDID block are 1080**i**.
Leaving `drm_force_mode` empty does not merely cost bandwidth here — it puts a deinterlacer in the
path. That alone justifies forcing a mode on this display.

**The projector advertises no 1080p30.** CEA VIC 34 is absent from its video data block. The
earlier `1920x1080@30` default was therefore a kernel-synthesized CVT mode — `modetest` reported
`type: userdef` — at 80192 kHz. It was legal for this panel (range descriptor: 24–61 Hz, 28–68 kHz,
150 MHz max pixel clock; HDMI VSDB: 190 MHz max TMDS) and it did lock, but it was not a mode the
display declares.

**`@23.98` is not expressible on the kernel cmdline.** `drm_mode_parse_cmdline_refresh()` reads the
refresh with `simple_strtol`, so the `.` ends the parse, the whole `video=` argument is discarded,
and the connector falls back to the preferred mode — 1080i60 — with no warning. A fractional parser
would not help either: `drm_mode_vrefresh()` rounds with `DIV_ROUND_CLOSEST`, so 23.976 and 24.000
both report `24` and the first match in the mode list wins. The two differ by 74 kHz, about 0.1% of
scanout. Hence `@24`.

The tradeoff being accepted: AirPlay mirroring is a 30 fps source and 24 Hz cannot show it evenly —
one frame in five is dropped or held, which is visible on motion and on cursor movement. That buys
321 → 297 MB/s, roughly 7.5%. Two alternatives at the same or better smoothness:

- `1280x720@60` — same 297 MB/s, smooth, trades resolution instead.
- `1920x1080@60` — smooth at full resolution, but 594 MB/s, which is the territory the FIFO
  underruns came from.

Set `drm_force_mode: ""` to use whatever the EDID prefers (here: 1080i60).

Note that the projector offers nothing above 1080p, so the 1440p rescale that inflated every
development sweep is gone on the real target.

`i915.enable_fbc=0` remains a deliberate non-default: apply only if tearing or flicker shows up.
