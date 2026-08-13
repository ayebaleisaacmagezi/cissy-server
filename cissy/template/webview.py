"""`main.dart` - the WebView screen, and the code that assembles the file.

`main_dart` is the composer. It works out which blocks the configured features
require and concatenates them, which is why almost everything else in this
module is a fragment rather than a whole widget.

Feature flags emit or omit whole blocks, so anything referenced by an emitted
block must itself be emitted, or the generated project fails its own lint run -
the `_load` and `_is_allowed_web_uri` conditions below exist for exactly that.

Written as plain string concatenation rather than a templating engine, because
Dart uses `$` for interpolation and `{}` for blocks, and every engine wants one
or both of those.
"""

from __future__ import annotations

import json

from ..config import AppConfig
from .common import _dart_bool, dart_string, deep_link_scheme, with_url_flag
from . import push
from .nav import _root_shell, has_tab_echo
from .screens import (
    _DOWNLOADS_SCREEN,
    _LIST_HELPERS,
    _SAVED_SCREEN,
    _SAVED_STORE,
    _SETTINGS_SCREEN,
)


# ── main.dart ────────────────────────────────────────────────────────────


def main_dart(
    config: AppConfig,
    splash_asset: str | None = None,
    offline_asset: str | None = None,
) -> str:
    features = set(config.features)
    has_share = "Native sharing" in features
    has_location = "Location" in features
    has_downloads = "Downloads" in features
    has_deep_links = "Deep links" in features
    has_pull_refresh = "Pull to refresh" in features
    has_camera = "Camera" in features
    has_uploads = "File upload" in features
    has_saved = "Saved items" in features
    has_settings = "Settings screen" in features
    has_fallback = config.offline_fallback_enabled
    has_custom_offline = has_fallback and bool(offline_asset)
    has_bridge = has_share or has_location or config.push_enabled
    has_policy = has_site_policy(config)
    has_nav = config.nav_style == "bottom" and len(config.nav_tabs) >= 2
    has_echo = has_nav and has_tab_echo(config)
    has_push = config.push_enabled
    has_inbox = has_push and has_nav and push.has_inbox(config)
    has_banner = has_push and config.push_foreground == "banner"
    has_downloads_screen = has_nav and has_downloads and any(
        tab.get("target") == "native:downloads" for tab in config.nav_tabs
    )

    out: list[str] = []
    add = out.append

    # ── imports ──
    add("import 'dart:async';")
    if has_policy:
        # UnmodifiableListView, for the user scripts the WebView is built with.
        add("import 'dart:collection';")
    if has_saved or has_push:
        add("import 'dart:convert';")
    if has_downloads or has_push:
        add("import 'dart:io';")
    add("")
    add("import 'package:flutter/material.dart';")
    add("import 'package:flutter/services.dart';")
    add("import 'package:flutter_inappwebview/flutter_inappwebview.dart';")
    add("import 'package:url_launcher/url_launcher.dart';")
    if has_share:
        add("import 'package:share_plus/share_plus.dart';")
    if has_location:
        add("import 'package:geolocator/geolocator.dart';")
    if has_downloads:
        add("import 'package:open_filex/open_filex.dart';")
        add("import 'package:path_provider/path_provider.dart';")
    if has_deep_links:
        add("import 'package:app_links/app_links.dart';")
    if has_fallback:
        add("import 'package:connectivity_plus/connectivity_plus.dart';")
    if has_push:
        for line in push.imports(config):
            add(line)
    if has_saved or has_push:
        add("import 'package:shared_preferences/shared_preferences.dart';")
    add("")

    # ── constants ──
    domains = [d.lower() for d in config.allowed_domains]
    add(f"const appTitle = {dart_string(config.display_name)};")
    add(f"const appVersion = {dart_string(config.version_name)};")
    add(f"const homeUrl = {dart_string(with_url_flag(config.website_url, config.url_flag))};")
    add(f"const allowedDomains = <String>{json.dumps(domains)};")
    add(f"const requireHttps = {_dart_bool(config.require_https)};")
    add(f"const externalLinkBehavior = {dart_string(config.external_link_behavior)};")
    if has_deep_links:
        add(f"const deepLinkScheme = {dart_string(deep_link_scheme(config))};")
    if has_push:
        add(f"const androidPackageId = {dart_string(config.android_package_id)};")
    add("")

    if has_push:
        add(push.constants(config))
        if has_banner:
            add("/// So a message arriving with the app open can put a bar up")
            add("/// from outside the widget tree.")
            add("final pushMessengerKey = GlobalKey<ScaffoldMessengerState>();")
            add("")

    if has_policy:
        add(_site_policy_script(config))

    if has_fallback:
        add(_OFFLINE_ERROR_TYPES)

    # ── entry point ──
    add(_main_function(has_push=has_push))
    add(_app_widget(config, has_nav=has_nav, has_banner=has_banner))

    if has_push:
        add(push.BACKGROUND_HANDLER)
        add(push.router(config))
        add(push.inbox(config))
        add(push.service(config))
        add(push.PROMPT_SHEET)

    if has_nav:
        add(_root_shell(config, splash_asset=splash_asset))

    if has_downloads:
        add(_DOWNLOADS_DIRECTORY)

    add(_screen_header(has_nav=has_nav, has_echo=has_echo))

    if has_fallback:
        add(
            "  StreamSubscription<List<ConnectivityResult>>?"
            " connectivitySubscription;\n"
            "  String? errorTitle;\n"
            "  String? errorMessage;\n"
            "  String? currentUrl;\n"
            "  bool errorIsOffline = false;\n"
            "  // Whether a retry is in flight, and whether the current load has\n"
            "  // already failed. Together they keep the error screen up until a\n"
            "  // page genuinely arrives - dropping it sooner flashes the broken\n"
            "  // page underneath for the length of a failed attempt.\n"
            "  bool retrying = false;\n"
            "  bool loadFailed = false;"
        )
    if has_push:
        add(
            "  // Whether the app has already put its own notification\n"
            "  // explanation up. Asked once per run at most, and only after a\n"
            "  // page has actually loaded."
        )
        add("  bool pushPromptShown = false;")
    add("")

    # ── initState ──
    add("  @override")
    add("  void initState() {")
    add("    super.initState();")
    if has_pull_refresh:
        add("    pullToRefreshController = PullToRefreshController(")
        add("      onRefresh: () async => controller?.reload(),")
        add("    );")
    if has_deep_links:
        if has_nav:
            add("    if (widget.primary) {")
            add("      _listenForDeepLinks();")
            add("    }")
        else:
            add("    _listenForDeepLinks();")
    if has_fallback:
        add("    connectivitySubscription =")
        add("        Connectivity().onConnectivityChanged.listen(_onConnectivityChanged);")
    if has_push:
        indent = "      " if has_nav else "    "
        if has_nav:
            add("    if (widget.primary) {")
        add(f"{indent}pendingPushUrl.addListener(_openPushTarget);")
        add(f"{indent}// A tap that started the app from cold is already waiting.")
        add(f"{indent}_openPushTarget();")
        if has_nav:
            add("    }")
    add("  }")
    add("")

    if has_fallback:
        add(_CONNECTIVITY_CHANGED)

    if has_deep_links:
        add(_DEEP_LINKS)

    if has_push:
        add(_PUSH_HOOKS)
    add(_LOAD_TIMEOUT)
    add(_finish_load(has_nav, has_fallback))
    add(_finish_failed_load(has_fallback, has_nav))

    if has_fallback:
        add(_FAILURE_CLASSIFIERS)

    add(_ALLOWED_HOST)

    if has_deep_links:
        add(_ALLOWED_WEB_URI)

    add(_DECIDE_NAVIGATION)

    # `_load` is only reachable from deep links, the retry buttons, or the
    # shell's cross-tab navigation. Emitting it otherwise leaves dead code,
    # which fails the generated project's own flutter_lints run.
    if has_deep_links or has_fallback or has_nav or has_push:
        add(_load_method(has_fallback))

    if has_nav:
        add(_SHELL_BACK_HELPERS)
    else:
        add(_HANDLE_BACK)

    add(_page_policies(config, has_uploads=has_uploads))

    if has_bridge:
        add(_bridge(has_share, has_location, has_push))

    if has_downloads:
        add(_DOWNLOAD)

    if has_saved and has_nav:
        add(_SAVE_CURRENT_PAGE)

    add(_SHOW_MESSAGE)
    add(
        _build_method(
            config,
            splash_asset=splash_asset,
            has_fallback=has_fallback,
            has_bridge=has_bridge,
            has_downloads=has_downloads,
            has_camera=has_camera,
            has_nav=has_nav,
            has_saved=has_saved,
            has_share=has_share,
            has_custom_offline=has_custom_offline,
            has_policy=has_policy,
            has_echo=has_echo,
            has_push=has_push,
        )
    )
    add(_dispose(has_fallback, has_push, has_nav))

    if has_custom_offline:
        add(_custom_error_view(offline_asset, config.theme_color))
    elif has_fallback:
        add(_ERROR_VIEW)

    if has_saved:
        add(_SAVED_STORE)
        add(_SAVED_SCREEN)

    if has_downloads_screen:
        add(_DOWNLOADS_SCREEN)

    if has_settings:
        add(_SETTINGS_SCREEN)

    if has_push:
        add(push.SETTINGS_SCREEN)
        add(push.INBOX_SCREEN)

    if has_saved or has_downloads_screen or has_push:
        add(_LIST_HELPERS)

    return "\n".join(out).rstrip() + "\n"


