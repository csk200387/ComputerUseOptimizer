# CUOP × agent-desktop parity checklist (macOS)

기준: vercel-labs/agent-desktop v1.3.1 (commit e8934a5, envelope `version: "2.6"`), `~/.local/src/agent-desktop`.
명령 · 플래그 목록은 오라클 바이너리의 `--help`에서 뽑았다(`~/.local/agent-desktop-oracle`). CUOP 진입점은 `cuop <command>`
(Python, `src/cuop/desktop/`). 상태: `done` = 구현 + 테스트(또는 실측) 근거 있음 · `todo` · `n/a (이유)`. 2026-11-21 갱신: 근거는 커밋 · `tests/` · DECISIONS · PROGRESS 작업 로그.
근거 칸은 테스트 이름 · 실측 기록 위치.

## 1. Envelope · 공통 계약

| 항목 | agent-desktop | CUOP | 근거 |
|---|---|---|---|
| 한 명령 = stdout JSON envelope 하나 | `{version, ok, command, data}` / `{…, error}` | done | |
| `version` | `"2.6"` 고정 문자열 | done (D2) | |
| `error.code` · `message` · `disposition` 항상 | 예 | done | |
| `error.suggestion` · `recovery` · `platform_detail` · `details` 비면 생략 | 예 | done | |
| `recovery {strategy, retryable, requires_fresh_snapshot, retry_after_ms}` (retry safe일 때만) | 예 | done | |
| `disposition {delivery, retry}` 값 집합 | delivery: unknown · not_delivered · delivery_uncertain · delivered_unverified · delivered_verified / retry: unknown · safe · unsafe | done | |
| 읽기 전용 명령 · INVALID_ARGS 에러 = not_delivered/safe | 예 | done | |
| 액션 결과 `data.action` · `steps[] {label, outcome, mechanism, verified}` · `post_state` · `surfaces` · `target` | 예 | done | |
| 상태 액션 사후 검증(`post_action_verification`, `verification_scope`, `verification_reason`) | 예 | done | |
| exit code 0 / 1(구조화 에러) / 2(인자 파싱 에러, envelope는 출력) | 예 | done | |
| 에러 코드: PERM_DENIED · ELEMENT_NOT_FOUND · APP_NOT_FOUND · STALE_REF · AMBIGUOUS_TARGET · SNAPSHOT_NOT_FOUND · POLICY_DENIED · ACTION_FAILED · ACTION_NOT_SUPPORTED · APP_UNRESPONSIVE · PLATFORM_NOT_SUPPORTED · TIMEOUT · INVALID_ARGS · WINDOW_NOT_FOUND · NOTIFICATION_NOT_FOUND · INTERNAL | 예 | done | |
| SANDBOX_NOT_FOUND | 예 | n/a (sandbox VM provider 범위 밖) | |
| `details.kind` (ref 재식별: retained_object_stale · saved_ref_absent · saved_ref_ambiguous · identity_not_unique · resolution_identity_unknown) | 예 | done | |
| TIMEOUT `details.kind`: actionability_timeout · wait_timeout · chain_deadline · focus_verification | 예 | done | |
| 비밀 필드(secure text) 값 가림 · `verification_reason: secure_field` | 예 | done | |
| `--stdin` 입력(type · set-value · clipboard-set), 크기 상한 10,000 B / 128 KiB, NUL 거부 | 예 | done | |

## 2. Refs · 스냅샷 저장 · 세션

