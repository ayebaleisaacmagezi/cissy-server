"""The bottom navigation shell.

`RootShell` keeps one live screen per tab inside an `IndexedStack`, so switching
tabs never reloads a page or loses scroll position. That is a deliberate choice
and it is the reason a tab cannot simply follow whatever URL is on screen: each
tab owns its own WebView, at its own URL.

The icon set is fixed because each name is mapped to an `IconData` at compile
time; `config.NAV_ICONS` is the same list from the other side.
"""

from __future__ import annotations

import json

from ..config import NAV_NATIVE_TARGETS, NAV_NOTIFICATIONS, AppConfig
from .common import dart_string, with_url_flag


# ── the navigation shell ─────────────────────────────────────────────────

# Tab icon names (config.NAV_ICONS) to the IconData the generated app uses.
_NAV_ICONS_DART = {
    "home": "Icons.home_rounded",
    "storefront": "Icons.storefront_rounded",
    "menu_book": "Icons.menu_book_rounded",
    "article": "Icons.article_rounded",
    "shopping_bag": "Icons.shopping_bag_rounded",
    "event": "Icons.event_rounded",
    "call": "Icons.call_rounded",
    "person": "Icons.person_rounded",
    "bookmark": "Icons.bookmark_rounded",
    "download": "Icons.download_rounded",
    "settings": "Icons.settings_rounded",
    "info": "Icons.info_rounded",
}


_NATIVE_DEFAULT_ICONS = {
    "native:saved": "Icons.bookmark_rounded",
    "native:downloads": "Icons.download_rounded",
    "native:settings": "Icons.settings_rounded",
    "native:notifications": "Icons.notifications_rounded",
}


def _nav_icon(tab: dict[str, str]) -> str:
    icon = tab.get("icon", "")
    if icon in _NAV_ICONS_DART:
        return _NAV_ICONS_DART[icon]
    return _NATIVE_DEFAULT_ICONS.get(tab.get("target", ""), "Icons.public_rounded")


def _resolve_target(config: AppConfig, target: str) -> str:
    """A tab's target as a full URL. Paths are relative to the website.

    Tabs are entry points, so they carry the URL flag for the same reason the
    home URL does: this is where a visit starts.
    """
    from urllib.parse import urljoin

    resolved = (
        urljoin(config.website_url, target) if target.startswith("/") else target
    )
    return with_url_flag(resolved, config.url_flag)


def tab_matches(config: AppConfig) -> list[list[str]]:
    """The paths each tab lights up for, in tab order.

    A tab's own path is included automatically - a Shop tab pointing at /shop
    should light up on /shop without anyone configuring it - and the extra
    paths from the Studio come after it. Native tabs match nothing: they are
    not showing a page of the website at all.

    Only targets written as a path contribute. A tab pointing at a full URL on
    another host has a path of "/" as far as this is concerned, and "/" matches
    every page there is.
    """
    matches: list[list[str]] = []
    for tab in config.nav_tabs:
        target = tab.get("target", "")
        if target in NAV_NATIVE_TARGETS or target == NAV_NOTIFICATIONS:
            matches.append([])
            continue
        paths = []
        if target.startswith("/"):
            paths.append(target)
        for extra in tab.get("match") or ():
            if extra not in paths:
                paths.append(extra)
        matches.append(paths)
    return matches


def has_tab_echo(config: AppConfig) -> bool:
    """Whether any tab can be lit by the page rather than by a tap."""
    return sum(len(paths) for paths in tab_matches(config)) > 0


_TAB_FOR_URL = """\
  /// The tab whose paths best fit this page, or null for none.
  ///
  /// Longest match wins, so a tab on "/" does not outrank a tab on
  /// "/account/orders" simply by matching first.
  static int? _tabForUrl(String url) {
    final path = Uri.tryParse(url)?.path ?? '';
    int? best;
    var bestLength = 0;
    for (var tab = 0; tab < navMatches.length; tab++) {
      for (final prefix in navMatches[tab]) {
        if (path.startsWith(prefix) && prefix.length > bestLength) {
          bestLength = prefix.length;
          best = tab;
        }
      }
    }
    return best;
  }
"""


