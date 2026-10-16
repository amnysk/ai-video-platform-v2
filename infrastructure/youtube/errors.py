"""YouTube adapter の例外（暫定）。統合時に ``domain.errors`` の upload 系例外へ対応付ける。

どの例外メッセージにも session URI（upload_id）・access token・refresh token を入れない。
"""

from __future__ import annotations


class YouTubeError(Exception):
    """YouTube adapter の例外の基底。"""


class YouTubeAuthError(YouTubeError):
    """認証・認可が人手でしか直らない（invalid_grant / forbidden / youtubeSignupRequired 等）。"""


class YouTubeQuotaError(YouTubeError):
    """quotaExceeded / uploadLimitExceeded。長い待機の後なら再試行できる。"""


class YouTubeRateLimitError(YouTubeError):
    """rateLimitExceeded / 429。backoff 後に再試行できる。"""


class YouTubeRejectedError(YouTubeError):
    """入力（metadata・本体）の拒否。同じ入力での再試行は無意味。"""


class YouTubeSessionExpiredError(YouTubeError):
    """resumable session が失効した（404/410）。ポートは通常 ``UploadExpired`` を返す。"""


class YouTubeTransientError(YouTubeError):
    """5xx・通信失敗・読めない応答。status query で結果を確かめてから再開する。"""
