#!/usr/bin/env python3
"""Idle screen for the UxPlay appliance: a clock and the advertised AirPlay
name, drawn straight into the framebuffer.

WHY IT WRITES PIXELS INSTEAD OF TEXT

The obvious implementation is a console program printing to /dev/tty1. It was
tried and it does not work here. UxPlay's kmssink opens the DRM device during
`gst_kms_sink_start` -- at service startup, before any client connects -- and
whoever opens the device first becomes DRM master. The fbdev helper calls
drm_master_internal_acquire() before pushing console updates to the screen,
and that fails while another master exists. The result is a clock frozen at
whatever minute uxplay-kms happened to start, which is worse than no clock.

Writing to /dev/fb0 does not go through that path. i915's fbdev emulation maps
the real scanout buffer with no shadow, so bytes written here land in the
framebuffer regardless of who holds master. Verified on the device: with
uxplay-kms running, `head -c 200000 /dev/zero | tr '\\0' '\\377' > /dev/fb0`
paints a white bar on the projector.

While a client is mirroring, the CRTC scans out UxPlay's buffer and these
writes are simply not visible; when the session ends the CRTC returns to the
fbdev buffer and the clock is there, current. Nothing has to be coordinated.

A second benefit: glyphs come from a PSF console font parsed here, so the font
is no longer subject to fbcon's blit-capability validation. The 32x16 face the
kernel refuses to load with setfont renders fine.

Configuration is entirely environmental -- see uxplay-idle.service.
"""

import gzip
import os
import signal
import struct
import subprocess
import sys
import time

FB = os.environ.get("IDLE_FB", "/dev/fb0")
TTY = os.environ.get("IDLE_TTY", "/dev/tty1")
FONTS = os.environ.get("IDLE_FONTS", "").split()
SUBTITLE = os.environ.get("IDLE_SUBTITLE", "")
TIME_FORMAT = os.environ.get("IDLE_TIME_FORMAT", "%H:%M")
BLANK_MINUTES = int(os.environ.get("IDLE_BLANK_MINUTES", "30") or 0)
POLL_SECONDS = max(1, int(os.environ.get("IDLE_POLL_SECONDS", "5") or 5))
PORT = os.environ.get("IDLE_PORT", "")
CLOCK_SCALE = int(os.environ.get("IDLE_CLOCK_SCALE", "0") or 0)

CLOCK_LEVEL = 0xFF          # white
SUBTITLE_LEVEL = 0xA0       # dimmer, so the time reads first


# --- PSF fonts ------------------------------------------------------------

def load_psf(path):
    """Parse a PSF1 or PSF2 console font. Returns a dict or raises."""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as fh:
        data = fh.read()

    if data[:2] == b"\x36\x04":                      # PSF1
        mode, charsize = data[2], data[3]
        count = 512 if mode & 0x01 else 256
        width, height, hdr = 8, charsize, 4
        row_bytes = 1
    elif data[:4] == b"\x72\xb5\x4a\x86":            # PSF2
        _ver, hdr, _flags, count, charsize, height, width = struct.unpack(
            "<7I", data[4:32])
        row_bytes = (width + 7) // 8
    else:
        raise ValueError("not a PSF font: %s" % path)

    return {
        "width": width, "height": height, "count": count,
        "charsize": charsize, "row_bytes": row_bytes,
        "data": data[hdr:hdr + count * charsize],
    }


def load_first_font(paths):
    for p in paths:
        try:
            return load_psf(p), p
        except Exception as exc:                     # noqa: BLE001 - any bad font is just skipped
            print("idle-clock: skipping %s (%s)" % (p, exc), file=sys.stderr)
    raise SystemExit("idle-clock: no usable font in IDLE_FONTS")


def glyph_rows(font, ch):
    """Row bitmaps for one character, MSB = leftmost pixel."""
    idx = ord(ch)
    if idx >= font["count"]:
        idx = ord("?")
    base = idx * font["charsize"]
    rb, w = font["row_bytes"], font["width"]
    shift = rb * 8 - w
    rows = []
    for y in range(font["height"]):
        off = base + y * rb
        rows.append(int.from_bytes(font["data"][off:off + rb], "big") >> shift)
    return rows


# --- framebuffer ----------------------------------------------------------

def fb_geometry(dev):
    base = "/sys/class/graphics/%s" % os.path.basename(dev)
    with open(base + "/virtual_size") as fh:
        xres, yres = (int(v) for v in fh.read().strip().split(","))
    with open(base + "/bits_per_pixel") as fh:
        bpp = int(fh.read().strip())
    try:
        with open(base + "/stride") as fh:
            stride = int(fh.read().strip())
    except OSError:
        stride = xres * bpp // 8
    return xres, yres, bpp, stride