| 항목 | agent-desktop | CUOP | 근거 |
|---|---|---|---|
| 한정 ref `@<snapshot_id>:eN`, bare `@eN` → INVALID_ARGS | 예 | done | |
| 스냅샷 refmap 디스크 저장 · `latest_snapshot_id` | `~/.agent-desktop` (`AGENT_DESKTOP_HOME`) | done (`~/.cuop`, `CUOP_HOME`) | |
| 세션 namespace: `--session`, `AGENT_DESKTOP_SESSION` → `CUOP_SESSION`; 다른 세션 스냅샷 조회 불가 | 예 | done | |
| macOS 재식별: retained AX 주소(원격 토큰) + pid · role · identifier 일치 확인, 죽으면 STALE_REF `retained_object_stale` | 예 (`_AXUIElementRemoteTokenCreate`) | done (D3, 토큰 + pid · 프로세스 세대 · role · identifier 확인) | DECISIONS D3 |
| 주소 없는 요소: capture proof(창 전체 완전 읽기 + role·안정 필드 유일) 기반 재식별 | 예 | done (창 전체 · 깊이 제한 없음일 때만 증명, D7) | |
| `--root` drill-down: 같은 snapshot id에 ref 추가 · 그 서브트리 옛 ref 교체 | 예 | done | |
| 부분 트리: `complete` 항상 · 시간 초과 시 `truncated` · `nodes_observed` · 노드 `subtree_truncated` · `identity_unproven` | 예 | done | |
| ref 부여 규칙: 상호작용 role 또는 주 액션 광고(포커스 · 컨텍스트 메뉴 · scroll-to만은 제외), scrollarea · disclosure 포함 | 예 | done | |
| role 공통 어휘(닫힌 집합, 미지 = unknown) · state 어휘 | 예 | done | |
| Native ID (`native_id`: AXIdentifier / AXDOMIdentifier) | 예 | done | |

## 3. 관찰 명령

| 명령 | 플래그 | CUOP | 근거 |
|---|---|---|---|
| `snapshot` | `--app` `--window-id` `--max-depth`(10) `--include-bounds` `-i/--interactive-only` `--compact` `--skeleton` `--surface {window,focused,menu,menubar,sheet,popover,alert,dock,menu-bar-extras}` `--root` `--timeout-ms`(3000) `--force-electron-a11y` | done | |
| `snapshot` 출력 `data`: `app` `window{id,title}` `snapshot_id` `ref_count` `complete` `tree` (`role` `name` `value` `description` `ref_id` `states` `available_actions` `children` `children_count` `bounds` `native_id`) | | done | |
| `snapshot` 여러 창 → AMBIGUOUS_TARGET + `details.candidates` | | done | |
| CUOP 추가: `--format text` (compact 텍스트 + Filter `--goal` + Diff `--diff`) | — | done (`--format text` · `--goal` · `--diff`, 액션 뒤 `data.observation`) | |
| `find` | `--app` `--window-id` `--role` `--name` `--value` `--text` `--description` `--native-id` `--exact` `--root` `--surface` `--state TOKEN[=BOOL]` `--timeout-ms`(5000) `--count` `--first` `--last` `--nth` `--limit`; `roles_present` | done | |
| `get` | `<REF>` `--property {text,value,title,bounds,role,states}`; bounds `live` | done | |
| `is` | `<REF>` `--property {visible,enabled,checked,focused,expanded,selected}`; `applicable` | done | |
| `screenshot` | `[PATH]` `--app` `--window-id` `--screen`; 경로 없으면 base64 | done (실캡처 확인 2026-11-21) | |
| `list-surfaces` | `--app` | done | |
| `open-system-surface` | `--surface {dock,menu-bar-extras}` (macOS) | done | |

## 4. ref 액션 (기본 headless, `--timeout-ms` 5000, auto-wait · actionability)

| 명령 | 플래그 | CUOP | 근거 |
|---|---|---|---|
| `click` | `<REF>` `--timeout-ms` | done | |
| `double-click` · `triple-click` | `<REF>`; headless면 POLICY_DENIED | done | |
| `right-click` | `<REF>`; headless AXShowMenu | done | |
| `type` | `<REF> [TEXT]` `--stdin` `--interval-ms` `--timeout-ms`(5000+30ms/자) | done | |
| `set-value` | `<REF> [VALUE]` `--stdin` | done | |
| `clear` · `focus` | `<REF>` | done | |
| `select` | `<REF> <VALUE>` | done | |
| `toggle` · `check` · `uncheck` | `<REF>`, 탭 버튼은 press | done | |
| `expand` · `collapse` | `<REF>` | done | |
| `scroll` | `<REF>` `--direction {up,down,left,right}` `--amount` | done | |
| `scroll-to` | `<REF>` | done | |
| actionability battery (visible · stable · enabled · supported_action · policy · editable · receives_events) + `checks[]` 보고 | | done (visible · enabled · supported_action · editable 폴링; receives_events는 headed 포인터만 hit test — D8) | |

