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

  section "Audio (out of scope, recorded for completeness)"
  run aplay -l

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
