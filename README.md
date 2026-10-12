# Canvas

Canvas is a native visual design app for Linux, built with Luma UI. Design
frames, vectors and pages, use components and auto layout, edit fills and
effects, and connect interactive prototypes. Your designs save automatically;
the design library supports search, projects, stars and grid/list views.

## Install

The signed x86_64 Flatpak is distributed through Luma Depot. The shared Luma
Platform runtime installs automatically. Canvas works offline without an account.
Release downloads: https://github.com/SimplyLuma/Canvas/releases.

The package identity is `org.projectluma.Canvas`. Automatic saves remain in
persistent private application data. Use Browse files, import/export or Save a
copy to choose files outside the application through the desktop file portal.

## Build the Flatpak

Install Flatpak and flatpak-builder, then add the signed runtime remote:

```sh
flatpak remote-add --user --if-not-exists luma https://dl.simplyluma.com/luma.flatpakrepo
flatpak install --user luma org.projectluma.Platform//44 org.projectluma.Sdk//44
CANVAS_BUILD_ROOT="${XDG_CACHE_HOME:-$HOME/.cache}/canvas-source-build"
flatpak-builder --user --disable-rofiles-fuse --force-clean --jobs=2 \
  --default-branch=stable --repo="$CANVAS_BUILD_ROOT/repo" \
  "$CANVAS_BUILD_ROOT/app" flatpak/org.projectluma.Canvas.json
```

Keep build outputs outside the source directory. All app/toolkit sources are
included: packaging needs no private repository or separately fetched toolkit.
`platform.lock` identifies the original toolkit revision;
`vendor/SOURCE-MANIFEST.json` hashes every vendored source input.

## Compatibility and testing

Export PNG, SVG and PDF; save editable `.canvas` documents and import supported
SVG, images and `.fig` documents. Figma import preserves supported embedded
components, instances, horizontal/vertical auto layouts, layout grids and
prototype interactions. Missing fonts and unsupported Figma constructs are
reported during import. Canvas does not promise lossless import of every Figma
file, modern wrapping/grid auto layout, or components absent from an export.

The interface adapts to narrow windows. Release qualification includes native
editing, saved-file recovery, a 1,001-design library, exports, real exported
prototype routes and compact-window workflows. A resized desktop window is not
physical-phone or on-screen-keyboard qualification.

Unit tests are in `tests` (`python3 -m unittest discover -s tests`). Native GTK
checks require GTK/PyGObject, Cairo and the pinned Luma toolkit with a display;
checks without those dependencies skip their GTK cases. Server runtime fixtures
are separate from unit tests and preserve isolated verification data.

## Licenses

Original software defaults to Apache-2.0. Derived source retains its declared
MPL-2.0/LGPL-2.1-or-later license. Artwork, icons and fonts keep their respective
licenses. See [LICENSE.md](LICENSE.md), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
and `LICENSES`. Release source includes the complete application and toolkit;
the separately distributed Luma runtime retains its own corresponding sources.
