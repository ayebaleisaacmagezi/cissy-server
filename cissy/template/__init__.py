"""The generated app's source code.

Ported from the desktop builder's `GeneratedAppTemplate`. This is the actual
product - everything else in this server exists to run `flutter build` over what
comes out of here.

Split across five modules, of which this file is the entire public surface.
Callers import from `cissy.template` and never from a module inside it, so the
split can be rearranged later without touching `generate.py` or the tests:

    common.py    escaping, derived names, the pinned dependency versions
    platform.py  pubspec, Android manifest, Gradle, launch screen, iOS plist
    nav.py       the bottom navigation shell
    screens.py   saved items, downloads, settings
    webview.py   main.dart and the WebView screen itself
"""

from __future__ import annotations

from .common import (
    DEPENDENCIES,
    dart_string,
    deep_link_scheme,
    organisation,
    project_name,
)
from .platform import (
    NOTIFICATION_COLOUR,
    SPLASH_DRAWABLE,
    android_manifest,
    gradle_properties,
    ios_usage_descriptions,
    launch_background,
    main_activity,
    notification_colour_resource,
    pubspec,
    splash_colour_resource,
    splash_icon_drawable,
    styles_v31,
)
from .push import STAT_ICON, STAT_ICON_SIZES
from .push import icon_tool as notification_icon_tool
from .webview import main_dart

__all__ = [
    "DEPENDENCIES",
    "NOTIFICATION_COLOUR",
    "SPLASH_DRAWABLE",
    "STAT_ICON",
    "STAT_ICON_SIZES",
    "android_manifest",
    "dart_string",
    "deep_link_scheme",
    "gradle_properties",
    "ios_usage_descriptions",
    "launch_background",
    "main_activity",
    "main_dart",
    "notification_colour_resource",
    "notification_icon_tool",
    "organisation",
    "project_name",
    "pubspec",
    "splash_colour_resource",
    "splash_icon_drawable",
    "styles_v31",
]
