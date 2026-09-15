"""失敗クラスの例外階層（docs/failure-policy.md §1）。

失敗の分類は**例外の型**で行う。メッセージ文字列で判定しない。
"""

from __future__ import annotations

from contracts.states import FailureClass


class DomainError(Exception):
    """このプロジェクトのドメイン例外の基底。"""


class TransientError(DomainError):
    """一時障害。Temporalの自動retryに委ねる。"""


class RetryableError(DomainError):
    """再実行で解決しうる失敗。上限付きでretryする。"""


class NeedsInputError(DomainError):
    """人間の判断が要る失敗。retryせず blocked にする。"""


class PermanentError(DomainError):
    """入力自体が不正。retryしても同じ結果になる。"""


class ScriptOutputUnparseableError(RetryableError):
    """生成器の出力が JSON としてパースできない（ADR-0014）。

    LLM は同じ入力でも違う出力を返すので ``permanent`` の条件
    （同じ入力で必ず同じ失敗になる）を満たさない。修復はしない。
    """


class ScriptSchemaViolationError(RetryableError):
    """パースはできたがスキーマ違反（ADR-0014）。"""


class PromptContractError(NeedsInputError):
    """同じ入力で規定ラウンド連続して同種の違反。

    生成揺れではなくプロンプトとスキーマの不整合。人間が直せば回復するので
    ``permanent`` にはしない（ADR-0014）。
    """


class ProviderTimeoutError(RetryableError):
    """外部生成器がタイムアウトした。課金済みかは不明なので予約は未照合のまま。"""


class ProviderInvocationError(RetryableError):
    """外部生成器が非zero終了した。exit code の数値で分岐しない（規約が非公開）。"""


class ProviderUnavailableError(NeedsInputError):
    """CLI不在・未認証・権限拒否。再実行しても同じだが人間が直せば回復する。"""


class UnreconciledReservationError(NeedsInputError):
    """evidence の無い予約が残っている（ADR-0013）。

    呼ばない・消さない・解放しない。人手照合を待つ。
    """


class StoryboardOutputUnparseableError(RetryableError):
    """storyboard 生成器の出力が JSON として読めない（ADR-0014 / ADR-0015）。"""


class StoryboardSchemaViolationError(RetryableError):
    """storyboard の出力が schema・時間軸・台本カバレッジの検査に落ちた（ADR-0014）。"""


class StoryboardInputMissingError(NeedsInputError):
    """現行の台本 Artifact が無い。台本工程の再実行（人間の判断）で回復する。"""


class StoryboardInputInvalidError(NeedsInputError):
    """保存された台本が読めない、または sha256 がメタデータと一致しない。"""


class GenerationSpecUnavailableError(NeedsInputError):
    """固定した生成仕様（commit / blob）が読めない（ADR-0016）。人間が checkout か設定を直す。"""


class WorkspaceUnavailableError(RetryableError):
    """一時作業領域を安全に用意・削除できない（ADR-0016）。

    root の検査違反・symlink・OS エラーを infrastructure がここへ写像する。
    """


class ProviderSubmitAmbiguousError(NeedsInputError):
    """有料ジョブの submit が戻らず、provider job 参照を記録できなかった（ADR-0017）。

    呼んだか分からない。``UnreconciledReservationError`` と同じ意味論で、再送しない・消さない。
    """


class ProviderRejectedError(NeedsInputError):
    """provider が依頼を拒否した（コンテンツポリシー・入力不正）。

    同じ入力を再送しても同じ結果になるが、プロンプトや素材を人間が直せば回復するので
    ``permanent`` にしない（ADR-0017）。
    """


class ProviderJobFailedError(RetryableError):
    """provider 側のジョブが失敗した。新しいラウンド（新しい予約）で再生成しうる。"""


class ProviderPollDeadlineError(RetryableError):
    """provider ジョブの完了を待ち切れなかった。参照は台帳に残るので再 await できる。"""


class MediaValidationError(RetryableError):
    """生成メディアが検査規則（形式・解像度・尺など）を満たさない。生成揺れとして扱う。"""


class ProductionInputMissingError(NeedsInputError):
    """production の入力（現行の storyboard / 台本 / シーン画像）が無い。"""


