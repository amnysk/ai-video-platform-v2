"""provider アカウントの利用可否と、残高不足による保留の語彙（ADR-0057 / ADR-0058）。

fal の残高不足は**障害ではなく、管理された一時停止**として扱う。この module は
fal 障害耐性（Agent B: breaker・分類・保留の記録）と日次生成（Agent C: 入口ゲート・
watchdog）の両側が import する唯一の定義（AGENTS.md §8）。値を別 module に書き直さない。

設計上の決定（親エージェント、ADR-0057 に転記する）:

- Episode の状態値は**増やさない**。保留した Episode は既存の ``blocked`` のまま、
  理由は追加テーブル（``provider_credit_holds``）に構造化して持つ。``EpisodeStatus`` の
  CHECK を作り直すと 0017 と同じ release gate 問題と状態遷移の変更が要り、既存 Workflow
  履歴の replay リスクも増えるため。
- breaker の単位は provider **アカウント**（``ProviderAccount``）。``fal_image`` と
  ``fal_video`` は同じアカウントを共有する。将来のチャンネル別アカウントは
  ``account_label`` を増やすだけで表せる（マルチテナント基盤は作らない）。
- breaker は**時間経過で自動解除しない**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from contracts.states import ProviderCall


class ProviderFamily(StrEnum):
    """課金アカウントを共有する provider の族。breaker はこの単位 + ラベルで持つ。"""

    FAL = "fal"


#: どの台帳 provider がどの族のアカウントで課金されるか。breaker の判定はこの表だけで引く。
PROVIDER_FAMILY: dict[ProviderCall, ProviderFamily] = {
    ProviderCall.FAL_IMAGE: ProviderFamily.FAL,
    ProviderCall.FAL_VIDEO: ProviderFamily.FAL,
}

#: 単一アカウント運用での既定ラベル。secret（API キー）から導出しない。
DEFAULT_ACCOUNT_LABEL = "default"


@dataclass(frozen=True, slots=True)
class ProviderAccount:
    """breaker の鍵。``label`` は人が付ける非 secret の名前（例: チャンネル名）。"""

    family: ProviderFamily
    label: str = DEFAULT_ACCOUNT_LABEL

    @property
    def key(self) -> str:
        return f"{self.family.value}:{self.label}"


class RefusalKind(StrEnum):
    """provider が submit を「受理しなかった」理由の分類（ADR-0057）。

    HTTP status だけでは決めない。provider の公式仕様・構造化エラー・応答本文の
    文書化された印からのみ ``BALANCE_EXHAUSTED`` / ``AUTH_INVALID`` を確定する。
    確定できないものは ``UNKNOWN``（安全側: breaker を開き、人が判断する）。
    """

    BALANCE_EXHAUSTED = "balance_exhausted"
    AUTH_INVALID = "auth_invalid"
    PERMISSION_DENIED = "permission_denied"
    UNKNOWN = "unknown"


class BreakerState(StrEnum):
    CLOSED = "closed"
    #: 新規の有料 submit を止める。時間経過では閉じない
    OPEN = "open"
    #: 人の明示承認の下で、上限付きの再開を許している
    HALF_OPEN = "half_open"


class HoldReason(StrEnum):
    """``provider_credit_holds.reason`` の値。Episode は ``blocked`` のまま。"""

    WAITING_FOR_CREDITS = "waiting_for_credits"
    #: 拒否理由を確定できなかった（RefusalKind.UNKNOWN）。人の判断を待つ
    PROVIDER_REFUSAL_UNKNOWN = "provider_refusal_unknown"


#: 保留中（未解放）の Episode がこの数に達したら、日次は新しい Episode を作らない
#: （企画・台本を溜め込みすぎない / ADR-0058）。
MAX_HELD_EPISODES = 2

#: 再開（release）で同時に動かしてよい Episode 数の上限（ADR-0057）。
RESUME_MAX_CONCURRENCY = 1


@dataclass(frozen=True, slots=True)
class BreakerSnapshot:
    """入口ゲート・watchdog が読む breaker の現在値。"""

    account_key: str
    state: BreakerState
    reason: RefusalKind | None
    opened_at: datetime | None


class ProviderHealthReader(Protocol):
    """breaker を読むだけの port。日次の入口ゲート・watchdog・API はこれだけに依存する。"""

    async def snapshot(self, account: ProviderAccount) -> BreakerSnapshot: ...

    async def open_hold_count(self) -> int: ...


__all__ = [
    "DEFAULT_ACCOUNT_LABEL",
    "MAX_HELD_EPISODES",
    "PROVIDER_FAMILY",
    "RESUME_MAX_CONCURRENCY",
    "BreakerSnapshot",
    "BreakerState",
    "HoldReason",
    "ProviderAccount",
    "ProviderFamily",
    "ProviderHealthReader",
    "RefusalKind",
]
