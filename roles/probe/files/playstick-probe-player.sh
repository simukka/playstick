#!/usr/bin/env bash
# Measured comparison of mpv video output x hardware decode on this SoC.
#
# The question this answers is the one group_vars/all.yml refuses to guess at:
# does mpv's GBM/EGL path beat software decode into a dumb buffer here?
#
# The repo already knows software avdec_h264 beats VA-API through GStreamer,
# and it knows why -- no memory:DMABuf ever appeared in the negotiated caps, so
# every frame was read back from GPU memory over an uncached mapping, at a
# resolution-independent 20-32 ms against a 33.3 ms budget. That is a property
# of one pipeline, not of the silicon. mpv's --vo=gpu --gpu-context=drm
# --hwdec=vaapi path exports the VA surface as a dmabuf and samples it on the
# GPU, so the readback is structurally absent -- and the vaapi-copy row below
# is the control that proves the harness can still see it when it is present.
#
# Judged the same way as uxplay-probe-matrix.sh: a run is "ok" only if it drops
# under 5% AND sustains 95% of the clip's nominal rate. Drop rate alone is
# blind to frames lost upstream of the output.
#
# Usage: playstick-probe-player.sh [seconds]   (default: 60s per run)
set -uo pipefail

OUT_DIR="${PROBE_RESULTS_DIR:-/var/log/uxplay-probe}"
CLIP_DIR="${PROBE_CLIP_DIR:-/usr/local/share/uxplay-probe}"
RUN_SECONDS="${1:-60}"
PROBE_VT="${PROBE_VT:-1}"
NOMINAL_FPS="${NOMINAL_FPS:-30}"
DRM_CONNECTOR="${DRM_CONNECTOR:-HDMI-A-1}"
DRM_MODE="${DRM_MODE:-}"
DRM_DEVICE="${DRM_DEVICE:-}"
mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
CSV="$OUT_DIR/player-$STAMP.csv"
LOG_DIR="$OUT_DIR/player-$STAMP-logs"
mkdir -p "$LOG_DIR"

export LIBVA_DRIVER_NAME="${LIBVA_DRIVER_NAME:-i965}"

# label|vo|gpu-context|hwdec
#   drm/no          the shipping default: software decode into a dumb buffer,
#                   the closest structural analogue of the pipeline that ships
#                   for UxPlay at 29.19 fps / 0.00% drop
#   drm/vaapi-copy  the CONTROL. Hardware decode *with* the readback. If this
#                   does not reproduce the slow rows from the GStreamer matrix,
#                   the harness is measuring something other than what it thinks
#   gpu/no          does GL/GBM/EGL work here at all, independently of VA-API
#   gpu/vaapi       the hypothesis: dmabuf interop, nothing read back
RUN_SPECS=(
  "drm-sw|drm||no"
  "drm-vaapi-copy|drm||vaapi-copy"
  "gpu-sw|gpu|drm|no"
  "gpu-vaapi|gpu|drm|vaapi"
)
if [ -n "${RUNS:-}" ]; then
  IFS=, read -r -a want <<<"$RUNS"
  filtered=()
  for spec in "${RUN_SPECS[@]}"; do
    for w in "${want[@]}"; do
      [ "${spec%%|*}" = "$w" ] && filtered+=("$spec")
    done
  done
  RUN_SPECS=("${filtered[@]}")
fi

# --- guard rails --------------------------------------------------------
# Same rule as the matrix: exactly one thing may hold DRM master, and if
# something already does, every row below measures the harness.
for svc in uxplay-kms uxplay-cage playstick-web; do
  if systemctl is-active --quiet "$svc.service" 2>/dev/null; then
    echo "ERROR: $svc.service is running and can take the display. Stop it first:" >&2
    echo "  systemctl stop $svc.service" >&2
    exit 1
  fi
done

if [ "$(id -u)" -ne 0 ]; then
  echo "WARNING: not running as root; mpv may fail to become DRM master." >&2
fi

command -v mpv >/dev/null 2>&1 || { echo "ERROR: mpv is not installed." >&2; exit 1; }

