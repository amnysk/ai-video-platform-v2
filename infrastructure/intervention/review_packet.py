"""EpisodeView → ReviewPacket（JSON）と Markdown。

JP-MAS-001 §8、``docs/domain/review-packet.md``。

- 素材は bucket / object_key / sha256 の参照だけ。signed URL を作らない。
- 文字列は全て書き出し前に安全化する（ログと同じ ``sanitize_text``: 秘密のパターン・URL の
  query / userinfo を落とす）。コメント本文・command の結果のような人や AI が入れた文字列にも
  秘密が混ざりうるため。
- 欠けているもの・確かめていないものは ``missing`` に列挙する（補完しない）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from contracts.intervention import EvidenceCheck
from contracts.research import ResearchArtifactType
from contracts.review_packet import (
    PacketAssetRef,
    PacketCandidate,
    PacketChangeSet,
    PacketCommand,
    PacketComment,
    PacketControl,
    PacketCosts,
    PacketDecision,
    PacketEpisodeState,
    PacketEvidence,
    PacketMissingItem,
    PacketRationale,
    PacketReservation,
    PacketReservations,
    PacketRevision,
    PacketScript,
    PacketScriptSegment,
    PacketUnmeasured,
    ReviewPacket,
)
from infrastructure.intervention.read_model import (
    DecisionView,
    EpisodeView,
    ReservationView,
)
from infrastructure.intervention.records import CONFIGURED_MODEL_NOTE
from infrastructure.logging.redaction import sanitize_text

#: 判断記録が付くべき成果物の slot（生成した役割の記録が残る工程）
_RECORDED_SLOTS = ("script", "storyboard")


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)[0]
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


def _decision(view: DecisionView) -> PacketDecision:
    p = view.payload
    return PacketDecision(
        decision_id=view.decision_id,
        decision_type=view.decision_type,
        derived=view.derived,
        actor_type=view.actor_type,
        actor_id=view.actor_id,
        question=p.get("question"),
        candidates=[
            PacketCandidate(
                id=str(c.get("id")),
                summary=str(c.get("summary", "")),
                benefits=list(c.get("benefits") or []),
                concerns=list(c.get("concerns") or []),
                scores=c.get("scores"),
                assessment_kind=c.get("assessment_kind"),
                self_assessed_scores=c.get("self_assessed_scores"),
                rejected_reason=c.get("rejected_reason"),
            )
            for c in p.get("candidates") or []
        ],
        selected_candidate=p.get("selected_candidate"),
        rationale=PacketRationale(
            kind=str(p.get("rationale_kind", "none")), text=p.get("rationale")
        ),
        evidence_refs=list(p.get("evidence_refs") or []),
        evidence_check=p.get("evidence_check"),
        tool_invocation_refs=list(p.get("tool_invocation_refs") or []),
        input_snapshot_ref=p.get("input_snapshot_ref"),
        target_revisions=list(p.get("target_revisions") or []),
        result_revision=p.get("result_revision"),
        uncertainties=list(p.get("uncertainties") or []),
        scene_id=view.scene_id,
        workflow_id=view.workflow_id,
        workflow_run_id=view.workflow_run_id,
    )


def _reservation(view: ReservationView) -> PacketReservation:
    return PacketReservation(
        reservation_id=view.reservation_id,
        provider=view.provider,
        scene_id=view.scene_id,
        estimated_cost_usd=view.estimated_cost_usd,
    )


def _missing(view: EpisodeView) -> list[PacketMissingItem]:
    items: list[tuple[str, str]] = []
    if not view.manifest.persisted:
        items.append(
            ("manifest", "no adopted manifest is recorded; members are the current artifacts")
        )
    members = {m.slot: m for m in view.manifest.members}
    if "script" not in members:
        items.append(("script", "no script revision"))
    elif view.script is None:
        items.append(("script", "the script revision could not be read"))
    for member in view.manifest.members:
        if not member.readable:
            items.append((f"revision:{member.slot}", "the stored body could not be read"))
        for reason in member.stale_reasons:
            items.append((f"stale:{member.slot}", reason))
    for annotation_id in view.stale_approvals:
        items.append(
            (f"approval:{annotation_id}", "bound to a manifest that is no longer adopted (INV-45)")
        )
    decided = {d.result_revision_id for d in view.decisions if d.result_revision_id}
    for slot in _RECORDED_SLOTS:
        member = members.get(slot)
        if member is not None and member.revision_id not in decided:
            items.append((f"decision:{slot}", "no decision record for the adopted revision"))
    for decision in view.decisions:
        if decision.derived:
            items.append(
                (
                    f"decision:{decision.decision_type}",
                    "derived from stored data when read; not recorded at decision time",
                )
            )
        if decision.payload.get("evidence_check") == EvidenceCheck.UNVERIFIED_REF.value:
            items.append(
                (
                    f"evidence:{decision.decision_id}",
                    "cited evidence was not found in the input snapshot",
                )
            )
    if view.agent_runs:
        items.append(("model", CONFIGURED_MODEL_NOTE))
        if any(r.token_usage is None for r in view.agent_runs):
            items.append(("token_usage", "token usage was not parsed (null, not zero)"))
    items.append(("cost:llm", "LLM cost is not recorded (unmeasured, not zero)"))
    for unmeasured in view.costs.unmeasured:
        items.append(
            (f"cost:{unmeasured.provider}", f"{unmeasured.calls} call(s) without a cost estimate")
        )
    verifications = [
        e
        for e in view.evidence
        if e.artifact_type == ResearchArtifactType.RESEARCH_SCRIPT_VERIFICATION.value
        and e.is_current
    ]
    if not verifications:
        items.append(("evidence", "no script verification for this episode"))
    elif view.script is not None and all(
        e.script_revision_id != view.script.revision_id for e in verifications
    ):
        items.append(("evidence", "the script verification is for another script revision"))
    for reservation in view.reservations.unknown_outcome:
        items.append(
            (
                f"reservation:{reservation.reservation_id}",
                "dispatched without a provider job reference; outcome unknown (not resent)",
            )
        )
    if view.unreadable_revisions:
        items.append(
            ("revisions", f"{len(view.unreadable_revisions)} stored body(ies) could not be read")
        )
    return [PacketMissingItem(item=i, reason=r) for i, r in items]


def build_packet(view: EpisodeView) -> ReviewPacket:
    """読み取りモデルから ReviewPacket を作る。何も読み書きしない（純粋）。"""
    control = view.control
    packet = ReviewPacket(
        episode_id=view.episode_id,
        manifest_id=view.manifest.manifest_id,
        manifest_persisted=view.manifest.persisted,
        episode=PacketEpisodeState(
            episode_id=view.episode_id,
            phase=view.phase,
            topic=view.topic,
            topic_plan_id=view.topic_plan_id,
            blocked_reason=view.blocked_reason,
            control=PacketControl(
                mode=control.mode,
                control_state=control.control_state,
                control_revision=control.control_revision,
                review_points=list(control.review_points),
                waiting_point=control.waiting_point,
                waiting_manifest_id=control.waiting_manifest_id,
                persisted=control.persisted,
            ),
        ),
        target_revisions=[
            PacketRevision(
                slot=m.slot,
                revision_id=m.revision_id,
                artifact_type=m.artifact_type,
                scene_id=m.scene_id,
                version=m.version,
                stale=m.stale,
                stale_reasons=list(m.stale_reasons),
            )
            for m in view.manifest.members
        ],
        decisions=[_decision(d) for d in view.decisions],
        script=None
        if view.script is None
        else PacketScript(
            revision_id=view.script.revision_id,
            title=view.script.title,
            hook=view.script.hook,
            language=view.script.language,
            total_duration_ms=view.script.total_duration_ms,
            segments=[
                PacketScriptSegment(
                    segment_id=s.segment_id,
                    narration=s.narration,
                    visual=s.visual,
                    duration_ms=s.duration_ms,
                )
                for s in view.script.segments
            ],
        ),
        evidence=[PacketEvidence(**e.model_dump()) for e in view.evidence],
        reviews=[
            PacketComment(
                annotation_id=a.annotation_id,
                kind=a.kind,
                status=a.status,
                old_version=a.old_version,
                approval_current=a.approval_current,
                anchor=dict(a.anchor),
                body=a.body,
                actor_type=a.actor_type,
                actor_id=a.actor_id,
                manifest_id=a.manifest_id,
                created_at=a.created_at,
            )
            for a in view.annotations
        ],
        stale_approvals=list(view.stale_approvals),
        commands=[
            PacketCommand(
                command_id=c.command_id,
                type=c.type,
                status=c.status,
                done=c.done,
                display_status=c.display_status,
            )
            for c in view.commands
        ],
        change_sets=[
            PacketChangeSet(
                change_set_id=c.change_set_id,
                kind=c.kind,
                status=c.status,
                base_revision_id=c.base_revision_id,
                actor_type=c.actor_type,
                estimated_cost_usd=c.estimated_cost_usd,
                impact=c.impact,
            )
            for c in view.change_sets
        ],
        costs=PacketCosts(
            provider_usd=view.costs.provider_usd,
            research_usd=view.costs.research_usd,
            measured_usd=view.costs.measured_usd,
            unmeasured=[
                PacketUnmeasured(provider=u.provider, calls=u.calls) for u in view.costs.unmeasured
            ],
            llm=view.costs.llm,
        ),
        open_reservations=PacketReservations(
            in_flight=[_reservation(r) for r in view.reservations.in_flight],
            unknown_outcome=[_reservation(r) for r in view.reservations.unknown_outcome],
            undispatched=[_reservation(r) for r in view.reservations.undispatched],
        ),
        asset_refs=[
            PacketAssetRef(
                revision_id=m.revision_id,
                slot=m.slot,
                artifact_type=m.artifact_type,
                bucket=m.bucket,
                object_key=m.object_key,
                sha256=m.sha256,
            )
            for m in view.manifest.members
        ],
        missing=_missing(view),
    )
    # 最後に全ての文字列を安全化して検証し直す（秘密・signed URL を書き出さない）
    return ReviewPacket.model_validate(_clean(packet.model_dump(mode="json")))


# --------------------------------------------------------------------------- Markdown


def _md(text: object) -> str:
    """表のセルを壊さない（改行・縦棒）。"""
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(packet: ReviewPacket) -> str:
    """人が読む形。中身は ``packet`` だけから作る（JSON と同じ事実）。"""
    ep = packet.episode
    lines: list[str] = [
        f"# ReviewPacket {packet.episode_id}",
        "",
        f"- phase: `{ep.phase}` / control: `{ep.control.control_state}` "
        f"(mode `{ep.control.mode}`, revision {ep.control.control_revision}"
        f"{'' if ep.control.persisted else ', defaults'})",
        f"- waiting point: {ep.control.waiting_point or '-'}",
        f"- manifest: {packet.manifest_id or '(none recorded; current artifacts)'}",
        f"- topic: {ep.topic or '-'}",
    ]
    if ep.blocked_reason:
        lines.append(f"- blocked: {ep.blocked_reason}")

    lines += ["", "## 対象 revision", "", "| slot | revision | type | stale |", "|---|---|---|---|"]
    for r in packet.target_revisions:
        stale = "; ".join(r.stale_reasons) if r.stale else ""
        lines.append(f"| {_md(r.slot)} | `{r.revision_id}` | {r.artifact_type} | {_md(stale)} |")

    lines += ["", "## 台本", ""]
    if packet.script is None:
        lines.append("（台本なし・読めない）")
    else:
        s = packet.script
        lines += [f"**{s.title}** — {s.hook}  (revision `{s.revision_id}`)", ""]
        lines += ["| segment | ms | narration | visual |", "|---|---|---|---|"]
        for g in s.segments:
            lines.append(
                f"| {g.segment_id} | {g.duration_ms} | {_md(g.narration)} | {_md(g.visual)} |"
            )

    lines += ["", "## 判断記録", ""]
    if not packet.decisions:
        lines.append("（記録なし）")
    for d in packet.decisions:
        derived = "（読み取り時に導出）" if d.derived else ""
        lines += [
            f"### {d.decision_type} {derived}",
            f"- 問い: {d.question or '-'}",
            f"- 選択: {d.selected_candidate or '-'}",
            f"- 理由（{d.rationale.kind}）: {d.rationale.text or '（記録なし）'}",
            f"- tool 参照: {', '.join(d.tool_invocation_refs) or '-'}",
            f"- 結果 revision: {d.result_revision or '-'}",
            f"- 根拠: {', '.join(d.evidence_refs) or '-'}（{d.evidence_check or '-'}）",
        ]
        for c in d.candidates:
            note = f" — 不採用: {c.rejected_reason}" if c.rejected_reason else ""
            scores = f" scores({c.assessment_kind})={c.scores}" if c.scores else ""
            selfs = f" self_assessed={c.self_assessed_scores}" if c.self_assessed_scores else ""
            lines.append(f"  - `{c.id}` {_md(c.summary)}{scores}{selfs}{note}")
        for u in d.uncertainties:
            lines.append(f"  - 留保: {u}")
        lines.append("")

    lines += ["## レビュー・コメント", ""]
    if not packet.reviews:
        lines.append("（なし）")
    for a in packet.reviews:
        flags = [a.status]
        if a.old_version:
            flags.append("古い版へのコメント")
        if a.approval_current is False:
            flags.append("旧 manifest の承認（流用しない）")
        lines.append(f"- [{a.kind}] ({', '.join(flags)}) {_md(a.body)} — anchor {a.anchor}")

    lines += ["", "## command", ""]
    for c in packet.commands:
        lines.append(f"- `{c.command_id}` {c.type}: {c.display_status}")
    if not packet.commands:
        lines.append("（なし）")
    if packet.change_sets:
        lines += ["", "## 変更案", ""]
        for cs in packet.change_sets:
            lines.append(
                f"- `{cs.change_set_id}` {cs.kind} [{cs.status}] by {cs.actor_type}, "
                f"見積り {cs.estimated_cost_usd or '-'} USD, 影響 {cs.impact or '-'}"
            )

    costs = packet.costs
    lines += [
        "",
        "## 費用",
        "",
        f"- 計測済み（見積り）: {costs.measured_usd} USD "
        f"(provider {costs.provider_usd} / research {costs.research_usd})",
        f"- LLM: {costs.llm}（0 ではない）",
    ]
    for u in costs.unmeasured:
        lines.append(f"- 見積りなし: {u.provider} × {u.calls}")
    res = packet.open_reservations
    lines += [
        f"- 送信済み・未精算: {len(res.in_flight)} / 成否不明: {len(res.unknown_outcome)} "
        f"/ 未送信: {len(res.undispatched)}",
        "",
        "## 素材の参照（bucket / key / sha256）",
        "",
    ]
    for ref in packet.asset_refs:
        lines.append(f"- {ref.slot}: `{ref.bucket}/{ref.object_key}` sha256 `{ref.sha256}`")
    for e in packet.evidence:
        lines.append(
            f"- evidence {e.artifact_type}: `{e.bucket}/{e.object_key}` sha256 `{e.sha256}`"
            f" verdict {e.verdict or '-'}"
        )

    lines += ["", "## 欠けている・確かめていない事柄", ""]
    for m in packet.missing:
        lines.append(f"- {m.item}: {m.reason}")
    return "\n".join(lines) + "\n"


__all__ = ["build_packet", "render_markdown"]
