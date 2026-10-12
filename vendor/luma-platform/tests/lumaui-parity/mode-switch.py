# SPDX-License-Identifier: Apache-2.0
"""Parity: LumaModeSwitch (C) and structure_placement.ModeSwitch (Python)."""

MODES = [("view", "View", "eye"), ("markup", "Mark up", "pen-line"), ("adjust", "Adjust", "sliders-horizontal")]


def c_modes(C, current=None):
    modes = C.ModeSwitch.new("Mode")
    for key, label, icon in MODES:
        modes.add(key, label, icon)
    if current is not None:
        modes.set_current(current)
    return modes


def py_modes(K, current=None):
    modes = K.ModeSwitch(MODES)
    if current is not None:
        modes.set_current(current)
    return modes


CASES = [
    ("three", lambda C, Gtk: c_modes(C), lambda K, Gtk: py_modes(K)),
    ("chosen", lambda C, Gtk: c_modes(C, "adjust"), lambda K, Gtk: py_modes(K, "adjust")),
]
