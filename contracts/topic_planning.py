"""Topic Planner の契約（ADR-0025）。

DailyEpisodeWorkflow → TopicPlannerWorkflow → TopicPlan → Episode → EpisodePipelineWorkflow。

**ここが Topic Planner の値の唯一の宣言元**（AGENTS.md §8）:

- Strategy（誰に・何を）: ``STRATEGY_PROFILES``。prompt は profile を**データとして**受け取る
- Content（どの形式で）: ``CONTENT_PROFILES``。Planner のコアは profile の中身で分岐しない
  （Shorts 固有値を Planner に持ち込まない。形式の説明は prompt へ渡す文字列だけ）
- 採点の重み・重複の閾値・cooldown・候補数: ``PlannerPolicy``（``DEFAULT_PLANNER_POLICY``）
- version: ``PLANNER_VERSION`` / ``StrategyProfile.version`` / prompt の version（``prompts``）
- 台本の locale（ADR-0026）: ``SCRIPT_LOCALES`` / ``DEFAULT_SCRIPT_LOCALE``。
  plan のある Episode の locale は ``StrategyProfile.language`` だけが決める

Settings は profile の **id** を選ぶだけで中身を持たない。
DB は採用時点の id と version を記録する。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from contracts.topic import TOPIC_MAX_CHARS

# ------------------------------------------------------------------ workflow 名・queue・version

#: Codex（LLM）を持つ planning worker の queue（``workers/planning/run_worker.SCRIPT_TASK_QUEUE``）
TOPIC_PLANNER_TASK_QUEUE = "script"
TOPIC_PLANNER_WORKFLOW: tuple[str, str] = ("TopicPlannerWorkflow", TOPIC_PLANNER_TASK_QUEUE)
TOPIC_PLAN_WORKFLOW_ID_PREFIX = "topic-plan"
#: 子 TopicPlannerWorkflow の execution timeout（秒）。Daily はこの時間まで Planner を待つ。
#: Schedule の overlap SKIP は Planner を含む Daily 全体に掛かる（ADR-0025）。
#: 同じ id の Planner が走っているときの待ち時間の上限もここから導く（``contracts.pipeline``）
TOPIC_PLANNER_EXECUTION_TIMEOUT_SECONDS = 90 * 60

#: 採点・重複判定・選択ロジックの version。ロジックを変えたら上げる（topic_plans に記録）
PLANNER_VERSION = "topic-planner-1"

TOPIC_FIND_PLAN = "topic_find_plan"
TOPIC_GATHER_CONTEXT = "topic_gather_context"
TOPIC_GENERATE_CANDIDATES = "topic_generate_candidates"
TOPIC_SELECT_AND_SAVE = "topic_select_and_save"

TOPIC_PLANNER_ACTIVITY_NAMES: tuple[str, ...] = (
    TOPIC_FIND_PLAN,
    TOPIC_GATHER_CONTEXT,
    TOPIC_GENERATE_CANDIDATES,
    TOPIC_SELECT_AND_SAVE,
)


def topic_plan_workflow_id(
    plan_date: str, strategy_profile_id: str, content_profile_id: str
) -> str:
    """同じ日・同じ profile の組の Planner は Temporal 上でも1つ（DB の一意性と同じ鍵）。"""
    return f"{TOPIC_PLAN_WORKFLOW_ID_PREFIX}-{plan_date}-{strategy_profile_id}-{content_profile_id}"


# ---------------------------------------------------------------------------------- 語彙


class TopicPlanStatus(StrEnum):
    #: 確定済み・まだ Episode に結び付いていない
    PLANNED = "planned"
    #: Episode に結び付いた（episodes.topic_plan_id）
    ASSIGNED = "assigned"


class AnalyticsMode(StrEnum):
    """Planner がどの Analytics で判断したか（topic_plans に記録）。"""

    NORMAL = "normal"
    STALE_ANALYTICS = "stale_analytics"
    NO_ANALYTICS = "no_analytics"


class DuplicateLevel(StrEnum):
    NONE = "none"
    #: Level 1: 正規化した鍵（subject+angle）または正規化タイトルが一致 → reject
    EXACT = "exact"
    #: Level 2: 意味的に同一（類似度 >= reject 閾値） → reject
    SEMANTIC = "semantic"
    #: Level 3: 同じ subject・別 angle。cooldown 内は reject、外は penalty
    SAME_SUBJECT = "same_subject"


HARD_DUPLICATE_LEVELS: frozenset[DuplicateLevel] = frozenset(
    {DuplicateLevel.EXACT, DuplicateLevel.SEMANTIC}
)


class TopicAngle(StrEnum):
    """切り口の語彙。重複判定（Level 3）と angle diversity に使う。"""

    REASON = "reason"  # なぜ〜したのか
    ORIGIN = "origin"  # 起源
    DAILY_LIFE = "daily_life"  # 当時の暮らし
    MYTH_VS_FACT = "myth_vs_fact"  # 誤解と事実
    PERSON = "person"  # 人物
    EVENT = "event"  # 出来事
    COMPARISON = "comparison"  # 比較（例: 西洋との）
    MYSTERY = "mystery"  # 謎
    LEGACY = "legacy"  # 現代への影響


# -------------------------------------------------------------------------- locale


class SpeechUnit(StrEnum):
    """ナレーションの長さを数える単位（ADR-0026 §読み上げ速度の予算）。"""

    #: 話し言葉の語の見積もり（英語など。``estimate_spoken_words``）
    WORD = "word"
    #: 空白以外の文字（句読点を含む。日本語など分かち書きしない言語）
    CHARACTER = "character"


class ScriptLocale(BaseModel):
    """台本の locale（ADR-0026）。prompt テンプレートの選択と ``ScriptArtifact.language`` の元。

    読み上げ速度の予算もここが唯一の宣言元: 1シーンのナレーションは
    ``floor(duration_ms × max_speech_units_per_second / 1000)`` 単位まで。
    prompt（予算の提示）と台本 activity（決定論的な検査）の両方がここから導く。
    """

    model_config = ConfigDict(frozen=True)

    #: BCP 47（``StrategyProfile.language`` と同じ語彙）
    locale: str
    #: ``ScriptArtifact.language`` に書く値（下流の音声・字幕・upload が読む ISO 639-1）
    artifact_language: str
    #: ナレーションの長さを数える単位
    speech_unit: SpeechUnit
    #: 1秒あたりに収めてよい ``speech_unit`` の上限（音声がシーン尺を超えない予算）
    max_speech_units_per_second: float = Field(gt=0)

    def count_speech_units(self, narration: str) -> int:
        if self.speech_unit is SpeechUnit.WORD:
            return estimate_spoken_words(narration)
        return sum(1 for ch in narration if not ch.isspace())

    def narration_budget(self, duration_ms: int) -> int:
        """``duration_ms`` のシーンに収めてよいナレーションの単位数（切り捨て）。

        式は ``floor(duration_ms × max_speech_units_per_second / 1000)``（prompt と同じ文言）。
        """
        return round(duration_ms * self.max_speech_units_per_second) // 1000

    def required_speech_ms(self, narration: str) -> int:
        """``narration`` が予算に収まる最短の尺（ms）。

        ``ceil(units / max_speech_units_per_second × 1000)`` を、``narration_budget`` と
        **同じ判定**になるように丸める: ``span_ms >= required_speech_ms(n)`` ⇔
        ``count_speech_units(n) <= narration_budget(span_ms)``。storyboard の区間検査が使う。
        """
        units = self.count_speech_units(narration)
        required = math.ceil(units * 1000 / self.max_speech_units_per_second)
        while self.narration_budget(required) < units:
            required += 1
        while required > 0 and self.narration_budget(required - 1) >= units:
            required -= 1
        return required


#: **台本 locale の唯一の宣言元**。prompt の registry（``prompts.script``）はこの鍵と一致する
SCRIPT_LOCALES: dict[str, ScriptLocale] = {
    loc.locale: loc
    for loc in (
        # 日本語の音声は未整備（ADR-0026 負債）。実測が無いので、速めの日本語ナレーション
        # （約 8 字/秒）より緩い 9 字/秒を上限にし、既存の日本語台本を新たに落とさない
        ScriptLocale(
            locale="ja-JP",
            artifact_language="ja",
            speech_unit=SpeechUnit.CHARACTER,
            max_speech_units_per_second=9.0,
        ),
        # Piper en_US-kristin-medium の実測（2026-09-19、本番 Episode 87bbf7de）は
        # 1.97〜2.72 語/秒（シーン前後の無音込み）。重なり判定は厳密なので最も遅い実測
        # 1.97 を約 5% 下回る 1.9 語/秒にする
        ScriptLocale(
            locale="en-US",
            artifact_language="en",
            speech_unit=SpeechUnit.WORD,
            max_speech_units_per_second=1.9,
        ),
    )
}


def script_locale_for_language(artifact_language: str) -> ScriptLocale:
    """``ScriptArtifact.language``（ISO 639-1）→ ``ScriptLocale``。未登録なら ``ValueError``。

    台本より下流（storyboard の区間検査など）は台本 Artifact の ``language`` しか知らない。
    """
    matches = [loc for loc in SCRIPT_LOCALES.values() if loc.artifact_language == artifact_language]
    if len(matches) != 1:
        raise ValueError(f"no unique script locale for artifact language {artifact_language!r}")
    return matches[0]


# ----------------------------------------------------- 英語の話し言葉の語数の見積もり

#: 単独で立つ句読点・ダッシュ（読まない）。語の中のハイフン・ダッシュ・スラッシュは語の区切り
_WORD_SPLIT_RE = re.compile(r"[\s\-\u2010-\u2015/]+")
#: 数字の塊（``1,200`` / ``5.2`` / ``1847``）。前後の記号・接尾辞は別に数える
_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
#: 数の直後で1語として読まれる接尾辞（``5B`` = five billion、``40%`` = forty percent）
_SCALE_SUFFIXES = frozenset({"k", "m", "b", "bn", "t", "%"})
#: 数の前で1語として読まれる通貨記号（``$5`` = five dollars）
_CURRENCY_SYMBOLS = frozenset("$€£¥")


def _group_words(group: int) -> int:
    """0〜999 を読む語数（``two hundred forty seven`` = 4。十の位と一の位は別語に数える）。"""
    hundreds, rest = divmod(group, 100)
    tens, units = divmod(rest, 10)
    words = 2 if hundreds else 0
    if rest:
        words += 2 if tens >= 2 and units else 1
    return words


def _integer_words(digits: str) -> int:
    """桁区切りを除いた整数を読む語数。3桁ごとの組 + 組ごとの位の語（thousand / million …）。"""
    value = int(digits)
    if value == 0:
        return 1
    groups: list[int] = []
    while value:
        value, group = divmod(value, 1000)
        groups.append(group)
    return sum(_group_words(g) + (1 if i and g else 0) for i, g in enumerate(groups))


def _number_words(number: str) -> int:
    integer, _, decimals = number.replace(",", "").partition(".")
    # 桁区切りの無い 4 桁の 1100〜2099 は年として読む（eighteen forty seven = 3 語と見積もる）
    if "," not in number and not decimals and len(integer) == 4 and 1100 <= int(integer) <= 2099:
        return 3
    words = _integer_words(integer)
    if decimals:
        words += 1 + len(decimals)  # point + 1 桁 1 語
    return words


def _part_words(part: str) -> int:
    words = 0
    rest = part
    while (match := _NUMBER_RE.search(rest)) is not None:
        prefix, suffix = rest[: match.start()], rest[match.end() :]
        words += sum(1 for ch in prefix if ch in _CURRENCY_SYMBOLS)
        if any(ch.isalpha() for ch in prefix):
            words += 1
        words += _number_words(match.group())
        head = re.match(r"[A-Za-z%]+", suffix)
        if head is not None and head.group().lower() in _SCALE_SUFFIXES:
            words += 1
            suffix = suffix[head.end() :]
        elif head is not None and head.group().lower() in {"st", "nd", "rd", "th", "s"}:
            suffix = suffix[head.end() :]  # 序数・複数（3rd / 1990s）は数の語に含める
        rest = suffix
    if any(ch.isalnum() for ch in rest):
        words += 1
    return words


def estimate_spoken_words(narration: str) -> int:
    """英語のナレーションを読み上げたときの語数の**保守的な**見積もり（ADR-0026 追補）。

    - 空白・ハイフン・ダッシュ・スラッシュで区切る（``30-year-old`` = 3 語）。
      単独で立つ句読点・ダッシュ（``—`` / ``--`` / ``...``）は 0 語
    - 英数字を含まない断片は 0 語、文字だけの断片は 1 語
    - 数字は読み方で数える: 4 桁の年（1100〜2099）= 3 語、それ以外は 3 桁の組ごとに
      百の位 2 語 + 十・一の位 1〜2 語 + 位の語 1 語（``1,200`` = 4 語）、
      小数は ``point`` + 1 桁 1 語。
      通貨記号・``%``・``K/M/B/T`` は 1 語を足す（``$5.2B`` = 5 語）。序数の接尾辞は足さない

    過大に数える方向に倒す（予算を超えると再生成になるだけで、音声は溢れない）。
    """
    return sum(_part_words(part) for part in _WORD_SPLIT_RE.split(narration) if part)


#: TopicPlan を持たない Episode（手動 API・ADR-0025 以前）の locale。従来どおり日本語
DEFAULT_SCRIPT_LOCALE = "ja-JP"


# ------------------------------------------------------------------------- profile


class StrategyProfile(BaseModel):
    """誰に・どの領域を届けるか。形式（Shorts / Long）は持たない。"""

    model_config = ConfigDict(frozen=True)

    strategy_id: str
    version: str
    channel_name: str
    market_country: str
    audience_age_groups: tuple[str, ...]
    language: str
    domain: str
    audience_description: str
    #: 柱（theme）→ 目標構成比。portfolio balance の基準。合計 1.0
    content_pillars: dict[str, float]
    #: era の語彙（候補の era はここから選ぶ）
    eras: tuple[str, ...]
    #: 若年層に効きやすい切り口（us_young_fit の決定論部分）
    preferred_angles: tuple[TopicAngle, ...]

    @field_validator("language")
    @classmethod
    def _language_is_a_script_locale(cls, v: str) -> str:
        """台本 locale の SSoT（ADR-0026）。未登録の locale の profile は作れない。"""
        if v not in SCRIPT_LOCALES:
            raise ValueError(f"language must be one of {sorted(SCRIPT_LOCALES)}: {v!r}")
        return v


class ContentProfile(BaseModel):
    """どの形式で作るか。Planner は ``format_brief`` を prompt に渡すだけで、中身で分岐しない。"""

    model_config = ConfigDict(frozen=True)

    content_profile_id: str
    version: str
    format_brief: str
    #: 台本の目標尺（秒, 下限・上限）。台本 prompt が locale ごとの言い回しで埋める（ADR-0026）
    script_duration_seconds: tuple[int, int]


STRATEGY_PROFILES: dict[str, StrategyProfile] = {
    p.strategy_id: p
    for p in (
        StrategyProfile(
            strategy_id="us_young_history_v1",
            version="1",
            channel_name="Japan's Past",
            market_country="US",
            audience_age_groups=("age18-24", "age25-34"),
            language="en-US",
            domain="japanese_history",
            audience_description=(
                "US viewers aged 18-34 with little prior knowledge of Japanese history; "
                "curious, skeptical of dry lectures, respond to surprising concrete details"
            ),
            content_pillars={
                "warriors_and_war": 0.20,
                "daily_life": 0.20,
                "ritual_and_religion": 0.15,
                "politics_and_power": 0.15,
                "culture_and_arts": 0.15,
                "mysteries_and_legends": 0.15,
            },
            eras=(
                "ancient",
                "heian",
                "kamakura",
                "muromachi",
                "sengoku",
                "edo",
                "meiji",
                "modern",
            ),
            preferred_angles=(
                TopicAngle.MYTH_VS_FACT,
                TopicAngle.REASON,
                TopicAngle.DAILY_LIFE,
                TopicAngle.MYSTERY,
                TopicAngle.COMPARISON,
            ),
        ),
    )
}

CONTENT_PROFILES: dict[str, ContentProfile] = {
    p.content_profile_id: p
    for p in (
        ContentProfile(
            content_profile_id="shorts",
            version="1",
            format_brief=(
                "vertical short video under 60 seconds; one idea, hook in the first seconds"
            ),
            # ADR-0026 以前の台本 prompt の「30〜45秒」はここへ移した
            script_duration_seconds=(30, 45),
        ),
        ContentProfile(
            content_profile_id="long_form",
            version="1",
            format_brief="horizontal video of 8-15 minutes; room for context and several beats",
            script_duration_seconds=(8 * 60, 15 * 60),
        ),
    )
}

DEFAULT_STRATEGY_PROFILE_ID = "us_young_history_v1"
DEFAULT_CONTENT_PROFILE_ID = "shorts"


# -------------------------------------------------------------------------- policy


class ScoreWeights(BaseModel):
    model_config = ConfigDict(frozen=True)

    analytics_fit: float = 0.30
    us_young_fit: float = 0.25
    novelty: float = 0.25
    portfolio_balance: float = 0.10
    production_fit: float = 0.10


class PlannerPolicy(BaseModel):
    """採点・重複判定の設定。数値の唯一の宣言元（コードへ散在させない）。"""

    model_config = ConfigDict(frozen=True)

    weights: ScoreWeights = ScoreWeights()
    #: 意味的類似度（0..1）の帯。>= reject は Level 2
    similarity_reject: float = 0.85
    similarity_strong_penalty: float = 0.70
    similarity_mild_penalty: float = 0.55
    strong_penalty: float = 0.35
    mild_penalty: float = 0.15
    #: Level 3（同 subject・別 angle）: この日数以内は reject、外なら penalty
    same_subject_cooldown_days: int = 30
    same_subject_penalty: float = 0.20
    #: portfolio balance を測る直近の plan 数
    portfolio_window: int = 20
    candidate_count_min: int = 10
    candidate_count_max: int = 20
    max_rounds: int = 3
    #: analytics_confidence が 1.0 になる量（``confidence_window_days`` の views と動画本数）
    confidence_window_days: int = 28
    confidence_full_views: int = 20_000
    confidence_full_videos: int = 20
    #: 意味的類似度で各特徴が占める配分（合計 1.0）
    feature_similarity_weights: dict[str, float] = {
        "subject": 0.5,
        "entities": 0.2,
        "theme": 0.1,
        "era": 0.1,
        "angle": 0.1,
    }
    #: us_young_fit のうち strategy の preferred_angles（決定論）が占める割合。残りは LLM 自己評価
    preferred_angle_share: float = 0.5
    #: strategy の柱に無い theme の us_young_fit 倍率
    off_pillar_factor: float = 0.5
    #: 直前の plan と era / angle が同じときの portfolio_balance 倍率
    recent_repeat_factor: float = 0.75


DEFAULT_PLANNER_POLICY = PlannerPolicy()


# ------------------------------------------------------------- Candidate（LLM 出力）契約

_SLUG = r"^[a-z0-9]+(?:_[a-z0-9]+)*$"


class TopicCandidate(BaseModel):
    """LLM が返す候補1件。**validation を通らないものは DB に入れない**（INV-23）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str = Field(
        min_length=5, max_length=TOPIC_MAX_CHARS, description="viewer-facing working title"
    )
    subject: str = Field(pattern=_SLUG, max_length=80, description="canonical snake_case subject")
    entities: list[str] = Field(max_length=8)  # strict 出力のため既定値なし（空なら [] を返させる）
    era: str = Field(pattern=_SLUG, max_length=40)
    theme: str = Field(pattern=_SLUG, max_length=60, description="one of the strategy pillars")
    angle: TopicAngle
    hook: str = Field(min_length=5, max_length=300)
    visual_concept: str = Field(min_length=5, max_length=500)
    reason: str = Field(min_length=5, max_length=500)
    #: LLM の自己評価（0..1）。採点では決定論の値と混ぜる入力の1つに過ぎない
    audience_fit: float = Field(ge=0.0, le=1.0)
    visual_fit: float = Field(ge=0.0, le=1.0)

    @field_validator("entities")
    @classmethod
    def _entities_are_slugs(cls, v: list[str]) -> list[str]:
        import re

        for e in v:
            if not re.fullmatch(_SLUG, e) or len(e) > 80:
                raise ValueError(f"entity must be a snake_case slug: {e!r}")
        return v


class TopicCandidateBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 件数の上下限は ``DEFAULT_PLANNER_POLICY`` が唯一の宣言元
    candidates: list[TopicCandidate] = Field(
        min_length=DEFAULT_PLANNER_POLICY.candidate_count_min,
        max_length=DEFAULT_PLANNER_POLICY.candidate_count_max,
    )


# -------------------------------------------------------- Workflow / Activity の入出力


@dataclass
class TopicPlannerInput:
    plan_date: str  # ISO YYYY-MM-DD
    strategy_profile_id: str = DEFAULT_STRATEGY_PROFILE_ID
    content_profile_id: str = DEFAULT_CONTENT_PROFILE_ID


@dataclass
class TopicPlannerResult:
    topic_plan_id: str
    topic: str
    #: True なら既存 Plan を再利用した（再生成していない）
    reused: bool
    analytics_mode: str


@dataclass
class FindPlanRequest:
    plan_date: str
    strategy_profile_id: str
    content_profile_id: str


@dataclass
class FindPlanResult:
    topic_plan_id: str | None = None
    topic: str | None = None
    analytics_mode: str | None = None


@dataclass
class MemoryItem:
    """過去・制作中の Topic（Content Memory。PostgreSQL の topic_plans / episodes から導出）。"""

    topic: str
    subject: str | None
    entities: list[str]
    era: str | None
    theme: str | None
    angle: str | None
    #: 起点日（plan_date または Episode の作成日）ISO
    day: str
    #: 出所の状態（Episode status または plan status）
    status: str