class ProductionInputInvalidError(NeedsInputError):
    """production の入力が読めない・sha256 不一致・相互に食い違う。"""


class VoiceLanguageUnsupportedError(NeedsInputError):
    """設定した音声モデルが台本の言語を話せない（ADR-0017 / Phase 4B）。

    同じ入力で必ず同じ結果になるが、音声モデルの設定か台本の言語を人間が直せば回復するので
    ``permanent`` にしない。
    """


class RenderInputMissingError(NeedsInputError):
    """render の入力（現行のマニフェスト / 台本 / storyboard / シーン素材）が無い（ADR-0019）。

    PostgreSQL の参照か MinIO の本体のどちらかが欠けている。

    production を人間が再実行すれば回復するので needs_input。
    """


class RenderInputStaleError(NeedsInputError):
    """マニフェストが指す Artifact が現行でない（上流が作り直された）。

    production の再実行で回復する。
    """


class RenderInputIntegrityError(PermanentError):
    """入力 Artifact の sha256 不一致・契約違反（ADR-0019 §11）。

    保存済みの内容は同じ入力で何度読んでも同じく壊れている（決定的）ので permanent。
    """


class RenderSourceMediaError(NeedsInputError):
    """シーン素材のメディアが読めない・未対応の形式・尺の食い違い。素材の作り直し（人間）で回復する。"""


class DurationReconciliationError(NeedsInputError):
    """シーン動画の実尺が storyboard の尺に合わせられない（凍結の許容を超えて短い。ADR-0019 §4）。

    速度変更・ループ・黙った品質低下はしない。素材か storyboard を人間が直す。
    """


class VoiceTimelineOverflowError(NeedsInputError):
    """ナレーション音声が次の音声と重なる、または総尺を許容以上にはみ出す（ADR-0019 §4）。"""


class RenderEngineFailedError(RetryableError):
    """描画エンジンが非zero終了・シグナルで落ちた。上限付きで再実行する（ADR-0019 §11）。"""


class RenderEngineTimeoutError(RenderEngineFailedError):
    """描画エンジンが時間内に終わらなかった。プロセスは停止済み。"""


class RenderEngineUnavailableError(NeedsInputError):
    """描画エンジンのバイナリが無い・固定した sha256 と一致しない・フォントが無い。

    同じ設定で再実行しても同じ結果になるが、運用者が導入・設定し直せば回復するので
    ``permanent`` にしない（ADR-0019 §11 / ``ProviderUnavailableError`` と同じ判断）。
    """


class RenderWorkspaceFullError(RetryableError):
    """作業領域の空きが足りない（事前検査 / ENOSPC）。自動削除はせず、空きが戻れば再実行で通る。"""


class FinalVideoValidationError(NeedsInputError):
    """完成動画が技術検査に落ちた（解像度・codec・音声欠落・尺など決定的な不一致。ADR-0019 §8）。

    engine・profile の不整合は運用者が直せば回復するので needs_input（ADR-0019 §9 の見直し）。
    """


class FinalVideoCorruptError(RetryableError):
    """完成動画がデコードできない・読み戻しの sha256 が一致しない。再描画で解決しうる。"""


class UnknownRenderProfileError(NeedsInputError):
    """登録されていない render profile id（ADR-0019）。API でも先に弾く。

    id を直して再開できるので Episode を terminal にしない（needs_input）。
    """


class UploadInputMissingError(NeedsInputError):
    """upload の入力（現行の final_video / 台本）の参照か本体が無い（ADR-0020 §11）。

    render の再実行で回復する。
    """


class UploadIntegrityError(PermanentError):
    """final_video の sha256 がメタデータ・契約と一致しない（ADR-0020 §11）。

    保存済みの内容は何度読んでも同じく壊れている（決定的）ので permanent。YouTube は呼ばない。
    """


class UploadsPausedError(NeedsInputError):
    """``UPLOADS_PAUSED`` が有効。session を開始する前に止める（failure-policy §7）。"""


class UploadAuthError(NeedsInputError):
    """OAuth の refresh が ``invalid_grant`` / 権限不足 / チャンネル未開設（ADR-0020 §11）。

    人間が同意をやり直せば回復する。401 の期限切れは adapter が 1 度だけ refresh してから判断する。
    """