_OFFLINE_ERROR_TYPES = """\
/// Error values that mean the device could not reach the network at all, as
/// opposed to the server answering with a failure. The two need different
/// advice: one is "check your connection", the other is not.
const offlineErrorTypes = <String>{
  'NOT_CONNECTED_TO_INTERNET',
  'HOST_LOOKUP',
  'CANNOT_CONNECT_TO_HOST',
  'NETWORK_CONNECTION_LOST',
  'SERVER_UNREACHABLE',
  'CANNOT_LOAD_FROM_NETWORK',
  'TIMEOUT',
};
"""


def _main_function(*, has_push: bool) -> str:
    """The entry point.

    With push on this becomes async, because two things have to finish before
    the first frame: Firebase itself, and reading the notification that started
    the app when it was not running. Doing either after runApp means the tap
    that opened the app is lost.
    """
    head = "Future<void> main() async {" if has_push else "void main() {"
    setup = (
        "  await Firebase.initializeApp();\n  await PushService.initialise();\n"
        if has_push
        else ""
    )
    return (
        f"{head}\n"
        "  WidgetsFlutterBinding.ensureInitialized();\n"
        "  SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);\n"
        "  SystemChrome.setSystemUIOverlayStyle(const SystemUiOverlayStyle(\n"
        "    statusBarColor: Colors.transparent,\n"
        "    systemNavigationBarColor: Colors.transparent,\n"
        "    statusBarIconBrightness: Brightness.dark,\n"
        "    systemNavigationBarIconBrightness: Brightness.dark,\n"
        "  ));\n"
        f"{setup}"
        "  runApp(const GeneratedWebViewApp());\n"
        "}\n"
    )


def _app_widget(config: AppConfig, *, has_nav: bool, has_banner: bool = False) -> str:
    # The seed is the client's brand colour; the neutral grey default exists so
    # an app with no colour set still looks intentional rather than half-themed.
    seed = (
        f"const Color(0xFF{config.theme_color[1:].upper()})"
        if config.theme_color
        else "Colors.grey"
    )
    home = "RootShell" if has_nav else "WebViewScreen"
    # A message arriving with the app open puts its bar up from the push
    # service, which has no BuildContext. Without the key attached here the
    # key exists, compiles, and its currentState is null forever.
    messenger = (
        "      scaffoldMessengerKey: pushMessengerKey,\n" if has_banner else ""
    )
    return f"""\
class GeneratedWebViewApp extends StatelessWidget {{
  const GeneratedWebViewApp({{super.key}});

  @override
  Widget build(BuildContext context) {{
    return MaterialApp(
{messenger}      title: appTitle,
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(seedColor: {seed}),
      ),
      home: const {home}(),
    );
  }}
}}
"""


