---
name: cuop-macos
description: >
  macOS specifics for cuop: permissions, headed mode, surfaces, Chromium/Electron apps, keys and known app quirks.
---

# cuop on macOS

- **Permissions.** Accessibility (required) and Screen Recording (screenshots only) belong to the app that launched
  the process tree (Terminal, iTerm, the app that started tmux). `cuop permissions` reads them and never prompts.
- **Surfaces.** `--surface menubar` (File/Edit/View), `menu` (an open menu or context menu), `sheet`, `popover`,
  `alert`, `focused`, `dock`, `menu-bar-extras` (system status items; with `--app`, that app's item). Menu-bar apps
  and Control Center have no window: `snapshot --app "Control Center"` reads the process.
- **Menus.** Clicking a menu-bar item opens its menu headless (AXPress). Then snapshot `--surface menu`.
  Menu commands act on the app's active window; a menu item that stays disabled in the background usually needs the
  app in front (`--headed click`).
- **Background apps and lists.** Pop-up and combo-box lists may not open while the app is in the background.
  If `select` reports `post_action_verification`, retry once with `--headed`.
- **Chromium and Electron.** cuop switches on web accessibility (`AXManualAccessibility` / `AXEnhancedUserInterface`)
  on the first snapshot. A cold renderer can need a few seconds: if the tree is thin, snapshot again. Some CEF apps
  (Spotify) expose almost nothing; use a screenshot there. A fresh Chrome profile started from a script needs
  `--use-mock-keychain --password-store=basic`, or a keychain prompt stalls page loading.
- **Keys.** Modifiers `cmd ctrl alt shift`, keys `return escape tab space delete up down left right f1…f12` and
  punctuation by symbol or name (`comma`, `period`, `slash`…). `cmd+q`-style quit shortcuts are refused without
  `--force`; use `close-app`.
- **Text.** `type` inserts at the caret via `AXSelectedText` (no key events); `set-value` replaces the whole value.
  Secure fields never echo their value. Formatting commands in rich-text editors apply to the selection.
- **Window ids** are `w-<CGWindowID>` and stay valid for the window's life (`list-windows`).
- **Open/Save panels** (a sandboxed app's panel, e.g. Word's Insert Picture, is a modal window of that app): type the
  location instead of browsing — `press cmd+shift+g`, type the full folder or file path, `press return`, then click
  the panel's default button. One snapshot of the panel is enough.
- **Microsoft Office** (ribbon buttons work headless; text goes in with `--headed type`):
  - Word builds the document text only after the app has been frontmost once: `focus-window --headed`, wait ~3 s,
    snapshot again; it then stays in the tree in the background. The body is one textbox; select with keys
    (`press cmd+a`), then click a ribbon toggle such as 굵게/Bold.
  - Excel never exposes the cell grid. Go to a cell with `--headed type` into the Name Box (이름 상자) + `press return`;
    the formula bar's name reads `B,6 =SUM(B2:B5)` (content or formula of the active cell, not its result). Type a
    value into the grid area (`layoutitem "콘텐츠 영역"`) with `--headed`. A multi-cell selection shows 합계/평균/개수 in
    the status bar; for a single formula result use a screenshot.
  - PowerPoint never exposes the slide canvas; the thumbnail cells name each slide's title. To type into a
    placeholder: `focus` the slide editor (`layoutitem "슬라이드 편집기 창…"`), `press tab` (selects the title),
    `press return`, then `--headed type` into the slide editor. Keys typed with no placeholder selected are dropped.
  - Office windows on another Space are not in the tree at all; bring the app to the front first.
