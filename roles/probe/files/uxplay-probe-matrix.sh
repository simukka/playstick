#!/usr/bin/env bash
# Measured comparison of decoder x sink x resolution on this SoC.
#
# The question this answers: which combination actually sustains realtime
# 1080p30, and what does each one cost in CPU. Everything is run with
# sync=true against a clip at native rate, so a combination that cannot keep
# up shows as dropped frames rather than as a slow batch job -- which is the
# same failure mode AirPlay mirroring would have.
#
# Usage: uxplay-probe-matrix.sh [seconds]   (default: whole clip)
set -uo pipefail

OUT_DIR="${PROBE_RESULTS_DIR:-/var/log/uxplay-probe}"
CLIP_DIR="${PROBE_CLIP_DIR:-/usr/local/share/uxplay-probe}"
RUN_SECONDS="${1:-0}"          # 0 = play the clip to the end
mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
CSV="$OUT_DIR/matrix-$STAMP.csv"
LOG_DIR="$OUT_DIR/matrix-$STAMP-logs"
mkdir -p "$LOG_DIR"

export LIBVA_DRIVER_NAME="${LIBVA_DRIVER_NAME:-i965}"
export GST_VA_ALL_DRIVERS=1
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/uxplay-probe}"
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"

DECODERS=(avdec_h264 vah264dec vaapih264dec)
SINKS=(fakesink kmssink waylandsink)
# Glue tried in order between decoder and sink. Empty first: vah264dec should
# hand DMABuf straight to kmssink with no copy. If negotiation refuses, the
# fallbacks cost a download and the CPU column will show it.
GLUES=("" "vapostproc ! video/x-raw !" "videoconvert !")

# --- guard rails --------------------------------------------------------
for svc in uxplay-kms uxplay-cage; do
  if systemctl is-active --quiet "$svc.service"; then
    echo "ERROR: $svc.service is running and holds the display. Stop it first:" >&2
    echo "  systemctl stop $svc.service" >&2
    exit 1
  fi
done

if [ "$(id -u)" -ne 0 ]; then
  echo "WARNING: not running as root; kmssink may fail to become DRM master." >&2
fi

shopt -s nullglob
CLIPS=("$CLIP_DIR"/*.h264)
if [ ${#CLIPS[@]} -eq 0 ]; then
  echo "ERROR: no test clips in $CLIP_DIR." >&2
  echo "Generate them on the control machine with scripts/make-testclip.sh" >&2
  echo "and re-run the probe role." >&2
  exit 1
fi

# --- helpers ------------------------------------------------------------
cpu_busy_total() {
  # Returns "busy total" jiffies from the aggregate cpu line.
  awk '/^cpu /{idle=$5+$6; total=0; for(i=2;i<=NF;i++) total+=$i; print total-idle, total}' /proc/stat
}

# Does this element exist at all? Saves a pile of pointless failures.
have_element() { gst-inspect-1.0 "$1" >/dev/null 2>&1; }

echo "run,clip,decoder,sink,glue,rendered,dropped,elapsed_s,fps,drop_pct,cpu_pct,status" > "$CSV"

run_id=0
for clip in "${CLIPS[@]}"; do
  clip_name="$(basename "$clip" .h264)"
  for dec in "${DECODERS[@]}"; do
    if ! have_element "$dec"; then
      echo "skip: $dec not registered"
      echo "$((++run_id)),$clip_name,$dec,-,-,0,0,0,0,0,0,element-missing" >> "$CSV"
      continue
    fi
    for sink in "${SINKS[@]}"; do
      if ! have_element "$sink"; then
        echo "skip: $sink not registered"
        echo "$((++run_id)),$clip_name,$dec,$sink,-,0,0,0,0,0,0,element-missing" >> "$CSV"
        continue
      fi

      status="negotiation-failed"
      for glue in "${GLUES[@]}"; do
        run_id=$((run_id + 1))
        log="$LOG_DIR/${clip_name}_${dec}_${sink}_${run_id}.log"

        pipeline="filesrc location=$clip ! h264parse ! $dec ! $glue \
fpsdisplaysink video-sink=$sink text-overlay=false sync=true"

        # waylandsink needs a compositor; cage supplies one and exits with
        # the child. Everything else talks to DRM (or nothing) directly.
        if [ "$sink" = "waylandsink" ]; then
          cmd=(cage -- sh -c "exec gst-launch-1.0 -v $pipeline")
        else
          cmd=(sh -c "exec gst-launch-1.0 -v $pipeline")
        fi

        printf '\n### %s | %s | %s | glue=%s\n' "$clip_name" "$dec" "$sink" "${glue:-none}"

        read -r busy0 total0 <<<"$(cpu_busy_total)"
        start=$(date +%s.%N)
        if [ "$RUN_SECONDS" -gt 0 ]; then
          timeout --signal=INT "$RUN_SECONDS" "${cmd[@]}" >"$log" 2>&1
        else
          timeout --signal=INT 180 "${cmd[@]}" >"$log" 2>&1
        fi
        rc=$?
        end=$(date +%s.%N)
        read -r busy1 total1 <<<"$(cpu_busy_total)"

        elapsed=$(awk -v a="$start" -v b="$end" 'BEGIN{printf "%.2f", b-a}')
        cpu_pct=$(awk -v b0="$busy0" -v t0="$total0" -v b1="$busy1" -v t1="$total1" \
          'BEGIN{d=t1-t0; if(d<=0){print "0.0"} else {printf "%.1f", 100*(b1-b0)/d}}')

        # fpsdisplaysink's last-message wording has drifted between releases,
        # so take the counters and compute the rate ourselves.
        rendered=$(grep -o 'rendered:[ ]*[0-9]*' "$log" | tail -1 | grep -o '[0-9]*$')
        dropped=$(grep -o 'dropped:[ ]*[0-9]*' "$log" | tail -1 | grep -o '[0-9]*$')
        rendered="${rendered:-0}"
        dropped="${dropped:-0}"

        if [ "$rendered" -gt 0 ]; then
          fps=$(awk -v r="$rendered" -v e="$elapsed" 'BEGIN{if(e>0) printf "%.2f", r/e; else print "0"}')
          drop_pct=$(awk -v r="$rendered" -v d="$dropped" \
            'BEGIN{t=r+d; if(t>0) printf "%.2f", 100*d/t; else print "0"}')
          # 124 is timeout's signal; expected when RUN_SECONDS caps the run.
          if [ $rc -eq 0 ] || [ $rc -eq 124 ]; then status="ok"; else status="ok-with-errors(rc=$rc)"; fi
        else
          fps=0; drop_pct=0
          status="failed(rc=$rc)"
        fi

        echo "$run_id,$clip_name,$dec,$sink,${glue:-none},$rendered,$dropped,$elapsed,$fps,$drop_pct,$cpu_pct,$status" >> "$CSV"
        printf '  rendered=%s dropped=%s fps=%s cpu=%s%% -> %s\n' \
          "$rendered" "$dropped" "$fps" "$cpu_pct" "$status"

        # First glue that works wins; no need to try the copies.
        [ "$status" = "ok" ] && break
      done
    done
  done
done

printf '\nResults: %s\n' "$CSV"
printf 'Logs:    %s\n' "$LOG_DIR"
printf '\n'
column -s, -t "$CSV" 2>/dev/null || cat "$CSV"