def _screen_header(*, has_nav: bool, has_echo: bool = False) -> str:
    if has_nav:
        constructor = """\
  const WebViewScreen({
    super.key,
    this.initialUrl = homeUrl,
    this.primary = true,
    this.onFirstLoad,
    this.onUrlChanged,
  });

  /// The page this tab opens on. Each web tab is its own WebView, kept alive
  /// by the shell's IndexedStack so switching tabs does not reload anything.
  final String initialUrl;

  /// Only the primary tab listens for deep links and reports the first load
  /// (which is what dismisses the shell's splash).
  final bool primary;
  final VoidCallback? onFirstLoad;

  /// Where this tab has got to, so the shell can light up whichever tab the
  /// page belongs to. Null on tabs the shell has no rules for.
  final ValueChanged<String>? onUrlChanged;
"""
    else:
        constructor = "  const WebViewScreen({super.key});\n"
    return f"""\
class WebViewScreen extends StatefulWidget {{
{constructor}
  @override
  State<WebViewScreen> createState() => _WebViewScreenState();
}}

class _WebViewScreenState extends State<WebViewScreen> {{
  InAppWebViewController? controller;
  PullToRefreshController? pullToRefreshController;
  Timer? loadTimeout;
  StreamSubscription<Uri>? deepLinkSubscription;
  bool showSplash = true;
  double progress = 0;"""


_DEEP_LINKS = """\
  void _listenForDeepLinks() {
    final appLinks = AppLinks();
    deepLinkSubscription = appLinks.uriLinkStream.listen(_handleIncomingLink);
  }

  Future<void> _handleIncomingLink(Uri uri) async {
    final target = uri.scheme == deepLinkScheme
        ? Uri.parse(homeUrl).replace(path: uri.path, query: uri.query)
        : uri;
    if (_isAllowedWebUri(target)) {
      await _load(target);
    }
  }
"""


_LOAD_TIMEOUT = """\
  void _startLoadTimeout() {
    loadTimeout?.cancel();
    loadTimeout = Timer(const Duration(seconds: 20), () {
      if (!mounted || progress >= 1) {
        return;
      }
      _finishFailedLoad(
        title: 'This is taking too long',
        message: 'The site did not respond in time. '
            'Check your connection and try again.',
        offline: true,
      );
    });
  }
"""


def _finish_load(has_nav: bool, has_fallback: bool) -> str:
    notify = "      widget.onFirstLoad?.call();\n" if has_nav else ""
    # The error screen comes down here and only here - on a load that finished
    # without failing. onLoadStop also fires after a failure, which is what the
    # loadFailed guard is for.
    clear = (
        "        if (!loadFailed) {\n"
        "          errorTitle = null;\n"
        "          errorMessage = null;\n"
        "          retrying = false;\n"
        "        }\n"
        if has_fallback
        else ""
    )
    return (
        "  void _finishLoad() {\n"
        "    loadTimeout?.cancel();\n"
        "    pullToRefreshController?.endRefreshing();\n"
        "    if (mounted) {\n"
        "      setState(() {\n"
        "        showSplash = false;\n"
        "        progress = 1;\n"
        f"{clear}"
        "      });\n"
        f"{notify}"
        "    }\n"
        "  }\n"
    )


def _finish_failed_load(has_fallback: bool, has_nav: bool) -> str:
    body = [
        "  void _finishFailedLoad({",
        "    required String title,",
        "    required String message,",
        "    required bool offline,",
        "  }) {",
        "    loadTimeout?.cancel();",
        "    pullToRefreshController?.endRefreshing();",
        "    if (mounted) {",
        "      setState(() {",
        "        showSplash = false;",
        "        progress = 1;",
    ]
    if has_fallback:
        body += [
            "        errorTitle = title;",
            "        errorMessage = message;",
            "        errorIsOffline = offline;",
            "        retrying = false;",
            "        loadFailed = true;",
        ]
    body += ["      });"]
    if has_nav:
        # A failed first load must also lift the shell splash, or an offline
        # launch would sit on the splash forever with the error hidden under it.
        body += ["      widget.onFirstLoad?.call();"]
    body += ["    }", "  }", ""]
    return "\n".join(body)


_CONNECTIVITY_CHANGED = """\
  /// The recovery half of the offline screen: the moment the device is back
  /// on a network, the page that failed reloads by itself.
  void _onConnectivityChanged(List<ConnectivityResult> results) {
    final online = results.any((result) => result != ConnectivityResult.none);
    if (online && errorTitle != null && errorIsOffline) {
      _retry();
    }
  }
"""