class UploadQuotaExceededError(RetryableError):
    """``quotaExceeded`` / ``uploadLimitExceeded`` / ``rateLimitExceeded``。

    長い backoff で再実行する。
    """


class UploadRejectedError(NeedsInputError):
    """YouTube がメタデータ・動画を拒否した（``invalidTitle`` / ``forbidden`` 等）。人間が直す。"""


class UploadOutcomeUnknownError(NeedsInputError):
    """bytes を送った後に結果が読めず、マーカー照合でも見つからない（ADR-0020 §4）。

    新しい session を開かない（二重投稿の防止）。人手照合と運用者の予約放棄を待つ。
    """


class UploadOwnershipLostError(NeedsInputError):
    """投稿中に入場トークンが別の実行へ移った（ADR-0020）。

    この試行は YouTube を呼ばずに降りる。Episode・job の記録は所有者の実行に任せる
    （record_failure はトークン不一致なので何も書かない）。retry しない。
    """


class InvalidTransitionError(DomainError):
    """表に無い状態遷移を永続化しようとした。"""


class ArtifactConflictError(DomainError):
    """同じキーに異なる内容を書こうとした（INV-11違反）。"""


_FAILURE_CLASS_BY_TYPE: dict[type[BaseException], FailureClass] = {
    TransientError: FailureClass.TRANSIENT,
    RetryableError: FailureClass.RETRYABLE,
    NeedsInputError: FailureClass.NEEDS_INPUT,
    PermanentError: FailureClass.PERMANENT,
}


def _iter_subclasses(cls: type[BaseException]) -> list[type[BaseException]]:
    found: list[type[BaseException]] = []
    for sub in cls.__subclasses__():
        found.append(sub)
        found.extend(_iter_subclasses(sub))
    return found


def _build_type_name_table() -> dict[str, FailureClass]:
    """型名 -> 失敗クラス。**継承から自動導出する。**

    Temporal の workflow 側は例外オブジェクトではなく型名しか受け取れない。
    ここを手書きの表にすると、新しいサブクラスを足した人が登録を忘れ、
    その失敗は黙って ``needs_input`` に落ちる（安全側だが意図とずれる）。
    定義を2箇所に書かないため、基底クラスの表から派生させる（AGENTS.md §8）。
    """
    table: dict[str, FailureClass] = {}
    for base, failure_class in _FAILURE_CLASS_BY_TYPE.items():
        table[base.__name__] = failure_class
        for sub in _iter_subclasses(base):
            # 多重継承時は最初に見つかった基底が勝つ。isinstance 分類と同じ順序。
            table.setdefault(sub.__name__, failure_class)
    return table


#: 型名 -> 失敗クラス。``classify_failure`` の isinstance 分類と一致する。
FAILURE_CLASS_BY_TYPE_NAME: dict[str, FailureClass] = _build_type_name_table()

#: retryしない失敗クラスの例外型名。TemporalのRetryPolicyへ渡す。
NON_RETRYABLE_ERROR_TYPE_NAMES: tuple[str, ...] = tuple(
    name
    for name, cls in FAILURE_CLASS_BY_TYPE_NAME.items()
    if cls in {FailureClass.NEEDS_INPUT, FailureClass.PERMANENT}
)


def classify_failure(exc: BaseException) -> FailureClass:
    """例外を失敗クラスへ分類する。

    分類できない例外は ``NEEDS_INPUT``。自動修復に流さず人間へ回す（INV-12）。
    ``PERMANENT`` は「同じ入力で必ず同じ失敗になる」と示せる場合だけなので、
    未知の例外をそこへ落とすことはしない。
    """
    for exc_type, failure_class in _FAILURE_CLASS_BY_TYPE.items():
        if isinstance(exc, exc_type):
            return failure_class
    return FailureClass.NEEDS_INPUT


def failure_class_from_type_name(type_name: str | None) -> FailureClass:
    """例外の型名から失敗クラスを引く。未知の型名は ``NEEDS_INPUT``（INV-12）。"""
    if type_name is None:
        return FailureClass.NEEDS_INPUT
    return FAILURE_CLASS_BY_TYPE_NAME.get(type_name, FailureClass.NEEDS_INPUT)
