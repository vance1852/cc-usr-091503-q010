from django.db import models


class Subject(models.TextChoices):
    MOTHER = "mother", "产妇"
    BABY = "baby", "婴儿"


class ProfessionalRole(models.TextChoices):
    MATRON = "matron", "月嫂"
    NUTRITIONIST = "nutritionist", "营养师"
    NURSE = "nurse", "护士"
    COORDINATOR = "coordinator", "服务专员"


class Case(models.Model):
    """一户家庭的在馆案例。"""

    class Status(models.TextChoices):
        ACTIVE = "active", "在馆"
        DISCHARGED = "discharged", "已离馆"

    family_name = models.CharField("家庭称呼", max_length=100)
    mother_name = models.CharField("产妇姓名", max_length=100)
    baby_name = models.CharField("婴儿称呼", max_length=100)
    expected_discharge_at = models.DateTimeField("预计离馆时间")
    actual_discharge_at = models.DateTimeField("实际离馆时间", null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.family_name}（{self.pk}）"


class Professional(models.Model):
    """专业人员：月嫂、营养师、护士、服务专员。"""

    name = models.CharField(max_length=100)
    role = models.CharField(max_length=20, choices=ProfessionalRole.choices)
    phone = models.CharField(max_length=50, blank=True, default="")

    def __str__(self):
        return f"{self.name}（{self.get_role_display()}）"


