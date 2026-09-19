"""业务逻辑层：时间线确认、摘要生成/签署/发布/更正、交接路径、离馆变更、通知与追踪。"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .exceptions import (
    DomainConflictError,
    DomainNotFoundError,
    DomainPermissionError,
    DomainValidationError,
)
from .models import (
    Anomaly,
    AuthorizedContact,
    Case,
    ContactChannel,
    DeliveryReceipt,
    FollowUpPlan,
    Handover,
    Notification,
    Professional,
    ProfessionalRole,
    Subject,
    Summary,
    SummarySection,
    SummaryVersion,
    SummaryView,
    TimelineEvent,
    TodoItem,
)

# 时间线类别 -> 有权确认该类别事实的专业角色
CATEGORY_ROLE_MAP = {
    TimelineEvent.Category.NIGHT_OBSERVATION: ProfessionalRole.MATRON,
    TimelineEvent.Category.NUTRITION: ProfessionalRole.NUTRITIONIST,
    TimelineEvent.Category.SUPPLY: ProfessionalRole.NURSE,
}

# 摘要章节 -> 签署角色 / 对应时间线类别
SECTION_PLAN = [
    (SummarySection.Category.NIGHT_CARE, ProfessionalRole.MATRON,
     TimelineEvent.Category.NIGHT_OBSERVATION),
    (SummarySection.Category.NUTRITION, ProfessionalRole.NUTRITIONIST,
     TimelineEvent.Category.NUTRITION),
    (SummarySection.Category.SUPPLIES, ProfessionalRole.NURSE,
     TimelineEvent.Category.SUPPLY),
]


def get_professional(professional_id):
    try:
        return Professional.objects.get(pk=professional_id)
    except Professional.DoesNotExist:
        raise DomainNotFoundError(f"专业人员 {professional_id} 不存在")


def get_case(case_id):
    try:
        return Case.objects.get(pk=case_id)
    except Case.DoesNotExist:
        raise DomainNotFoundError(f"案例 {case_id} 不存在")


# ---------------------------------------------------------------- 时间线

def confirm_timeline_event(event, professional):
    """专业人员确认自己负责类别的时间线事实。幂等：重复确认直接返回。"""
    required = CATEGORY_ROLE_MAP.get(event.category)
    if required and professional.role != required:
        raise DomainPermissionError(
            f"{event.get_category_display()}须由{ProfessionalRole(required).label}确认，"
            f"当前操作者为{professional.get_role_display()}"
        )
    if event.status == TimelineEvent.Status.CONFIRMED:
        return event
    if event.status == TimelineEvent.Status.RETRACTED:
        raise DomainConflictError("该记录已撤回，不能确认")
    event.status = TimelineEvent.Status.CONFIRMED
    event.confirmed_by = professional
    event.confirmed_at = timezone.now()
    event.save(update_fields=["status", "confirmed_by", "confirmed_at"])
    return event


# ---------------------------------------------------------------- 摘要生成

def _event_payload(event):
    return {
        "event_id": event.id,
        "content": event.content,
        "occurred_at": event.occurred_at.isoformat(),
        "confirmed_by": event.confirmed_by.name if event.confirmed_by else None,
        "confirmed_at": event.confirmed_at.isoformat() if event.confirmed_at else None,
    }


def _build_fact_sections(version, case, subject):
    """从已确认时间线事实生成专业章节；待确认事实单独列出，不进入建议也不掩盖。"""
    events = case.timeline_events.filter(subject=subject)
    for category, role, event_category in SECTION_PLAN:
        confirmed = [
            _event_payload(e)
            for e in events.filter(category=event_category,
                                   status=TimelineEvent.Status.CONFIRMED)
        ]
        pending = [
            {"event_id": e.id, "content": e.content, "occurred_at": e.occurred_at.isoformat()}
            for e in events.filter(category=event_category,
                                   status=TimelineEvent.Status.PENDING)
        ]
        SummarySection.objects.create(
            version=version,
            category=category,
            required_role=role,
            content={
                "items": confirmed,
                "pending_confirmation": pending,
                "note": "" if confirmed else "暂无已确认记录",
            },
        )


def _build_todos_section(version, case, subject):
    todos = case.todos.filter(subject=subject, status=TodoItem.Status.PENDING)
    SummarySection.objects.create(
        version=version,
        category=SummarySection.Category.TODOS,
        required_role=ProfessionalRole.COORDINATOR,
        content={
            "items": [
                {
                    "todo_id": t.id,
                    "title": t.title,
                    "detail": t.detail,
                    "due_at": t.due_at.isoformat() if t.due_at else None,
                }
                for t in todos
            ],
            "note": "" if todos.exists() else "暂无待办事项",
        },
    )


def _build_contacts_section(version, case, subject):
    channels = case.contact_channels.all()
    followups = case.followups.filter(
        subject=subject, status=FollowUpPlan.Status.SCHEDULED
    ).order_by("scheduled_at")
    SummarySection.objects.create(
        version=version,
        category=SummarySection.Category.CONTACTS,
        required_role=ProfessionalRole.COORDINATOR,
        content={
            "channels": [
                {"label": c.label, "kind": c.kind, "value": c.value, "hours": c.hours}
                for c in channels
            ],
            "followups": [
                {
                    "followup_id": f.id,
                    "purpose": f.purpose,
                    "channel": f.channel,
                    "scheduled_at": f.scheduled_at.isoformat(),
                }
                for f in followups
            ],
        },
    )


def _anomalies_content(case, subject):
    open_anomalies = case.anomalies.filter(
        subject=subject, status=Anomaly.Status.OPEN
    ).order_by("id")
    return {
        "open": [
            {
                "anomaly_id": a.id,
                "description": a.description,
                "severity": a.severity,
                "status_label": "待处理",
            }
            for a in open_anomalies
        ],
        "note": "无未闭环异常" if not open_anomalies.exists()
        else "以下异常尚未闭环，列为待处理事项，返家后须跟进",
    }


def _build_anomalies_section(version, case, subject):
    SummarySection.objects.create(
        version=version,
        category=SummarySection.Category.ANOMALIES,
        required_role=None,  # 系统章节，发布时自动刷新
        content=_anomalies_content(case, subject),
    )


def compose_summaries(case, coordinator):
    """为产妇与婴儿分别生成摘要草稿（仅纳入已确认事实）。"""
    if coordinator.role != ProfessionalRole.COORDINATOR:
        raise DomainPermissionError("只有服务专员可以生成返家摘要")
    versions = {}
    with transaction.atomic():
        for subject in (Subject.MOTHER, Subject.BABY):
            summary, _ = Summary.objects.get_or_create(case=case, subject=subject)
            if summary.versions.filter(status=SummaryVersion.Status.DRAFT).exists():
                raise DomainConflictError(
                    f"{Subject(subject).label}摘要存在未发布草稿，请先发布或更正"
                )
            next_no = (summary.versions.order_by("-version_no")
                       .values_list("version_no", flat=True).first() or 0) + 1
            version = SummaryVersion.objects.create(
                summary=summary, version_no=next_no, created_by=coordinator
            )
            _build_fact_sections(version, case, subject)
            _build_todos_section(version, case, subject)
            _build_contacts_section(version, case, subject)
            _build_anomalies_section(version, case, subject)
            versions[subject] = version
    return versions


# ---------------------------------------------------------------- 签署

def sign_section(section_id, professional):
    """专业人员签署自己负责的章节。并发安全：同事务内锁定行并校验状态。"""
    with transaction.atomic():
        section = (
            SummarySection.objects.select_for_update()
            .select_related("version")
            .get(pk=section_id)
        )
        if section.version.status != SummaryVersion.Status.DRAFT:
            raise DomainConflictError("该版本已发布或废止，不能签署")
        if not section.needs_signoff:
            raise DomainValidationError("系统生成的章节无需签署")
        if professional.role != section.required_role:
            raise DomainPermissionError(
                f"该章节须由{ProfessionalRole(section.required_role).label}签署"
            )
        if section.is_signed:
            if section.signed_by_id == professional.id:
                return section  # 同人重复签署，幂等返回
            raise DomainConflictError(
                f"该章节已由 {section.signed_by.name} 于 "
                f"{timezone.localtime(section.signed_at):%Y-%m-%d %H:%M} 签署"
            )
        section.signed_by = professional
        section.signed_at = timezone.now()
        section.save(update_fields=["signed_by", "signed_at"])
        return section


# ---------------------------------------------------------------- 发布 / 更正

def _notify_old_version_viewers(case, old_version, new_version):
    """通知所有查看过旧版本的授权联系人：摘要已更正。"""
    viewer_ids = (
        SummaryView.objects.filter(version=old_version)
        .values_list("contact_id", flat=True).distinct()
    )
    for contact in AuthorizedContact.objects.filter(id__in=viewer_ids):
        Notification.objects.create(
            case=case,
            contact=contact,
            version=new_version,
            kind=Notification.Kind.CORRECTION,
            message=(
                f"您查看过的返家摘要（{old_version.summary.get_subject_display()} "
                f"v{old_version.version_no}）已更正为 v{new_version.version_no}。"
                f"更正说明：{new_version.change_note or '无'}。请以新版本为准。"
            ),
        )


def publish_version(version, publisher):
    """发布摘要版本：全部章节签署 + 无未闭环阻断异常。旧版本废止并通知其查看者。"""
    with transaction.atomic():
        version = (
            SummaryVersion.objects.select_for_update()
            .select_related("summary__case")
            .get(pk=version.pk)
        )
        if version.status != SummaryVersion.Status.DRAFT:
            raise DomainConflictError("只有草稿版本可以发布")
        case = version.summary.case
        subject = version.summary.subject

        unsigned = [
            s.get_category_display()
            for s in version.sections.all()
            if s.needs_signoff and not s.is_signed
        ]
        if unsigned:
            raise DomainConflictError(
                "以下章节尚未签署：" + "、".join(unsigned),
                extra={"unsigned_sections": unsigned},
            )

        blocking = list(
            case.anomalies.filter(
                subject=subject,
                severity=Anomaly.Severity.BLOCKING,
                status=Anomaly.Status.OPEN,
            )
        )
        if blocking:
            raise DomainConflictError(
                "存在未闭环的阻断级异常，禁止发布",
                extra={"blocking_anomalies": [
                    {"anomaly_id": a.id, "description": a.description} for a in blocking
                ]},
            )

        # 发布时刻刷新异常章节，保证快照准确（提示级异常明确列为待处理）
        anomalies_section = version.sections.get(
            category=SummarySection.Category.ANOMALIES
        )
        anomalies_section.content = _anomalies_content(case, subject)
        anomalies_section.save(update_fields=["content"])

        old_version = version.summary.versions.filter(
            status=SummaryVersion.Status.PUBLISHED
        ).first()
        now = timezone.now()
        if old_version:
            old_version.status = SummaryVersion.Status.SUPERSEDED
            old_version.save(update_fields=["status"])

        version.status = SummaryVersion.Status.PUBLISHED
        version.published_at = now
        version.save(update_fields=["status", "published_at"])

        # 送达登记：每个授权联系人一条回执记录
        for contact in case.contacts.all():
            DeliveryReceipt.objects.create(
                version=version, contact=contact, delivered_at=now
            )

        if old_version:
            _notify_old_version_viewers(case, old_version, version)
    return version


def correct_summary(summary, editor, change_note, section_updates):
    """对已发布摘要更正：生成新草稿版本。内容变化的章节需重新签署，未变化的保留签署。"""
    if not change_note.strip():
        raise DomainValidationError("更正必须填写更正说明")
    current = summary.versions.filter(status=SummaryVersion.Status.PUBLISHED).first()
    if current is None:
        raise DomainConflictError("当前没有已发布版本，无法更正")
    with transaction.atomic():
        next_no = summary.versions.order_by("-version_no").first().version_no + 1
        new_version = SummaryVersion.objects.create(
            summary=summary,
            version_no=next_no,
            change_note=change_note,
            created_by=editor,
        )
        for old_section in current.sections.all():
            update = section_updates.get(old_section.category)
            changed = update is not None
            SummarySection.objects.create(
                version=new_version,
                category=old_section.category,
                required_role=old_section.required_role,
                content=update if changed else old_section.content,
                # 未变化的章节沿用原签署；变化的章节必须重新签署
                signed_by=None if changed else old_section.signed_by,
                signed_at=None if changed else old_section.signed_at,
            )
    return new_version


# ---------------------------------------------------------------- 交接路径

def initiate_handover(case, actor, path_type, receiver_name, receiver_org="",
                      receiver_contact="", matron=None, service_address="",
                      scheduled_at=None, notes=""):
    """发起交接。三种路径各有必填校验；交接前两份摘要都必须已发布。"""
    if actor.role != ProfessionalRole.COORDINATOR:
        raise DomainPermissionError("只有服务专员可以发起交接")
    if case.handovers.filter(status=Handover.Status.INITIATED).exists():
        raise DomainConflictError("已存在待接收方确认的交接，请先完成或取消")
    unpublished = [
        Subject(s).label
        for s in (Subject.MOTHER, Subject.BABY)
        if not Summary.objects.filter(case=case, subject=s)
        .exclude(versions__isnull=True)
        .filter(versions__status=SummaryVersion.Status.PUBLISHED)
        .exists()
    ]
    if unpublished:
        raise DomainConflictError(
            "、".join(unpublished) + "摘要尚未发布，不能发起交接"
        )

    scheduled_at = scheduled_at or case.expected_discharge_at
    if path_type == Handover.PathType.EARLY_DISCHARGE:
        if scheduled_at >= case.expected_discharge_at:
            raise DomainValidationError("提前离馆的交接时间必须早于预计离馆时间")
    elif path_type == Handover.PathType.TRANSFER_FACILITY:
        if not receiver_org:
            raise DomainValidationError("转往其他机构必须填写接收机构名称")
    elif path_type == Handover.PathType.HOME_SERVICE:
        if matron is None or matron.role != ProfessionalRole.MATRON:
            raise DomainValidationError("月嫂上门服务必须指定月嫂")
        if not service_address:
            raise DomainValidationError("月嫂上门服务必须填写服务地址")
    else:
        raise DomainValidationError(f"未知的交接路径：{path_type}")

    return Handover.objects.create(
        case=case,
        path_type=path_type,
        receiver_name=receiver_name,
        receiver_org=receiver_org,
        receiver_contact=receiver_contact,
        matron=matron,
        service_address=service_address,
        scheduled_at=scheduled_at,
        initiated_by=actor,
        notes=notes,
    )


def receiver_confirm(handover_id, receiver_name):
    """接收方确认，责任自此转移。"""
    with transaction.atomic():
        handover = Handover.objects.select_for_update().get(pk=handover_id)
        if handover.status != Handover.Status.INITIATED:
            raise DomainConflictError("该交接不在待确认状态")
        now = timezone.now()
        handover.status = Handover.Status.RECEIVER_CONFIRMED
        handover.receiver_name = receiver_name or handover.receiver_name
        handover.receiver_confirmed_at = now
        handover.save(update_fields=["status", "receiver_name", "receiver_confirmed_at"])
        case = handover.case
        case.status = Case.Status.DISCHARGED
        case.actual_discharge_at = handover.scheduled_at
        case.save(update_fields=["status", "actual_discharge_at"])
    return handover


# ---------------------------------------------------------------- 离馆时间变更

def change_discharge(case, new_time, actor, reason=""):
    """变更预计离馆时间：重排随访与待办，更新待确认交接，通知授权联系人。"""
    if actor.role != ProfessionalRole.COORDINATOR:
        raise DomainPermissionError("只有服务专员可以变更离馆时间")
    old_time = case.expected_discharge_at
    if new_time == old_time:
        raise DomainValidationError("新离馆时间与原时间相同")
    delta = new_time - old_time
    report = {"old": old_time, "new": new_time, "rescheduled_followups": 0,
              "rescheduled_todos": 0, "handovers_updated": 0}

    with transaction.atomic():
        case.expected_discharge_at = new_time
        case.save(update_fields=["expected_discharge_at"])

        for followup in case.followups.filter(status=FollowUpPlan.Status.SCHEDULED):
            followup.scheduled_at = new_time + timedelta(days=followup.offset_days)
            followup.save(update_fields=["scheduled_at"])
            report["rescheduled_followups"] += 1

        for todo in case.todos.filter(status=TodoItem.Status.PENDING, due_at__isnull=False):
            todo.due_at = todo.due_at + delta
            todo.save(update_fields=["due_at"])
            report["rescheduled_todos"] += 1

        for handover in case.handovers.filter(status=Handover.Status.INITIATED):
            handover.scheduled_at = new_time
            handover.save(update_fields=["scheduled_at"])
            report["handovers_updated"] += 1

        direction = "提前" if delta < timedelta(0) else "推迟"
        for contact in case.contacts.all():
            Notification.objects.create(
                case=case,
                contact=contact,
                kind=Notification.Kind.DISCHARGE_CHANGED,
                message=(
                    f"离馆时间已{direction}：由 {timezone.localtime(old_time):%Y-%m-%d %H:%M} "
                    f"调整为 {timezone.localtime(new_time):%Y-%m-%d %H:%M}。"
                    + (f"原因：{reason}" if reason else "")
                ),
            )
    return report


# ---------------------------------------------------------------- 家庭交接页 / 专员追踪

def family_handover_page(case, contact_id):
    """家庭视角：当前有效建议、待办、联系渠道、版本变化。打开即记录查看与回执。"""
    try:
        contact = case.contacts.get(pk=contact_id)
    except AuthorizedContact.DoesNotExist:
        raise DomainPermissionError("该联系人无权查看此家庭的交接页")

    now = timezone.now()
    advice = {}
    version_changes = []
    with transaction.atomic():
        for summary in case.summaries.all():
            current = summary.current_version
            if current:
                SummaryView.objects.get_or_create(version=current, contact=contact)
                DeliveryReceipt.objects.filter(
                    version=current, contact=contact, acknowledged_at__isnull=True
                ).update(acknowledged_at=now)
                advice[summary.subject] = {
                    "version_no": current.version_no,
                    "published_at": current.published_at.isoformat(),
                    "sections": [
                        {
                            "category": s.category,
                            "category_label": s.get_category_display(),
                            "content": s.content,
                            "signed_by": s.signed_by.name if s.signed_by else None,
                        }
                        for s in current.sections.all()
                    ],
                }
            for v in summary.versions.exclude(published_at__isnull=True):
                version_changes.append({
                    "subject": summary.subject,
                    "version_no": v.version_no,
                    "status": v.status,
                    "published_at": v.published_at.isoformat(),
                    "change_note": v.change_note,
                })
    version_changes.sort(key=lambda x: (x["published_at"], x["version_no"]))

    pending_todos = case.todos.filter(status=TodoItem.Status.PENDING)
    open_anomalies = case.anomalies.filter(status=Anomaly.Status.OPEN)
    handover = case.handovers.exclude(status=Handover.Status.CANCELLED).last()

    return {
        "family": case.family_name,
        "expected_discharge_at": case.expected_discharge_at.isoformat(),
        "current_advice": advice,
        "pending_items": {
            "todos": [
                {
                    "todo_id": t.id,
                    "subject": t.subject,
                    "title": t.title,
                    "due_at": t.due_at.isoformat() if t.due_at else None,
                }
                for t in pending_todos
            ],
            "open_anomalies": [
                {
                    "anomaly_id": a.id,
                    "subject": a.subject,
                    "description": a.description,
                    "severity": a.severity,
                    "status_label": "待处理",
                }
                for a in open_anomalies
            ],
        },
        "contact_channels": [
            {"label": c.label, "kind": c.kind, "value": c.value, "hours": c.hours}
            for c in case.contact_channels.all()
        ],
        "version_changes": version_changes,
        "handover": None if handover is None else {
            "path_type": handover.path_type,
            "status": handover.status,
            "scheduled_at": handover.scheduled_at.isoformat(),
            "responsibility_transferred": handover.responsibility_transferred,
        },
    }


def tracking_view(case):
    """服务专员视角：每章节由谁何时签署、每版本送达与回执、交接进展。"""
    summaries = []
    for summary in case.summaries.all():
        versions = []
        for v in summary.versions.all():
            versions.append({
                "version_no": v.version_no,
                "status": v.status,
                "published_at": v.published_at.isoformat() if v.published_at else None,
                "sections": [
                    {
                        "category": s.category,
                        "required_role": s.required_role,
                        "signed_by": s.signed_by.name if s.signed_by else None,
                        "signed_at": s.signed_at.isoformat() if s.signed_at else None,
                    }
                    for s in v.sections.all()
                ],
                "receipts": [
                    {
                        "contact": r.contact.name,
                        "delivered_at": r.delivered_at.isoformat(),
                        "acknowledged_at": r.acknowledged_at.isoformat()
                        if r.acknowledged_at else None,
                    }
                    for r in v.receipts.all()
                ],
            })
        summaries.append({"subject": summary.subject, "versions": versions})

    return {
        "case_id": case.id,
        "family": case.family_name,
        "expected_discharge_at": case.expected_discharge_at.isoformat(),
        "summaries": summaries,
        "handovers": [
            {
                "handover_id": h.id,
                "path_type": h.path_type,
                "status": h.status,
                "receiver_name": h.receiver_name,
                "scheduled_at": h.scheduled_at.isoformat(),
                "receiver_confirmed_at": h.receiver_confirmed_at.isoformat()
                if h.receiver_confirmed_at else None,
                "responsibility_transferred": h.responsibility_transferred,
            }
            for h in case.handovers.all()
        ],
        "notifications_sent": case.notifications.count(),
    }
