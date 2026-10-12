# SPDX-License-Identifier: Apache-2.0
"""Parity: TitleIsland (structure_island.py), folded: ☰ with a title, ‹ with a subtitle and a trailing
button, and no lead."""

CASES = [
    ("menu", lambda C, Gtk: C.TitleIsland.new(C.TitleIslandLead.MENU, "Documents", "Home · 24 items"),
     lambda K, Gtk: K.TitleIsland("Documents", "Home · 24 items", lead="menu")),
    ("back", lambda C, Gtk: C.TitleIsland.new(C.TitleIslandLead.BACK, "Launch", "Edited just now"),
     lambda K, Gtk: K.TitleIsland("Launch", "Edited just now", lead="back")),
    ("no-lead", lambda C, Gtk: C.TitleIsland.new(C.TitleIslandLead.NONE, "September 2026", ""),
     lambda K, Gtk: K.TitleIsland("September 2026", lead=None)),
]
