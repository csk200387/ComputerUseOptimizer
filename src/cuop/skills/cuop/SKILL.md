---
name: cuop
description: >
  Operate macOS desktop apps through the accessibility tree with the cuop CLI or its MCP server (desktop_* tools).
  Use when an agent must see or act on a desktop GUI: click, type, select, toggle, scroll, drag, press shortcuts,
  manage apps and windows, read the clipboard. Cheaper than screenshots: observe as compact text, act on refs,
  read the diff the action returns. Command set and JSON envelope follow agent-desktop 2.6.
---

# cuop

cuop reads a desktop app's accessibility tree and lets you act on its elements by ref. Every CLI command prints one JSON
envelope: `{"version":"2.6","ok":true,"command":"…","data":{…}}` or `{…,"ok":false,"error":{"code","message","disposition"}}`.
The MCP server (`cuop mcp --format text`) exposes the same commands as `desktop_<command>` tools (sub-commands as
`desktop_session_start`, `desktop_trace_show` …; arguments use the public names `ref_id`, `from`, `states` …).
`--toolset core` serves the 24 everyday tools with short descriptions — tool definitions are re-read every turn, so a
small catalog saves tokens. MCP calls take `headed: true` (physical input for that call) and `snapshot` takes `goal`.

## Fewest calls (the MCP server sends this as its instructions)

Every model turn re-reads the whole conversation, so the number of calls drives the cost more than the size of one
observation.

- Start with one snapshot; pass `goal` = words shown on screen for what you need.
- Most actions return the window's diff (`data.observation`). Trust it: do not snapshot, find or get the same screen
  again to confirm. If it is missing, `snapshot --diff` (changes only).
- `find` only for a ref you have not seen (a menu command: `find --surface menubar --name …`). Do not `get` a property
  that the tree already shows.
- Words go in with `type` or `set-value`; `press` is for single keys and shortcuts. `--headed` when a background app
  ignores semantic input (search fields, lists, typing into Office).
- Apps that keep their content out of the tree (Excel's cell grid, PowerPoint's slide canvas, canvases, images): do
  not search the tree for it; go straight to keys and at most one screenshot (see cuop-macos → Microsoft Office).
  Other apps: use the tree.
- Word builds its body text only after it has been frontmost once (allow a few seconds); Open/Save panels take a typed
  path (`press cmd+shift+g`, type the full path, `press return`) instead of folder browsing.

## The loop (token-cheap form)

```
cuop snapshot --app Calculator --format text          # text tree; header [@sab3] … ; one line per element
cuop click @sab3:e22 --format text                    # acts, then returns data.observation = what changed
cuop snapshot --app Calculator --format text --diff   # only changes since the view you last received
```

- A text line is `eN role "name" ="value" [states]`, indented by depth. Qualify it with the header's snapshot id:
  `@sab3:e22`. A bare `e22` or `@e22` is rejected (`INVALID_ARGS`).
- The same element keeps the same `eN` across snapshots in one session, so a diff line `~ e9 "11"` refers to the
  element you already saw. Diff lines: `~ id "new name" ="new value" [+state,-state]` changed, `+ parent` followed by
  an indented new subtree, `- id …` removed (with its subtree), `^ parent: ids…` children reordered, `(no change)`.
- In text mode every action returns `data.observation.text` (a diff of the acted window). Read it instead of
  taking another snapshot. If it says `(no change)` the action probably did nothing visible: inspect before retrying.
- Large windows come back filtered (`(filtered: … named controls first)`) or as a skeleton; containers show `… +N`
  for hidden children. Drill into one with `cuop snapshot --root @sab3:e40 --format text`, or pass `--goal "words on screen"`
  to keep only elements related to the current sub-goal (use words that appear in the UI, in the UI's language).
- Do not use `-i` when you need to read results: `-i` keeps interactive elements only and drops static text such as
  a calculator display. The default text view already removes empty wrappers and closed menus.
- Small text inside a value (superscripts) is written `^(…)`: a calculator showing 2¹⁰ reads `2^(10)`.
- `--format json` returns the agent-desktop tree (`ref_id`, `role`, `name`, `states`, `available_actions`) for tools
  that parse it; it costs 3–10× more tokens.

## Refs and recovery

- Refs resolve through the element's own accessibility address: identical twins each keep their ref. If the element
  is gone or the app restarted you get `STALE_REF` (`details.kind: retained_object_stale`) — snapshot again.
- `SNAPSHOT_NOT_FOUND`: the snapshot belongs to another session or expired (64 kept per session).
- Retry only when `error.disposition.retry` is `safe` (`delivery: not_delivered`). Anything else may have landed:
  read the UI first. `delivery_uncertain` means the app did not answer while acting.
- `delivered_verified` = cuop read back the intended state; `delivered_unverified` = sent, effect not proven.

## Choosing the action

| Goal | Use |
|---|---|
| Press a button, open a menu item, follow a link | `click @ref` |
| Fill a field | `set-value @ref "text"` (replaces) or `type @ref "text"` (inserts at the caret) |
| Pick from a popup/combo box/list | `select @ref "Option"` (try `--headed` if a background app won't open its list) |
| Checkbox, switch, tab | `check` / `uncheck` (idempotent), `toggle` only when state is unknown |
| Disclosure, tree row | `expand` / `collapse` |
| Scroll a list | `scroll @ref --direction down --amount 3`, or `scroll-to @ref` |
| Keyboard shortcut | `press cmd+s --app TextEdit` (goes to that app; quit shortcuts need `--force`, use `close-app`) |
| Menu bar | `snapshot --app X --surface menubar --format text`, then `click` the item |
| Find one element | `find --app X --role button --name Save --exact --first` |
| Wait for something | `wait --text "Done" --app X`, `wait --event window-opened --app X`, or `-w "role:text"` on the action |

Some background apps accept a value but do not react to it (a search field that only searches on real key
presses, a list that will not open): the field changes but the observation shows no result. Repeat that step with
`--headed` (MCP: `headed: true`), which brings the window forward and sends real keys or clicks.

Headless is the default: semantic accessibility calls, no pointer movement, no focus change. `--headed` permits
physical input (double/triple-click, hover, drag, mouse-*, physical typing) and brings the exact window forward;
use it only when the user allows it. cuop never operates the terminal or app that runs the agent.

## Apps and windows

`list-apps`, `list-windows --app X`, `launch "App"` (background unless `--activate`), `close-app "App"`,
`focus-window`, `move-window`, `resize-window`, `minimize`, `maximize`, `restore`. When an app has several windows,
snapshot answers `AMBIGUOUS_TARGET` with `details.candidates`; pass `--window-id w-1234`.

## Seeing what the agent does

`cuop session start --name task --cursor` → export `CUOP_SESSION`. The on-screen HUD (top right, click-through,
never focused, not captured by screenshots) shows the step number, command and target, and outlines the target.
`cuop trace show` lists the steps; `cuop trace export` writes an HTML timeline. Traces redact names, values and text.

## When the tree is not enough

`cuop screenshot --app X [path]` captures one window (needs Screen Recording). Use it for canvases, images and
colour/position questions; act with `--headed mouse-click --xy X,Y` using screen points from `get @ref --property bounds`.
