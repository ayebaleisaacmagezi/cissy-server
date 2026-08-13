"""The app manifest - the contract between the UI, the generator and the build.

Schema and validation rules are lifted from the desktop builder's
`BuilderProject` so that a project means the same thing in both products.

Signing passwords are absent by design. Only the keystore filename and the key
alias are stored; the passwords are supplied per build and never written here.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .errors import ValidationError

SCHEMA_VERSION = 1

# The first seven are kept identical to the desktop builder's feature names so
# a manifest copied between the two products keeps its meaning. The rest are
# native modules this server grew later; the desktop builder ignores them.
FEATURES = (
    "File upload",
    "Downloads",
    "Native sharing",
    "Pull to refresh",
    "Camera",
    "Location",
    "Deep links",
    "Saved items",
    "Settings screen",
)

EXTERNAL_LINK_BEHAVIOURS = ("browser", "webview", "block")

NAV_STYLES = ("none", "bottom")

# What a navigation tab may open besides a page of the website. Each native
# target only exists when its module is enabled, which validate() enforces.
NAV_NATIVE_TARGETS = {
    "native:saved": "Saved items",
    "native:downloads": "Downloads",
    "native:settings": "Settings screen",
}

# Notifications is a native target too, but it is not backed by a feature flag -
# it exists when push is switched on. Kept out of the map above so the
# feature-name lookup there stays honest.
NAV_NOTIFICATIONS = "native:notifications"

# The Material icon names a tab may use. A fixed set because the generated
# Dart maps each name to an IconData at compile time.
NAV_ICONS = (
    "home",
    "storefront",
    "menu_book",
    "article",
    "shopping_bag",
    "event",
    "call",
    "person",
    "bookmark",
    "download",
    "settings",
    "info",
)

_HEX_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")

# Characters a CSS selector never needs, and which would break out of the
# context it is injected into. A selector travels from a text field into a CSS
# rule, inside a JavaScript string, inside a Dart string - so a stray brace
# escapes the rule and a stray quote breaks the generated Dart at compile time,
# three minutes into a build, with a message that mentions none of this.
# Rejected rather than escaped, because nothing legal is being refused.
_SELECTOR_BANNED = frozenset('{};"\'\\<>`')
MAX_HIDE_SELECTORS = 20
MAX_SELECTOR_LENGTH = 120

# Extra paths a navigation tab lights up for. A handful covers the real case -
# /account also meaning /profile and /orders - and a long list is a sign the
# tab wants splitting.
MAX_TAB_MATCHES = 10

# What the app does with a message that arrives while it is open. Android and
# iOS both hand a foreground message to the app instead of showing it, so this
# is a decision somebody has to make rather than a platform default.
PUSH_FOREGROUND = ("notification", "banner", "silent")

# A notification category. The id is the FCM topic the customer's backend sends
# to, so it is far more permanent than the label: published installs subscribe
# by id, and renaming one strands every device already subscribed.
_TOPIC_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
MAX_PUSH_TOPICS = 12

DEFAULT_PUSH_PROMPT_TITLE = "Stay updated"
DEFAULT_PUSH_PROMPT_BODY = (
    "Get told when something happens that you care about. You can turn this "
    "off at any time."
)

# A class name the app adds to <body>, and a query parameter appended to the
# entry URL. Both are narrow on purpose: they are written into a page this
# server does not own.
_BODY_CLASS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
_URL_FLAG_RE = re.compile(r"^[a-z][a-z0-9_]*=[a-z0-9_.-]+$")

# Written into Info.plist. Vague reasons are a common App Store rejection, so
# these name the app and say what the permission is actually for.
DEFAULT_PERMISSION_REASONS = {
    "camera": "{app} uses the camera so you can take and upload photos.",
    "photos": "{app} needs access to your photos so you can upload them.",
    "location": "{app} uses your location to show information relevant to where you are.",
}

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
    # The developer's own offline screen, as a self-contained HTML document.
    # Empty means the built-in Material screen. Baked into the app as an asset
    # at generate time; a link to app://retry is the screen's Try-again button.
    offline_custom_html: str = ""
    # The client's brand colour, seeded into the generated app's whole theme.
    # Empty means the neutral grey default - never this product's own blue.
    theme_color: str = ""
    nav_style: str = "none"
    # Each tab: {"label": ..., "icon": <NAV_ICONS>, "target": url | /path |
    # native:*, "match": [<path>, ...]}. Values are not all strings - "match"
    # is a list - which is why the annotation is Any.
    nav_tabs: tuple[dict[str, Any], ...] = ()
    # Taking the website's own navigation down, so a native bar does not simply
    # sit underneath it. Three mechanisms in descending order of reliability:
    # CSS the app injects, a class the app adds to <body> for the site's own
    # stylesheet to key off, and a query parameter on the entry URL. All three
    # apply only on an allowed host - the app is not entitled to restyle
    # somebody else's page.
    hide_selectors: tuple[str, ...] = ()
    body_class: str = ""
    url_flag: str = ""
    # Push notifications. The customer owns the Firebase project; this server
    # only ever holds the two client configuration files, which are not
    # secrets - they ship inside the app. The service-account key that can
    # actually send a message never comes here at all.
    push_enabled: bool = False
    # Each topic: {"id": <FCM topic>, "label": ..., "default": bool}
    push_topics: tuple[dict[str, Any], ...] = ()
    push_prompt_title: str = ""
    push_prompt_body: str = ""
    push_foreground: str = "notification"
    # Where the app POSTs its FCM token, for customers whose site cannot run
    # the JavaScript bridge. Empty means the app never phones anywhere.
    push_token_endpoint: str = ""
    firebase_android_file: str | None = None
    firebase_ios_file: str | None = None
    custom_user_agent: str | None = None
    version_name: str = "1.0.0"
    version_code: int = 1
    # Filename only, relative to the app's assets directory. Never an absolute
    # path - the server owns where things live.
    icon_file: str | None = None
    splash_file: str | None = None
    keystore_file: str | None = None
    key_alias: str | None = None
    # Why the app wants each permission, shown by iOS in the system prompt.
    # Apple rejects builds that request a permission without one.
    permission_descriptions: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def permission_description(self, key: str) -> str:
        """The stored reason, or a truthful default naming the app."""
        stored = (self.permission_descriptions or {}).get(key, "").strip()
        if stored:
            return stored
        return DEFAULT_PERMISSION_REASONS[key].format(app=self.display_name)

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

    def next_version_code(self, used: Collection[int] = ()) -> int:
        """The version code the next build should carry.

        Not simply `version_code + 1`. A newly created app has never built
        anything, so incrementing straight away would ship its first build as
        version code 2 and quietly burn 1 - which reads as a bug to anyone
        looking at their first artifact.

        It also leaves a hand-set code alone when nothing has used it, which is
        what someone migrating an existing Play listing needs: they set 47
        because that is where their published app is, and the first build here
        must be 47 rather than 48.

        Once a code has been built under, the next one has to clear every code
        already used, not just the current one - Play rejects a reused code
        even if the config has since been edited downwards.
        """
        if self.version_code not in used:
            return self.version_code
        return max(used) + 1

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
            "offline_custom_html": self.offline_custom_html,
            "theme_color": self.theme_color,
            "nav_style": self.nav_style,
            "nav_tabs": [
                {**tab, "match": list(tab.get("match") or ())}
                for tab in self.nav_tabs
            ],
            "hide_selectors": list(self.hide_selectors),
            "body_class": self.body_class,
            "url_flag": self.url_flag,
            "push_enabled": self.push_enabled,
            "push_topics": [dict(topic) for topic in self.push_topics],
            "push_prompt_title": self.push_prompt_title,
            "push_prompt_body": self.push_prompt_body,
            "push_foreground": self.push_foreground,
            "push_token_endpoint": self.push_token_endpoint,
            "firebase_android_file": self.firebase_android_file,
            "firebase_ios_file": self.firebase_ios_file,
            "custom_user_agent": self.custom_user_agent,
            "version_name": self.version_name,
            "version_code": self.version_code,
            "icon_file": self.icon_file,
            "splash_file": self.splash_file,
            # Note the absence of any password field. See the module docstring.
            "keystore_file": self.keystore_file,
            "key_alias": self.key_alias,
            "permission_descriptions": dict(self.permission_descriptions or {}),
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
            offline_custom_html=_text(data, "offline_custom_html"),
            theme_color=_text(data, "theme_color"),
            nav_style=_text(data, "nav_style") or "none",
            nav_tabs=_tab_list(data, "nav_tabs"),
            hide_selectors=tuple(_str_list(data, "hide_selectors")),
            body_class=_text(data, "body_class"),
            url_flag=_text(data, "url_flag"),
            push_enabled=_flag(data, "push_enabled", False),
            push_topics=_topic_list(data, "push_topics"),
            push_prompt_title=_text(data, "push_prompt_title"),
            push_prompt_body=_text(data, "push_prompt_body"),
            push_foreground=_text(data, "push_foreground") or "notification",
            push_token_endpoint=_text(data, "push_token_endpoint"),
            firebase_android_file=_optional_text(data, "firebase_android_file"),
            firebase_ios_file=_optional_text(data, "firebase_ios_file"),
            custom_user_agent=_optional_text(data, "custom_user_agent"),
            version_name=_text(data, "version_name") or "1.0.0",
            version_code=_positive_int(data, "version_code", 1),
            icon_file=_optional_text(data, "icon_file"),
            splash_file=_optional_text(data, "splash_file"),
            keystore_file=_optional_text(data, "keystore_file"),
            key_alias=_optional_text(data, "key_alias"),
            permission_descriptions=_str_map(data, "permission_descriptions"),
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

    # Selectors keep their case: `.mobileNav` and `.mobilenav` are different
    # elements to a browser, so lower-casing them the way domains are would
    # quietly stop them matching.
    selectors = []
    for selector in config.hide_selectors:
        cleaned = selector.strip()
        if cleaned and cleaned not in selectors:
            selectors.append(cleaned)

    topics = []
    seen_topics = set()
    for topic in config.push_topics:
        identifier = str(topic.get("id", "")).strip().lower()
        label = str(topic.get("label", "")).strip()
        if not identifier and not label:
            continue
        if identifier in seen_topics:
            continue
        seen_topics.add(identifier)
        topics.append({
            "id": identifier,
            "label": label,
            "default": bool(topic.get("default", False)),
        })

    style = config.nav_style.strip().lower() or "none"
    tabs = []
    if style != "none":
        for tab in config.nav_tabs:
            matches = []
            for match in tab.get("match") or ():
                path = str(match).strip()
                if path and path not in matches:
                    matches.append(path)
            cleaned = {
                "label": str(tab.get("label", "")).strip(),
                "icon": str(tab.get("icon", "")).strip().lower(),
                "target": str(tab.get("target", "")).strip(),
                "match": matches,
            }
            if cleaned["label"] or cleaned["target"]:
                tabs.append(cleaned)

    return replace(
        config,
        name=config.name.strip(),
        app_name=config.app_name.strip(),
        website_url=config.website_url.strip(),
        android_package_id=config.android_package_id.strip().lower(),
        ios_bundle_id=config.ios_bundle_id.strip(),
        allowed_domains=tuple(domains),
        features=tuple(features),
        offline_custom_html=config.offline_custom_html.strip(),
        theme_color=config.theme_color.strip().lower(),
        nav_style=style,
        nav_tabs=tuple(tabs),
        hide_selectors=tuple(selectors),
        body_class=config.body_class.strip().lower(),
        url_flag=config.url_flag.strip().lower().lstrip("?&"),
        push_topics=tuple(topics),
        push_prompt_title=config.push_prompt_title.strip(),
        push_prompt_body=config.push_prompt_body.strip(),
        push_foreground=config.push_foreground.strip().lower() or "notification",
        push_token_endpoint=config.push_token_endpoint.strip(),
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
            "at least two parts - for example com.example.app.",
            detail=config.android_package_id,
        )

    if not _PACKAGE_RE.match(config.ios_bundle_id.lower()):
        raise ValidationError(
            "The iOS bundle ID must be dot-separated with at least two parts - "
            "for example com.example.app.",
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

    if config.theme_color and not _HEX_COLOR_RE.match(config.theme_color):
        raise ValidationError(
            "The theme colour must be a six-digit hex value like #b3561d.",
            detail=config.theme_color,
        )

    # The screen ships inside every APK, so a pasted photo-heavy page would
    # quietly bloat the app. Inline SVG artwork fits comfortably under this.
    if len(config.offline_custom_html) > 200_000:
        raise ValidationError(
            "The custom offline screen is too large - keep it under 200 KB. "
            "Use inline SVG artwork rather than embedded photos."
        )

    if config.nav_style not in NAV_STYLES:
        raise ValidationError(
            "Navigation must be set to none or bottom.", detail=config.nav_style
        )

    if config.nav_style != "none":
        _validate_nav_tabs(config)

    _validate_website_nav(config)

    if config.push_enabled:
        _validate_push(config)

    if not _VERSION_NAME_RE.match(config.version_name):
        raise ValidationError(
            "The version name should look like 1.4.0.",
            detail=config.version_name,
        )

    if config.version_code < 1:
        raise ValidationError("The version code must be 1 or higher.")


def _validate_push(config: AppConfig) -> None:
    """Check the notification settings. Only runs when push is switched on.

    The Firebase files are deliberately not required here. Somebody switches
    push on and then goes to Firebase to create the project, so demanding the
    file at this point would refuse the save that records the decision. The
    build is where a missing file becomes an error, because that is the first
    moment it would produce a broken app.
    """
    if config.push_foreground not in PUSH_FOREGROUND:
        raise ValidationError(
            "A notification arriving while the app is open must show a "
            "notification, show a bar inside the app, or do nothing visible.",
            detail=config.push_foreground,
        )

    if len(config.push_topics) > MAX_PUSH_TOPICS:
        raise ValidationError(
            f"That is {len(config.push_topics)} notification categories. Keep "
            f"it to {MAX_PUSH_TOPICS} - the list is a screen your users read."
        )

    for topic in config.push_topics:
        identifier = str(topic.get("id", ""))
        label = str(topic.get("label", ""))
        if not label:
            raise ValidationError("Every notification category needs a name.")
        if not _TOPIC_ID_RE.match(identifier):
            raise ValidationError(
                f'The topic for "{label}" must start with a letter and use only '
                f"lower-case letters, digits, hyphens and underscores - it is "
                f"what your backend sends to.",
                detail=identifier,
            )

    for value, what in (
        (config.push_prompt_title, "heading"),
        (config.push_prompt_body, "explanation"),
    ):
        if len(value) > 300:
            raise ValidationError(
                f"The notification prompt {what} is too long to fit on a phone "
                f"screen. Keep it under 300 characters."
            )

    if config.push_token_endpoint:
        parsed = urlparse(config.push_token_endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValidationError(
                "The push token endpoint must be an https:// address. The app "
                "posts a device token to it, which is not something to send "
                "over plain HTTP.",
                detail=config.push_token_endpoint,
            )


def _validate_website_nav(config: AppConfig) -> None:
    """Check what the app will inject into a page this server does not own."""
    if len(config.hide_selectors) > MAX_HIDE_SELECTORS:
        raise ValidationError(
            f"That is {len(config.hide_selectors)} selectors to hide. Keep it "
            f"to {MAX_HIDE_SELECTORS} - past that it is usually the page that "
            f"needs a different approach, not a longer list."
        )

    for selector in config.hide_selectors:
        if len(selector) > MAX_SELECTOR_LENGTH:
            raise ValidationError(
                f"That selector is {len(selector)} characters. Keep each one "
                f"under {MAX_SELECTOR_LENGTH}.",
                detail=selector,
            )
        bad = sorted(_SELECTOR_BANNED.intersection(selector))
        if bad:
            raise ValidationError(
                f"A selector cannot contain {' '.join(bad)}. Write it the way "
                f"you would in a stylesheet - \".mobile-nav\" or \"#footer .menu\" - "
                f"with no braces and no rule body.",
                detail=selector,
            )
        if any(character < " " or character == "\x7f" for character in selector):
            raise ValidationError(
                "A selector must be a single line.", detail=selector
            )

    if config.body_class and not _BODY_CLASS_RE.match(config.body_class):
        raise ValidationError(
            "The body class must start with a letter and use only lower-case "
            "letters, digits, hyphens and underscores - for example "
            "web2app-native.",
            detail=config.body_class,
        )

    if config.url_flag and not _URL_FLAG_RE.match(config.url_flag):
        raise ValidationError(
            "The entry URL flag must be a single name=value pair, with no "
            "question mark - for example source=web2app.",
            detail=config.url_flag,
        )


def _validate_nav_tabs(config: AppConfig) -> None:
    if not 2 <= len(config.nav_tabs) <= 5:
        raise ValidationError("Bottom navigation needs between 2 and 5 tabs.")

    features = set(config.features)
    web_tabs = 0
    for tab in config.nav_tabs:
        label = tab.get("label", "")
        target = tab.get("target", "")
        icon = tab.get("icon", "")
        if not label:
            raise ValidationError("Every navigation tab needs a label.")
        if len(label) > 14:
            raise ValidationError(
                "Tab labels must be 14 characters or fewer so they fit "
                "under the icon.",
                detail=label,
            )
        if icon and icon not in NAV_ICONS:
            raise ValidationError(f'Unknown tab icon "{icon}".')

        matches = tab.get("match") or ()
        if matches and (target in NAV_NATIVE_TARGETS or target == NAV_NOTIFICATIONS):
            raise ValidationError(
                f'The "{label}" tab opens a native screen, so it cannot also '
                f"light up for pages of the website."
            )
        if len(matches) > MAX_TAB_MATCHES:
            raise ValidationError(
                f'The "{label}" tab lists {len(matches)} pages to light up '
                f"for. Keep it to {MAX_TAB_MATCHES}."
            )
        for match in matches:
            if not match.startswith("/"):
                raise ValidationError(
                    f'"{match}" must be a path starting with / - it is matched '
                    f"against the page being shown, not against a full URL.",
                    detail=label,
                )

        if target == NAV_NOTIFICATIONS:
            if not config.push_enabled:
                raise ValidationError(
                    f'The "{label}" tab opens the notifications screen, but '
                    f"push notifications are switched off. Turn them on or "
                    f"remove the tab."
                )
        elif target in NAV_NATIVE_TARGETS:
            needed = NAV_NATIVE_TARGETS[target]
            if needed not in features:
                raise ValidationError(
                    f'The "{label}" tab opens the {needed} screen, but that '
                    f"module is switched off. Enable it or remove the tab."
                )
        elif target.startswith("/"):
            web_tabs += 1
        elif target.startswith(("http://", "https://")):
            _validate_url(target, config.require_https)
            web_tabs += 1
        else:
            raise ValidationError(
                f'The "{label}" tab must open a full URL, a path starting '
                f"with /, or a native screen.",
                detail=target,
            )
    if web_tabs == 0:
        raise ValidationError(
            "At least one navigation tab must open the website - otherwise "
            "the app has no web content at all."
        )


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


def _str_map(data: dict[str, Any], key: str) -> dict[str, str]:
    value = data.get(key)
    if not isinstance(value, dict):
        return {}
    return {
        str(k): v.strip()
        for k, v in value.items()
        if isinstance(v, str) and v.strip()
    }


def _str_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _topic_list(data: dict[str, Any], key: str) -> tuple[dict[str, Any], ...]:
    value = data.get(key)
    if not isinstance(value, list):
        return ()
    topics = []
    for item in value:
        if isinstance(item, dict):
            topics.append(
                {
                    "id": str(item.get("id", "")),
                    "label": str(item.get("label", "")),
                    "default": bool(item.get("default", False)),
                }
            )
    return tuple(topics)


def _tab_list(data: dict[str, Any], key: str) -> tuple[dict[str, Any], ...]:
    value = data.get(key)
    if not isinstance(value, list):
        return ()
    tabs = []
    for item in value:
        if isinstance(item, dict):
            # "match" is the one value on a tab that is not a string. Coercing
            # it the way the others are would turn ["/account"] into the string
            # "['/account']", which matches nothing, forever, and says nothing
            # about why.
            raw = item.get("match")
            matches = (
                [m.strip() for m in raw if isinstance(m, str) and m.strip()]
                if isinstance(raw, list)
                else []
            )
            tabs.append(
                {
                    "label": str(item.get("label", "")),
                    "icon": str(item.get("icon", "")),
                    "target": str(item.get("target", "")),
                    "match": matches,
                }
            )
    return tuple(tabs)