## 5. 키보드 · 마우스 (물리 입력, `--headed`)

| 명령 | 플래그 | CUOP | 근거 |
|---|---|---|---|
| `press` | `<COMBO>` `--app` `--window-id` `--force`; 위험 단축키(cmd+q 등) POLICY_DENIED | done | |
| `key-down` · `key-up` · `mouse-down` · `mouse-up` | 항상 거부(ACTION_NOT_SUPPORTED, headless mouse-* 는 POLICY_DENIED) | done | |
| `hover` | `[REF]` `--xy` `--duration`(양수 거부) | done | |
| `drag` | `--from` `--to` `--from-xy` `--to-xy` `--duration` `--drop-delay`(500) `--timeout-ms` `--wait-for-scope {from,to}` | done | |
| `mouse-move` | `--xy` | done | |
| `mouse-click` | `--xy` `--button {left,right,middle}` `--count` `--modifiers` | done | |
| `mouse-wheel` | `--x` `--y` `--dx` `--dy` `--modifiers` | done | |

## 6. 앱 · 창

| 명령 | 플래그 | CUOP | 근거 |
|---|---|---|---|
| `launch` | `<APP>` (이름 · 번들 ID) `--timeout` `--arg` `--env` `--cwd` `--no-attach` `--activate` `--cdp [PORT]`; `window` · `renderer` | done | |
| `close-app` | `<APP>` `--force`; 보호 프로세스 INVALID_ARGS | done | |
| `list-apps` | `--app` `--timeout-ms`; `{name,pid,bundle_id,presentation}` | done | |
| `list-windows` | `--app` `--timeout-ms`; `{id,title,app_name,pid,bounds,is_focused,accessible,visible}` | done | |
| `list-displays` | `{id,bounds,is_primary,scale}` | done | |
| `focus-window` | `--window-id` `--app` `--title` | done | |
| `resize-window` · `move-window` | `--app` `--window-id` `--width/--height` · `--x/--y`; `verification_scope` · `tolerance` | done | |
| `minimize` · `maximize` · `restore` | `--app` `--window-id` | done | |

## 7. 알림 · 클립보드 · 대기 · 묶음

| 명령 | 플래그 | CUOP | 근거 |
|---|---|---|---|
| `list-notifications` | `--app` `--text` `--limit` | done (실측: 목록 1건 · 숫자만) | |
| `dismiss-notification` · `dismiss-all-notifications` · `notification-action` | `<INDEX>` `--expected-app` `--expected-title` `--app`; mutation은 `--headed` | done (구현 · 단위 검사만 — 사용자 알림을 지우는 실측은 하지 않음, D9) | |
| `clipboard-get` | `--format {text,auto,image,file-urls}` `--out` | done | |
| `clipboard-set` | `[TEXT]` `--stdin` `--image PATH` `--file-url PATH` | done | |
| `clipboard-clear` | | done | |
| `wait` | `[MS]` `--element` `--predicate {exists,enabled,visible,actionable,value}` `--value` `--action` `--window` `--text` `--count` `--menu` `--menu-closed` `--notification` `--event {window-opened,window-closed,app-launched,app-terminated,focus-changed,surface-appeared,surface-dismissed}` `--window-id` `--app` `--timeout`(30000) | done | |
| 전역 `-w/--wait-for` · `--wait-for-gone` · `--wait-timeout`(30000), selector `role:text` | snapshot · press · ref 액션 | done | |
| `batch` | `<JSON>` `--stop-on-error` `--timeout-ms`; `args` 키, 중첩 금지 | done | |

