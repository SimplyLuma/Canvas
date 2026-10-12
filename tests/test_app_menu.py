# SPDX-License-Identifier: MPL-2.0
"""The identity menu is short at the top and every row runs a real command."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def menu_module():
    source = (ROOT / "luma_canvas" / "app_menu.py").read_text("utf-8")
    tree = ast.parse(source)
    top = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign) and node.target.id == "TOP")
    return ast.literal_eval(top)


def ids(rows):
    found = []
    for row in rows:
        if row is None:
            continue
        label, target = row
        if isinstance(target, tuple):
            found += ids(target)
        elif not target.startswith(("@", "win.")):
            found.append(target)
    return found


class AppMenuTest(unittest.TestCase):
    def test_top_level_is_short(self) -> None:
        rows = [row for row in menu_module() if row is not None]
        self.assertLessEqual(len(rows), 16)

    def test_every_row_is_a_registered_command(self) -> None:
        window = (ROOT / "luma_canvas" / "window.py").read_text("utf-8")
        for command_id in ids(menu_module()):
            self.assertIn(f'Command("{command_id}"', window, command_id)

    def test_labels_are_sentence_case(self) -> None:
        def labels(rows):
            for row in rows:
                if row is None:
                    continue
                label, target = row
                yield label
                if isinstance(target, tuple):
                    yield from labels(target)
        for label in labels(menu_module()):
            words = label.split()
            self.assertTrue(all(word[0].islower() or word[0] in "0123456789…" or index == 0 or word in {"Filer", "Trash", "Canvas"}
                                for index, word in enumerate(words)), label)


if __name__ == "__main__":
    unittest.main()
