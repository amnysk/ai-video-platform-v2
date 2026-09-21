"""決定的な固定コーパス（Fake Provider の唯一の資料源）。

通常のテストと worker の Fake 実行（``RESEARCH_PROVIDER=fake``）が同じ資料を使うので、
``tests/`` ではなくここに置く。実ネットワーク・時計・乱数に依存しない。

本文は実在の歴史事実（関ヶ原の戦い 1600 年、鉄砲伝来 1543 年、明治維新 1868 年など）に基づく短い文。
Evidence / Trend の**難しい場合**を 1 件ずつ持つ。
意図は ``CorpusDocument.note`` と下の定数名で分かる:

- 同じ本文を転載した資料（独立性: ``SEKIGAHARA_A`` と ``SEKIGAHARA_COPY`` は 1 つの源）
- snippet だけで本文を取れない資料（403）・取得が失敗する URL（timeout）・PDF（テキスト化しない）・
  切り詰められる資料・リダイレクトする URL（final_url が違う）
- 年代がずれた資料（``MEIJI_WRONG_YEAR``）
- 量化子の違い（「一部」と「すべて」）
- 因果を明言しない資料（``NO_CAUSATION``）
- 異説を述べる資料（``TANEGASHIMA_DISSENT``）

**誤りを含む資料**（``MEIJI_WRONG_YEAR`` / ``*_ALL``）は検査用の意図的な誤りで、事実ではない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from domain.research.ports import FetchErrorKind, SearchKind, SearchQuery

#: Fake の統計を「観測した」時刻（固定）
OBSERVED_AT = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)

SEKIGAHARA_A = "https://history-a.example/sekigahara"
SEKIGAHARA_B = "https://history-b.example/edo-bakufu"
SEKIGAHARA_COPY = "https://content-farm.example/sekigahara-copy"
SEKIGAHARA_SOME = "https://history-c.example/sekigahara-defection"
SEKIGAHARA_ALL = "https://blog.example/sekigahara-all-defected"
SEKIGAHARA_SHORT_LINK = "https://short.example/sekigahara"
TANEGASHIMA_A = "https://history-a.example/tanegashima"
TANEGASHIMA_DISSENT = "https://history-d.example/tanegashima-year-debate"
GUN_ADOPTION_SOME = "https://history-c.example/gun-adoption-some"
GUN_ADOPTION_ALL = "https://blog.example/gun-adoption-all"
NO_CAUSATION = "https://history-b.example/gun-and-nagashino"
MEIJI_A = "https://history-a.example/meiji-restoration"
MEIJI_WRONG_YEAR = "https://blog.example/meiji-1858"
MEIJI_SNIPPET_ONLY = "https://paywall.example/meiji-analysis"
TIMEOUT_URL = "https://slow.example/sekigahara-archive"
PDF_URL = "https://papers.example/sekigahara.pdf"
TRUNCATED_URL = "https://history-e.example/long-chronicle"
UNKNOWN_URL = "https://nowhere.example/missing"

SEKIGAHARA_BODY = (
    "関ヶ原の戦いは、1600年（慶長5年）10月21日（旧暦9月15日）に美濃国関ヶ原で行われた合戦である。"
    "徳川家康が率いる東軍と、石田三成らが中心となった西軍が戦い、東軍が勝利した。"
)

YT_SEKIGAHARA_LONG = "https://www.youtube.com/watch?v=sekigahara01"
YT_SEKIGAHARA_SHORT = "https://www.youtube.com/watch?v=sekigahara02"
YT_TANEGASHIMA = "https://www.youtube.com/watch?v=tanegashima01"
YT_MEIJI = "https://www.youtube.com/watch?v=meiji01"


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    url: str
    title: str
    snippet: str
    #: 取得できる本文。``None`` は snippet だけで本文を取れない資料
    body: str | None
    kind: SearchKind = "web"
    published_at: datetime | None = None
    #: query にこの語が含まれると hit する（決定的な対応）
    keywords: tuple[str, ...] = ()
    content_type: str = "text/html"
    #: 本文取得が失敗する資料（``body`` があっても取得は失敗する）
    fetch_failure: FetchErrorKind | None = None
    fetch_status_code: int | None = None
    #: 本文取得が切り詰められる資料（``body`` は先頭部分）
    truncated: bool = False
    #: 取得すると別 URL へ転送される（``final_url`` が ``requested_url`` と違う）
    redirect_to: str | None = None
    note: str = ""
    # YouTube の観測値（``kind == "youtube"`` のとき）
    video_id: str | None = None
    channel_id: str | None = None
    channel_title: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    subscriber_count: int | None = None
    duration_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ResearchCorpus:
    documents: tuple[CorpusDocument, ...] = field(default_factory=tuple)

    def by_url(self, url: str) -> CorpusDocument | None:
        return next((d for d in self.documents if d.url == url), None)

    def search(self, query: SearchQuery) -> list[CorpusDocument]:
        """query との対応が決定的。語の一致数（多い順）、同点は URL 順。"""
        tokens = [t for t in query.text.split() if t]
        scored: list[tuple[int, CorpusDocument]] = []
        for doc in self.documents:
            if doc.kind != query.kind or doc.redirect_to is not None:
                continue
            if (
                query.published_after
                and doc.published_at
                and doc.published_at < query.published_after
            ):
                continue
            if (
                query.published_before
                and doc.published_at
                and doc.published_at > query.published_before
            ):
                continue
            score = sum(1 for k in doc.keywords if k in query.text)
            score += sum(1 for t in tokens if t in doc.title)
            if score:
                scored.append((score, doc))
        scored.sort(key=lambda item: (-item[0], item[1].url))
        return [doc for _, doc in scored]


def _d(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def default_corpus() -> ResearchCorpus:
    return ResearchCorpus(_DOCUMENTS)


_DOCUMENTS: tuple[CorpusDocument, ...] = (
    CorpusDocument(
        url=SEKIGAHARA_A,
        title="関ヶ原の戦い",
        snippet="1600年、美濃国関ヶ原で徳川家康の東軍が石田三成らの西軍に勝利した合戦。",
        body=SEKIGAHARA_BODY,
        published_at=_d(2024, 3, 1),
        keywords=("関ヶ原", "1600", "徳川家康"),
        note="基準の資料。SEKIGAHARA_COPY が本文を転載している",
    ),
    CorpusDocument(
        url=SEKIGAHARA_B,
        title="江戸幕府の成立",
        snippet="関ヶ原の戦いに勝った徳川家康は1603年に征夷大将軍となった。",
        body=(
            "1600年の関ヶ原の戦いに勝利した徳川家康は、1603年に征夷大将軍に任じられ、"
            "江戸に幕府を開いた。"
        ),
        published_at=_d(2023, 6, 15),
        keywords=("関ヶ原", "江戸幕府", "徳川家康", "1603"),
        note="SEKIGAHARA_A とは別の源（書き方も出典も異なる）",
    ),
    CorpusDocument(
        url=SEKIGAHARA_COPY,
        title="関ヶ原の戦いとは（まとめ）",
        snippet="1600年、美濃国関ヶ原で徳川家康の東軍が石田三成らの西軍に勝利した合戦。",
        body=SEKIGAHARA_BODY,
        published_at=_d(2025, 1, 20),
        keywords=("関ヶ原", "1600", "まとめ"),
        note="SEKIGAHARA_A の本文をそのまま転載（独立した資料ではない。sha256 が一致する）",
    ),
    CorpusDocument(
        url=SEKIGAHARA_SOME,
        title="関ヶ原の寝返り",
        snippet="小早川秀秋ら一部の武将が戦いの最中に寝返った。",
        body="関ヶ原の戦いでは、西軍に属していた小早川秀秋ら一部の武将が戦いの最中に東軍側へ寝返った。",
        published_at=_d(2022, 11, 2),
        keywords=("寝返り", "小早川秀秋"),
        note="量化子「一部」（SEKIGAHARA_ALL と食い違う）",
    ),
    CorpusDocument(
        url=SEKIGAHARA_ALL,
        title="関ヶ原の寝返り（俗説）",
        snippet="西軍の武将はみな東軍へ寝返ったという記述。",
        body="関ヶ原の戦いでは、西軍のすべての武将が東軍側へ寝返った。",
        published_at=_d(2021, 5, 9),
        keywords=("寝返り",),
        note="量化子「すべて」。検査用の意図的な誤り（事実ではない）",
    ),
    CorpusDocument(
        url=SEKIGAHARA_SHORT_LINK,
        title="関ヶ原の戦い（短縮リンク）",
        snippet="短縮 URL。",
        body=None,
        redirect_to=SEKIGAHARA_A,
        keywords=("関ヶ原",),
        note="取得すると SEKIGAHARA_A へ転送される。Source の URL は final_url から作る",
    ),
    CorpusDocument(
        url=TANEGASHIMA_A,
        title="鉄砲伝来",
        snippet="1543年、種子島に漂着したポルトガル人により鉄砲が伝えられたとされる。",
        body="1543年、種子島にポルトガル人が漂着し、鉄砲が日本に伝えられたとされる。",
        published_at=_d(2024, 2, 10),
        keywords=("鉄砲伝来", "種子島", "1543"),
    ),
    CorpusDocument(
        url=TANEGASHIMA_DISSENT,
        title="鉄砲伝来の年をめぐる異説",
        snippet="一般に1543年とされるが、1542年とする説もある。",
        body=(
            "鉄砲伝来は一般に1543年とされるが、ポルトガル側の記録に基づき1542年とする説もあり、"
            "年代には異説がある。"
        ),
        published_at=_d(2023, 9, 30),
        keywords=("鉄砲伝来", "異説", "1542"),
        note="異説を述べる資料（TANEGASHIMA_A と年が食い違う）",
    ),
    CorpusDocument(
        url=GUN_ADOPTION_SOME,
        title="戦国大名と鉄砲",
        snippet="織田信長など一部の大名が鉄砲を積極的に取り入れた。",
        body="鉄砲が伝わると、織田信長など一部の戦国大名が積極的にこれを取り入れた。",
        published_at=_d(2022, 4, 4),
        keywords=("鉄砲", "戦国大名"),
        note="量化子「一部」",
    ),
    CorpusDocument(
        url=GUN_ADOPTION_ALL,
        title="戦国大名と鉄砲（俗説）",
        snippet="すべての大名がただちに鉄砲を採用したという記述。",
        body="鉄砲が伝わると、すべての戦国大名がただちに主力兵器として採用した。",
        published_at=_d(2020, 8, 8),
        keywords=("鉄砲", "戦国大名"),
        note="量化子「すべて」。検査用の意図的な誤り（事実ではない）",
    ),
    CorpusDocument(
        url=NO_CAUSATION,
        title="鉄砲伝来と長篠の戦い（年表）",
        snippet="1543年に鉄砲が伝来し、1575年に長篠の戦いがあった。",
        body="鉄砲は1543年に伝来した。その後、1575年には長篠の戦いがあった。",
        published_at=_d(2024, 7, 1),
        keywords=("鉄砲", "長篠", "1575"),
        note="出来事を並べるだけで因果を明言しない（因果は根拠にならない）",
    ),
    CorpusDocument(
        url=MEIJI_A,
        title="明治維新",
        snippet="1868年に江戸幕府から明治政府へ政権が移った変革。",
        body=(
            "明治維新は、1868年（慶応4年・明治元年）に江戸幕府から明治政府へ政権が移った"
            "一連の変革を指す。1867年には大政奉還が行われた。"
        ),
        published_at=_d(2024, 5, 5),
        keywords=("明治維新", "1868", "大政奉還"),
    ),
    CorpusDocument(
        url=MEIJI_WRONG_YEAR,
        title="明治維新の年（ブログ）",
        snippet="明治維新は1858年に成立したと書かれたブログ。",
        body="明治維新は1858年に成立し、その年に新政府が発足した。",
        published_at=_d(2019, 12, 1),
        keywords=("明治維新", "1858"),
        note="年代がずれた資料。検査用の意図的な誤り（1868 年が正しい）",
    ),
    CorpusDocument(
        url=MEIJI_SNIPPET_ONLY,
        title="明治維新の経済的背景（有料記事）",
        snippet="1868年の政権交代の前後で財政はどう変わったか。続きは会員向け。",
        body=None,
        published_at=_d(2025, 2, 2),
        keywords=("明治維新", "財政"),
        fetch_failure="http_4xx",
        fetch_status_code=403,
        note="snippet だけで本文を取得できない（403）。snippet は本文確認済みの根拠にならない",
    ),
    CorpusDocument(
        url=TIMEOUT_URL,
        title="関ヶ原の戦い（アーカイブ）",
        snippet="古い記事のアーカイブ。",
        body="（取得できない）",
        keywords=("関ヶ原", "アーカイブ"),
        fetch_failure="timeout",
        note="取得が timeout する（再試行できる失敗）",
    ),
    CorpusDocument(
        url=PDF_URL,
        title="関ヶ原の戦いに関する論文（PDF）",
        snippet="PDF。",
        body=None,
        content_type="application/pdf",
        keywords=("関ヶ原", "論文"),
        fetch_failure="bad_content_type",
        note="PDF はテキスト化しない（本文確認済みにならない）",
    ),
    CorpusDocument(
        url=TRUNCATED_URL,
        title="関ヶ原年代記（長文）",
        snippet="長い年代記。",
        body="関ヶ原の戦いは1600年に起きた。年代記の続きは長いので、ここで打ち切られた",
        keywords=("関ヶ原", "年代記"),
        truncated=True,
        note="切り詰められる資料（本文確認済みにならない）",
    ),
    CorpusDocument(
        url=YT_SEKIGAHARA_LONG,
        title="【日本史】関ヶ原の戦いを10分で解説",
        snippet="関ヶ原の戦いの経過をわかりやすく解説します。",
        body=None,
        kind="youtube",
        published_at=_d(2026, 8, 10),
        keywords=("関ヶ原",),
        video_id="sekigahara01",
        channel_id="UC_hist_1",
        channel_title="歴史ちゃんねる",
        view_count=152_000,
        like_count=4_300,
        comment_count=210,
        subscriber_count=88_000,
        duration_seconds=612,
    ),
    CorpusDocument(
        url=YT_SEKIGAHARA_SHORT,
        title="関ヶ原の戦い 30秒で分かる",
        snippet="30秒解説。",
        body=None,
        kind="youtube",
        published_at=_d(2026, 8, 25),
        keywords=("関ヶ原",),
        video_id="sekigahara02",
        channel_id="UC_hist_2",
        channel_title="日本史ショート",
        view_count=980_000,
        like_count=31_000,
        comment_count=None,
        subscriber_count=None,
        duration_seconds=30,
    ),
    CorpusDocument(
        url=YT_TANEGASHIMA,
        title="鉄砲伝来の謎 1543年に何が起きたか",
        snippet="種子島への鉄砲伝来を追う。",
        body=None,
        kind="youtube",
        published_at=_d(2026, 7, 1),
        keywords=("鉄砲伝来", "鉄砲"),
        video_id="tanegashima01",
        channel_id="UC_hist_1",
        channel_title="歴史ちゃんねる",
        view_count=42_000,
        like_count=900,
        comment_count=55,
        subscriber_count=88_000,
        duration_seconds=845,
    ),
    CorpusDocument(
        url=YT_MEIJI,
        title="明治維新とは？ 1868年の変革",
        snippet="明治維新をざっくり。",
        body=None,
        kind="youtube",
        published_at=_d(2026, 9, 1),
        keywords=("明治維新",),
        video_id="meiji01",
        channel_id="UC_hist_3",
        channel_title="近代史ラボ",
        view_count=310_000,
        like_count=8_800,
        comment_count=640,
        subscriber_count=1_200_000,
        duration_seconds=1_260,
    ),
)