## 8. 세션 · trace · 오버레이 · 시스템

| 명령 | 플래그 | CUOP | 근거 |
|---|---|---|---|
| 전역 플래그 | `--session` `--headed` `--trace PATH` `--trace-strict` `-v/--verbose` `--agent-id` `--debug` `--screenshot PATH.html` | done (`--session` `--headed` `--trace` `--trace-strict` `-v` `--agent-id`(받기만) `--format` `--goal`; `--debug/--screenshot PATH.html`은 trace export로 대체 — D10) | |
| `session start` · `end` · `list` · `gc` | `--name` `--no-trace` `--screenshots` `--cursor` / `[ID]` / `--older-than` `--ended` | done (`--multi-agent`는 받기만 · `--screenshots`는 artifacts events 그대로(D10) · gc는 agent-desktop 규칙: 기본 끝난 것만, 잠금 · 5분 기록이면 남김) | |
| `trace show` · `trace export` | `--limit` `--event` / `--out` `--limit`; 민감 필드 redaction | done (JSONL 세그먼트 병합 · HTML 뷰어, 민감 필드 가림) | |
| `cursor-overlay enable` · `disable` | 스타일 플래그(`--label` `--accent` …) | done (CUOP HUD: enable/disable/status; 스타일 플래그 대신 고정 HUD — D10) | |
| `status` | 플랫폼 · 권한 · `supported_surfaces` · 최근 스냅샷 · `session_id` · `tracing` · `state_root` | done | |
| `permissions` | `--request` `--guide`; accessibility · screen_recording · automation `{state}` | done | |
| `version` | `{version, target, os}` | done | |
| `skills` (`list` · `get NAME [REF]` `--full` · `path`) | 바이너리에 포함된 skill 문서 | done (`skills list|get desktop|platform|path`) | |
| `mcp` | 모든 명령을 `desktop_<command>` MCP 도구로(stdio), 전역 플래그 상속 | done (`cuop mcp`: agent-desktop 카탈로그와 같은 이름 68개 — 하위 명령 `desktop_session_start` 등 · 공개 인자 `ref_id` `from` `states` `args` `output_path` `commands_json`, sandbox만 n/a. JSON 모드는 structuredContent, 인라인 스크린샷은 이미지 블록. 확장: `--toolset core`(24개, D14) · 조작 도구 `headed` · snapshot `goal`) | |
| `sandbox` | install · create · start · stop · status · list · rm · exec | n/a (macOS VM provider — BRIEF §3-1에서 제외 허용) | |
| C 라이브러리(FFI) | `libagent_desktop_ffi` | n/a (BRIEF §3-1에서 제외 허용) | |
| Windows · Linux 전용 surface(`start-menu` · `action-center` · `taskbar`) | | n/a (macOS 아님) | |

## 9. macOS 특화

| 항목 | CUOP | 근거 |
|---|---|---|
| Electron `AXManualAccessibility` · Chromium `AXEnhancedUserInterface` 자동 켜기 (`--force-electron-a11y`) | done | |
| 메뉴 막대 항목 click = AXPress(headless) · application-owned `menu` / `menubar` 스냅샷(`data.window` 생략) | done | |
| 창 없는 백그라운드 앱(메뉴 막대 앱 · Control Center) 프로세스 관찰 | done | |
| 탭 버튼 `check` = press (AXValue 쓰기 금지) | done | |
| Web wrapper(이름 · 값 · 액션 없는 group)는 논리 깊이를 쓰지 않음 | done | |
| 위험 단축키 거부(cmd+q · cmd+shift+q · cmd+alt+esc · ctrl+cmd+q · cmd+shift+delete) | done | |
| 호스트 앱(터미널 · tmux 서버를 띄운 앱) 조작 금지 (CUOP 안전 규칙) | done | |