_FAILURE_CLASSIFIERS = """\
  /// Turns a transport-level failure into a message a normal user can act on.
  void _failFromError(WebResourceError error) {
    // Compared as a string rather than against the enum constants: those are
    // `static final` in flutter_inappwebview, so a const set of them will not
    // compile, and the string values are stable across package versions.
    final offline = offlineErrorTypes.contains(error.type.toValue());
    _finishFailedLoad(
      title: offline ? 'You appear to be offline' : 'Something went wrong',
      message: offline
          ? 'Check your internet connection and try again.'
          : 'The page could not be loaded. Please try again.',
      offline: offline,
    );
  }

  /// Turns an HTTP status code into a message a normal user can act on.
  void _failFromStatus(int? statusCode) {
    final code = statusCode ?? 0;
    if (code == 404 || code == 410) {
      _finishFailedLoad(
        title: 'Page not found',
        message: 'That page no longer exists. Return to the home page to '
            'keep going.',
        offline: false,
      );
      return;
    }
    if (code == 401 || code == 403) {
      _finishFailedLoad(
        title: 'Access denied',
        message: 'You do not have permission to view this page. Sign in and '
            'try again.',
        offline: false,
      );
      return;
    }
    _finishFailedLoad(
      title: 'Something went wrong',
      message: code >= 500
          ? 'The server is having trouble right now. Please try again in a '
              'moment.'
          : 'The page could not be loaded. Please try again.',
      offline: false,
    );
  }

  Future<void> _retry() async {
    if (retrying) {
      return;
    }
    // Ask the radio before asking the network: with no connectivity at all, a
    // reload is doomed and would only churn. The spinner still acknowledges
    // the tap, then the screen simply stays.
    final results = await Connectivity().checkConnectivity();
    final online = results.any((result) => result != ConnectivityResult.none);
    if (!online) {
      if (!mounted) {
        return;
      }
      setState(() => retrying = true);
      await Future<void>.delayed(const Duration(milliseconds: 600));
      if (mounted) {
        setState(() => retrying = false);
      }
      return;
    }
    await _load(Uri.parse(currentUrl ?? homeUrl));
  }

  void _goHome() {
    _load(Uri.parse(homeUrl));
  }
"""


_ALLOWED_HOST = """\
  bool _isAllowedHost(String host) {
    final normalized = host.toLowerCase();
    return allowedDomains.any(
      (domain) => normalized == domain || normalized.endsWith('.$domain'),
    );
  }
"""


_ALLOWED_WEB_URI = """\
  bool _isAllowedWebUri(Uri uri) {
    if (uri.scheme != 'http' && uri.scheme != 'https') {
      return false;
    }
    if (requireHttps && uri.scheme != 'https') {
      return false;
    }
    return _isAllowedHost(uri.host);
  }
"""


# The behaviour names match config.EXTERNAL_LINK_BEHAVIOURS exactly. They differ
# from the desktop builder's ('inApp' / 'externalBrowser'), so this is one of the
# few places the port is deliberately not a transcription.
_DECIDE_NAVIGATION = """\
  Future<NavigationActionPolicy> _decideNavigation(Uri uri) async {
    if (uri.scheme != 'http' && uri.scheme != 'https') {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
      return NavigationActionPolicy.CANCEL;
    }
    if (requireHttps && uri.scheme != 'https') {
      _showMessage('Blocked insecure HTTP navigation.');
      return NavigationActionPolicy.CANCEL;
    }
    if (_isAllowedHost(uri.host) || externalLinkBehavior == 'webview') {
      return NavigationActionPolicy.ALLOW;
    }
    if (externalLinkBehavior == 'browser') {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    } else {
      _showMessage('Blocked link outside the allowed domains.');
    }
    return NavigationActionPolicy.CANCEL;
  }
"""


def _load_method(has_fallback: bool) -> str:
    # The error screen stays up while the new attempt runs; it is _finishLoad
    # that takes it down, and only on success. `retrying` puts the spinner on
    # the screen's button for the duration.
    spin = (
        "      retrying = errorTitle != null;\n"
        if has_fallback
        else ""
    )
    return (
        "  Future<void> _load(Uri uri) async {\n"
        "    setState(() {\n"
        "      progress = 0;\n"
        f"{spin}"
        "    });\n"
        "    _startLoadTimeout();\n"
        "    await controller?.loadUrl(\n"
        "      urlRequest: URLRequest(url: WebUri(uri.toString())),\n"
        "    );\n"
        "  }\n"
    )


_HANDLE_BACK = """\
  Future<void> _handleBack() async {
    if (await controller?.canGoBack() == true) {
      await controller?.goBack();
      return;
    }
    SystemNavigator.pop();
  }
"""


# With a navigation shell the system back button is the shell's to handle, so
# the web view only exposes its history instead of deciding what back means.
_SHELL_BACK_HELPERS = """\
  Future<bool> canGoBack() async => await controller?.canGoBack() == true;

  Future<void> goBack() async {
    await controller?.goBack();
  }
"""


_SAVE_CURRENT_PAGE = """\
  Future<void> _saveCurrentPage() async {
    final url = (await controller?.getUrl())?.toString();
    if (url == null) {
      return;
    }
    final title = await controller?.getTitle();
    await SavedStore.save(
      title == null || title.isEmpty ? url : title,
      url,
    );
    _showMessage('Saved. Find it in the Saved tab.');
  }
"""


_FILE_INPUT_POLICY = """\
    await controller?.evaluateJavascript(source: \"\"\"
      (() => {
        const disable = (root) => root.querySelectorAll('input[type="file"]')
          .forEach((input) => input.disabled = true);
        disable(document);
        new MutationObserver(() => disable(document)).observe(
          document.documentElement,
          { childList: true, subtree: true },
        );
      })();
    \"\"\");
"""

# Re-applying the site policy after a load has finished. The host is checked
# again in Dart as well as inside the script: a page can navigate between the
# script being installed and this running.
_SITE_POLICY_REAPPLY = """\
    final address = await controller?.getUrl();
    final host =
        address == null ? '' : (Uri.tryParse(address.toString())?.host ?? '');
    if (!_isAllowedHost(host)) {
      return;
    }
    await controller?.evaluateJavascript(source: sitePolicyScript);
"""


def has_site_policy(config: AppConfig) -> bool:
    """Whether anything is injected into the customer's own pages."""
    return bool(config.hide_selectors or config.body_class)


