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

from ..config import NAV_ICONS, AppConfig
from .common import dart_string, splash_widget, with_url_flag


# ── the navigation shell ─────────────────────────────────────────────────

# Tab icon names (config.NAV_ICONS) to the IconData the generated app uses.
#
# Derived rather than written out. Every name in NAV_ICONS is a Flutter icon
# with a rounded variant - that is the rule the list is chosen by - so a second
# hand-kept copy of it here could only ever drift out of step with the first.
_NAV_ICONS_DART = {name: f"Icons.{name}_rounded" for name in NAV_ICONS}


def _nav_icon(tab: dict[str, str]) -> str:
    # A globe for anything unknown. validate() refuses an unknown name, so this
    # is only reached by a tab saved before the name was retired.
    return _NAV_ICONS_DART.get(tab.get("icon", ""), "Icons.public_rounded")


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
    paths from the Studio come after it.

    Only targets written as a path contribute. A tab pointing at a full URL on
    another host has a path of "/" as far as this is concerned, and "/" matches
    every page there is.
    """
    matches: list[list[str]] = []
    for tab in config.nav_tabs:
        target = tab.get("target", "")
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


def _root_shell(
    config: AppConfig, *, splash_asset: str | None, icon_asset: str | None = None
) -> str:
    # Every tab is a page of the website, so tab N is web view N. There used to
    # be a slot table here because a native tab owned no web view and shifted
    # every index after it.
    tabs = list(config.nav_tabs)
    targets = [tab.get("target", "") for tab in tabs]
    web_count = len(tabs)
    # The icon splash needs no upload of its own, so a splash now exists
    # whenever the style is the icon one - not only when a file was given.
    has_splash = config.splash_style != "image" or splash_asset is not None
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
    add("  int index = 0;")
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
    add("")
    if has_echo:
        add("  /// The paths each tab lights up for, in tab order.")
        add(f"  static const navMatches = <List<String>>{json.dumps(matches)};")
        add("")
    add("  late final List<Widget> pages = [")
    for position, target in enumerate(targets):
        url = dart_string(_resolve_target(config, target))
        add("    WebViewScreen(")
        add(f"      key: webKeys[{position}],")
        add(f"      initialUrl: {url},")
        if position == 0:
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
    add("  }")
    add("")
    add("  Future<void> _handleBack() async {")
    add("    final web = webKeys[index].currentState;")
    add("    if (await web?.canGoBack() == true) {")
    add("      await web?.goBack();")
    add("      return;")
    add("    }")
    add("    if (index != 0) {")
    add("      setState(() => index = 0);")
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
        widget = splash_widget(
            config, splash_asset=splash_asset, icon_asset=icon_asset, indent=12
        ).splitlines()
        add("          if (showSplash)")
        # The builder indents for the deeper of the two call sites, so the
        # first line is placed and the rest keep their shape relative to it.
        add(f"            {widget[0]}")
        for line in widget[1:]:
            add(line)
    add("        ],")
    add("      ),")
    add("    );")
    add("  }")
    add("}")
    add("")
    return "\n".join(out)
