"""Small helpers shared by everything that emits generated source.

Nothing here writes a whole file. `dart_string` is the one that matters: any
value originating from user input has to go through it, because it produces a
correctly escaped Dart literal and nothing else in this package escapes
anything.
"""

from __future__ import annotations

import json
import re

from ..config import AppConfig


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
