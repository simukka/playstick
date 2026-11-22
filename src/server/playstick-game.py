#!/usr/bin/python3
"""playstick-game: one game on the projector, and nothing else.

A libretro frontend small enough to read in one sitting. The playstick daemon
starts it on tty1 the way it starts mpv, and it does four things:

  * loads one emulator core (an Ubuntu libretro package) and one cartridge,
  * draws each frame full screen through SDL2's KMSDRM driver -- no X, no
    compositor, the same direct-to-GPU path mpv's --vo=drm takes,
  * reads the pads' button state off the control socket the daemon opened,
  * and writes the game's sound back down that socket, as mono 16-bit PCM at
    the rate it was asked for, for the daemon to pass on to the phones.

There is no menu, no configuration file and no on-screen message. Everything
a person can do -- start, stop, which game -- is done from a phone, through the
daemon. The protocol on the control socket is described in playstick/game.py.

WHY PYTHON

The emulation is in the core, which is C, and the drawing is SDL, which is C.
What is left for this file is a few calls a frame: run the core, upload one
texture of at most 256x240, present it, and resample a frame's worth of sound
(about 800 samples). That is a few percent of one of the stick's four cores,
and it keeps the frontend in the language the rest of the daemon is in.

FRAME PACING

The core is run at its own rate, not the display's. Each pass of the loop
works out how many frames are due by the wall clock and runs that many (zero,
one, or two to catch up), then presents once on vsync. A 60.1 Hz NES on a
60 Hz projector therefore runs at the right speed and drops one repeat in ten
seconds, and on a 50 Hz mode it still runs at the right speed, just less
smoothly. The sound depends on this: it is made at the core's real-time rate,
so the phones' buffers neither starve nor overflow.

SAVES

A cartridge's battery-backed memory, if it has any, is read from
<saves>/<sha1 of the ROM>.srm before the game starts and written back when it
stops. So a Game Boy game remembers where you got to.
"""

import argparse
import ctypes as C
import hashlib
import json
import os
import select
import socket
import struct
import sys
import time

# -- libretro: the subset of libretro.h this frontend uses ------------------

RETRO_API_VERSION = 1
RETRO_DEVICE_JOYPAD = 1
RETRO_DEVICE_ID_JOYPAD_MASK = 256
RETRO_MEMORY_SAVE_RAM = 0

ENV_GET_CAN_DUPE = 3
ENV_SET_MESSAGE = 6
ENV_SHUTDOWN = 7
ENV_SET_PERFORMANCE_LEVEL = 8
ENV_GET_SYSTEM_DIRECTORY = 9
ENV_SET_PIXEL_FORMAT = 10
ENV_SET_INPUT_DESCRIPTORS = 11
ENV_GET_VARIABLE = 15
ENV_SET_VARIABLES = 16
ENV_GET_VARIABLE_UPDATE = 17
ENV_SET_SUPPORT_NO_GAME = 18
ENV_GET_SAVE_DIRECTORY = 31
ENV_SET_SYSTEM_AV_INFO = 32
ENV_SET_CONTROLLER_INFO = 35
ENV_SET_GEOMETRY = 37
ENV_EXPERIMENTAL = 0x10000

PIXEL_0RGB1555, PIXEL_XRGB8888, PIXEL_RGB565 = 0, 1, 2

# The libretro joypad ids, indexed by the bit in the daemon's mask. The order
# of the mask is UP, DOWN, LEFT, RIGHT, A, B, START, SELECT -- see game.py.
JOYPAD_B, JOYPAD_SELECT, JOYPAD_START = 0, 2, 3
JOYPAD_UP, JOYPAD_DOWN, JOYPAD_LEFT, JOYPAD_RIGHT, JOYPAD_A = 4, 5, 6, 7, 8
MASK_TO_ID = (JOYPAD_UP, JOYPAD_DOWN, JOYPAD_LEFT, JOYPAD_RIGHT, JOYPAD_A,
              JOYPAD_B, JOYPAD_START, JOYPAD_SELECT)


class SystemInfo(C.Structure):
    _fields_ = [("library_name", C.c_char_p),
                ("library_version", C.c_char_p),
                ("valid_extensions", C.c_char_p),
                ("need_fullpath", C.c_bool),
                ("block_extract", C.c_bool)]