shopt -s nullglob
CLIPS=("$CLIP_DIR"/*.mp4 "$CLIP_DIR"/*.mkv)
if [ ${#CLIPS[@]} -eq 0 ]; then
  echo "ERROR: no muxed test clips in $CLIP_DIR." >&2
  echo "The .h264 elementary streams the matrix uses cannot exercise a" >&2
  echo "container, so this probe needs .mp4/.mkv. Point PROBE_CLIP_DIR at a" >&2
  echo "folder on the NAS share to measure real films instead." >&2
  exit 1
fi

cpu_busy_total() {
  awk '/^cpu /{idle=$5+$6; total=0; for(i=2;i<=NF;i++) total+=$i; print total-idle, total}' /proc/stat
}

echo "run,clip,vo,gpu_context,hwdec,rendered,dropped,elapsed_s,fps,drop_pct,cpu_pct,hwdec_used,rc,status" > "$CSV"

run_id=0
for clip in "${CLIPS[@]}"; do
  clip_name="$(basename "$clip")"
  clip_name="${clip_name%.*}"
  for spec in "${RUN_SPECS[@]}"; do
    IFS='|' read -r label vo ctx hwdec <<<"$spec"
    run_id=$((run_id + 1))
    log="$LOG_DIR/${clip_name}_${label}_${run_id}.log"

    args=(--no-config --vo="$vo" --hwdec="$hwdec" --ao=null
          --drm-connector="$DRM_CONNECTOR"
          --profile=fast --video-sync=audio --framedrop=vo --interpolation=no
          --osd-level=0 --no-osc --input-terminal=no --idle=no --keep-open=no
          # -v on the video decoder is how the fallback is caught: mpv silently
          # drops from vaapi to vaapi-copy when dmabuf import fails, which is
          # exactly the readback this probe exists to distinguish.
          --msg-level=all=info,vd=v,vo=v
          --term-status-msg='STATS fps=${estimated-vf-fps} drop=${frame-drop-count} decdrop=${decoder-frame-drop-count} pos=${=time-pos}')
    [ -n "$ctx" ] && args+=(--gpu-context="$ctx")
    [ -n "$DRM_MODE" ] && args+=(--drm-mode="$DRM_MODE")
    [ -n "$DRM_DEVICE" ] && args+=(--drm-device="$DRM_DEVICE")

    inner="mpv $(printf '%q ' "${args[@]}") $(printf '%q' "$clip")"

    # On a real VT, for the reason the matrix learned the hard way: over a bare
    # SSH session there is no console at all, so DRM master cannot be acquired
    # and every sink row measures the harness instead of the design.
    if [ -n "$PROBE_VT" ] && [ "$PROBE_VT" != "0" ] && command -v openvt >/dev/null 2>&1; then
      cmd=(openvt -c "$PROBE_VT" -f -s -w -- sh -c "$inner >$log 2>&1")
    else
      cmd=(sh -c "$inner >$log 2>&1")
    fi

    printf '\n### %s | vo=%s%s | hwdec=%s\n' "$clip_name" "$vo" \
      "${ctx:+ ctx=$ctx}" "$hwdec"

    read -r busy0 total0 <<<"$(cpu_busy_total)"
    start=$(date +%s.%N)
    timeout --signal=INT "$RUN_SECONDS" "${cmd[@]}"
    rc=$?
    end=$(date +%s.%N)
    read -r busy1 total1 <<<"$(cpu_busy_total)"

    elapsed=$(awk -v a="$start" -v b="$end" 'BEGIN{printf "%.2f", b-a}')
    cpu_pct=$(awk -v b0="$busy0" -v t0="$total0" -v b1="$busy1" -v t1="$total1" \
      'BEGIN{d=t1-t0; if(d<=0){print "0.0"} else {printf "%.1f", 100*(b1-b0)/d}}')

    # Last status line wins. Frames rendered is derived from the sustained fps
    # and the position mpv actually reached, because mpv reports a rate rather
    # than a count.
    stats=$(grep -o 'STATS .*' "$log" | tail -1)
    fps=$(sed -n 's/.*fps=\([0-9.]*\).*/\1/p' <<<"$stats")
    dropped=$(sed -n 's/.*drop=\([0-9]*\).*/\1/p' <<<"$stats")
    pos=$(sed -n 's/.*pos=\([0-9.]*\).*/\1/p' <<<"$stats")
    fps="${fps:-0}"; dropped="${dropped:-0}"; pos="${pos:-0}"
    rendered=$(awk -v f="$fps" -v p="$pos" 'BEGIN{printf "%d", f*p}')
    drop_pct=$(awk -v r="$rendered" -v d="$dropped" \
      'BEGIN{t=r+d; if(t>0) printf "%.2f", 100*d/t; else print "0"}')

    # Which hwdec mpv actually settled on, not which one it was asked for.
    hwdec_used=$(grep -oE 'Using hardware decoding \([a-z0-9-]+\)' "$log" | tail -1 \
                 | sed 's/.*(\(.*\))/\1/')
    hwdec_used="${hwdec_used:-none}"

    if awk "BEGIN{exit !($fps > 0)}"; then
      if awk "BEGIN{exit !($drop_pct < 5 && $fps >= 0.95 * $NOMINAL_FPS)}"; then
        status="ok"
      else
        status="degraded"
      fi
    else
      status="failed"
    fi

    echo "$run_id,$clip_name,$vo,${ctx:-none},$hwdec,$rendered,$dropped,$elapsed,$fps,$drop_pct,$cpu_pct,$hwdec_used,$rc,$status" >> "$CSV"
    printf '  fps=%s dropped=%s cpu=%s%% hwdec=%s -> %s\n' \
      "$fps" "$dropped" "$cpu_pct" "$hwdec_used" "$status"
  done
done

printf '\nResults: %s\n' "$CSV"
printf 'Logs:    %s\n' "$LOG_DIR"
printf '\n'
column -s, -t "$CSV" 2>/dev/null || cat "$CSV"
printf '\nSet player_vo and player_hwdec in group_vars/all.yml from the winning row.\n'
printf 'Watch the hwdec_used column: a "vaapi" run that reports vaapi-copy fell\n'
printf 'back to the readback path, and its numbers are not measuring the hypothesis.\n'
