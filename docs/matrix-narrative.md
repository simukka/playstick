# What a Benchmark Measures When You Aren't Looking

*Notes from putting an AirPlay receiver on a 2016 HDMI stick, in which the hardware
surprises me once and my own test harness fools me twice.*

---

## The setup

I had an ASUS VivoStick TS10 — one of those HDMI-stick PCs from around 2016 — and a
projector. The idea was simple: make the stick an AirPlay mirroring receiver using
[UxPlay](https://github.com/FDH2/UxPlay), rendering straight to the Intel integrated
graphics with no X server anywhere. Provision it over SSH with Ansible. Plug it into the
projector's HDMI port and forget it exists.

The hardware is thin:

| | |
|---|---|
| SoC | Intel Atom x5-Z8350, 4 × 1.44 GHz Airmont (Cherry Trail) |
| GPU | Gen8 LP, 12 EUs |
| RAM | 2 GB nominal, **1537 MB** usable after stolen graphics memory |
| Storage | 32 GB eMMC |
| OS | Ubuntu Server 26.04, kernel 7.0.0-14 |

That RAM figure matters. So does the CPU: four Airmont cores at 1.44 GHz will not decode
1080p30 H.264 in software. Hardware decode wasn't an optimisation for this project, it was
the load-bearing assumption. If VA-API didn't work, there was no project.

So the first thing I wrote wasn't a config — it was a gate. The Ansible role that installs
the graphics stack ends with an assertion that `vainfo` reports
`VAProfileH264High : VAEntrypointVLD`, and **fails the entire run** if it doesn't. I did not
want a box that limped along in software and looked like it worked until someone tried to
mirror a video.

That gate passed. `Intel i965 driver for Intel(R) CherryView - 2.4.1`, VA-API 1.23,
hardware H.264 decode confirmed. Good.

Now the actual question: which pipeline should UxPlay use?

---

## Designing the sweep

UxPlay lets you pick the H.264 decoder (`-vd`) and the video sink (`-vs`) independently.
That's a small matrix, and I had no business guessing at it, so I wrote a script to measure:

- **Decoders:** `avdec_h264` (software, the baseline), `vah264dec` (the modern `va`
  plugin), `vaapih264dec` (the older gstreamer-vaapi element)
- **Sinks:** `fakesink` (decode-only ceiling), `kmssink` (direct DRM/KMS, no compositor),
  `waylandsink` (under `cage`, a minimal kiosk compositor)
- **Clips:** 1080p30 and 720p30, H.264 High with CABAC, encoded to resemble what AirPlay
  actually sends, with noise layered in so the stream wouldn't compress into something
  unrealistically cheap to decode

Three design decisions I'd defend:

**Everything runs with `sync=true`.** The clip plays at its native rate. A combination that
can't keep up shows up as *dropped frames*, not as a slow batch job. That's the same
failure mode a real mirroring session would have. A benchmark that lets the pipeline run
as fast as it likes answers a question nobody asked.

**`fakesink` gives a ceiling.** Decode with the display thrown away isolates decoder cost
from sink cost. If `fakesink` can't hold 30 fps, nothing downstream will save you.

**The "glue" column.** `vah264dec` outputs VA surfaces; `kmssink` wants DMABuf or system
memory. The zero-copy path is VA → DMABuf → kmssink with nothing in between. If caps
negotiation refuses, GStreamer needs a `vapostproc` or `videoconvert` in the middle and you
pay for a full-frame copy. So the script tries empty glue first, and falls back. Whatever
ends up in that column tells you what the pipeline actually had to do.

I encoded the clips on my laptop, not the stick. x264 on a 1.44 GHz Airmont would have
taken longer than the measurement it fed.

---

## The decoder result, which I got backwards

Here are the `fakesink` rows — the decode ceiling. All three decoders sustain 30 fps, so
the only thing that separates them is what they cost:

| clip | decoder | fps | CPU |
|---|---|---|---|
| 1080p30 | `vaapih264dec` | 29.56 | **19.6%** |
| 1080p30 | `avdec_h264` (software) | 29.41 | 46.3% |
| 1080p30 | `vah264dec` | 29.57 | **91.7%** |
| 720p30 | `vaapih264dec` | 29.60 | **11.9%** |
| 720p30 | `avdec_h264` | 29.67 | 65.0% |
| 720p30 | `vah264dec` | 29.66 | 58.7% |

`vaapih264dec` wins by roughly 4.7×.

And `vah264dec` — at 91.7% CPU for 1080p — is **worse than decoding in software.**

This is not what I expected, and it's not what the documentation says. UxPlay's README
recommends `vah264dec` and describes `vaapih264dec` as the deprecated legacy element. That
advice is almost certainly right on modern Intel graphics with the `iHD` driver. On Gen8 LP
with the old `i965` driver, it is exactly backwards. GstVA is evidently doing something
pathological against this driver — most likely shuffling surfaces through memory it
shouldn't need to touch.

I had set `vah264dec` as the default in my playbook. I'd taken it from the docs. The
measurement is the only reason I found out.

**The lesson is narrow and worth stating precisely:** vendor documentation describes the
hardware the vendor cares about. This SoC was released in 2016 and is deprecated by
everyone involved. "Deprecated" describes the maintainers' intentions, not your silicon's
behaviour.

---

## The sink results, which looked like a disaster

Then I looked at the rows where the frames actually reached a screen.

```
run  clip          decoder       sink      glue           rendered dropped  fps   drop%  cpu%   status
3    clip-1080p30  avdec_h264    kmssink   vapostproc !   170      187      2.76  52.38  53.7   ok
9    clip-1080p30  vah264dec     kmssink   vapostproc !   185      183      3.00  49.73  25.5   ok
16   clip-1080p30  vaapih264dec  kmssink   videoconvert ! 349      326      5.70  48.30  16.5   ok
28   clip-720p30   vah264dec     kmssink   vapostproc !   362      360      5.92  49.86  14.4   ok
34   clip-720p30   vaapih264dec  kmssink   vapostproc !   307      305      4.99  49.84  12.5   ok
```

Two to six frames per second, with about half of all frames dropped. Against a target of
thirty.

Every single `waylandsink` row failed outright — twelve of them, mostly with `rc=134`,
which is SIGABRT. Every attempt at the zero-copy path (empty glue into `kmssink`) failed
too, dying after roughly a second.

Taken at face value, this says the project is dead. The decoder is fine, but nothing can
get the decoded frames onto a display.

I nearly wrote that conclusion down.

---

## The number that didn't fit

Look at the CPU column again.

Row 34: 4.99 fps, 49.84% of frames dropped, **12.5% CPU**.

A machine that is failing to keep up is *busy*. That's what "can't keep up" means. This
machine was doing nothing. Twelve percent of four cores, while dropping half the frames it
was handed.

**Low throughput plus low CPU is not saturation. It's blocking.** Something in that
pipeline was sitting in a wait state — and a benchmark that reports "slow" when the real
answer is "blocked on something" is not measuring performance at all. It's measuring an
obstruction, and obstructions have causes you can go find.

So I went looking, and found three.

**1. I wasn't testing the configuration I was shipping.** The probe ran bare `kmssink`.
The systemd unit runs `kmssink driver-name=i915 force-modesetting=true`. That's not a
cosmetic difference: without `force-modesetting`, kmssink uses an *overlay plane* when the
connector is already active, instead of driving the primary plane. Different code path,
different scaling behaviour, different everything. I had benchmarked a configuration that
existed nowhere in my deployment.

**2. The display was 2560×1440.** I was testing against a monitor whose EDID preferred
1440p, so every 1080p frame needed scaling that the real setup wouldn't necessarily ask
for. I knew the mode list — it was sitting in my own facts report — and I hadn't connected
it to the sink results.

**3. There was no console.** This is the big one. The probe ran over SSH. I ran `fgconsole`
on the device and got:

```
Couldn't get a file descriptor referring to the console.
```

There is no VT. None. And `kmssink` needs to be **DRM master** to drive a display, while
`cage` needs a *seat* to get one. The actual service acquires both by owning tty1 through
systemd's `TTYPath=/dev/tty1` and `StandardInput=tty-force`. My probe had neither, so it
was scraping along whatever degraded path the kernel would still allow.

That single fact explains the whole ugly block at once: the outright failures, the SIGABRTs
from cage, and the blocked-not-busy profile of the rows that limped through.

**My benchmark was measuring my benchmark.** The numbers were real. They were reproducible.
They were about the harness.

---

## Then I broke it again while fixing it

The fix seemed obvious: make the sink strings match what the service runs, and give the
pipelines a real VT with `openvt`.

So I changed the sink list from bare element names to full strings with properties, and
the script built its pipeline like this:

```sh
fpsdisplaysink video-sink=$sink text-overlay=false sync=true
```

With `$sink` now being `kmssink driver-name=i915 force-modesetting=true`, that expands to:

```
fpsdisplaysink video-sink=kmssink driver-name=i915 force-modesetting=true ...
```

Read that as GStreamer's parser would. `video-sink=kmssink`, then `driver-name` and
`force-modesetting` as properties **of fpsdisplaysink** — the very element I was using to
count frames. The properties I had just added specifically to fix the measurement would
have been silently attached to the wrong element, and I'd have gotten another set of
plausible, reproducible, meaningless numbers.

The value has to reach GStreamer's parser *quoted*. Which is fiddlier than it sounds,
because there are two layers in the way — `sh -c` and `gst_parse_launchv()` — and the
second one **rejoins its argv with spaces before parsing**. So shell-level quoting is not
enough; literal quote characters have to survive into the string GStreamer finally sees:

```sh
pipeline="... fpsdisplaysink video-sink=\\\"$sink\\\" text-overlay=false sync=true"
```

I didn't trust that. I wrote a four-line script that echoed what each layer produced and
confirmed the string arriving at gst-launch was:

```
fpsdisplaysink video-sink="kmssink driver-name=i915 force-modesetting=true" text-overlay=false sync=true
```

Ten seconds of verification for a bug that would have wasted the entire next run — and,
worse, produced numbers convincing enough to act on.

---

## What I'd take away

**A benchmark that doesn't reproduce production conditions measures itself.** The gap that
ruined my sink numbers wasn't subtle in hindsight — no VT, wrong sink properties, wrong
display mode. But each one was invisible in the results. The CSV had a `status` column
saying `ok`.

**Learn the shape of your failure modes.** "Slow and busy" and "slow and idle" are
different diagnoses with different causes. Low CPU alongside low throughput should stop you
cold — you're blocked, not saturated, and blocked has a *reason* rather than a *limit*.
That single mismatched number was the only thing standing between me and abandoning a
perfectly viable design.

**Instrument the thing you ship, not a sketch of it.** My probe and my systemd unit had
drifted apart because I wrote them at different times for different purposes. If your test
harness and your deployment build their command lines separately, they will disagree, and
the harness will be the one that's wrong.

**Documentation ages out of alignment with hardware.** `vah264dec` over `vaapih264dec` is
good advice that happens to be inverted on ten-year-old silicon. Measure on the hardware
you have.

**Verify the plumbing when the plumbing is load-bearing.** Shell quoting through three
layers is exactly the kind of thing that fails silently and produces output that looks
fine.

---

## Where it stands

The decoder question is settled: `vaapih264dec`, at 19.6% CPU for 1080p30, with comfortable
headroom on a 2016 Atom.

The sink question is **open**, and I want to be precise about that: it is open, not
answered badly. The first sweep didn't show that `kmssink` and `waylandsink` perform
poorly. It showed that I measured them wrong. The re-run — correct sink properties, on a
real VT, with `skip-vsync` in the mix — is what will actually answer it.

That distinction is the whole point. A bad number and an invalid number look identical in a
spreadsheet, and only one of them is telling you something about your system.

---

*Part of [uxplay-atom](../README.md), a small Ansible repo for provisioning and measuring
an AirPlay receiver on marginal hardware.*