def _site_policy_script(config: AppConfig) -> str:
    """The script that takes the website's own navigation down.

    Emitted once as a Dart constant and used twice: installed as a user script
    that runs at document start, and run again when a load finishes.

    Document start is the half that matters visually. Waiting for the load to
    finish means the site's own navigation bar paints, then disappears, on
    every page - which reads as a bug rather than as a native app. Running it
    again afterwards is for pages that change route without a real navigation,
    where nothing fires but the stylesheet may have been replaced.

    A stylesheet rather than `display:none` per element, so anything the page
    adds later is covered without an observer watching for it. The class on
    <body> does need one, because at document start there is no body yet.
    """
    rules = []
    if config.hide_selectors:
        selector = ", ".join(config.hide_selectors)
        rules.append(f"{selector} {{ display: none !important; }}")
    css = json.dumps("\n".join(rules))
    domains = json.dumps([d.lower() for d in config.allowed_domains])

    out = [
        "/// Injected into the website at document start, and again once a load",
        "/// finishes. See the host check: this runs inside pages the app did not",
        "/// write, so it does nothing at all anywhere but an allowed domain.",
        "const sitePolicyScript = r\"\"\"",
        "(() => {",
        f"  const allowed = {domains};",
        "  const host = (location.hostname || '').toLowerCase();",
        "  const permitted = allowed.some(",
        "    (domain) => host === domain || host.endsWith('.' + domain),",
        "  );",
        "  if (!permitted) return;",
    ]
    if rules:
        out += [
            f"  const css = {css};",
            "  let sheet = document.getElementById('cissy-site-policy');",
            "  if (!sheet) {",
            "    sheet = document.createElement('style');",
            "    sheet.id = 'cissy-site-policy';",
            "    (document.head || document.documentElement).appendChild(sheet);",
            "  }",
            "  if (sheet.textContent !== css) sheet.textContent = css;",
        ]
    if config.body_class:
        out += [
            f"  const marker = {json.dumps(config.body_class)};",
            "  const mark = () => {",
            "    if (!document.body) return false;",
            "    document.body.classList.add(marker);",
            "    return true;",
            "  };",
            "  if (!mark()) {",
            "    new MutationObserver((_, observer) => {",
            "      if (mark()) observer.disconnect();",
            "    }).observe(document.documentElement, { childList: true });",
            "  }",
        ]
    out += ["})();", "\"\"\";", ""]
    return "\n".join(out)


def _page_policies(config: AppConfig, *, has_uploads: bool) -> str:
    """What the app runs against a page once it has finished loading.

    Two kinds of policy, and the difference is who the page belongs to.
    Disabling file inputs is about what this app can do - it ships no picker -
    so it applies to whatever is on screen. Hiding the site's navigation and
    marking <body> are edits to somebody's page, so they stop at the edge of
    the configured domains.
    """
    blocks = []
    if not has_uploads:
        # The picker cannot be disabled at the WebView level, so file inputs
        # are disabled in the page instead. The observer catches inputs added
        # later by scripts, which a one-off pass would miss.
        blocks.append(_FILE_INPUT_POLICY)
    if has_site_policy(config):
        blocks.append(_SITE_POLICY_REAPPLY)

    if not blocks:
        return "  Future<void> _applyPagePolicies() async {}\n"
    return (
        "  Future<void> _applyPagePolicies() async {\n"
        + "\n".join(blocks)
        + "  }\n"
    )


def _bridge(has_share: bool, has_location: bool, has_push: bool = False) -> str:
    parts = ["""\
  /// Only pages on an allowed HTTPS origin may call into the app. Without this
  /// an injected script or a redirect could reach the share sheet and location.
  Future<bool> _bridgeOriginAllowed() async {
    final current = await controller?.getUrl();
    final uri = current == null ? null : Uri.tryParse(current.toString());
    return uri != null && uri.scheme == 'https' && _isAllowedHost(uri.host);
  }

  Future<Object?> _handleBridge(List<dynamic> arguments) async {
    if (!await _bridgeOriginAllowed()) {
      return {'status': 'denied', 'message': 'Origin is not allowed.'};
    }
    final payload = arguments.isNotEmpty && arguments.first is Map
        ? Map<String, dynamic>.from(arguments.first as Map)
        : <String, dynamic>{};
    // The origin check above was awaited, so the widget may already be gone.
    // Handlers below reach for `context`, and using it after an async gap
    // without this guard is both a real crash and an analyzer error in the
    // generated project's own lint run.
    if (!mounted) {
      return {'status': 'denied', 'message': 'The app is no longer visible.'};
    }
    final action = payload['action'];
    if (action == 'ping') {
      return {'status': 'ok'};
    }
"""]
    if has_share:
        parts.append("""\
    if (action == 'share') {
      final box = context.findRenderObject() as RenderBox?;
      await SharePlus.instance.share(
        ShareParams(
          text: (payload['text'] ?? '').toString(),
          sharePositionOrigin: box == null
              ? null
              : box.localToGlobal(Offset.zero) & box.size,
        ),
      );
      return {'status': 'ok'};
    }
""")
    if has_push:
        parts.append("""\
    if (action == 'getPlatform') {
      return {'status': 'ok', 'platform': Platform.isIOS ? 'ios' : 'android'};
    }
    // The whole of strategy B. The site reads the token from a page where it
    // already knows who is signed in, and posts it to its own backend over
    // its own session - no endpoint to configure here, and no authentication
    // for us to design on the customer's behalf.
    if (action == 'getPushToken') {
      final value = await PushService.token();
      if (value == null) {
        return {'status': 'unavailable'};
      }
      return {'status': 'ok', 'token': value};
    }
    if (action == 'requestNotificationPermission') {
      final granted = await PushService.requestPermission();
      return {'status': granted ? 'ok' : 'denied'};
    }
""")
    if has_location:
        parts.append("""\
    if (action == 'getLocation') {
      var permission = await Geolocator.checkPermission();
      if (permission == LocationPermission.denied) {
        permission = await Geolocator.requestPermission();
      }
      if (permission == LocationPermission.denied ||
          permission == LocationPermission.deniedForever) {
        return {'status': 'denied'};
      }
      final position = await Geolocator.getCurrentPosition();
      return {
        'status': 'ok',
        'latitude': position.latitude,
        'longitude': position.longitude,
      };
    }
""")
    parts.append("    return {'status': 'unsupported', 'action': action};\n  }\n")
    return "".join(parts)


