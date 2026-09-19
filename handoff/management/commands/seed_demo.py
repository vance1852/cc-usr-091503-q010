from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from handoff import services
from handoff.models import (
    AuthorizedContact,
    CareEvent,
    DischargeCase,
    Role,
    Staff,
)


class Command(BaseCommand):
    help = "构造题目场景的演示数据：部分事实已确认、部分仍待确认，家庭提前一天离馆"

    def handle(self, *args, **options):
        nanny = Staff.objects.create(name="王月嫂", role=Role.NANNY, phone="13800000001")
        dietitian = Staff.objects.create(name="李营养", role=Role.NUTRITIONIST)
        coordinator = Staff.objects.create(name="赵专员", role=Role.COORDINATOR)
        Staff.objects.create(name="孙接收", role=Role.RECEIVER,
                            organization="阳光康复中心")

        now = timezone.now()
        case = DischargeCase.objects.create(
            mother_name="周妈妈", baby_name="周宝宝",
            planned_discharge_at=now + timedelta(days=2),
        )
        contact = AuthorizedContact.objects.create(
            case=case, name="周爸爸", relation="父亲", channel="13900000000"
        )

        def add(subject, category, content, staff, hours_ago, confirmed):
            event = CareEvent.objects.create(
                case=case, subject=subject, category=category, content=content,
                occurred_at=now - timedelta(hours=hours_ago), recorded_by=staff,
            )
            if confirmed:
                services.confirm_event(event.id, staff.id)
            return event

        # 已确认事实
        add("mother", "night_observation",
            "夜间睡眠约6小时，分3次哺乳，恶露量正常。", nanny, 10, True)
        add("baby", "night_observation",
            "夜醒2次，喂奶后可自主入睡，无发热。", nanny, 9, True)
        add("baby", "supply_note",
            "奶粉仅用院方指定品牌段数；奶瓶每次煮沸消毒。", coordinator, 20, True)
        add("mother", "followup_task",
            "出院后48小时内社区家访，准备出院小结复印件。", coordinator, 5, True)
        # 营养师建议仍处于待确认阶段 —— 不得带入交接包
        add("mother", "diet_advice",
            "（草稿）催奶汤每日两次——营养师尚未确认", dietitian, 2, False)

        self.stdout.write(self.style.SUCCESS(
            f"演示案件已创建 case_id={case.id} contact_id={contact.id}"
        ))
