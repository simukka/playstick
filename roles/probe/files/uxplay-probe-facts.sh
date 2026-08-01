#!/usr/bin/env bash
# Inventory what this particular VivoStick actually is, and what the attached
# projector actually reports. Everything downstream -- connector ids, plane
# ids, whether hardware decode exists at all -- is read off this.
#
# Usage: uxplay-probe-facts.sh [iperf3-server-address]
set -uo pipefail

OUT_DIR="${PROBE_RESULTS_DIR:-/var/log/uxplay-probe}"
IPERF_SERVER="${1:-${PROBE_IPERF_SERVER:-}}"
mkdir -p "$OUT_DIR"
REPORT="$OUT_DIR/facts-$(date +%Y%m%d-%H%M%S).txt"

export LIBVA_DRIVER_NAME="${LIBVA_DRIVER_NAME:-i965}"
export GST_VA_ALL_DRIVERS=1

section() { printf '\n=== %s ===\n' "$1"; }
run() { printf '\n--- %s\n' "$*"; "$@" 2>&1 || printf '(exit %d)\n' "$?"; }

{
  printf 'uxplay-probe facts: %s on %s\n' "$(date -Is)" "$(hostname)"

  section "Platform"
  run uname -a
  run cat /etc/os-release
  run free -m
  run nproc
  grep -m1 'model name' /proc/cpuinfo

  section "Graphics hardware"
  run lspci -nnk
  printf '\n--- i915 kernel messages\n'
  dmesg 2>/dev/null | grep -iE 'i915|drm' || echo '(none, or dmesg restricted)'
  run cat /proc/cmdline

  # What the projector is telling us. If the connector reads 'disconnected'
  # or the mode list is empty/absurd, that is the EDID problem the plan warns
  # about, and the fix is a video= kernel argument.
  section "DRM connectors and modes (the projector's EDID)"
  for conn in /sys/class/drm/card*-*/; do
    [ -e "$conn/status" ] || continue
    printf '\n%s: %s (enabled: %s)\n' \
      "$(basename "$conn")" "$(cat "$conn/status")" "$(cat "$conn/enabled" 2>/dev/null)"
    printf '  modes:\n'
    sed 's/^/    /' "$conn/modes" 2>/dev/null | head -20 || echo '    (none)'
  done

  # connector-id and plane-id for kmssink. Defaults usually work when HDMI is
  # the only output, but the numbers are worth having when they do not.
  section "modetest: connectors and planes"
  run modetest -c
  run modetest -p

  section "VA-API"
  run vainfo --display drm --device /dev/dri/renderD128
  run ls -l /dev/dri/

  section "GStreamer elements of interest"
  for el in avdec_h264 vah264dec vaapih264dec vapostproc kmssink waylandsink glimagesink fbdevsink; do
    if gst-inspect-1.0 "$el" >/dev/null 2>&1; then
      printf '  %-16s present\n' "$el"
    else
      printf '  %-16s MISSING\n' "$el"
    fi
  done

  section "Display server sanity (there should be none)"
  pgrep -a Xorg || echo '  no Xorg: good'
  pgrep -a wayland || echo '  no stray wayland compositor: good'
  run systemctl get-default

  # UxPlay runs with -a and the movie player runs with --ao=null, so nothing
  # on this box has ever produced a sample. This section is what the decision
  # to change that should be made from.
  #
  # On Cherry Trail, HDMI audio is NOT an HDA codec at hw:0,3 the way it is on
  # desktop Intel -- it comes out of the i915-created hdmi-lpe-audio platform
  # device, on a card whose number is not stable across kernels. The signal
  # worth having is eld_valid: it is the only thing here that distinguishes a
  # live HDMI sink from a PCM that merely exists.
  section "Audio (HDMI via the Cherry Trail LPE path)"
  run aplay -l
  run cat /proc/asound/cards
  printf '\n--- ELD (is a sink actually attached?)\n'
  found_eld=0
  for eld in /proc/asound/card*/eld*; do
    [ -e "$eld" ] || continue
    found_eld=1
    printf '  %s\n' "$eld"
    grep -E 'eld_valid|monitor_present|monitor_name|sad0' "$eld" | sed 's/^/    /'
  done
  [ "$found_eld" = 1 ] || echo '  (no ELD nodes -- LPE audio exposes none on some kernels)'
  printf '\n--- sound modules\n'
  lsmod | grep -E 'snd_hdmi_lpe|snd_hda|snd_soc' || echo '  (none bound)'

  section "Movie player"
  run mpv --version
  printf '\n--- video outputs\n'
  mpv --vo=help 2>&1 | grep -E '^\s+(drm|gpu|gpu-next|image)\b' || echo '  (none of interest)'
  printf '\n--- gpu contexts\n'
  mpv --gpu-context=help 2>&1 | sed 's/^/  /' | head -20
  # A large slice of a modern library is H.265. If this profile is absent,
  # software HEVC on 4x1.44 GHz Airmont is not going to save it, and the
  # honest answer is "H.264 only" rather than a slideshow.
  printf '\n--- decodable profiles\n'
  vainfo --display drm --device /dev/dri/renderD128 2>/dev/null \
    | grep -E 'H264|HEVC|VP9|VC1' | sed 's/^/  /' || echo '  (vainfo failed)'

  section "NAS share"
  NAS_MOUNT="${PROBE_NAS_MOUNT:-/srv/movies}"
  run findmnt -o TARGET,SOURCE,FSTYPE,OPTIONS "$NAS_MOUNT"
  if [ -d "$NAS_MOUNT" ]; then
    # Walking the share is what the player does every scan interval, and over
    # SDIO-attached Wi-Fi the per-file stat is the whole cost. Measure it here
    # rather than guess at a scan interval.
    printf '\n--- how slow is a library scan, really\n'
    scan_start=$SECONDS
    scan_files=$(find "$NAS_MOUNT" -maxdepth 2 -type f 2>/dev/null | wc -l)
    printf '  %s files under %s in %d s\n' "$scan_files" "$NAS_MOUNT" "$((SECONDS - scan_start))"
  fi

  # The TS10's Wi-Fi is SDIO-attached. 1080p mirroring wants ~10-25 Mbps
  # sustained with low jitter; if this comes back thin, a USB Ethernet
  # adapter is the fix and it is better to know now than mid-demo.
  section "Network"
  run ip -br addr
  run ip -br link
  command -v iw >/dev/null && run iw dev
  if [ -n "$IPERF_SERVER" ]; then
    printf '\n--- iperf3 -c %s (10s)\n' "$IPERF_SERVER"
    iperf3 -c "$IPERF_SERVER" -t 10 2>&1 || echo '(iperf3 failed -- is `iperf3 -s` running on the control machine?)'
  else
    printf '\n(iperf3 skipped: no server address given)\n'
  fi

  printf '\n=== end ===\n'
} | tee "$REPORT"

printf '\nReport written to %s\n' "$REPORT" >&2
