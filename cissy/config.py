"""The app manifest — the contract between the UI, the generator and the build.

Schema and validation rules are lifted from the desktop builder's
`BuilderProject` so that a project means the same thing in both products.

Signing passwords are absent by design. Only the keystore filename and the key
alias are stored; the passwords are supplied per build and never written here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .errors import ValidationError

SCHEMA_VERSION = 1

# Kept identical to the desktop builder's feature names so a manifest copied
# between the two products keeps its meaning.
FEATURES = (
    "File upload",
    "Downloads",
    "Native sharing",
    "Pull to refresh",
    "Camera",
    "Location",
    "Deep links",
)

EXTERNAL_LINK_BEHAVIOURS = ("browser", "webview", "block")

# Two or more dot-separated segments, each starting with a letter. Matches what
# both Gradle and the Play console will accept.
_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_VERSION_NAME_RE = re.compile(r"^\d+(\.\d+){0,2}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Turn an app name into an id usable as a directory name and a URL path."""
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "app"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AppConfig:
    """Everything needed to generate and build one app."""

    id: str
    name: str
    website_url: str
    android_package_id: str
    ios_bundle_id: str
    app_name: str = ""
    allowed_domains: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    require_https: bool = True
    external_link_behavior: str = "browser"
    javascript_enabled: bool = True
    dom_storage_enabled: bool = True
    cache_enabled: bool = True
    offline_fallback_enabled: bool = True
    custom_user_agent: str | None = None
    version_name: str = "1.0.0"
    version_code: int = 1
    # Filename only, relative to the app's assets directory. Never an absolute
    # path — the server owns where things live.
    icon_file: str | None = None
    splash_file: str | None = None
    keystore_file: str | None = None
    key_alias: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def display_name(self) -> str:
        return self.app_name or self.name

    @property
    def has_signing(self) -> bool:
        """Whether a build can produce something Play will accept.

        Passwords are not part of this: they are supplied per build. This only
        says the durable half of the configuration is present.
        """
        return bool(self.keystore_file) and bool(self.key_alias)

    def next_version_code(self) -> int:
        return self.version_code + 1

    # ── serialisation ────────────────────────────────────────────────────

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "app_name": self.app_name,
            "website_url": self.website_url,
            "android_package_id": self.android_package_id,
            "ios_bundle_id": self.ios_bundle_id,
            "allowed_domains": list(self.allowed_domains),
            "features": list(self.features),
            "require_https": self.require_https,
            "external_link_behavior": self.external_link_behavior,
            "javascript_enabled": self.javascript_enabled,
            "dom_storage_enabled": self.dom_storage_enabled,
            "cache_enabled": self.cache_enabled,
            "offline_fallback_enabled": self.offline_fallback_enabled,
            "custom_user_agent": self.custom_user_agent,
            "version_name": self.version_name,
            "version_code": self.version_code,
            "icon_file": self.icon_file,
            "splash_file": self.splash_file,
            # Note the absence of any password field. See the module docstring.
            "keystore_file": self.keystore_file,
            "key_alias": self.key_alias,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "AppConfig":
        version = data.get("schema_version", SCHEMA_VERSION)
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            raise ValidationError(
                f"This project was written by a newer version of Cissy "
                f"(schema {version}). Update the server before opening it."
            )
        return cls(
            id=_text(data, "id", required=True),
            name=_text(data, "name", required=True),
            app_name=_text(data, "app_name"),
            website_url=_text(data, "website_url", required=True),
            android_package_id=_text(data, "android_package_id", required=True),
            ios_bundle_id=_text(data, "ios_bundle_id", required=True),
            allowed_domains=tuple(_str_list(data, "allowed_domains")),
            features=tuple(_str_list(data, "features")),
            require_https=_flag(data, "require_https", True),
            external_link_behavior=_text(data, "external_link_behavior") or "browser",
            javascript_enabled=_flag(data, "javascript_enabled", True),
            dom_storage_enabled=_flag(data, "dom_storage_enabled", True),
            cache_enabled=_flag(data, "cache_enabled", True),
            offline_fallback_enabled=_flag(data, "offline_fallback_enabled", True),
            custom_user_agent=_optional_text(data, "custom_user_agent"),
            version_name=_text(data, "version_name") or "1.0.0",
            version_code=_positive_int(data, "version_code", 1),
            icon_file=_optional_text(data, "icon_file"),
            splash_file=_optional_text(data, "splash_file"),
            keystore_file=_optional_text(data, "keystore_file"),
            key_alias=_optional_text(data, "key_alias"),
            created_at=_text(data, "created_at") or utc_now(),
            updated_at=_text(data, "updated_at") or utc_now(),
        )

    # ── editing ──────────────────────────────────────────────────────────

    def updated(self, **changes: Any) -> "AppConfig":
        """Return a copy with `changes` applied and `updated_at` refreshed."""
        return replace(self, **changes, updated_at=utc_now())