class TimelineEvent(models.Model):
    """中心照护时间线中的一条事实记录，确认后才能进入返家摘要。"""

    class Category(models.TextChoices):
        NIGHT_OBSERVATION = "night_observation", "夜间观察"
        NUTRITION = "nutrition", "饮食建议"
        SUPPLY = "supply", "用品注意"
        VITAL = "vital", "体征记录"
        OTHER = "other", "其他"

    class Status(models.TextChoices):
        PENDING = "pending", "待确认"
        CONFIRMED = "confirmed", "已确认"
        RETRACTED = "retracted", "已撤回"

    case = models.ForeignKey(Case, related_name="timeline_events", on_delete=models.CASCADE)
    subject = models.CharField(max_length=10, choices=Subject.choices)
    category = models.CharField(max_length=30, choices=Category.choices)
    content = models.TextField()
    occurred_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    confirmed_by = models.ForeignKey(
        Professional, null=True, blank=True, on_delete=models.SET_NULL
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["occurred_at", "id"]


class Anomaly(models.Model):
    """异常记录。未闭环的阻断级异常会阻止摘要发布；提示级异常在摘要中明确列为待处理。"""

    class Severity(models.TextChoices):
        BLOCKING = "blocking", "阻断"
        ADVISORY = "advisory", "提示"

    class Status(models.TextChoices):
        OPEN = "open", "未闭环"
        CLOSED = "closed", "已闭环"

    case = models.ForeignKey(Case, related_name="anomalies", on_delete=models.CASCADE)
    subject = models.CharField(max_length=10, choices=Subject.choices)
    description = models.TextField()
    severity = models.CharField(max_length=20, choices=Severity.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    raised_by = models.ForeignKey(
        Professional, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="raised_anomalies",
    )
    resolution = models.TextField(blank=True, default="")
    closed_by = models.ForeignKey(
        Professional, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="closed_anomalies",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Summary(models.Model):
    """某一对象（产妇或婴儿）的返家摘要，内容通过版本承载。"""

    case = models.ForeignKey(Case, related_name="summaries", on_delete=models.CASCADE)
    subject = models.CharField(max_length=10, choices=Subject.choices)

    class Meta:
        unique_together = ("case", "subject")

    @property
    def current_version(self):
        return self.versions.filter(status=SummaryVersion.Status.PUBLISHED).first()


class SummaryVersion(models.Model):
    """摘要版本。更正产生新版本，旧版本标记为已废止。"""

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"
        SUPERSEDED = "superseded", "已废止"

    summary = models.ForeignKey(Summary, related_name="versions", on_delete=models.CASCADE)
    version_no = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    change_note = models.TextField("更正说明", blank=True, default="")
    created_by = models.ForeignKey(
        Professional, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("summary", "version_no")
        ordering = ["-version_no"]


class SummarySection(models.Model):
    """摘要版本中的章节。专业章节须由对应角色签署后才能发布。"""

    class Category(models.TextChoices):
        NIGHT_CARE = "night_care", "夜间照护"
        NUTRITION = "nutrition", "饮食建议"
        SUPPLIES = "supplies", "用品说明"
        TODOS = "todos", "待办事项"
        CONTACTS = "contacts", "联系与随访"
        ANOMALIES = "anomalies", "待处理异常"
        HANDOVER = "handover", "交接安排"

    version = models.ForeignKey(
        SummaryVersion, related_name="sections", on_delete=models.CASCADE
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    required_role = models.CharField(
        max_length=20, choices=ProfessionalRole.choices, null=True, blank=True,
        help_text="为空的章节由系统生成，无需签署",
    )
    content = models.JSONField(default=dict)
    signed_by = models.ForeignKey(
        Professional, null=True, blank=True, on_delete=models.SET_NULL
    )
    signed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("version", "category")

    @property
    def is_signed(self):
        return self.signed_by_id is not None

    @property
    def needs_signoff(self):
        return self.required_role is not None


class TodoItem(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待完成"
        DONE = "done", "已完成"
        CANCELLED = "cancelled", "已取消"

    case = models.ForeignKey(Case, related_name="todos", on_delete=models.CASCADE)
    subject = models.CharField(max_length=10, choices=Subject.choices)
    title = models.CharField(max_length=200)
    detail = models.TextField(blank=True, default="")
    due_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)


class SupplyItem(models.Model):
    case = models.ForeignKey(Case, related_name="supplies", on_delete=models.CASCADE)
    subject = models.CharField(max_length=10, choices=Subject.choices)
    name = models.CharField(max_length=200)
    instruction = models.TextField()
    caution = models.TextField(blank=True, default="")


class FollowUpPlan(models.Model):
    """随访计划。离馆时间变更时按相对偏移重排。"""

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "已排期"
        DONE = "done", "已完成"
        CANCELLED = "cancelled", "已取消"

    case = models.ForeignKey(Case, related_name="followups", on_delete=models.CASCADE)
    subject = models.CharField(max_length=10, choices=Subject.choices)
    purpose = models.CharField(max_length=200)
    channel = models.CharField(max_length=50, default="phone")
    offset_days = models.IntegerField(help_text="相对离馆时间的天数偏移")
    scheduled_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)


class ContactChannel(models.Model):
    case = models.ForeignKey(Case, related_name="contact_channels", on_delete=models.CASCADE)
    label = models.CharField(max_length=100)
    kind = models.CharField(max_length=20, default="phone")
    value = models.CharField(max_length=200)
    hours = models.CharField(max_length=100, blank=True, default="")


class Handover(models.Model):
    """交接路径。接收方确认后才完成责任转移。"""

    class PathType(models.TextChoices):
        EARLY_DISCHARGE = "early_discharge", "提前离馆"
        TRANSFER_FACILITY = "transfer_facility", "转往其他机构"
        HOME_SERVICE = "home_service", "月嫂上门服务"

    class Status(models.TextChoices):
        INITIATED = "initiated", "待接收方确认"
        RECEIVER_CONFIRMED = "receiver_confirmed", "接收方已确认"
        CANCELLED = "cancelled", "已取消"

    case = models.ForeignKey(Case, related_name="handovers", on_delete=models.CASCADE)
    path_type = models.CharField(max_length=30, choices=PathType.choices)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.INITIATED)
    receiver_name = models.CharField(max_length=100)
    receiver_org = models.CharField(max_length=200, blank=True, default="")
    receiver_contact = models.CharField(max_length=100, blank=True, default="")
    matron = models.ForeignKey(
        Professional, null=True, blank=True, on_delete=models.SET_NULL,
        help_text="月嫂上门服务时指定的月嫂",
    )
    service_address = models.CharField(max_length=300, blank=True, default="")
    scheduled_at = models.DateTimeField()
    initiated_by = models.ForeignKey(
        Professional, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="initiated_handovers",
    )
    initiated_at = models.DateTimeField(auto_now_add=True)
    receiver_confirmed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    @property
    def responsibility_transferred(self):
        return self.status == self.Status.RECEIVER_CONFIRMED


class AuthorizedContact(models.Model):
    """有权查看交接页的家庭联系人。"""

    case = models.ForeignKey(Case, related_name="contacts", on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    relation = models.CharField(max_length=50)
    phone = models.CharField(max_length=50, blank=True, default="")
    is_primary = models.BooleanField(default=False)


class SummaryView(models.Model):
    """授权联系人对某一摘要版本的首次查看记录。"""

    version = models.ForeignKey(SummaryVersion, related_name="views", on_delete=models.CASCADE)
    contact = models.ForeignKey(AuthorizedContact, related_name="views", on_delete=models.CASCADE)
    viewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("version", "contact")


class DeliveryReceipt(models.Model):
    """某一版本对某一联系人的送达与回执。"""

    version = models.ForeignKey(
        SummaryVersion, related_name="receipts", on_delete=models.CASCADE
    )
    contact = models.ForeignKey(
        AuthorizedContact, related_name="receipts", on_delete=models.CASCADE
    )
    delivered_at = models.DateTimeField()
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("version", "contact")


class Notification(models.Model):
    """发给授权联系人的通知（更正通知、离馆时间变更、交接进展）。"""

    class Kind(models.TextChoices):
        CORRECTION = "correction_notice", "更正通知"
        DISCHARGE_CHANGED = "discharge_changed", "离馆时间变更"
        HANDOVER_UPDATE = "handover_update", "交接进展"

    case = models.ForeignKey(Case, related_name="notifications", on_delete=models.CASCADE)
    contact = models.ForeignKey(
        AuthorizedContact, related_name="notifications", on_delete=models.CASCADE
    )
    version = models.ForeignKey(
        SummaryVersion, null=True, blank=True, on_delete=models.SET_NULL
    )
    kind = models.CharField(max_length=30, choices=Kind.choices)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
