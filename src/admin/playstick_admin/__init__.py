"""Playstick admin desktop application (host-only).

Runs on the machine that encodes and prepares the library -- never on the
stick. See src/prep/playstick_prep.py for the preparation pipeline this drives
and src/server/playstick/ for the on-device daemon it talks to over HTTP.
"""
