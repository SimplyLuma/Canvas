#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
fail() { printf 'Canvas source contract: FAIL: %s\n' "$1" >&2; exit 1; }

for required in \
  luma-app.toml platform.lock \
  flatpak/org.projectluma.Canvas.json \
  data/org.projectluma.Canvas.desktop \
  data/org.projectluma.Canvas.metainfo.xml \
  packaging/luma-canvas.spec \
  verification/luma-native-evidence.json; do
  [ -f "$repo_root/$required" ] || fail "missing source contract: $required"
done

python3 - "$repo_root" <<'PY' || exit 1
import json, pathlib, sys, tomllib
root = pathlib.Path(sys.argv[1])
with (root / "platform.lock").open("rb") as stream:
    lock = tomllib.load(stream)
with (root / "luma-app.toml").open("rb") as stream:
    manifest = tomllib.load(stream)
flatpak = json.loads((root / "flatpak/org.projectluma.Canvas.json").read_text())
expected_id = "org.projectluma.Canvas"
expected_commit = "39c7d18d367981a355535cba1164063748913ad4"
expected_platform = "0.1.0-1.luma.18"
assert manifest["schema_version"] == "0.2"
assert manifest["application"]["id"] == expected_id
assert manifest["responsive"]["test_widths"] == [360, 500, 1024, 1920]
assert set(manifest["responsive"]["input_modes"]) == {"pointer", "touch", "keyboard"}
assert set(manifest["responsive"]["presentation_modes"]) == {"windowed", "fullscreen-mobile"}
assert manifest["distribution"]["portal_first"] is True
assert lock["luma"]["repository"] == "https://github.com/ProjectLuma/Luma"
assert lock["luma"]["commit"] == expected_commit
assert lock["platform"]["version"] == expected_platform
assert lock["platform"]["status"] == "admitted"
assert flatpak["id"] == expected_id
assert flatpak["command"] == "luma-canvas"
platform_sources = flatpak["modules"][0]["sources"]
assert platform_sources == [{"type": "git", "url": lock["luma"]["repository"], "commit": expected_commit}]
assert not any(arg.startswith("--filesystem=") for arg in flatpak["finish-args"])
PY

window="$repo_root/luma_canvas/window.py"
grep -Fq 'class CanvasWindow(AppWindow):' "$window" || fail 'window does not subclass luma_appkit.AppWindow'
grep -Fq 'CommandRegistry((' "$window" || fail 'commands do not use the AppKit registry'
grep -Fq 'Island(' "$window" || fail 'workspace does not use AppKit Islands'
grep -Fq 'spacing=9' "$window" || fail 'workspace does not use the canonical nine-pixel island gap'
grep -Fq 'LumaSemantics.SemanticObject.new' "$window" || fail 'semantic object tree is absent'
grep -Fq 'send_notification' "$window" || fail 'declared notification portal is unused'
grep -Fq 'Gtk.FileDialog' "$window" || fail 'declared file chooser portal is unused'
grep -Fq 'os.replace' "$repo_root/luma_canvas/persistence.py" || fail 'save is not atomic'
grep -Fq 'manifest.json' "$repo_root/luma_canvas/persistence.py" || fail 'native format lacks readable metadata'

# App-owned content styling is supported. Keep the ownership boundary strict:
# one packaged sheet, Canvas-scoped selectors, semantic colours, no frame/menu CSS.
python3 "$repo_root/tests/content_style_contract.py" || fail 'content styling escapes Canvas ownership'
for forbidden in \
  'LumaUI.Context.new_from_environment' \
  'LumaUI.InspectorSection' \
  'LumaUI.SegmentedControl' \
  'LumaUI.ColorWell'; do
  ! grep -R -Fq "$forbidden" "$repo_root/luma_canvas" || fail "unadmitted platform surface copied into app: $forbidden"
done

forbidden_runtime=$(grep -REni '(waydroid|android\.|webview|webkit|electron)' \
  "$repo_root/luma_canvas" "$repo_root/bin" "$repo_root/packaging" || true)
if [ -n "$forbidden_runtime" ]; then
  printf '%s\n' "$forbidden_runtime"
  fail 'application depends on an Android or browser runtime'
fi

PYTHONPATH="$repo_root" python3 -m unittest discover \
  -s "$repo_root/tests" -p 'test_*.py' >/dev/null || fail 'unit tests failed'
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$repo_root/data/org.projectluma.Canvas.desktop" || fail 'desktop entry is invalid'
fi
if command -v appstreamcli >/dev/null 2>&1; then
  appstreamcli validate --no-net "$repo_root/data/org.projectluma.Canvas.metainfo.xml" || fail 'AppStream metadata is invalid'
fi
if command -v luma >/dev/null 2>&1; then
  (cd "$repo_root" && luma lint luma-app.toml && luma inspect luma-app.toml >/dev/null && luma test luma-app.toml) || fail 'Luma SDK contract failed'
fi

printf 'Canvas source contract: PASS\n'
