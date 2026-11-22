"""Which files are games the stick can play, read from their headers. Qt-free.

The test is the cartridge header, not the file name. A file called .nes that is
really a zip, or a .gb that is a save file, would otherwise reach the projector
and fail there, in front of a child holding a phone. Every system here has a
header with a fixed signature, so the check is a few byte comparisons at known
offsets and needs no emulator.

WHAT EACH SYSTEM PROVES, AND WHAT IT DOES NOT

  NES   The iNES magic "NES\\x1a" at 0. The mapper number is in the header too,
        and nestopia plays nearly all of them. A mapper outside the common set
        is a warning, not a refusal: homebrew uses odd ones, and only trying it
        on the stick can say for sure.
  GB    The Nintendo logo bitmap at 0x104, which every licensed and almost
        every homebrew cartridge carries because the boot ROM checks it. Byte
        0x143 marks a colour game: 0x80 runs on both, 0xC0 only on a Color.
        The title is at 0x134.
  SMS   "TMR SEGA" at 0x7FF0, or at 0x3FF0 or 0x1FF0 on a smaller cartridge.
        The export BIOS checks it, so a Master System game has one.

Anything else is refused with a sentence saying why. That sentence is what the
Games page shows in the row.
"""

import hashlib
import os
import re

# The extensions a scan looks at. Everything else in a folder is ignored, so a
# folder of ROMs with a readme and box art in it scans cleanly.
EXTENSIONS = {".nes": "nes", ".gb": "gb", ".gbc": "gb", ".sms": "sms"}

# The first 0x30 bytes of the logo at 0x104. All 48 are compared; a partial
# match is not a Game Boy cartridge.
GB_LOGO = bytes.fromhex(
    "CEED6666CC0D000B03730083000C000D0008111F8889000E"
    "DCCC6EE6DDDDD999BBBB67636E0EECCCDDDC999FBBB9333E")

SMS_SIGNATURE = b"TMR SEGA"
SMS_OFFSETS = (0x7FF0, 0x3FF0, 0x1FF0)

# The mappers nestopia is known to play well, and homebrew mostly uses. Numbers
# outside this set get a warning rather than a refusal.
NES_COMMON_MAPPERS = frozenset((0, 1, 2, 3, 4, 7, 9, 10, 11, 30, 34, 66, 71))

# Larger than any cartridge of these systems by a wide margin. A file bigger
# than this is not a ROM, and reading it whole to hash it would be a waste.
MAX_BYTES = 8 * 1024 * 1024

SYSTEM_NAMES = {"nes": "NES", "gb": "Game Boy", "gbc": "Game Boy Color",
                "sms": "Master System"}


class Rom:
    """What a header said about one file."""

    def __init__(self, path, system, title, warning="", data=b""):
        self.path = path
        self.system = system
        self.title = title
        self.warning = warning
        self.data = data

    @property
    def ident(self):
        """sha1 of the ROM bytes, cut to the sixteen hex characters the stick
        uses for every id. The same ROM imported twice, or from two folders,
        is one game."""
        return hashlib.sha1(self.data).hexdigest()[:16]

    @property
    def extension(self):
        return {"nes": ".nes", "gb": ".gb", "gbc": ".gbc",
                "sms": ".sms"}[self.system]


class NotARom(Exception):
    """Raised with the sentence the Games page shows for a refused file."""


def read(path):
    """Read and check one file. Returns a Rom, or raises NotARom."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in EXTENSIONS:
        raise NotARom("not a .nes, .gb, .gbc or .sms file")
    try:
        size = os.path.getsize(path)
        if size > MAX_BYTES:
            raise NotARom("%d MB is too big for a cartridge" % (size >> 20))
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise NotARom("could not be read: %s" % (exc.strerror or exc)) from exc
    return identify(path, data)


def identify(path, data):
    """The header check on bytes already read. Separate from read() so the
    tests can feed it small synthetic headers rather than real ROMs."""
    fallback = title_from_filename(path)
    if data[:4] == b"NES\x1a":
        return _nes(path, data, fallback)
    if len(data) >= 0x150 and data[0x104:0x134] == GB_LOGO:
        return _gb(path, data, fallback)
    for offset in SMS_OFFSETS:
        if data[offset:offset + 8] == SMS_SIGNATURE:
            return Rom(path, "sms", fallback, data=data)
    raise NotARom("the header is not an NES, Game Boy or Master System one")


def _nes(path, data, title):
    if len(data) < 16:
        raise NotARom("the NES header is cut short")
    mapper = (data[7] & 0xF0) | (data[6] >> 4)
    prg_banks = data[4]
    if prg_banks == 0:
        raise NotARom("the NES header says it has no program")
    warning = ""
    if mapper not in NES_COMMON_MAPPERS:
        warning = ("mapper %d is unusual; it may not play on the stick"
                   % mapper)
    return Rom(path, "nes", title, warning, data)


def _gb(path, data, fallback):
    cgb = data[0x143]
    system = "gbc" if cgb in (0x80, 0xC0) else "gb"
    # The title is 16 bytes on an old cartridge and 11 on a newer one, padded
    # with zeros. Only printable ASCII is kept; the rest is manufacturer code.
    raw = data[0x134:0x143 if cgb in (0x80, 0xC0) else 0x144]
    header = re.sub(r"[^ -~]+", "", raw.split(b"\0", 1)[0].decode(
        "ascii", "replace")).strip()
    title = fallback or header.title()
    return Rom(path, system, title, data=data)


# "Lan Master (World) (v1.1).nes" -> "Lan Master". The bracketed tags are
# region and version flags that dump groups add, not part of the name.
_TAGS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


def title_from_filename(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = _TAGS.sub("", stem).replace("_", " ").strip()
    return re.sub(r"\s+", " ", stem)


def sort_title(title):
    """The shelf key: "The Legend of..." files under L, as films do."""
    lowered = title.lower()
    for article in ("the ", "a ", "an "):
        if lowered.startswith(article):
            return lowered[len(article):]
    return lowered


def scan(root):
    """Every file under `root` with a ROM extension, as (path, Rom or error).

    Dot directories are skipped, as the film scan skips them, so a scan of the
    share never offers the copies this app placed under .playstick.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in EXTENSIONS:
                continue
            path = os.path.join(dirpath, name)
            try:
                found.append((path, read(path)))
            except NotARom as exc:
                found.append((path, str(exc)))
    return found