def _root_shell(config: AppConfig, *, splash_asset: str | None) -> str:
    tabs = list(config.nav_tabs)
    targets = [tab.get("target", "") for tab in tabs]
    web_slots = []          # per tab: its web view's key index, or -1
    web_count = 0
    for target in targets:
        if target in NAV_NATIVE_TARGETS or target == NAV_NOTIFICATIONS:
            web_slots.append(-1)
        else:
            web_slots.append(web_count)
            web_count += 1
    primary_index = web_slots.index(0)
    saved_index = targets.index("native:saved") if "native:saved" in targets else -1
    downloads_index = (
        targets.index("native:downloads") if "native:downloads" in targets else -1
    )
    has_settings_tab = "native:settings" in targets
    inbox_index = (
        targets.index(NAV_NOTIFICATIONS) if NAV_NOTIFICATIONS in targets else -1
    )
    needs_open = saved_index >= 0 or has_settings_tab or inbox_index >= 0
    has_splash = splash_asset is not None
    matches = tab_matches(config)
    has_echo = has_tab_echo(config)

    out: list[str] = []
    add = out.append
    add("/// The app's skeleton: one live screen per tab, kept alive together so")
    add("/// switching tabs never reloads a page or loses scroll position.")
    add("class RootShell extends StatefulWidget {")
    add("  const RootShell({super.key});")
    add("")
    add("  @override")
    add("  State<RootShell> createState() => _RootShellState();")
    add("}")
    add("")
    add("class _RootShellState extends State<RootShell> {")
    add(f"  int index = {primary_index};")
    if has_echo:
        add("")
        add("  /// A tab lit by the page on screen rather than by a tap.")
        add("  ///")
        add("  /// Each tab owns its own WebView, so following a link to /account")
        add("  /// inside the Home tab cannot move the user to the Account tab -")
        add("  /// that tab is a different WebView at a different URL. Lighting it")
        add("  /// up says where they are without pretending they switched.")
        add("  int? echoIndex;")
    if has_splash:
        add("  bool showSplash = true;")
    add("  final webKeys = List.generate(")
    add(f"    {web_count},")
    add("    (_) => GlobalKey<_WebViewScreenState>(),")
    add("  );")
    if saved_index >= 0:
        add("  final savedKey = GlobalKey<_SavedScreenState>();")
    if downloads_index >= 0:
        add("  final downloadsKey = GlobalKey<_DownloadsScreenState>();")
    if inbox_index >= 0:
        add("  final inboxKey = GlobalKey<PushInboxScreenState>();")
    add("")
    if has_echo:
        add("  /// The paths each tab lights up for, in tab order.")
        add(f"  static const navMatches = <List<String>>{json.dumps(matches)};")
        add("")
    add("  /// Which web view each tab uses; -1 marks a native screen.")
    add(f"  static const webSlots = <int>{json.dumps(web_slots)};")
    add("")
    add("  late final List<Widget> pages = [")
    for position, tab in enumerate(tabs):
        target = targets[position]
        slot = web_slots[position]
        if target == "native:saved":
            add("    SavedScreen(key: savedKey, onOpen: _openInPrimary),")
        elif target == "native:downloads":
            add("    DownloadsScreen(key: downloadsKey),")
        elif target == "native:settings":
            add("    SettingsScreen(onOpen: _openInPrimary),")
        elif target == NAV_NOTIFICATIONS:
            add("    PushInboxScreen(key: inboxKey, onOpen: _openInPrimary),")
        else:
            url = dart_string(_resolve_target(config, target))
            add("    WebViewScreen(")
            add(f"      key: webKeys[{slot}],")
            add(f"      initialUrl: {url},")
            if slot == 0:
                if has_splash:
                    add("      onFirstLoad: _dismissSplash,")
            else:
                add("      primary: false,")
            if has_echo:
                add(f"      onUrlChanged: (url) => _onUrlChanged({position}, url),")
            add("    ),")
    add("  ];")
    add("")
    if has_splash:
        add("  void _dismissSplash() {")
        add("    if (mounted && showSplash) {")
        add("      setState(() => showSplash = false);")
        add("    }")
        add("  }")
        add("")
    if needs_open:
        add("  void _openInPrimary(String url) {")
        add(f"    setState(() => index = {primary_index});")
        add("    webKeys[0].currentState?._load(Uri.parse(url));")
        add("  }")
        add("")
    if has_echo:
        add("  void _onUrlChanged(int from, String url) {")
        add("    // Background tabs go on loading pages of their own. Only the")
        add("    // one on screen gets to say where the user is.")
        add("    if (from != index) {")
        add("      return;")
        add("    }")
        add("    final match = _tabForUrl(url);")
        add("    final echo = match == null || match == index ? null : match;")
        add("    if (echo != echoIndex) {")
        add("      setState(() => echoIndex = echo);")
        add("    }")
        add("  }")
        add("")
        add(_TAB_FOR_URL)
    add("  void _selectTab(int value) {")
    if has_echo:
        add("    // A tap is a decision. Whatever the page was echoing, the tab")
        add("    // the user picked is now the one that is right.")
        add("    setState(() {")
        add("      index = value;")
        add("      echoIndex = null;")
        add("    });")
    else:
        add("    setState(() => index = value);")
    if saved_index >= 0:
        add(f"    if (value == {saved_index}) {{")
        add("      savedKey.currentState?.refresh();")
        add("    }")
    if downloads_index >= 0:
        add(f"    if (value == {downloads_index}) {{")
        add("      downloadsKey.currentState?.refresh();")
        add("    }")
    if inbox_index >= 0:
        # The background isolate writes the inbox, so what is on screen can be
        # out of date the moment the app comes back.
        add(f"    if (value == {inbox_index}) {{")
        add("      inboxKey.currentState?.refresh();")
        add("    }")
    add("  }")
    add("")
    add("  Future<void> _handleBack() async {")
    add("    final slot = webSlots[index];")
    add("    if (slot >= 0) {")
    add("      final web = webKeys[slot].currentState;")
    add("      if (await web?.canGoBack() == true) {")
    add("        await web?.goBack();")
    add("        return;")
    add("      }")
    add("    }")
    add(f"    if (index != {primary_index}) {{")
    add(f"      setState(() => index = {primary_index});")
    add("      return;")
    add("    }")
    add("    SystemNavigator.pop();")
    add("  }")
    add("")
    add("  @override")
    add("  Widget build(BuildContext context) {")
    add("    return PopScope(")
    add("      canPop: false,")
    add("      onPopInvokedWithResult: (didPop, result) async {")
    add("        if (!didPop) {")
    add("          await _handleBack();")
    add("        }")
    add("      },")
    add("      child: Stack(")
    add("        fit: StackFit.expand,")
    add("        children: [")
    add("          Scaffold(")
    add("            body: IndexedStack(index: index, children: pages),")
    add("            bottomNavigationBar: NavigationBar(")
    add("              selectedIndex: %s," % ("echoIndex ?? index" if has_echo else "index"))
    add("              onDestinationSelected: _selectTab,")
    add("              destinations: const [")
    for tab in tabs:
        add("                NavigationDestination(")
        add(f"                  icon: Icon({_nav_icon(tab)}),")
        add(f"                  label: {dart_string(tab.get('label', ''))},")
        add("                ),")
    add("              ],")
    add("            ),")
    add("          ),")
    if has_splash:
        add("          if (showSplash)")
        add("            Image.asset(")
        add(f"              {dart_string(splash_asset)},")
        add("              fit: BoxFit.cover,")
        add("              width: double.infinity,")
        add("              height: double.infinity,")
        add("            ),")
    add("        ],")
    add("      ),")
    add("    );")
    add("  }")
    add("}")
    add("")
    return "\n".join(out)
