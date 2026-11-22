"""Games in the admin app: the header check, the import, and the push.

No real ROM is committed. Each test builds the few header bytes that the
check reads and leaves the rest zero, which is enough for roms.identify and
says plainly which bytes it depends on.
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "admin"))
sys.path.insert(0, os.path.join(ROOT, "src", "prep"))

from playstick_admin.core import games, nas_store, roms             # noqa: E402
from playstick_admin.core.db import Database                        # noqa: E402


def nes(mapper=0, banks=2):
    head = bytearray(16)
    head[:4] = b"NES\x1a"
    head[4] = banks
    head[6] = (mapper & 0x0F) << 4
    head[7] = mapper & 0xF0
    return bytes(head) + b"\0" * (16384 * banks)


def gb(cgb=0x00, title=b"LANMASTER"):
    data = bytearray(0x8000)
    data[0x104:0x134] = roms.GB_LOGO
    data[0x134:0x134 + len(title)] = title
    data[0x143] = cgb
    return bytes(data)


def sms(offset=0x7FF0):
    data = bytearray(0x8000)
    data[offset:offset + 8] = roms.SMS_SIGNATURE
    return bytes(data)


class HeaderCheck(unittest.TestCase):
    def test_each_system_is_told_apart_by_its_header(self):
        self.assertEqual(roms.identify("a.nes", nes()).system, "nes")
        self.assertEqual(roms.identify("a.gb", gb()).system, "gb")
        self.assertEqual(roms.identify("a.gbc", gb(0x80)).system, "gbc")
        self.assertEqual(roms.identify("a.gb", gb(0xC0)).system, "gbc")
        self.assertEqual(roms.identify("a.sms", sms()).system, "sms")
        self.assertEqual(roms.identify("a.sms", sms(0x3FF0)).system, "sms")

    def test_the_name_ignores_what_a_file_is_called(self):
        """A Game Boy cartridge named .nes is still a Game Boy cartridge."""
        self.assertEqual(roms.identify("wrong.nes", gb()).system, "gb")

    def test_a_file_with_no_known_header_is_refused_with_a_reason(self):
        with self.assertRaises(roms.NotARom) as caught:
            roms.identify("save.gb", b"\0" * 0x8000)
        self.assertIn("header", str(caught.exception))

    def test_an_nes_header_with_no_program_is_refused(self):
        with self.assertRaises(roms.NotARom):
            roms.identify("a.nes", nes(banks=0))

    def test_an_unusual_mapper_is_a_warning_not_a_refusal(self):
        self.assertEqual(roms.identify("a.nes", nes(mapper=4)).warning, "")
        self.assertIn("mapper 200", roms.identify("a.nes",
                                                  nes(mapper=200)).warning)

    def test_the_title_comes_from_the_file_without_dump_tags(self):
        rom = roms.identify("/x/Lan Master (World) (v1.1).nes", nes())
        self.assertEqual(rom.title, "Lan Master")

    def test_the_id_is_the_bytes_not_the_path(self):
        self.assertEqual(roms.identify("a/x.nes", nes()).ident,
                         roms.identify("b/y.nes", nes()).ident)
        self.assertRegex(roms.identify("x.nes", nes()).ident, r"^[0-9a-f]{16}$")

    def test_the_shelf_key_drops_a_leading_article(self):
        self.assertEqual(roms.sort_title("The Last Ninja"), "last ninja")


class FakeClient:
    def __init__(self):
        self.synced, self.thumbs, self.deleted = [], [], []

    def sync_films(self, films, reconcile=True, kind="film"):
        self.synced.append((list(films), reconcile, kind))
        return {"written": len(films), "removed": 0}

    def put_thumb(self, ident, data, mime):
        self.thumbs.append((ident, mime))

    def admin_library(self):
        return {"films": []}

    def delete_films(self, ids):
        self.deleted.extend(ids)


class Importing(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.nas = os.path.join(self.dir, "nas")
        os.makedirs(self.nas)
        self.db = Database(os.path.join(self.dir, "admin.sqlite"))
        self.db.migrate()
        self.addCleanup(self.db.close)

    def write(self, data, name):
        path = os.path.join(self.dir, "roms", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def rom(self, data=None, name="Lan Master.nes"):
        return roms.read(self.write(data or nes(), name))

    def test_a_rom_lands_on_the_share_under_the_apps_own_folder(self):
        rom = self.rom()
        ident = games.import_rom(self.db, self.nas, rom)
        row = games.get(self.db, ident)
        self.assertEqual(row["rel"], ".playstick/roms/%s.nes" % ident)
        with open(os.path.join(self.nas, row["rel"]), "rb") as handle:
            self.assertEqual(handle.read(), rom.data)
        # Nothing left in staging, and nothing on the share the orphan walk
        # would report: the game table claims the ROM.
        self.assertFalse(os.listdir(nas_store.staging_root(self.nas)))
        self.assertEqual(nas_store.orphans(self.db, self.nas), [])

    def test_importing_again_keeps_the_edits(self):
        ident = games.import_rom(self.db, self.nas, self.rom())
        games.update(self.db, ident, {"title": "Better Name", "players": 2})
        games.import_rom(self.db, self.nas, self.rom(name="copy.nes"))
        row = games.get(self.db, ident)
        self.assertEqual((row["title"], row["players"]), ("Better Name", 2))
        self.assertEqual(len(games.all_games(self.db)), 1)

    def test_an_edit_cannot_change_the_system_or_the_path(self):
        ident = games.import_rom(self.db, self.nas, self.rom())
        games.update(self.db, ident, {"system": "gb", "rel": "/etc/x"})
        row = games.get(self.db, ident)
        self.assertEqual((row["system"], row["rel"]),
                         ("nes", ".playstick/roms/%s.nes" % ident))

    def test_a_push_is_a_game_batch_and_the_poster_follows(self):
        ident = games.import_rom(self.db, self.nas, self.rom())
        games.set_poster(self.db, ident, b"\xff\xd8poster", "image/jpeg")
        client = FakeClient()
        games.push_one(client, self.db, ident)
        films, reconcile, kind = client.synced[0]
        self.assertEqual((kind, reconcile), ("game", False))
        self.assertEqual((films[0]["system"], films[0]["media_rel"]),
                         ("nes", ".playstick/roms/%s.nes" % ident))
        self.assertEqual(client.thumbs, [(ident, "image/jpeg")])
        self.assertIsNotNone(games.get(self.db, ident)["pushed_at"])

    def test_push_all_reconciles_games_only(self):
        games.import_rom(self.db, self.nas, self.rom())
        client = FakeClient()
        games.push_all(client, self.db)
        self.assertEqual(client.synced[0][1:], (True, "game"))

    def test_removing_takes_it_off_the_stick_and_the_share(self):
        ident = games.import_rom(self.db, self.nas, self.rom())
        rel = games.get(self.db, ident)["rel"]
        client = FakeClient()
        games.remove(client, self.db, self.nas, ident)
        self.assertEqual(client.deleted, [ident])
        self.assertFalse(os.path.exists(os.path.join(self.nas, rel)))
        self.assertIsNone(games.get(self.db, ident))

    def test_a_folder_scan_reports_each_file(self):
        self.rom()
        self.rom(gb(), name="Tile.gb")
        self.write(b"junk" * 100, "broken.sms")
        found = {os.path.basename(p): r for p, r in
                 roms.scan(os.path.join(self.dir, "roms"))}
        self.assertEqual(found["Tile.gb"].system, "gb")
        self.assertIsInstance(found["broken.sms"], str)


if __name__ == "__main__":
    unittest.main()
