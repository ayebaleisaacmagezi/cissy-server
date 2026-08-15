"""Small helpers shared by everything that emits generated source.

Nothing here writes a whole file. `dart_string` is the one that matters: any
value originating from user input has to go through it, because it produces a
correctly escaped Dart literal and nothing else in this package escapes
anything.
"""

from __future__ import annotations

import json
import re

from ..config import (
    DEFAULT_SPLASH_DARK,
    DEFAULT_SPLASH_LIGHT,
    AppConfig,
)


DEPENDENCIES = {
    "flutter_inappwebview": "^6.1.5",
    "url_launcher": "^6.3.2",
    "share_plus": "^11.1.0",
    "geolocator": "^14.0.2",
    "path_provider": "^2.1.5",
    "open_filex": "^4.7.0",
    "app_links": "^6.4.1",
    "connectivity_plus": "^6.1.0",
    "shared_preferences": "^2.3.2",
    "flutter_launcher_icons": "^0.14.4",
}


def dart_string(value: object) -> str:
    """A Dart string literal. JSON's escaping rules are a subset of Dart's."""
    return json.dumps("" if value is None else str(value))


def deep_link_scheme(config: AppConfig) -> str:
    """A custom URL scheme derived from the app name.

    Schemes must start with a letter, so leading digits are stripped rather than
    producing something Android silently refuses to register.
    """
    value = re.sub(r"[^a-z0-9]+", "", config.display_name.strip().lower())
    value = re.sub(r"^[^a-z]+", "", value)
    return value or "cissyapp"


# Dart keywords that cannot be a package name. Short list because a package
# name is only ever the last segment of a validated package id.
_RESERVED = {"test", "async", "await", "class", "const", "new", "void", "is", "in"}


def project_name(config: AppConfig) -> str:
    """The pubspec package name - the last segment of the package id.

    Taken from the package id rather than the app name so that
    `flutter create --org <rest> --project-name <last>` produces exactly the
    configured applicationId and bundle id, with nothing left to patch
    afterwards.
    """
    segment = config.android_package_id.rsplit(".", 1)[-1]
    value = re.sub(r"[^a-z0-9_]+", "_", segment.lower()).strip("_")
    if not value or value[0].isdigit():
        value = f"app_{value}" if value else "cissy_app"
    return f"{value}_app" if value in _RESERVED else value


def organisation(config: AppConfig) -> str:
    """Everything before the last segment of the package id."""
    parts = config.android_package_id.rsplit(".", 1)
    return parts[0] if len(parts) == 2 else "com.cissytech"


def with_url_flag(url: str, flag: str) -> str:
    """Append the entry flag to a URL, if one is configured.

    Only the entry points carry it - the home URL and the navigation tabs. The
    moment the visitor follows a link inside the site it is gone, and rewriting
    every outbound URL to keep it would mean editing links the site's own
    routing depends on. So this is a hint to the customer's server about how the
    session started, and the Studio says as much next to the field.
    """
    if not flag:
        return url
    base, separator, fragment = url.partition("#")
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}{flag}{separator}{fragment}"


def _dart_bool(value: bool) -> str:
    return "true" if value else "false"


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower()


def dart_color(value: str, fallback: str) -> str:
    """A hex string as a Dart Color literal."""
    return f"const Color(0xFF{(value or fallback).lstrip('#').upper()})"


def splash_widget(
    config: AppConfig,
    *,
    splash_asset: str | None,
    icon_asset: str | None,
    indent: int = 18,
) -> str:
    """What covers the app while the first page loads.

    Two shapes. The icon on a background is the default and needs no upload of
    its own: the launcher icon is already there, it cannot be cropped by a
    phone whose aspect ratio nobody predicted, and the background can follow
    the system into dark mode - which one flat image cannot do at all.

    Returned as an expression rather than a statement, so each caller places it
    where its own widget tree needs it.
    """
    pad = " " * indent
    if config.splash_style == "image":
        if not splash_asset:
            return "const SizedBox.expand()"
        return "\n".join([
            "Image.asset(",
            f"{pad}  {dart_string(splash_asset)},",
            f"{pad}  fit: BoxFit.cover,",
            f"{pad}  width: double.infinity,",
            f"{pad}  height: double.infinity,",
            f"{pad})",
        ])

    background = _splash_background(config, indent)
    if not icon_asset:
        # Nothing to centre. A plain surface rather than an empty box, so the
        # moment still reads as the app starting rather than as a failure.
        return "\n".join([
            "ColoredBox(",
            f"{pad}  color: {background},",
            f"{pad}  child: const SizedBox.expand(),",
            f"{pad})",
        ])

    return "\n".join([
        "ColoredBox(",
        f"{pad}  color: {background},",
        f"{pad}  child: Center(",
        f"{pad}    child: Image.asset(",
        f"{pad}      {dart_string(icon_asset)},",
        f"{pad}      width: 132,",
        f"{pad}      height: 132,",
        f"{pad}    ),",
        f"{pad}  ),",
        f"{pad})",
    ])


def _splash_background(config: AppConfig, indent: int) -> str:
    """The colour under the icon, chosen by the phone's own dark-mode setting.

    platformBrightnessOf rather than Theme.of: the splash is painted before the
    app's own theme has settled, and the question is what the phone is doing,
    not what the app decided.
    """
    pad = " " * indent
    light = dart_color(config.splash_bg_light, DEFAULT_SPLASH_LIGHT)
    dark = dart_color(config.splash_bg_dark, DEFAULT_SPLASH_DARK)
    if light == dark:
        return light
    return "\n".join([
        "MediaQuery.platformBrightnessOf(context) == Brightness.dark",
        f"{pad}      ? {dark}",
        f"{pad}      : {light}",
    ])
