"""envelope · 에러 코드 · disposition — agent-desktop 2.6 JSON 계약 (docs/json-output.md)."""

from typing import Any, Literal

VERSION = "2.6"  # 따르는 envelope 계약 버전 (DECISIONS D2)

Code = Literal[
    "PERM_DENIED",
    "ELEMENT_NOT_FOUND",
    "APP_NOT_FOUND",
    "STALE_REF",
    "AMBIGUOUS_TARGET",
    "SNAPSHOT_NOT_FOUND",
    "POLICY_DENIED",
    "ACTION_FAILED",
    "ACTION_NOT_SUPPORTED",
    "APP_UNRESPONSIVE",
    "PLATFORM_NOT_SUPPORTED",
    "TIMEOUT",
    "INVALID_ARGS",
    "WINDOW_NOT_FOUND",
    "NOTIFICATION_NOT_FOUND",
    "INTERNAL",
]
Delivery = Literal["unknown", "not_delivered", "delivery_uncertain", "delivered_unverified", "delivered_verified"]

# 코드별 기본 복구 안내 — 바뀔 수 있는 문장이므로 에이전트는 code · disposition으로 분기한다
SUGGESTIONS: dict[str, str] = {
    "STALE_REF": "Take a fresh snapshot and retry with the new ref",
    "SNAPSHOT_NOT_FOUND": "Snapshot again; refs belong to the snapshot (and session) that printed them",
    "AMBIGUOUS_TARGET": "Pass --window-id (see details.candidates) or act on a control unique in the window",
    "APP_NOT_FOUND": "Check the name with list-apps, or launch the app",
    "WINDOW_NOT_FOUND": "Check list-windows; the app may have no window on this Space",
    "PERM_DENIED": "Grant the permission to the app that launches cuop (System Settings > Privacy & Security)",
    "POLICY_DENIED": "Add the global --headed flag only if physical input is intended",
    "TIMEOUT": "Raise --timeout-ms or narrow the scope (--root, --surface)",
}
# upstream recovery_for_code (crates/core/src/response/output.rs): retry가 safe이고 재시도할 가치가 있을 때만
RECOVERY: dict[str, tuple[str, bool, int | None]] = {  # code → (strategy, requires_fresh_snapshot, retry_after_ms)
    "STALE_REF": ("refresh_snapshot_then_retry_original", True, None),
    "SNAPSHOT_NOT_FOUND": ("refresh_snapshot_then_retry_original", True, None),
    "POLICY_DENIED": ("request_explicit_policy_then_retry_original", False, None),
    "APP_UNRESPONSIVE": ("inspect_state_then_retry_original", True, 250),
    "TIMEOUT": ("inspect_state_then_retry_original", True, 250),
}


def disposition(delivery: Delivery) -> dict[str, str]:
    """retry는 delivery에서 따라온다: not_delivered만 safe, unknown은 unknown, 나머지는 unsafe."""
    retry = "safe" if delivery == "not_delivered" else "unknown" if delivery == "unknown" else "unsafe"
    return {"delivery": delivery, "retry": retry}


class CuopError(Exception):
    """명령이 실패를 구조화해 던지는 유일한 예외. delivery 기본값은 not_delivered(아무것도 보내지 않은 거부)."""

    def __init__(
        self,
        code: Code,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        suggestion: str | None = None,
        delivery: Delivery = "not_delivered",
        platform_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code: Code = code
        self.message = message
        self.details = details or {}
        self.suggestion = suggestion if suggestion is not None else SUGGESTIONS.get(code)
        self.delivery: Delivery = delivery
        self.platform_detail = platform_detail

    def to_json(self) -> dict[str, Any]:
        disp = disposition(self.delivery)
        out: dict[str, Any] = {"code": self.code, "message": self.message, "disposition": disp}
        if self.suggestion:
            out["suggestion"] = self.suggestion
        rec = RECOVERY.get(self.code)
        if disp["retry"] == "safe" and rec is not None and self.details.get("retryable", True) is not False:
            recovery: dict[str, Any] = {"strategy": rec[0], "retryable": True, "requires_fresh_snapshot": rec[1]}
            if rec[2] is not None:
                recovery["retry_after_ms"] = rec[2]
            out["recovery"] = recovery
        if self.platform_detail:
            out["platform_detail"] = self.platform_detail
        if self.details:
            out["details"] = self.details
        return out


def ok(command: str, data: Any) -> dict[str, Any]:
    return {"version": VERSION, "ok": True, "command": command, "data": data}


def fail(command: str, error: CuopError) -> dict[str, Any]:
    return {"version": VERSION, "ok": False, "command": command, "error": error.to_json()}
