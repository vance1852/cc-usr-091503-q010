from django.db import models


class Role(models.TextChoices):
    NANNY = "nanny", "月嫂"
    NUTRITIONIST = "nutritionist", "营养师"
    COORDINATOR = "coordinator", "出馆服务专员"
    RECEIVER = "receiver", "接收机构人员"
    FAMILY = "family", "家属"


# 照护时间线事件类别 -> 必须由哪个角色的专业人员确认
CATEGORY_RESPONSIBLE_ROLE = {
    "night_observation": Role.NANNY,
    "diet_advice": Role.NUTRITIONIST,
    "supply_note": Role.COORDINATOR,
    "followup_task": Role.COORDINATOR,
    "contact_record": Role.COORDINATOR,
}

# 事件类别 -> 交接包板块
CATEGORY_SECTION_KIND = {
    "night_observation": "summary",
    "diet_advice": "summary",
    "supply_note": "supply",
    "followup_task": "todo",
    "contact_record": "contact",
}


class Staff(models.Model):
    """中心专业人员 / 接收方人员（也可用于家属代表）。"""

    name = models.CharField(max_length=64)
    role = models.CharField(max_length=16, choices=Role.choices)
    phone = models.CharField(max_length=32, blank=True)
    organization = models.CharField(max_length=128, blank=True)

    class Meta:
        db_table = "staff"

    def __str__(self):
        return f"{self.name}({self.get_role_display()})"


class DischargeCase(models.Model):
    """一户家庭的离馆案件。"""

    class Path(models.TextChoices):
        EARLY = "early", "提前离馆返家"
        TRANSFER = "transfer", "转往其他机构"
        NANNY_HOME = "nanny_home", "月嫂上门继续服务"

    class TransferStatus(models.TextChoices):
        NOT_SET = "not_set", "未设置交接路径"
        PENDING = "pending", "待接收方确认"
        ACKED = "acked", "接收方已确认，责任已转移"

    mother_name = models.CharField(max_length=64)
    baby_name = models.CharField(max_length=64)
    planned_discharge_at = models.DateTimeField()
    path = models.CharField(max_length=16, choices=Path.choices, blank=True, default="")
    transfer_status = models.CharField(
        max_length=16, choices=TransferStatus.choices, default=TransferStatus.NOT_SET
    )
    # 接收目标：机构名 / 月嫂名 / 家属称谓
    target_name = models.CharField(max_length=128, blank=True, default="")
    target_contact = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "discharge_case"

    @property
    def responsibility_transferred(self) -> bool:
        return self.transfer_status == self.TransferStatus.ACKED