_DOWNLOAD = r"""  Future<void> _download(DownloadStartRequest request) async {
    try {
      final uri = Uri.parse(request.url.toString());
      if (!_isAllowedHost(uri.host)) {
        _showMessage('Blocked download from an unapproved domain.');
        return;
      }
      final client = HttpClient();
      final httpRequest = await client.getUrl(uri);
      final cookies = await CookieManager.instance().getCookies(url: request.url);
      if (cookies.isNotEmpty) {
        httpRequest.headers.set(
          HttpHeaders.cookieHeader,
          cookies.map((cookie) => '${cookie.name}=${cookie.value}').join('; '),
        );
      }
      final response = await httpRequest.close();
      final directory = await downloadsDirectory();
      final rawName = request.suggestedFilename ??
          (uri.pathSegments.isEmpty ? 'download' : uri.pathSegments.last);
      // A server-supplied filename reaches the filesystem, so path separators
      // and reserved characters are stripped rather than trusted.
      final fileName = rawName.replaceAll(RegExp(r'[<>:"/\\|?*]'), '_');
      final file = File('${directory.path}/$fileName');
      await response.pipe(file.openWrite());
      client.close();
      _showMessage('Downloaded $fileName');
      await OpenFilex.open(file.path);
    } catch (error) {
      _showMessage('Download failed: $error');
    }
  }
"""


_SHOW_MESSAGE = """\
  void _showMessage(String message) {
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(message)),
      );
    }
  }
"""