@dataclass
class FeaturePerformance:
    """Analytics から学んだ特徴（theme / angle / era / subject）ごとの相対成績。"""

    feature: str  # "theme:ritual_and_religion" など
    #: チャンネル平均比の成績（1.0 = 平均）
    relative_performance: float
    videos: int


@dataclass
class AnalyticsSummary:
    mode: str  # AnalyticsMode
    snapshot_id: str | None = None
    #: 0..1。データ量に応じた信頼度。0 なら analytics_fit は採点に効かない
    confidence: float = 0.0
    features: list[FeaturePerformance] = field(default_factory=list)
    #: 窓（"7d" / "28d" / "90d"）ごとのチャンネル合計（views など）。prompt 用の要約
    totals: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class PlanningContext:
    request: TopicPlannerInput
    analytics: AnalyticsSummary
    memory: list[MemoryItem]


@dataclass
class GenerateCandidatesRequest:
    context: PlanningContext
    round: int
    #: 前の round で全滅した subject（次の round の prompt で避けさせる）
    avoid_subjects: list[str] = field(default_factory=list)


@dataclass
class GenerateCandidatesResult:
    #: ``TopicCandidate.model_dump(mode="json")`` の list（validation 済み）
    candidates: list[dict]
    prompt_version: str


@dataclass
class SelectAndSaveRequest:
    context: PlanningContext
    candidates: list[dict]
    prompt_version: str
    #: 候補を生成した round（topic_candidates.round に記録）
    round: int = 1


@dataclass
class SelectAndSaveResult:
    #: 選べる候補が無ければ None（全て hard duplicate / cooldown）
    topic_plan_id: str | None
    topic: str | None
    reused: bool
    rejected_subjects: list[str] = field(default_factory=list)