class CareEvent(models.Model):
    """中心照护时间线条目：仅“已确认”事实可进入交接包。"""

    class Category(models.TextChoices):
        NIGHT_OBSERVATION = "night_observation", "月嫂夜间观察"
        DIET_ADVICE = "diet_advice", "营养师饮食建议"
        SUPPLY_NOTE = "supply_note", "婴儿用品注意事项"
        FOLLOWUP_TASK = "followup_task", "返家待办"
        CONTACT_RECORD = "contact_record", "后续联系记录"

    class Status(models.TextChoices):
        PENDING = "pending", "待确认"
        CONFIRMED = "confirmed", "已确认"

    case = models.ForeignKey(
        DischargeCase, on_delete=models.CASCADE, related_name="events"
    )
    subject = models.CharField(max_length=8)  # mother / baby
    category = models.CharField(max_length=20, choices=Category.choices)
    content = models.TextField()
    occurred_at = models.DateTimeField()
    recorded_by = models.ForeignKey(
        Staff, on_delete=models.PROTECT, related_name="recorded_events"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    confirmed_by = models.ForeignKey(
        Staff, null=True, blank=True, on_delete=models.PROTECT,
        related_name="confirmed_events",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "care_event"
        indexes = [models.Index(fields=["case", "subject", "category"])]


class HandoffPacket(models.Model):
    """一名对象（产妇/婴儿）的带版本交接包。"""

    class Subject(models.TextChoices):
        MOTHER = "mother", "产妇"
        BABY = "baby", "婴儿"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"
        SUPERSEDED = "superseded", "已被新版本替代"

    case = models.ForeignKey(
        DischargeCase, on_delete=models.CASCADE, related_name="packets"
    )
    subject = models.CharField(max_length=8, choices=Subject.choices)
    version = models.PositiveIntegerField()
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT
    )
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "handoff_packet"
        constraints = [
            models.UniqueConstraint(
                fields=["case", "subject", "version"], name="uq_packet_version"
            )
        ]

    @property
    def is_current(self) -> bool:
        return self.status == self.Status.PUBLISHED


class PacketSection(models.Model):
    """交接包内一个板块：由唯一一名负责专业人员确认自己的部分。"""

    class Kind(models.TextChoices):
        SUMMARY = "summary", "返家摘要"
        TODO = "todo", "待办事项"
        SUPPLY = "supply", "用品说明"
        CONTACT = "contact", "后续联系记录"

    class State(models.TextChoices):
        DRAFT = "draft", "待确认"
        CONFIRMED = "confirmed", "专业人员已确认"
        PENDING_DATA = "pending_data", "暂无已确认事实-待处理"

    packet = models.ForeignKey(
        HandoffPacket, on_delete=models.CASCADE, related_name="sections"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    responsible = models.ForeignKey(Staff, on_delete=models.PROTECT)
    content = models.TextField(blank=True, default="")
    state = models.CharField(max_length=14, choices=State.choices, default=State.DRAFT)
    # 板块由哪些时间线事实汇集而来
    source_events = models.ManyToManyField(CareEvent, related_name="in_sections")

    class Meta:
        db_table = "packet_section"
        constraints = [
            models.UniqueConstraint(
                fields=["packet", "kind", "responsible"],
                name="uq_section_owner",
            )
        ]


class SectionConfirmation(models.Model):
    """专业人员对自己板块的签署；一对一唯一约束兜底并发重复签署。"""

    section = models.OneToOneField(
        PacketSection, on_delete=models.CASCADE, related_name="confirmation"
    )
    confirmer = models.ForeignKey(Staff, on_delete=models.PROTECT)
    signature = models.CharField(max_length=128)
    confirmed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "section_confirmation"


class Anomaly(models.Model):
    """未闭环异常：BLOCK 阻断发布；PENDING 可随包发布但必须显式标为待处理。"""

    class Severity(models.TextChoices):
        BLOCK = "block", "阻断"
        PENDING = "pending", "待处理"

    class Status(models.TextChoices):
        OPEN = "open", "未闭环"
        CLOSED = "closed", "已闭环"

    packet = models.ForeignKey(
        HandoffPacket, null=True, blank=True, on_delete=models.CASCADE,
        related_name="anomalies",
    )
    case = models.ForeignKey(
        DischargeCase, on_delete=models.CASCADE, related_name="anomalies"
    )
    section = models.ForeignKey(
        PacketSection, null=True, blank=True, on_delete=models.CASCADE,
        related_name="anomalies",
    )
    severity = models.CharField(max_length=8, choices=Severity.choices)
    description = models.TextField()
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.OPEN
    )
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    resolution = models.TextField(blank=True, default="")

    class Meta:
        db_table = "anomaly"


class VersionChange(models.Model):
    """版本变化说明（更正、离馆时间变更等）。"""

    packet = models.ForeignKey(
        HandoffPacket, on_delete=models.CASCADE, related_name="changes"
    )
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "version_change"


class AuthorizedContact(models.Model):
    """可查看交接页的授权联系人（家属）。"""

    case = models.ForeignKey(
        DischargeCase, on_delete=models.CASCADE, related_name="contacts"
    )
    name = models.CharField(max_length=64)
    relation = models.CharField(max_length=32)
    channel = models.CharField(max_length=128)  # 手机号 / 微信等

    class Meta:
        db_table = "authorized_contact"


class Delivery(models.Model):
    """某版本交接包向某联系人的送达与回执记录。"""

    packet = models.ForeignKey(
        HandoffPacket, on_delete=models.CASCADE, related_name="deliveries"
    )
    contact = models.ForeignKey(
        AuthorizedContact, on_delete=models.CASCADE, related_name="deliveries"
    )
    delivered_at = models.DateTimeField(auto_now_add=True)
    viewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "delivery"
        constraints = [
            models.UniqueConstraint(
                fields=["packet", "contact"], name="uq_delivery"
            )
        ]


class Notification(models.Model):
    """新版本发布后，通知已查看过旧版的授权联系人。"""

    contact = models.ForeignKey(
        AuthorizedContact, on_delete=models.CASCADE, related_name="notifications"
    )
    packet = models.ForeignKey(
        HandoffPacket, on_delete=models.CASCADE, related_name="notifications"
    )
    from_version = models.PositiveIntegerField()
    message = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "notification"