class Screen:
    def __init__(self, dev):
        self.xres, self.yres, self.bpp, self.stride = fb_geometry(dev)
        if self.bpp != 32:
            raise SystemExit(
                "idle-clock: %d bpp framebuffer unsupported (expected 32)" % self.bpp)
        self.bypp = self.bpp // 8
        self.fh = open(dev, "r+b", buffering=0)

    def blank_frame(self):
        return bytearray(self.stride * self.yres)

    def text_width(self, font, text, scale):
        return len(text) * font["width"] * scale

    def draw_text(self, frame, font, text, x0, y0, scale, level):
        """Blit `text` at (x0, y0), integer-scaled. Background stays black."""
        w, h, bypp = font["width"], font["height"], self.bypp
        pixel = bytes([level]) * bypp          # greyscale: channel order is moot
        run = pixel * scale
        blank_run = b"\x00" * (scale * bypp)
        for i, ch in enumerate(text):
            gx = x0 + i * w * scale
            if gx < 0 or gx + w * scale > self.xres:
                continue
            for y, bits in enumerate(glyph_rows(font, ch)):
                if not bits:
                    continue
                rowbuf = bytearray()
                for x in range(w):
                    rowbuf += run if (bits >> (w - 1 - x)) & 1 else blank_run
                start = y0 + y * scale
                for r in range(scale):
                    yy = start + r
                    if 0 <= yy < self.yres:
                        off = yy * self.stride + gx * bypp
                        frame[off:off + len(rowbuf)] = rowbuf

    def show(self, frame):
        self.fh.seek(0)
        self.fh.write(frame)


# --- layout ---------------------------------------------------------------

def pick_scale(screen, font, text, subtitle):
    """Largest integer scale that leaves room for the subtitle underneath."""
    if CLOCK_SCALE > 0:
        return CLOCK_SCALE
    by_width = int(screen.xres * 0.90) // (len(text) * font["width"])
    by_height = int(screen.yres * 0.55) // font["height"]
    return max(1, min(by_width, by_height))


def compose(screen, font, text, subtitle):
    frame = screen.blank_frame()
    scale = pick_scale(screen, font, text, subtitle)
    clock_w = screen.text_width(font, text, scale)
    clock_h = font["height"] * scale

    sub_scale = 0
    sub_w = sub_h = 0
    if subtitle:
        sub_scale = max(1, scale // 4)
        # Shrink rather than run off the edge if the name is long.
        while sub_scale > 1 and screen.text_width(font, subtitle, sub_scale) > screen.xres * 0.94:
            sub_scale -= 1
        sub_w = screen.text_width(font, subtitle, sub_scale)
        sub_h = font["height"] * sub_scale

    gap = clock_h // 6 if subtitle else 0
    total_h = clock_h + gap + sub_h
    top = max(0, (screen.yres - total_h) // 2)

    screen.draw_text(frame, font, text, (screen.xres - clock_w) // 2, top,
                     scale, CLOCK_LEVEL)
    if subtitle:
        screen.draw_text(frame, font, subtitle, (screen.xres - sub_w) // 2,
                         top + clock_h + gap, sub_scale, SUBTITLE_LEVEL)
    return frame


# --- session detection ----------------------------------------------------

def session_active():
    """True while a client holds a TCP connection to UxPlay.

    Deliberately NOT 'is the DRM node open': uxplay-kms holds that from
    startup to shutdown, so it can never distinguish idle from mirroring. This
    only drives the blank countdown -- drawing is unconditional, because
    writes made during a session are invisible rather than harmful.
    """
    if not PORT:
        return False
    try:
        out = subprocess.run(
            ["ss", "-H", "-tn", "state", "established", "sport = :%s" % PORT],
            capture_output=True, text=True, timeout=5).stdout
        return bool(out.strip())
    except Exception:                                # noqa: BLE001
        return False


# --- main -----------------------------------------------------------------

def write_tty(seq):
    try:
        with open(TTY, "w") as fh:
            fh.write(seq)
    except OSError:
        pass


def main():
    font, font_path = load_first_font(FONTS)
    screen = Screen(FB)
    print("idle-clock: %dx%d %dbpp stride=%d font=%s (%dx%d)" % (
        screen.xres, screen.yres, screen.bpp, screen.stride,
        os.path.basename(font_path), font["width"], font["height"]),
        file=sys.stderr)

    # Clear the console text too. fbcon is blocked while UxPlay is master, but
    # before that it is not, and a leftover boot log would be repainted over
    # these pixels on the next kernel message.
    write_tty("\033[H\033[2J\033[?25l")

    state = {"stop": False}

    def stop(_sig, _frm):
        state["stop"] = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    blank_seconds = BLANK_MINUTES * 60
    last_active = time.time()
    shown = None
    blanked = False

    while not state["stop"]:
        now = time.time()
        stamp = time.strftime(TIME_FORMAT)

        if session_active():
            last_active = now
            shown = None            # force a redraw when the session ends
            blanked = False
        elif blank_seconds and now - last_active >= blank_seconds:
            if not blanked:
                screen.show(screen.blank_frame())
                blanked = True
                shown = None
        elif stamp != shown:
            screen.show(compose(screen, font, stamp, SUBTITLE))
            shown = stamp

        # Sleep in short slices so SIGTERM is handled promptly.
        deadline = now + POLL_SECONDS
        while not state["stop"] and time.time() < deadline:
            time.sleep(0.25)

    screen.show(screen.blank_frame())
    write_tty("\033[H\033[2J\033[?25h")


if __name__ == "__main__":
    main()
