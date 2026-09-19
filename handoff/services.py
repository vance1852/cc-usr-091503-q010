"""领域服务：交接包构建、签署、发布、交接路径、版本通知。

关键规则：
1. 只有已确认的照护时间线事实才能进入交接包；未确认/过期事实绝不带入。
2. 每个板块由对应专业人员签署自己的部分；无已确认事实的板块标为“待处理”，
   并登记 PENDING 异常，绝不允许留空。
3. 存在未闭环的 BLOCK 异常，或存在有内容但未签署板块时，发布被阻断。
4. 三种交接路径（提前离馆/转机构/月嫂上门）校验不同，接收方确认后责任才转移。
5. 发布后的更正产生新版本；新版本发布时通知查看过旧版的授权联系人。
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import (
    CATEGORY_RESPONSIBLE_ROLE,
    CATEGORY_SECTION_KIND,
    Anomaly,
    AuthorizedContact,
    CareEvent,
    Delivery,
    DischargeCase,
    HandoffPacket,
    Notification,
    PacketSection,
    SectionConfirmation,
    Staff,
    VersionChange,
)

PENDING_DATA_PLACEHOLDER = "【待处理】当前暂无对应专业人员已确认的事实，不得据旧摘要填写；确认后补发新版本。"


# ---------------------------------------------------------------- 事实确认

@transaction.atomic
def confirm_event(event_id: int, staff_id: int) -> CareEvent:
    event = CareEvent.objects.select_for_update().get(pk=event_id)
    staff = Staff.objects.get(pk=staff_id)
    expected_role = CATEGORY_RESPONSIBLE_ROLE[event.category]
    if staff.role != expected_role:
        raise ValidationError(
            f"{event.get_category_display()}必须由{expected_role}确认，"
            f"当前确认人角色为{staff.role}"
        )
    if event.status != CareEvent.Status.CONFIRMED:
        event.status = CareEvent.Status.CONFIRMED
        event.confirmed_by = staff
        event.confirmed_at = timezone.now()
        event.save(update_fields=["status", "confirmed_by", "confirmed_at"])
    return event


@transaction.atomic
def correct_event(event_id: int, new_content: str, staff_id: int) -> CareEvent:
    """已发布事实的更正：内容更新后回到待确认，必须重新确认并经新版本发布。"""
    event = CareEvent.objects.select_for_update().get(pk=event_id)
    staff = Staff.objects.get(pk=staff_id)
    expected_role = CATEGORY_RESPONSIBLE_ROLE[event.category]
    if staff.role != expected_role:
        raise ValidationError("只有对应专业人员可以更正自己的事实")
    used_in_published = event.in_sections.filter(
        packet__status=HandoffPacket.Status.PUBLISHED
    ).exists()
    event.content = new_content
    if used_in_published:
        # 已发布内容被更正：旧事实失效，需要重新确认后走新版本
        event.status = CareEvent.Status.PENDING
        event.confirmed_by = None
        event.confirmed_at = None
    event.save()
    return event


# ---------------------------------------------------------------- 建包

def _default_staff_for_role(role: str) -> Staff | None:
    return Staff.objects.filter(role=role).order_by("id").first()


@transaction.atomic
def build_packet(case_id: int, subject: str) -> HandoffPacket:
    case = DischargeCase.objects.get(pk=case_id)
    # 已有草稿则在原草稿上刷新；否则取最新版本号 +1（已发布过即为新版本）
    packet = (
        HandoffPacket.objects.select_for_update()
        .filter(case=case, subject=subject, status=HandoffPacket.Status.DRAFT)
        .order_by("-version")
        .first()
    )
    if packet is None:
        last = (
            HandoffPacket.objects.filter(case=case, subject=subject)
            .order_by("-version")
            .first()
        )
        version = (last.version + 1) if last else 1
        packet = HandoffPacket.objects.create(
            case=case, subject=subject, version=version
        )
        if version > 1:
            VersionChange.objects.create(
                packet=packet,
                text="基于已发布版本生成更正草稿：仅纳入截至当前已确认的事实。",
            )

    confirmed_events = CareEvent.objects.filter(
        case=case, subject=subject, status=CareEvent.Status.CONFIRMED
    ).order_by("occurred_at")

    # (板块种类, 负责人) -> 已确认事实
    groups: dict[tuple[str, Staff], list[CareEvent]] = {}
    for event in confirmed_events:
        role = CATEGORY_RESPONSIBLE_ROLE[event.category]
        responsible = event.confirmed_by
        if responsible is None or responsible.role != role:
            continue
        kind = CATEGORY_SECTION_KIND[event.category]
        groups.setdefault((kind, responsible), []).append(event)

    # 该专业线下尚无任何已确认事实时，仍显式建立“待处理”板块，不留空白
    for category, role in CATEGORY_RESPONSIBLE_ROLE.items():
        kind = CATEGORY_SECTION_KIND[category]
        if any(k == kind and r.role == role for k, r in groups):
            continue
        staff = _default_staff_for_role(role)
        if staff is None:
            Anomaly.objects.get_or_create(
                case=case,
                packet=packet,
                section=None,
                severity=Anomaly.Severity.BLOCK,
                status=Anomaly.Status.OPEN,
                defaults={"description": f"缺少{role}角色人员，无法确认{kind}板块"},
            )
            continue
        groups.setdefault((kind, staff), [])

    for (kind, responsible), events in groups.items():
        section = PacketSection.objects.filter(
            packet=packet, kind=kind, responsible=responsible
        ).first()
        created = section is None
        if created:
            section = PacketSection(packet=packet, kind=kind,
                                    responsible=responsible)
        if events:
            new_content = "\n".join(
                f"[{e.occurred_at:%Y-%m-%d %H:%M}] {e.get_category_display()}：{e.content}"
                for e in events
            )
            new_source_ids = {e.id for e in events}
            old_source_ids = (
                set(section.source_events.values_list("id", flat=True))
                if not created else set()
            )
            already_signed = (not created) and SectionConfirmation.objects.filter(
                section=section
            ).exists()
            invalidated = False
            if already_signed and (
                section.content != new_content or old_source_ids != new_source_ids
            ):
                # 事实集合或内容已变：旧签署失效，需专业人员重新确认自己的部分
                SectionConfirmation.objects.filter(section=section).delete()
                section.state = PacketSection.State.DRAFT
                invalidated = True
            section.content = new_content
            if not already_signed and not invalidated:
                section.state = PacketSection.State.DRAFT
            section.save()
            section.source_events.set(events)
            Anomaly.objects.filter(
                packet=packet, section=section, status=Anomaly.Status.OPEN
            ).delete()
        else:
            section.content = PENDING_DATA_PLACEHOLDER
            section.state = PacketSection.State.PENDING_DATA
            section.save()
            section.source_events.clear()
            SectionConfirmation.objects.filter(section=section).delete()
            Anomaly.objects.get_or_create(
                case=case,
                packet=packet,
                section=section,
                severity=Anomaly.Severity.PENDING,
                status=Anomaly.Status.OPEN,
                defaults={
                    "description": (
                        f"{responsible.get_role_display()}{responsible.name}负责的"
                        f"{section.get_kind_display()}暂无已确认事实，标为待处理"
                    )
                },
            )

    return packet


# ---------------------------------------------------------------- 签署

@transaction.atomic
def sign_section(section_id: int, staff_id: int, signature: str) -> SectionConfirmation:
    section = PacketSection.objects.select_for_update().get(pk=section_id)
    staff = Staff.objects.get(pk=staff_id)
    if staff.id != section.responsible_id:
        raise ValidationError("专业人员只能确认自己负责的板块")
    if section.state == PacketSection.State.PENDING_DATA:
        raise ValidationError("待处理板块尚无已确认事实，不能签署；请先确认事实后重建")
    # OneToOne 唯一约束 + 行锁兜底并发重复签署
    if SectionConfirmation.objects.filter(section=section).exists():
        raise ValidationError("该板块已完成签署，不可重复签署")
    try:
        confirmation = SectionConfirmation.objects.create(
            section=section, confirmer=staff, signature=signature or staff.name
        )
    except Exception as exc:  # 并发下唯一约束竞争
        raise ValidationError("该板块已完成签署（并发冲突）") from exc
    section.state = PacketSection.State.CONFIRMED
    section.save(update_fields=["state"])
    return confirmation


# ---------------------------------------------------------------- 发布

def _publish_blockers(packet: HandoffPacket) -> list[str]:
    blockers: list[str] = []
    open_blocks = Anomaly.objects.filter(
        Q(packet=packet) | Q(packet__isnull=True, case=packet.case),
        status=Anomaly.Status.OPEN,
        severity=Anomaly.Severity.BLOCK,
    )
    for anomaly in open_blocks:
        blockers.append(f"未闭环阻断异常：{anomaly.description}")
    for section in packet.sections.all():
        if section.state == PacketSection.State.DRAFT:
            blockers.append(
                f"{section.get_kind_display()}（{section.responsible.name}）尚未签署"
            )
        if not section.content:
            blockers.append(f"{section.get_kind_display()}内容为空，禁止以空白发布")
    return blockers


@transaction.atomic
def publish_packet(packet_id: int) -> HandoffPacket:
    packet = HandoffPacket.objects.select_for_update().get(pk=packet_id)
    if packet.status == HandoffPacket.Status.PUBLISHED:
        raise ValidationError("该版本已发布")
    blockers = _publish_blockers(packet)
    if blockers:
        # 明确阻断，不吞掉任何问题
        raise ValidationError({"publish_blocked": blockers})

    prior_published = list(
        HandoffPacket.objects.select_for_update()
        .filter(case=packet.case, subject=packet.subject,
                status=HandoffPacket.Status.PUBLISHED)
    )
    packet.status = HandoffPacket.Status.PUBLISHED
    packet.published_at = timezone.now()
    packet.save(update_fields=["status", "published_at"])
    for prior in prior_published:
        prior.status = HandoffPacket.Status.SUPERSEDED
        prior.save(update_fields=["status"])
        # 旧版本上的待处理异常随版本替代而归档，不再计入案件当前未闭环项
        Anomaly.objects.filter(
            packet=prior, status=Anomaly.Status.OPEN
        ).update(
            status=Anomaly.Status.CLOSED,
            closed_at=timezone.now(),
            resolution=f"随 v{prior.version} 被 v{packet.version} 替代而归档",
        )

    # 通知已查看过任一旧版的授权联系人
    if prior_published:
        old_viewer_ids = (
            Delivery.objects.filter(packet__in=prior_published)
            .exclude(viewed_at=None)
            .values_list("contact_id", flat=True)
            .distinct()
        )
        for contact_id in old_viewer_ids:
            old_version = max(p.version for p in prior_published)
            Notification.objects.create(
                contact_id=contact_id,
                packet=packet,
                from_version=old_version,
                message=(
                    f"您查看过的{packet.get_subject_display()}交接摘要 v{old_version} "
                    f"已更新为 v{packet.version}，请以新版本为准，旧版内容已失效。"
                ),
            )
    return packet


# ---------------------------------------------------------------- 送达/回执/异常

@transaction.atomic
def deliver_packet(packet_id: int, contact_id: int) -> Delivery:
    packet = HandoffPacket.objects.get(pk=packet_id)
    if packet.status != HandoffPacket.Status.PUBLISHED:
        raise ValidationError("只能送达已发布的版本")
    contact = AuthorizedContact.objects.get(pk=contact_id, case_id=packet.case_id)
    delivery, _ = Delivery.objects.get_or_create(packet=packet, contact=contact)
    return delivery


@transaction.atomic
def mark_viewed(delivery_id: int) -> Delivery:
    delivery = Delivery.objects.select_for_update().get(pk=delivery_id)
    if delivery.viewed_at is None:
        delivery.viewed_at = timezone.now()
        delivery.save(update_fields=["viewed_at"])
    return delivery


@transaction.atomic
def resolve_anomaly(anomaly_id: int, resolution: str, staff_id: int) -> Anomaly:
    anomaly = Anomaly.objects.select_for_update().get(pk=anomaly_id)
    staff = Staff.objects.get(pk=staff_id)
    anomaly.status = Anomaly.Status.CLOSED
    anomaly.closed_at = timezone.now()
    anomaly.resolution = f"{staff.name}处理：{resolution}"
    anomaly.save(update_fields=["status", "closed_at", "resolution"])
    return anomaly


# ---------------------------------------------------------------- 离馆时间变更

@transaction.atomic
def reschedule(case_id: int, new_discharge_at) -> DischargeCase:
    case = DischargeCase.objects.select_for_update().get(pk=case_id)
    old = case.planned_discharge_at
    if timezone.is_naive(new_discharge_at):
        new_discharge_at = timezone.make_aware(new_discharge_at)
    case.planned_discharge_at = new_discharge_at
    case.save(update_fields=["planned_discharge_at"])

    # 已发布的包不静默改动：为每个对象生成更正草稿，记载版本变化
    for subject, _ in HandoffPacket.Subject.choices:
        current = (
            HandoffPacket.objects.filter(
                case=case, subject=subject, status=HandoffPacket.Status.PUBLISHED
            )
            .order_by("-version")
            .first()
        )
        if current is None:
            continue
        draft, created = (
            HandoffPacket.objects.select_for_update()
            .get_or_create(
                case=case, subject=subject, status=HandoffPacket.Status.DRAFT,
                defaults={"version": current.version + 1},
            )
        )
        if created:
            VersionChange.objects.create(
                packet=draft,
                text=(
                    f"家庭提前离馆，离馆时间由 {old:%Y-%m-%d %H:%M} "
                    f"变更为 {new_discharge_at:%Y-%m-%d %H:%M}，请重新核对全部建议。"
                ),
            )
        else:
            VersionChange.objects.create(
                packet=draft,
                text=f"离馆时间再次变更为 {new_discharge_at:%Y-%m-%d %H:%M}。",
            )
    return case


# ---------------------------------------------------------------- 交接路径

_PATH_RULES = {
    DischargeCase.Path.EARLY: {
        "label": "提前离馆返家",
        "need_target": True,
        "receiver_role": None,  # 由家属（授权联系人）确认
    },
    DischargeCase.Path.TRANSFER: {
        "label": "转往其他机构",
        "need_target": True,
        "receiver_role": "receiver",
    },
    DischargeCase.Path.NANNY_HOME: {
        "label": "月嫂上门继续服务",
        "need_target": True,
        "receiver_role": "nanny",
    },
}


@transaction.atomic
def set_transfer_path(case_id: int, path: str, target_name: str,
                      target_contact: str = "") -> DischargeCase:
    case = DischargeCase.objects.select_for_update().get(pk=case_id)
    if path not in _PATH_RULES:
        raise ValidationError("未知交接路径")
    if not target_name:
        raise ValidationError(f"{_PATH_RULES[path]['label']}必须登记接收目标")
    if case.transfer_status == DischargeCase.TransferStatus.ACKED:
        raise ValidationError("责任已转移，变更路径需新接收方重新确认")
    case.path = path
    case.target_name = target_name
    case.target_contact = target_contact
    case.transfer_status = DischargeCase.TransferStatus.PENDING
    case.save(update_fields=["path", "target_name", "target_contact", "transfer_status"])
    return case


@transaction.atomic
def acknowledge_transfer(case_id: int, staff_id: int | None = None,
                         contact_id: int | None = None) -> DischargeCase:
    case = DischargeCase.objects.select_for_update().get(pk=case_id)
    if not case.path:
        raise ValidationError("尚未选择交接路径")
    rules = _PATH_RULES[case.path]
    if rules["receiver_role"] is None:
        # 提前离馆：由家属授权联系人确认接收
        if contact_id is None:
            raise ValidationError("提前离馆须由家属授权联系人确认")
        AuthorizedContact.objects.get(pk=contact_id, case=case)
    else:
        if staff_id is None:
            raise ValidationError("该路径须由接收方专业人员确认")
        staff = Staff.objects.get(pk=staff_id)
        if staff.role != rules["receiver_role"]:
            raise ValidationError(
                f"{rules['label']}的接收方须为{rules['receiver_role']}角色"
            )
    # 交接路径要求当前建议均已发布，避免把草稿责任转出去
    for subject, _ in HandoffPacket.Subject.choices:
        current = current_packet(case.id, subject)
        if current is None:
            raise ValidationError(f"{subject} 尚无已发布交接包，不能转移责任")
    case.transfer_status = DischargeCase.TransferStatus.ACKED
    case.save(update_fields=["transfer_status"])
    return case


# ---------------------------------------------------------------- 查询

def current_packet(case_id: int, subject: str) -> HandoffPacket | None:
    return (
        HandoffPacket.objects.filter(
            case_id=case_id, subject=subject, status=HandoffPacket.Status.PUBLISHED
        )
        .order_by("-version")
        .first()
    )


def handoff_page(case_id: int) -> dict:
    """家庭打开交接页：当前有效建议 / 待办 / 联系渠道 / 版本变化 分区呈现。"""
    case = DischargeCase.objects.get(pk=case_id)
    subjects_payload = {}
    for subject, label in HandoffPacket.Subject.choices:
        current = current_packet(case_id, subject)
        if current is None:
            subjects_payload[subject] = {"current_version": None, "sections": []}
            continue
        sections = []
        for section in current.sections.prefetch_related(
            "confirmation__confirmer", "responsible"
        ).all():
            sections.append({
                "kind": section.kind,
                "kind_display": section.get_kind_display(),
                "state": section.state,
                "state_display": section.get_state_display(),
                "responsible": str(section.responsible),
                "content": section.content,
                "confirmed_by": (
                    section.confirmation.confirmer.name
                    if hasattr(section, "confirmation") else None
                ),
                "confirmed_at": (
                    section.confirmation.confirmed_at
                    if hasattr(section, "confirmation") else None
                ),
            })
        subjects_payload[subject] = {
            "current_version": current.version,
            "published_at": current.published_at,
            "sections": sections,
            "changes": [
                {"text": c.text, "at": c.created_at}
                for c in current.changes.order_by("id")
            ],
            "pending_anomalies": [
                a.description for a in current.anomalies.filter(
                    status=Anomaly.Status.OPEN, severity=Anomaly.Severity.PENDING
                )
            ],
        }

    pending_todos = []
    for subject_payload in subjects_payload.values():
        for s in subject_payload.get("sections", []):
            if s["kind"] == PacketSection.Kind.TODO:
                pending_todos.append({
                    "content": s["content"], "state": s["state_display"],
                })
    pending_todos.extend(
        {"content": a.description, "state": "未闭环-待处理"}
        for a in case.anomalies.filter(status=Anomaly.Status.OPEN)
    )

    return {
        "case": {
            "id": case.id,
            "mother": case.mother_name,
            "baby": case.baby_name,
            "planned_discharge_at": case.planned_discharge_at,
            "path": case.path,
            "path_display": case.get_path_display(),
            "transfer_status": case.transfer_status,
            "transfer_status_display": case.get_transfer_status_display(),
            "target": case.target_name,
            "responsibility_transferred": case.responsibility_transferred,
        },
        "current_effective_advice": subjects_payload,
        "pending_items": pending_todos,
        "contact_channels": [
            {"name": c.name, "relation": c.relation, "channel": c.channel}
            for c in case.contacts.all()
        ],
    }
