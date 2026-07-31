![playstick](logo.svg)

**Old tech meets modern software.**

Playstick turns an **ASUS VivoStick TS10** into a compact, dedicated AirPlay mirroring receiver 
that plugs directly into a projector's HDMI port.

It provisions and measures a minimal Ubuntu Server installation running [`UxPlay`](https://github.com/simukka/UxPlay), 
with video rendered directly through the Intel integrated graphics stack **without an X server or desktop environment**.

The device is managed entirely over SSH from a separate control machine, currently a ThinkPad X1.

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
inventory.yml            single host: vivostick  <- fill in host + user
group_vars/all.yml       every tunable lives here
site.yml                 base -> trim -> graphics -> uxplay -> idle -> probe
fetch-results.yml        run the probes, pull output into results/
scripts/make-testclip.sh build H.264 test clips on the CONTROL machine
roles/
  base/      apt components, uxplay service account, zram
  trim/      strip services/packages the kiosk does not need
  graphics/  i965 VA-API stack, modetest, the hardware-decode assert gate
  uxplay/    uxplay + avahi + cage/seatd, both systemd units, tty1
  idle/      the framebuffer clock shown when nothing is mirroring
  probe/     measurement scripts and test clips
docs/        matrix-narrative.md — how this was measured, and what fooled me
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
```

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
ssh-copy-id simukka@10.0.1.228
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

```bash
# 0. control machine: a test clip (skip if using ./actl for everything)
./scripts/make-testclip.sh             # needs ffmpeg; writes to roles/probe/files/

# 1. point the inventory at the stick
$EDITOR inventory.yml                  # ansible_host, ansible_user
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

Neither UxPlay unit is enabled by `site.yml`. That is deliberate — which one wins is a probe
result, not a guess.

Step 3 is not optional on first provision: `drm_force_mode` writes the kernel cmdline, and until
the box reboots you are still measuring the old display mode.

The clips total ~114 MB and are copied to the device by the `probe` role — a few seconds over the
USB Ethernet link. Later runs reuse whatever is already in `/usr/local/share/uxplay-probe`.
Shorten them with `DURATION=30 ./scripts/make-testclip.sh` if you want faster sweeps.

## Choosing the output path

Two units are installed, `Conflicts=` each other, and only one should ever be enabled.

**`uxplay-kms.service`** — pure DRM/KMS, no compositor at all.

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

Enable the winner:

```bash
ansible vivostick -b -m systemd -a 'name=uxplay-kms.service enabled=true state=started'
```

Expect the hotplug behaviour to be the deciding factor rather than raw throughput: a projector
that gets unplugged between sessions is the normal case, and wlroots handles that re-read where
bare `kmssink` may not.

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

**Stop it during probe sweeps.** `uxplay-probe-matrix.sh` runs pipelines on VT1 via `openvt`;
`systemctl stop uxplay-idle` first removes the variable.

```bash
systemctl disable --now uxplay-idle.service    # rollback
```

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