class GameGeometry(C.Structure):
    _fields_ = [("base_width", C.c_uint), ("base_height", C.c_uint),
                ("max_width", C.c_uint), ("max_height", C.c_uint),
                ("aspect_ratio", C.c_float)]


class SystemTiming(C.Structure):
    _fields_ = [("fps", C.c_double), ("sample_rate", C.c_double)]


class AvInfo(C.Structure):
    _fields_ = [("geometry", GameGeometry), ("timing", SystemTiming)]


class GameInfo(C.Structure):
    _fields_ = [("path", C.c_char_p), ("data", C.c_void_p),
                ("size", C.c_size_t), ("meta", C.c_char_p)]


class Variable(C.Structure):
    _fields_ = [("key", C.c_char_p), ("value", C.c_char_p)]


ENVIRONMENT = C.CFUNCTYPE(C.c_bool, C.c_uint, C.c_void_p)
VIDEO_REFRESH = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_size_t)
AUDIO_SAMPLE = C.CFUNCTYPE(None, C.c_int16, C.c_int16)
AUDIO_BATCH = C.CFUNCTYPE(C.c_size_t, C.POINTER(C.c_int16), C.c_size_t)
INPUT_POLL = C.CFUNCTYPE(None)
INPUT_STATE = C.CFUNCTYPE(C.c_int16, C.c_uint, C.c_uint, C.c_uint, C.c_uint)


# -- the control socket -----------------------------------------------------