def normalise(config: AppConfig) -> AppConfig:
    """Clean up user input without rejecting it.

    Runs before validation so that a trailing space or an upper-case domain is
    silently fixed rather than reported as an error.
    """
    domains = []
    for domain in config.allowed_domains:
        cleaned = domain.strip().lower()
        if cleaned and cleaned not in domains:
            domains.append(cleaned)

    features = [f for f in FEATURES if f in set(config.features)]

    agent = (config.custom_user_agent or "").strip()

    return replace(
        config,
        name=config.name.strip(),
        app_name=config.app_name.strip(),
        website_url=config.website_url.strip(),
        android_package_id=config.android_package_id.strip().lower(),
        ios_bundle_id=config.ios_bundle_id.strip(),
        allowed_domains=tuple(domains),
        features=tuple(features),
        custom_user_agent=agent or None,
        key_alias=(config.key_alias or "").strip() or None,
    )


def validate(config: AppConfig) -> None:
    """Raise ValidationError on anything that would produce a broken app.

    Checked here rather than at build time so the problem surfaces while the
    user is looking at the field, not three minutes into Gradle.
    """
    if not config.name:
        raise ValidationError("The project needs a name.")

    if not config.display_name:
        raise ValidationError("The app needs a name to show under its icon.")

    _validate_url(config.website_url, config.require_https)

    if not _PACKAGE_RE.match(config.android_package_id):
        raise ValidationError(
            "The Android package ID must be lower-case, dot-separated and have "
            "at least two parts — for example com.cissytech.portal.",
            detail=config.android_package_id,
        )

    if not _PACKAGE_RE.match(config.ios_bundle_id.lower()):
        raise ValidationError(
            "The iOS bundle ID must be dot-separated with at least two parts — "
            "for example com.cissytech.portal.",
            detail=config.ios_bundle_id,
        )

    if config.external_link_behavior not in EXTERNAL_LINK_BEHAVIOURS:
        raise ValidationError(
            "External links must be set to open in the browser, stay in the "
            "app, or be blocked."
        )

    unknown = sorted(set(config.features) - set(FEATURES))
    if unknown:
        raise ValidationError(f"Unknown feature: {', '.join(unknown)}.")

    if not _VERSION_NAME_RE.match(config.version_name):
        raise ValidationError(
            "The version name should look like 1.4.0.",
            detail=config.version_name,
        )

    if config.version_code < 1:
        raise ValidationError("The version code must be 1 or higher.")


def _validate_url(value: str, require_https: bool) -> None:
    if not value:
        raise ValidationError("The website URL is required.")

    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError(
            "The website URL must be a full address starting with http:// or "
            "https://.",
            detail=value,
        )
    if require_https and parsed.scheme != "https":
        raise ValidationError(
            "This app requires HTTPS. Use an https:// URL, or turn off "
            '"Require HTTPS" if the site genuinely has no certificate.',
            detail=value,
        )


# ── json helpers ─────────────────────────────────────────────────────────
# Tolerant readers: a wrong type is treated as absent rather than crashing, so
# a hand-edited config.json degrades to defaults instead of a 500.


def _text(data: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if required:
        raise ValidationError(f'The project file is missing "{key}".')
    return ""


def _optional_text(data: dict[str, Any], key: str) -> str | None:
    return _text(data, key) or None


def _flag(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key)
    return value if isinstance(value, bool) else default


def _positive_int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return default


def _str_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]
