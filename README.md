# uxplay-atom

Provisioning and measurement harness for an **ASUS VivoStick TS10** acting as an AirPlay
mirroring receiver plugged straight into a projector's HDMI port — video rendered directly on the
Intel integrated graphics, **with no X server anywhere**.

Managed entirely over SSH from a control machine (ThinkPad X1).

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
| Network | SDIO-attached Wi-Fi |

Three consequences shape everything here:

1. **Software H.264 decode is not viable.** Airmont at 1.44 GHz cannot sustain 1080p30 H.264.
   VA-API is a hard requirement, and the `graphics` role *fails the play* if hardware VLD is
   absent rather than shipping a box that silently limps.
2. **Cherry Trail is i965-only.** `intel-media-driver` (iHD) does not cover Gen8 LP, so
   `LIBVA_DRIVER_NAME=i965` is pinned everywhere to stop libva autodetection wandering off.
3. **2 GB RAM / 32 GB eMMC.** No desktop, no build toolchain — hence the archive UxPlay package
   rather than a source build, and zram rather than a swapfile.

Verified present in Ubuntu 26.04 "Resolute": `uxplay 1.73.2-1`, `i965-va-driver 2.4.1+dfsg1-2build1`,
`gstreamer1.0-vaapi 1.26.8-2`.

**Audio is deliberately out of scope.** UxPlay runs with `-a`. The Cherry Trail LPE/SST audio
stack is the least reliable part of this platform, and skipping it removes the largest single
source of failure. If audio is wanted later, a USB audio adapter sidesteps the SoC path entirely.

## Layout

```
ansible.cfg              inventory path, pipelining, longer timeouts
inventory.yml            single host: vivostick  <- fill in host + user
group_vars/all.yml       every tunable lives here
site.yml                 base -> graphics -> uxplay -> probe
fetch-results.yml        run the probes, pull output into results/
scripts/make-testclip.sh build H.264 test clips on the CONTROL machine
roles/
  base/      multiverse, uxplay service account, zram
  graphics/  i965 VA-API stack, modetest, the hardware-decode assert gate
  uxplay/    uxplay + avahi + cage/seatd, both systemd units, tty1, ufw
  probe/     measurement scripts and test clips
results/     probe output fetched back (gitignored)
```

Only `ansible.builtin` modules are used — a plain `ansible-core` install is enough, no collections.

## Usage

```bash
# 0. control machine: ansible + a test clip
sudo apt install ansible-core          # or: python3 -m venv venv && venv/bin/pip install ansible-core
./scripts/make-testclip.sh             # needs ffmpeg; writes to roles/probe/files/

# 1. point the inventory at the stick
$EDITOR inventory.yml                  # ansible_host, ansible_user
ansible -m ping vivostick

# 2. provision
ansible-playbook site.yml --check --diff
ansible-playbook site.yml

# 3. find out what this box can actually do
iperf3 -s &                            # on the control machine
ansible-playbook fetch-results.yml -e probe_iperf_server=<x1-ip>
ansible-playbook fetch-results.yml -e run_matrix=true   # ~20 min, owns the display
```

Neither UxPlay unit is enabled by `site.yml`. That is deliberate — which one wins is a probe
result, not a guess.

The clips total ~114 MB and are copied to the device by the `probe` role. Over SDIO Wi-Fi that
first run is slow; it is a one-time cost, and `-e run_matrix=true` on later runs reuses what is
already in `/usr/local/share/uxplay-probe`. Shorten them with `DURATION=30 ./scripts/make-testclip.sh`
if the link makes it painful.

## Choosing the output path

Two units are installed, `Conflicts=` each other, and only one should ever be enabled.

**`uxplay-kms.service`** — pure DRM/KMS, no compositor at all.

UxPlay's GStreamer pipeline ends in `kmssink`, which writes to a DRM plane on `/dev/dri/card0`.
It becomes DRM master simply by being the first process to open the card: i915's fbdev console
emulation lives in the kernel and holds no master, so the only real contender is a getty painting
tty1 — which is why the unit conflicts with `getty@tty1` and the `base` role masks it. tty2 stays
as a rescue console.

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

## Reading the matrix

`uxplay-probe-matrix.sh` sweeps decoder × sink × resolution and writes a CSV:

| dimension | values |
|---|---|
| decoder | `avdec_h264` (software baseline), `vah264dec`, `vaapih264dec` |
| sink | `fakesink` (decode-only ceiling), `kmssink`, `waylandsink` (under cage) |
| clip | 1280×720@30, 1920×1080@30 |

Everything runs with `sync=true` against a clip at native rate, so a combination that cannot keep
up shows as **dropped frames** — the same failure mode AirPlay mirroring would have — rather than
as a slow batch job.

The `glue` column records what GStreamer needed between decoder and sink. Empty is the good
answer: `vah264dec` handing DMABuf straight to `kmssink` with no copy. A `vapostproc` or
`videoconvert` there means negotiation refused the zero-copy path and you are paying for a
download, which the `cpu_pct` column will confirm.

Sanity check on the results: if `avdec_h264` holds 1080p30, something is misidentified — that
should not be possible on this SoC.

## Known risk: network throughput

The VivoStick's Wi-Fi is SDIO-attached, and 1080p mirroring wants roughly **10–25 Mbps sustained
with low jitter**. The facts probe runs `iperf3` back to the control machine for exactly this
reason. If the number comes back thin, a USB Ethernet adapter on the USB 3.0 port is the fix —
better learned now than mid-demo. Note that AirPlay discovery is mDNS, so the iOS device must be
on the same L2 segment.

## Rollback

```bash
systemctl disable --now uxplay-kms.service uxplay-cage.service
systemctl unmask --now getty@tty1.service     # restore the local console
```

## Tunables

All in `group_vars/all.yml`: `uxplay_advert_name`, `uxplay_port`, `uxplay_decoder`,
`uxplay_extra_args`, `libva_driver`, `enable_zram`, `claim_tty1`, `probe_iperf_server`.

Two kernel-cmdline knobs are deliberately **not** applied by default, because they should be
responses to observed problems rather than cargo cult:

- `video=HDMI-A-1:1920x1080@60` — only if the facts probe shows the projector's EDID is unusable
  (connector `disconnected`, or an empty/absurd mode list).
- `i915.enable_fbc=0` — only if tearing or flicker shows up.