def _build_method(
    config: AppConfig,
    *,
    splash_asset: str | None,
    has_fallback: bool,
    has_bridge: bool,
    has_downloads: bool,
    has_camera: bool,
    has_nav: bool = False,
    has_saved: bool = False,
    has_share: bool = False,
    has_custom_offline: bool = False,
    has_policy: bool = False,
    has_echo: bool = False,
    has_push: bool = False,
) -> str:
    splash = (
        "const SizedBox.expand()"
        if not splash_asset
        else (
            "Image.asset(\n"
            f"                  {dart_string(splash_asset)},\n"
            "                  fit: BoxFit.cover,\n"
            "                  width: double.infinity,\n"
            "                  height: double.infinity,\n"
            "                )"
        )
    )
    user_agent = (
        "null" if not config.custom_user_agent else dart_string(config.custom_user_agent)
    )
    cache_mode = (
        "CacheMode.LOAD_DEFAULT" if config.cache_enabled else "CacheMode.LOAD_NO_CACHE"
    )

    out: list[str] = []
    add = out.append
    add("  @override")
    add("  Widget build(BuildContext context) {")
    if has_nav:
        # The shell owns the back button; each tab is a plain Scaffold. The
        # top app bar only exists when a module puts a button in it - with
        # nothing to hold, it would just be a strip of chrome between the
        # user and the website.
        add("    return Scaffold(")
        add("        backgroundColor: Theme.of(context).colorScheme.surface,")
        if has_saved or has_share:
            add("        appBar: AppBar(")
            add("          title: const Text(appTitle),")
            add("          actions: [")
            if has_saved:
                add("            IconButton(")
                add("              tooltip: 'Save this page',")
                add("              icon: const Icon(Icons.bookmark_add_outlined),")
                add("              onPressed: _saveCurrentPage,")
                add("            ),")
            if has_share:
                add("            IconButton(")
                add("              tooltip: 'Share this page',")
                add("              icon: const Icon(Icons.share_outlined),")
                add("              onPressed: () async {")
                add("                final url = (await controller?.getUrl())?.toString();")
                add("                if (url != null) {")
                add("                  await SharePlus.instance.share(ShareParams(text: url));")
                add("                }")
                add("              },")
                add("            ),")
            add("          ],")
            add("        ),")
    else:
        add("    return PopScope(")
        add("      canPop: false,")
        add("      onPopInvokedWithResult: (didPop, result) async {")
        add("        if (!didPop) {")
        add("          await _handleBack();")
        add("        }")
        add("      },")
        add("      child: Scaffold(")
        add("        backgroundColor: Theme.of(context).colorScheme.surface,")
    add("        body: SafeArea(")
    add("          child: Stack(")
    add("            fit: StackFit.expand,")
    add("            children: [")
    add("              InAppWebView(")
    if has_nav:
        add("                initialUrlRequest:")
        add("                    URLRequest(url: WebUri(widget.initialUrl)),")
    else:
        add("                initialUrlRequest: URLRequest(url: WebUri(homeUrl)),")
    add("                pullToRefreshController: pullToRefreshController,")
    if has_policy:
        # At document start, so the site's own navigation never paints before
        # it is hidden. Main frame only, which is UserScript's default - an
        # iframe on the page belongs to whoever put it there.
        add("                initialUserScripts: UnmodifiableListView([")
        add("                  UserScript(")
        add("                    source: sitePolicyScript,")
        add("                    injectionTime:")
        add("                        UserScriptInjectionTime.AT_DOCUMENT_START,")
        add("                  ),")
        add("                ]),")
    add("                initialSettings: InAppWebViewSettings(")
    add(f"                  javaScriptEnabled: {_dart_bool(config.javascript_enabled)},")
    add(f"                  domStorageEnabled: {_dart_bool(config.dom_storage_enabled)},")
    add(f"                  userAgent: {user_agent},")
    add("                  useShouldOverrideUrlLoading: true,")
    add(f"                  useOnDownloadStart: {_dart_bool(has_downloads)},")
    add(f"                  cacheEnabled: {_dart_bool(config.cache_enabled)},")
    add(f"                  cacheMode: {cache_mode},")
    add("                ),")
    add("                onWebViewCreated: (value) {")
    add("                  controller = value;")
    if has_bridge:
        add("                  value.addJavaScriptHandler(")
        add("                    handlerName: 'CissyBridge',")
        add("                    callback: _handleBridge,")
        add("                  );")
    add("                  _startLoadTimeout();")
    add("                },")
    add("                shouldOverrideUrlLoading: (_, action) async {")
    add("                  final target = action.request.url;")
    add("                  final uri = target == null")
    add("                      ? null")
    add("                      : Uri.tryParse(target.toString());")
    add("                  return uri == null")
    add("                      ? NavigationActionPolicy.CANCEL")
    add("                      : _decideNavigation(uri);")
    add("                },")
    add("                onLoadStart: (_, url) {")
    add("                  setState(() {")
    add("                    progress = 0;")
    if has_fallback:
        add("                    currentUrl = url?.toString() ?? currentUrl;")
        # The error screen is NOT cleared here. A load that is about to fail
        # also starts, and clearing on start flashes the broken page beneath
        # the overlay until the failure comes back. _finishLoad clears it.
        add("                    loadFailed = false;")
    add("                  });")
    add("                  _startLoadTimeout();")
    add("                },")
    add("                onProgressChanged: (_, value) {")
    add("                  if (mounted) {")
    add("                    setState(() => progress = value / 100);")
    add("                  }")
    add("                },")
    if has_echo:
        # pushState changes the address without a load, which is how a
        # single-page site moves between routes. onLoadStop alone would leave
        # the bar showing where the user was several routes ago.
        add("                onUpdateVisitedHistory: (_, url, __) {")
        add("                  final reached = url?.toString();")
        add("                  if (reached != null) {")
        add("                    widget.onUrlChanged?.call(reached);")
        add("                  }")
        add("                },")
    add("                onLoadStop: (_, url) async {")
    add("                  await _applyPagePolicies();")
    add("                  _finishLoad();")
    if has_push:
        add("                  await _maybeAskAboutPush();")
    if has_echo:
        add("                  final reached = url?.toString();")
        add("                  if (reached != null) {")
        add("                    widget.onUrlChanged?.call(reached);")
        add("                  }")
    if has_saved:
        add("                  final visited = url?.toString();")
        add("                  if (visited != null && visited.startsWith('http')) {")
        add("                    final title = await controller?.getTitle();")
        add("                    await SavedStore.recordVisit(")
        add("                      title == null || title.isEmpty ? visited : title,")
        add("                      visited,")
        add("                    );")
        add("                  }")
    add("                },")
    add("                onReceivedError: (_, request, error) {")
    add("                  if (request.isForMainFrame == true) {")
    if has_fallback:
        add("                    _failFromError(error);")
    else:
        add("                    _finishFailedLoad(")
        add("                      title: 'Something went wrong',")
        add("                      message: 'The page could not be loaded.',")
        add("                      offline: false,")
        add("                    );")
    add("                  }")
    add("                },")
    add("                onReceivedHttpError: (_, request, response) {")
    add("                  if (request.isForMainFrame == true) {")
    if has_fallback:
        add("                    _failFromStatus(response.statusCode);")
    else:
        add("                    _finishFailedLoad(")
        add("                      title: 'Something went wrong',")
        add("                      message: 'The page could not be loaded.',")
        add("                      offline: false,")
        add("                    );")
    add("                  }")
    add("                },")
    if has_fallback:
        # A certificate that fails verification is never bypassed - Play
        # rejects apps that let users click through SSL errors. The load is
        # cancelled and the native "not secure" screen explains why.
        add("                onReceivedServerTrustAuthRequest: (_, challenge) async {")
        add("                  _finishFailedLoad(")
        add("                    title: 'Connection not secure',")
        add("                    message: \"The site's security certificate could \"")
        add("                        'not be verified, so the page was blocked '")
        add("                        'to protect you.',")
        add("                    offline: false,")
        add("                  );")
        add("                  return ServerTrustAuthResponse(")
        add("                    action: ServerTrustAuthResponseAction.CANCEL,")
        add("                  );")
        add("                },")
    if has_camera:
        add("                onPermissionRequest: (_, request) async {")
        add("                  final current = await controller?.getUrl();")
        add("                  final uri = current == null")
        add("                      ? null")
        add("                      : Uri.tryParse(current.toString());")
        add("                  final allowed = uri != null &&")
        add("                      uri.scheme == 'https' &&")
        add("                      _isAllowedHost(uri.host);")
        add("                  return PermissionResponse(")
        add("                    resources: request.resources,")
        add("                    action: allowed")
        add("                        ? PermissionResponseAction.GRANT")
        add("                        : PermissionResponseAction.DENY,")
        add("                  );")
        add("                },")
    if has_downloads:
        add("                onDownloadStartRequest: (_, request) => _download(request),")
    add("              ),")
    if has_custom_offline:
        add("              if (errorTitle != null)")
        add("                _CustomErrorView(")
        add("                  onRetry: _retry,")
        add("                  onHome: _goHome,")
        add("                ),")
    elif has_fallback:
        add("              if (errorTitle != null)")
        add("                _ErrorView(")
        add("                  title: errorTitle!,")
        add("                  message: errorMessage ?? '',")
        add("                  isOffline: errorIsOffline,")
        add("                  retrying: retrying,")
        add("                  onRetry: _retry,")
        add("                  onHome: _goHome,")
        add("                ),")
    add("              if (showSplash)")
    add("                ColoredBox(")
    add("                  color: Colors.transparent,")
    add(f"                  child: {splash},")
    add("                ),")
    add("            ],")
    add("          ),")
    add("        ),")
    if has_nav:
        add("    );")
    else:
        add("      ),")
        add("    );")
    add("  }")
    return "\n".join(out) + "\n"


