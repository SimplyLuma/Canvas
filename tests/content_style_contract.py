# SPDX-License-Identifier: Apache-2.0
"""Reject content CSS that can escape Canvas or introduce a private palette."""
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]
sheets = [p for p in root.rglob('*.css') if not {'build', '.git', '.flatpak-builder'}.intersection(p.parts)]
assert sheets == [root / 'luma_canvas/content.css'], sheets
css = re.sub(r'/\*.*?\*/', '', sheets[0].read_text(), flags=re.S)
assert not re.search(r'#[0-9a-fA-F]{3,8}\b|\b(?:rgb|hsl)a?\s*\(|@define-color', css)
for selectors, declarations in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
    for selector in selectors.split(','):
        assert selector.strip().startswith('.canvas-'), selector
        assert not re.search(r'\b(?:window|popover|menu|headerbar|decoration)\b|\.luma-island\b', selector), selector
for package in ('packaging/luma-canvas.spec', 'flatpak/org.projectluma.Canvas.json'):
    assert 'luma_canvas/content.css' in (root / package).read_text(), package
    assert 'data/icons/hicolor' in (root / package).read_text(), package
print('Canvas content styling contract: PASS')