class Control:
    """The daemon's end: pads in, sound out. Never blocks the frame loop."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buf = b""
        self.pads = [0, 0]
        self.quit = False

    def send(self, kind, payload=b""):
        try:
            self.sock.sendall(struct.pack("!cH", kind, len(payload)) + payload)
        except OSError:
            self.quit = True

    def poll(self):
        """Take whatever the daemon has sent, without waiting."""
        while True:
            ready, _, _ = select.select([self.sock], [], [], 0)
            if not ready:
                break
            try:
                chunk = self.sock.recv(4096)
            except OSError:
                chunk = b""
            if not chunk:
                self.quit = True
                break
            self.buf += chunk
        while len(self.buf) >= 3:
            kind, n = struct.unpack("!cH", self.buf[:3])
            if len(self.buf) < 3 + n:
                break
            payload, self.buf = self.buf[3:3 + n], self.buf[3 + n:]
            if kind == b"I" and n == 3:
                port, mask = struct.unpack("!BH", payload)
                if port < len(self.pads):
                    self.pads[port] = mask
            elif kind == b"Q":
                self.quit = True


# -- sound ------------------------------------------------------------------

class Resampler:
    """Stereo int16 at the core's rate to mono int16 at the phones' rate.

    Linear interpolation, with the fractional position carried from one batch
    to the next so there is no click at frame boundaries. Good enough for the
    sound chips of these consoles, whose interesting content sits well under
    the 12 kHz a 24 kHz output can carry.
    """

    def __init__(self, src_rate, dst_rate):
        self.step = src_rate / float(dst_rate)
        self.pos = 0.0
        self.last = 0
        self.out = []

    def push(self, mono):
        """`mono` is a list of samples at the source rate."""
        step, pos, out = self.step, self.pos, self.out
        prev = self.last
        n = len(mono)
        while pos < n:
            i = int(pos)
            frac = pos - i
            a = mono[i - 1] if i > 0 else prev
            b = mono[i]
            out.append(int(a + (b - a) * frac))
            pos += step
        self.pos = pos - n
        if n:
            self.last = mono[-1]

    def take(self):
        data, self.out = self.out, []
        return struct.pack("<%dh" % len(data), *data)


# -- the frontend -----------------------------------------------------------

class Frontend:
    def __init__(self, args):
        self.args = args
        self.core = C.CDLL(args.core)
        self.control = Control(args.control)
        self.pixel = PIXEL_0RGB1555
        self.frame = None           # (data pointer, width, height, pitch)
        self.geometry = None
        self.resampler = None
        self.shutdown = False
        self._keep = []             # ctypes objects the core holds pointers to
        self._dirs = {}

    # -- libretro callbacks -------------------------------------------------

    def _environment(self, cmd, data):
        cmd &= ~ENV_EXPERIMENTAL
        try:
            if cmd == ENV_GET_CAN_DUPE:
                C.cast(data, C.POINTER(C.c_bool))[0] = True
                return True
            if cmd == ENV_SET_PIXEL_FORMAT:
                fmt = C.cast(data, C.POINTER(C.c_int))[0]
                if fmt in (PIXEL_0RGB1555, PIXEL_XRGB8888, PIXEL_RGB565):
                    self.pixel = fmt
                    return True
                return False
            if cmd in (ENV_GET_SYSTEM_DIRECTORY, ENV_GET_SAVE_DIRECTORY):
                # The same bytes object every time, held in _dirs, because
                # the core keeps the pointer and reads it later.
                path = self._dirs.setdefault(cmd, self.args.saves.encode())
                C.cast(data, C.POINTER(C.c_char_p))[0] = path
                return True
            if cmd == ENV_GET_VARIABLE:
                # No settings: every core option stays at its default.
                C.cast(data, C.POINTER(Variable))[0].value = None
                return False
            if cmd == ENV_GET_VARIABLE_UPDATE:
                C.cast(data, C.POINTER(C.c_bool))[0] = False
                return True
            if cmd == ENV_SET_GEOMETRY:
                self.geometry = C.cast(data, C.POINTER(GameGeometry))[0]
                return True
            if cmd == ENV_SET_SYSTEM_AV_INFO:
                av = C.cast(data, C.POINTER(AvInfo))[0]
                self.geometry = av.geometry
                return True
            if cmd == ENV_SHUTDOWN:
                self.shutdown = True
                return True
            if cmd in (ENV_SET_VARIABLES, ENV_SET_INPUT_DESCRIPTORS,
                       ENV_SET_SUPPORT_NO_GAME, ENV_SET_CONTROLLER_INFO,
                       ENV_SET_PERFORMANCE_LEVEL, ENV_SET_MESSAGE):
                return True
        except Exception as exc:                # noqa: BLE001 - C boundary
            say("environment %d: %s" % (cmd, exc))
        return False

    def _video(self, data, width, height, pitch):
        if data:
            self.frame = (data, width, height, pitch)
        else:
            # A duplicate frame: the last one is still the picture.
            self.frame = None

    def _audio_sample(self, left, right):
        self.resampler.push([(left + right) >> 1])

    def _audio_batch(self, data, frames):
        n = frames * 2
        samples = data[:n]
        self.resampler.push([(samples[i] + samples[i + 1]) >> 1
                             for i in range(0, n, 2)])
        return frames

    def _input_poll(self):
        self.control.poll()

    def _input_state(self, port, device, _index, ident):
        if device != RETRO_DEVICE_JOYPAD or port > 1:
            return 0
        mask = self.control.pads[port]
        if ident == RETRO_DEVICE_ID_JOYPAD_MASK:
            out = 0
            for bit, joy in enumerate(MASK_TO_ID):
                if mask & (1 << bit):
                    out |= 1 << joy
            return out
        for bit, joy in enumerate(MASK_TO_ID):
            if joy == ident:
                return 1 if mask & (1 << bit) else 0
        return 0

    # -- loading ------------------------------------------------------------

    def load(self):
        core = self.core
        core.retro_api_version.restype = C.c_uint
        if core.retro_api_version() != RETRO_API_VERSION:
            raise RuntimeError("the core speaks a different libretro API")
        callbacks = [
            ("retro_set_environment", ENVIRONMENT(self._environment)),
            ("retro_set_video_refresh", VIDEO_REFRESH(self._video)),
            ("retro_set_audio_sample", AUDIO_SAMPLE(self._audio_sample)),
            ("retro_set_audio_sample_batch", AUDIO_BATCH(self._audio_batch)),
            ("retro_set_input_poll", INPUT_POLL(self._input_poll)),
            ("retro_set_input_state", INPUT_STATE(self._input_state)),
        ]
        for name, fn in callbacks:
            self._keep.append(fn)
            getattr(core, name)(fn)
        core.retro_init()

        info = SystemInfo()
        core.retro_get_system_info(C.byref(info))
        with open(self.args.rom, "rb") as handle:
            rom = handle.read()
        self.sha = hashlib.sha1(rom).hexdigest()
        game = GameInfo(path=self.args.rom.encode(), data=None, size=0,
                        meta=None)
        if not info.need_fullpath:
            buf = C.create_string_buffer(rom, len(rom))
            self._keep.append(buf)
            game.data = C.cast(buf, C.c_void_p)
            game.size = len(rom)
        core.retro_load_game.restype = C.c_bool
        if not core.retro_load_game(C.byref(game)):
            raise RuntimeError("the core would not load this cartridge")

        av = AvInfo()
        core.retro_get_system_av_info(C.byref(av))
        self.geometry = av.geometry
        self.fps = av.timing.fps or 60.0
        self.resampler = Resampler(av.timing.sample_rate or 48000.0,
                                   self.args.rate)
        self._load_sram()
        return info.library_name.decode("utf-8", "replace")

    def _sram(self):
        core = self.core
        core.retro_get_memory_data.restype = C.c_void_p
        core.retro_get_memory_size.restype = C.c_size_t
        size = core.retro_get_memory_size(RETRO_MEMORY_SAVE_RAM)
        ptr = core.retro_get_memory_data(RETRO_MEMORY_SAVE_RAM)
        return (ptr, size) if ptr and size else (None, 0)

    def _sram_path(self):
        return os.path.join(self.args.saves, self.sha + ".srm")

    def _load_sram(self):
        ptr, size = self._sram()
        if not ptr:
            return
        try:
            with open(self._sram_path(), "rb") as handle:
                data = handle.read(size)
        except OSError:
            return
        C.memmove(ptr, data, len(data))

    def save_sram(self):
        ptr, size = self._sram()
        if not ptr:
            return
        data = C.string_at(ptr, size)
        if not data.strip(b"\0\xff"):
            return                      # nothing ever written; keep no file
        path = self._sram_path()
        try:
            os.makedirs(self.args.saves, exist_ok=True)
            with open(path + ".part", "wb") as handle:
                handle.write(data)
            os.replace(path + ".part", path)
        except OSError as exc:
            say("could not save %s: %s" % (path, exc))

    # -- the screen ---------------------------------------------------------

    def open_screen(self):
        if self.args.headless:
            self.screen = (0, 0)
            return
        os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
        import sdl2                                 # python3-sdl2
        self.sdl = sdl2
        if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) != 0:
            raise RuntimeError("SDL: %s" % sdl2.SDL_GetError().decode())
        sdl2.SDL_SetHint(sdl2.SDL_HINT_RENDER_SCALE_QUALITY, b"0")
        self.window = sdl2.SDL_CreateWindow(
            b"playstick-game", 0, 0, 0, 0,
            sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP)
        if not self.window:
            raise RuntimeError("SDL window: %s" % sdl2.SDL_GetError().decode())
        sdl2.SDL_ShowCursor(0)
        self.renderer = sdl2.SDL_CreateRenderer(
            self.window, -1,
            sdl2.SDL_RENDERER_ACCELERATED | sdl2.SDL_RENDERER_PRESENTVSYNC)
        if not self.renderer:
            raise RuntimeError("SDL renderer: %s"
                               % sdl2.SDL_GetError().decode())
        mode = sdl2.SDL_DisplayMode()
        sdl2.SDL_GetCurrentDisplayMode(0, C.byref(mode))
        self.screen = (mode.w, mode.h)
        if mode.refresh_rate and abs(mode.refresh_rate - self.fps) > 2:
            say("the display runs at %d Hz and the game at %.2f; it will play "
                "at the right speed, less smoothly" % (mode.refresh_rate,
                                                       self.fps))
        fmt = {PIXEL_XRGB8888: sdl2.SDL_PIXELFORMAT_ARGB8888,
               PIXEL_RGB565: sdl2.SDL_PIXELFORMAT_RGB565,
               PIXEL_0RGB1555: sdl2.SDL_PIXELFORMAT_ARGB1555}[self.pixel]
        g = self.geometry
        self.texture = sdl2.SDL_CreateTexture(
            self.renderer, fmt, sdl2.SDL_TEXTUREACCESS_STREAMING,
            max(g.max_width, g.base_width), max(g.max_height, g.base_height))

    def _dest(self, width, height):
        """The largest whole-number scale that fits, centred. The aspect the
        core asks for is honoured horizontally, so an NES picture is 8:7
        pixels stretched to 4:3 as it was on a television."""
        sw, sh = self.screen
        aspect = self.geometry.aspect_ratio or (width / float(height))
        scale = max(1, sh // height)
        h = height * scale
        w = int(round(h * aspect))
        if w > sw:
            w = sw
            h = int(round(w / aspect))
        return self.sdl.SDL_Rect((sw - w) // 2, (sh - h) // 2, w, h)

    def present(self):
        if self.args.headless:
            # No vsync to wait on: sleep to the next frame instead.
            self.frame = None
            time.sleep(max(0.0, min(1.0 / self.fps, self._due - time.monotonic())))
            return
        sdl2 = self.sdl
        if self.frame is not None:
            data, width, height, pitch = self.frame
            self.src = sdl2.SDL_Rect(0, 0, width, height)
            sdl2.SDL_UpdateTexture(self.texture, C.byref(self.src),
                                   C.c_void_p(data), pitch)
            self.dst = self._dest(width, height)
            self.frame = None
        sdl2.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 255)
        sdl2.SDL_RenderClear(self.renderer)
        if getattr(self, "src", None) is not None:
            sdl2.SDL_RenderCopy(self.renderer, self.texture,
                                C.byref(self.src), C.byref(self.dst))
        sdl2.SDL_RenderPresent(self.renderer)

    def close_screen(self):
        sdl2 = getattr(self, "sdl", None)
        if sdl2 is None:
            return
        sdl2.SDL_DestroyTexture(self.texture)
        sdl2.SDL_DestroyRenderer(self.renderer)
        sdl2.SDL_DestroyWindow(self.window)
        sdl2.SDL_Quit()

    # -- the loop -----------------------------------------------------------

    def run(self):
        sdl2 = getattr(self, "sdl", None)
        event = sdl2.SDL_Event() if sdl2 else None
        period = 1.0 / self.fps
        due = time.monotonic()
        while not self.control.quit and not self.shutdown:
            while sdl2 and sdl2.SDL_PollEvent(C.byref(event)):
                pass
            now = time.monotonic()
            ran = 0
            # At most two frames to catch up; further behind than that is a
            # stall, and running ten frames at once to make it up would be a
            # game that jumps.
            while due <= now and ran < 2:
                self.core.retro_run()
                due += period
                ran += 1
            if due < now - period:
                due = now
            if ran:
                pcm = self.resampler.take()
                if pcm:
                    self.control.send(b"A", pcm)
            self._due = due
            self.present()


def say(text):
    print("playstick-game: %s" % text, file=sys.stderr, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--core", required=True)
    parser.add_argument("--rom", required=True)
    parser.add_argument("--control", required=True)
    parser.add_argument("--rate", type=int, default=24000)
    parser.add_argument("--saves", default="/var/lib/playstick/saves")
    # No screen at all: run the core against the wall clock and send its
    # sound. For the tests, and for checking a core on the stick over SSH
    # without taking the display.
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args(argv)

    front = Frontend(args)
    try:
        name = front.load()
        front.open_screen()
    except Exception as exc:                    # noqa: BLE001 - reported
        say(str(exc))
        front.control.send(b"E", str(exc).encode("utf-8")[:1000])
        return 1
    front.control.send(b"R", json.dumps({
        "core": name, "fps": round(front.fps, 3), "rate": args.rate,
        "screen": list(front.screen)}).encode())
    say("%s: %s at %.2f fps" % (name, os.path.basename(args.rom), front.fps))
    try:
        front.run()
    finally:
        front.save_sram()
        front.core.retro_unload_game()
        front.core.retro_deinit()
        front.close_screen()
    return 0


if __name__ == "__main__":
    sys.exit(main())