def _dispose(has_fallback: bool, has_push: bool = False, has_nav: bool = False) -> str:
    connectivity = (
        "    connectivitySubscription?.cancel();\n" if has_fallback else ""
    )
    # Only the screen that added the listener removes it. addListener was
    # guarded by widget.primary, so removing it unguarded would take the
    # listener off on behalf of a tab that never installed one.
    if has_push:
        guard = "    if (widget.primary) {\n" if has_nav else ""
        indent = "      " if has_nav else "    "
        close = "    }\n" if has_nav else ""
        listener = f"{guard}{indent}pendingPushUrl.removeListener(_openPushTarget);\n{close}"
    else:
        listener = ""
    return (
        "  @override\n"
        "  void dispose() {\n"
        "    loadTimeout?.cancel();\n"
        "    deepLinkSubscription?.cancel();\n"
        f"{connectivity}"
        f"{listener}"
        "    super.dispose();\n"
        "  }\n"
        "}\n"
    )


_PUSH_HOOKS = """\
  /// Open whatever a notification tap asked for.
  ///
  /// Cleared as it is read, so a rebuild does not send the user back to the
  /// same page a second time.
  void _openPushTarget() {
    final target = pendingPushUrl.value;
    if (target == null) {
      return;
    }
    pendingPushUrl.value = null;
    _load(Uri.parse(target));
  }

  /// Put the app's own explanation up, at most once per run.
  ///
  /// After a page has loaded rather than on launch: both platforms give an app
  /// one chance at the system prompt, and somebody who has not seen the app
  /// yet has no reason to say yes.
  Future<void> _maybeAskAboutPush() async {
    if (pushPromptShown) {
      return;
    }
    pushPromptShown = true;
    if (!await PushPrompt.due()) {
      return;
    }
    if (!mounted) {
      return;
    }
    await PushPrompt.show(context);
  }
"""


_DOWNLOADS_DIRECTORY = """\
/// Downloads live in their own folder so the downloads screen can list them
/// without guessing which files in the documents directory are the app's own.
Future<Directory> downloadsDirectory() async {
  final base = await getApplicationDocumentsDirectory();
  final dir = Directory('${base.path}/downloads');
  if (!dir.existsSync()) {
    dir.createSync(recursive: true);
  }
  return dir;
}
"""


_ERROR_VIEW = """\
/// Shown instead of the browser's own failure page, so a broken load still
/// looks like this app and always offers a way forward.
class _ErrorView extends StatelessWidget {
  const _ErrorView({
    required this.title,
    required this.message,
    required this.isOffline,
    required this.retrying,
    required this.onRetry,
    required this.onHome,
  });

  final String title;
  final String message;
  final bool isOffline;
  final bool retrying;
  final VoidCallback onRetry;
  final VoidCallback onHome;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return ColoredBox(
      color: colors.surface,
      child: Center(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 32),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                isOffline
                    ? Icons.wifi_off_rounded
                    : Icons.error_outline_rounded,
                size: 52,
                color: colors.onSurfaceVariant,
              ),
              const SizedBox(height: 20),
              Text(
                title,
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const SizedBox(height: 10),
              Text(
                message,
                textAlign: TextAlign.center,
                style: TextStyle(
                  color: colors.onSurfaceVariant,
                  height: 1.45,
                ),
              ),
              const SizedBox(height: 26),
              FilledButton.icon(
                onPressed: retrying ? null : onRetry,
                icon: retrying
                    ? const SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2.4),
                      )
                    : const Icon(Icons.replay_rounded, size: 18),
                label: Text(retrying ? 'Trying…' : 'Try again'),
              ),
              const SizedBox(height: 6),
              TextButton(
                onPressed: onHome,
                child: const Text('Go to home page'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
"""


def _custom_error_view(offline_asset: str, theme_color: str) -> str:
    """The developer's own offline screen instead of the built-in one.

    The pasted HTML ships inside the APK as an asset and is shown in its own
    WebView on top of the broken page. The contract with that HTML: a link to
    app://retry is the Try-again button, app://home returns to the start page,
    and the app's theme colour arrives as the --accent CSS variable.
    """
    accent = (
        "      onLoadStop: (viewController, _) async {\n"
        "        await viewController.evaluateJavascript(\n"
        "          source: \"document.documentElement.style\"\n"
        f"              \".setProperty('--accent', '{theme_color}');\",\n"
        "        );\n"
        "      },\n"
        if theme_color
        else ""
    )
    return (
        "/// The developer's own offline screen, baked into the app as an asset.\n"
        "/// Its links are the controls: app://retry retries, app://home goes to\n"
        "/// the start page. Everything else stays trapped in the overlay - this\n"
        "/// screen shows precisely when the network is gone.\n"
        "class _CustomErrorView extends StatelessWidget {\n"
        "  const _CustomErrorView({required this.onRetry, required this.onHome});\n"
        "\n"
        "  final VoidCallback onRetry;\n"
        "  final VoidCallback onHome;\n"
        "\n"
        "  @override\n"
        "  Widget build(BuildContext context) {\n"
        "    return ColoredBox(\n"
        "      color: Theme.of(context).colorScheme.surface,\n"
        "      child: InAppWebView(\n"
        f"        initialFile: {dart_string(offline_asset)},\n"
        "        initialSettings: InAppWebViewSettings(\n"
        "          useShouldOverrideUrlLoading: true,\n"
        "          supportZoom: false,\n"
        "        ),\n"
        "        shouldOverrideUrlLoading: (_, action) async {\n"
        "          final uri = action.request.url;\n"
        "          if (uri == null) {\n"
        "            return NavigationActionPolicy.CANCEL;\n"
        "          }\n"
        "          if (uri.scheme == 'app') {\n"
        "            if (uri.host == 'retry') {\n"
        "              onRetry();\n"
        "            } else if (uri.host == 'home') {\n"
        "              onHome();\n"
        "            }\n"
        "            return NavigationActionPolicy.CANCEL;\n"
        "          }\n"
        "          // Only the initial asset load itself may navigate.\n"
        "          if (uri.scheme == 'file' || uri.scheme == 'about') {\n"
        "            return NavigationActionPolicy.ALLOW;\n"
        "          }\n"
        "          return NavigationActionPolicy.CANCEL;\n"
        "        },\n"
        f"{accent}"
        "      ),\n"
        "    );\n"
        "  }\n"
        "}\n"
    )
