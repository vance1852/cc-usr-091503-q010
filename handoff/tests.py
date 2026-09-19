import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.db import OperationalError, connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from handoff import services
from handoff.models import (
    Role,
    Anomaly,
    AuthorizedContact,
    CareEvent,
    DischargeCase,
    Delivery,
    HandoffPacket,
    Notification,
    PacketSection,
    SectionConfirmation,
    Staff,
)


def make_case():
    now = timezone.now()
    return DischargeCase.objects.create(
        mother_name="妈妈", baby_name="宝宝",
        planned_discharge_at=now + timedelta(days=1),
    )


class BaseDomainTest(TransactionTestCase):
    def setUp(self):
        self.nanny = Staff.objects.create(name="王月嫂", role=Role.NANNY)
        self.dietitian = Staff.objects.create(name="李营养", role=Role.NUTRITIONIST)
        self.coordinator = Staff.objects.create(name="赵专员", role=Role.COORDINATOR)
        self.receiver = Staff.objects.create(
            name="孙接收", role=Role.RECEIVER, organization="阳光康复中心"
        )
        self.case = make_case()
        self.contact = AuthorizedContact.objects.create(
            case=self.case, name="爸爸", relation="父亲", channel="1390000"
        )

    def add_event(self, subject, category, content, staff, confirmed=True):
        event = CareEvent.objects.create(
            case=self.case, subject=subject, category=category, content=content,
            occurred_at=timezone.now(), recorded_by=staff,
        )
        if confirmed:
            services.confirm_event(event.id, staff.id)
        return event

    def build_sign_publish(self, subject, expect_pending=True):
        packet = services.build_packet(self.case.id, subject)
        for section in packet.sections.filter(state=PacketSection.State.DRAFT):
            services.sign_section(section.id, section.responsible_id,
                                  section.responsible.name)
        packet = services.publish_packet(packet.id)
        packet.refresh_from_db()
        return packet


class ConfirmedFactsOnlyTest(BaseDomainTest):
    def test_unconfirmed_facts_never_enter_packet(self):
        self.add_event("mother", "night_observation", "夜眠6小时", self.nanny)
        self.add_event("mother", "diet_advice", "催奶汤每日两次（未确认）",
                       self.dietitian, confirmed=False)
        packet = services.build_packet(self.case.id, "mother")
        all_text = "\n".join(packet.sections.values_list("content", flat=True))
        self.assertIn("夜眠6小时", all_text)
        self.assertNotIn("催奶汤", all_text)

    def test_empty_data_marked_pending_not_blank(self):
        """营养师建议还在确认中：对应板块不能空白，必须显式待处理并登记异常。"""
        self.add_event("mother", "night_observation", "夜眠6小时", self.nanny)
        packet = services.build_packet(self.case.id, "mother")
        diet_sections = packet.sections.filter(responsible=self.dietitian)
        self.assertTrue(diet_sections.exists())
        for section in diet_sections:
            self.assertEqual(section.state, PacketSection.State.PENDING_DATA)
            self.assertIn("待处理", section.content)
        self.assertTrue(
            packet.anomalies.filter(
                severity=Anomaly.Severity.PENDING, status=Anomaly.Status.OPEN
            ).exists()
        )
        # 没有任何板块以空白存在
        self.assertFalse(packet.sections.filter(content="").exists())

    def test_wrong_role_cannot_confirm(self):
        event = self.add_event("mother", "night_observation", "x", self.nanny,
                               confirmed=False)
        with self.assertRaises(ValidationError):
            services.confirm_event(event.id, self.dietitian.id)

    def test_wrong_staff_cannot_sign_section(self):
        self.add_event("mother", "night_observation", "夜眠6小时", self.nanny)
        packet = services.build_packet(self.case.id, "mother")
        section = packet.sections.get(responsible=self.nanny)
        with self.assertRaises(ValidationError):
            services.sign_section(section.id, self.dietitian.id, "越权签署")


class PublishGateTest(BaseDomainTest):
    def test_publish_blocked_until_signed(self):
        self.add_event("mother", "night_observation", "夜眠6小时", self.nanny)
        packet = services.build_packet(self.case.id, "mother")
        with self.assertRaises(ValidationError) as ctx:
            services.publish_packet(packet.id)
        blockers = ctx.exception.detail["publish_blocked"]
        self.assertTrue(any("尚未签署" in b for b in blockers))

    def test_block_anomaly_prevents_publish_and_is_explicit(self):
        # 制造 BLOCK 异常：缺少 coordinator 角色
        Staff.objects.filter(role=Role.COORDINATOR).delete()
        self.add_event("mother", "night_observation", "夜眠6小时", self.nanny)
        packet = services.build_packet(self.case.id, "mother")
        with self.assertRaises(ValidationError) as ctx:
            services.publish_packet(packet.id)
        blockers = ctx.exception.detail["publish_blocked"]
        self.assertTrue(any("阻断异常" in b for b in blockers))

    def test_pending_anomaly_allows_publish_but_flagged(self):
        self.add_event("mother", "night_observation", "夜眠6小时", self.nanny)
        packet = self.build_sign_publish("mother")
        self.assertEqual(packet.status, HandoffPacket.Status.PUBLISHED)
        page = services.handoff_page(self.case.id)
        # 待处理事项显式呈现，不用空白掩盖
        pending = page["pending_items"]
        self.assertTrue(any("待处理" in p.get("state", "") or
                            "暂无已确认事实" in p["content"] for p in pending))


class ConcurrentSigningTest(BaseDomainTest):
    def test_only_one_signature_wins_under_concurrency(self):
        self.add_event("baby", "night_observation", "夜醒2次", self.nanny)
        packet = services.build_packet(self.case.id, "baby")
        section = packet.sections.get(responsible=self.nanny)
        section_id = section.id
        start = threading.Barrier(8)
        outcomes = []

        def worker():
            try:
                start.wait(timeout=10)
                for _ in range(3):  # 遇 SQLite 锁重试，唯一约束冲突不重试
                    try:
                        services.sign_section(section_id, self.nanny.id, "王月嫂")
                        outcomes.append(("ok", None))
                        break
                    except ValidationError as e:
                        outcomes.append(("rejected", str(e.detail)))
                        break
                    except OperationalError as e:
                        if "locked" in str(e).lower():
                            continue
                        outcomes.append(("error", str(e)))
                        break
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: worker(), range(8)))

        self.assertEqual(SectionConfirmation.objects.filter(section_id=section_id).count(), 1)
        self.assertEqual(sum(1 for kind, _ in outcomes if kind == "ok"), 1)
        self.assertEqual(sum(1 for kind, _ in outcomes if kind == "rejected"), 7)
        section.refresh_from_db()
        self.assertEqual(section.state, PacketSection.State.CONFIRMED)


class RescheduleTest(BaseDomainTest):
    def test_early_discharge_creates_versioned_correction_draft(self):
        self.add_event("baby", "night_observation", "夜醒2次", self.nanny)
        v1 = self.build_sign_publish("baby")
        self.assertEqual(v1.version, 1)

        earlier = timezone.now() + timedelta(hours=12)  # 提前一天
        services.reschedule(self.case.id, earlier)

        # 已发布的 v1 不被静默改动，仍为当前版本
        v1.refresh_from_db()
        self.assertEqual(v1.status, HandoffPacket.Status.PUBLISHED)
        draft = HandoffPacket.objects.get(
            case=self.case, subject="baby", status=HandoffPacket.Status.DRAFT
        )
        self.assertEqual(draft.version, 2)
        self.assertTrue(
            draft.changes.filter(text__contains="离馆时间").exists()
        )

        # v2 发布后 v1 被替代
        for section in draft.sections.filter(state=PacketSection.State.DRAFT):
            services.sign_section(section.id, section.responsible_id,
                                  section.responsible.name)
        services.publish_packet(draft.id)
        v1.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual(v1.status, HandoffPacket.Status.SUPERSEDED)
        self.assertEqual(draft.status, HandoffPacket.Status.PUBLISHED)


class CorrectionNotificationTest(BaseDomainTest):
    def test_new_version_notifies_only_old_viewers(self):
        self.add_event("baby", "supply_note", "奶瓶每次煮沸消毒", self.coordinator)
        v1 = self.build_sign_publish("baby")
        delivery = services.deliver_packet(v1.id, self.contact.id)
        services.mark_viewed(delivery.id)

        # 第二位联系人已送达但未查看
        other = AuthorizedContact.objects.create(
            case=self.case, name="外婆", relation="外婆", channel="1380001"
        )
        services.deliver_packet(v1.id, other.id)

        # 用品注意事项被更正：旧事实失效，回到待确认
        event = CareEvent.objects.get(category="supply_note")
        event = services.correct_event(event.id, "奶瓶蒸汽消毒5分钟（更正版）",
                                       self.coordinator.id)
        self.assertEqual(event.status, CareEvent.Status.PENDING)
        services.confirm_event(event.id, self.coordinator.id)

        v2_draft = services.build_packet(self.case.id, "baby")
        self.assertEqual(v2_draft.version, 2)
        # 内容变更使旧签署失效，必须重签
        supply_section = v2_draft.sections.get(
            kind=PacketSection.Kind.SUPPLY, responsible=self.coordinator
        )
        self.assertEqual(supply_section.state, PacketSection.State.DRAFT)
        services.sign_section(supply_section.id, self.coordinator.id, "赵专员")
        for section in v2_draft.sections.filter(state=PacketSection.State.DRAFT):
            services.sign_section(section.id, section.responsible_id,
                                  section.responsible.name)
        services.publish_packet(v2_draft.id)

        notes = Notification.objects.filter(contact=self.contact)
        self.assertEqual(notes.count(), 1)
        note = notes.get()
        self.assertEqual(note.from_version, 1)
        self.assertEqual(note.packet.version, 2)
        self.assertIn("已更新为 v2", note.message)
        # 未查看旧版的联系人不被打扰
        self.assertFalse(Notification.objects.filter(contact=other).exists())

    def test_draft_cannot_be_delivered(self):
        self.add_event("baby", "night_observation", "夜醒2次", self.nanny)
        draft = services.build_packet(self.case.id, "baby")
        with self.assertRaises(ValidationError):
            services.deliver_packet(draft.id, self.contact.id)


class TransferPathTest(BaseDomainTest):
    def _publish_both(self):
        self.add_event("mother", "night_observation", "夜眠6小时", self.nanny)
        self.add_event("baby", "night_observation", "夜醒2次", self.nanny)
        self.build_sign_publish("mother")
        self.build_sign_publish("baby")

    def test_cannot_transfer_before_publish(self):
        services.set_transfer_path(self.case.id, DischargeCase.Path.TRANSFER,
                                   "阳光康复中心")
        with self.assertRaises(ValidationError):
            services.acknowledge_transfer(self.case.id, staff_id=self.receiver.id)

    def test_three_paths_different_acknowledgers(self):
        self._publish_both()

        # 提前离馆：家属联系人确认，专业人员不能代确认
        services.set_transfer_path(self.case.id, DischargeCase.Path.EARLY,
                                   "自行回家")
        with self.assertRaises(ValidationError):
            services.acknowledge_transfer(self.case.id, staff_id=self.nanny.id)
        case = services.acknowledge_transfer(
            self.case.id, contact_id=self.contact.id
        )
        self.assertTrue(case.responsibility_transferred)

        # 改路径需重新确认（新建案件验证转机构路径）
        case2 = make_case()
        contact2 = AuthorizedContact.objects.create(
            case=case2, name="爸爸2", relation="父亲", channel="1380002"
        )
        self.case = case2
        self.contact = contact2
        self._publish_both()
        services.set_transfer_path(case2.id, DischargeCase.Path.TRANSFER,
                                   "阳光康复中心")
        with self.assertRaises(ValidationError):
            services.acknowledge_transfer(case2.id, staff_id=self.nanny.id)
        case2 = services.acknowledge_transfer(case2.id, staff_id=self.receiver.id)
        self.assertTrue(case2.responsibility_transferred)

        # 月嫂上门：须由月嫂角色确认
        case3 = make_case()
        AuthorizedContact.objects.create(
            case=case3, name="爸爸3", relation="父亲", channel="1380003"
        )
        self.case = case3
        self._publish_both()
        services.set_transfer_path(case3.id, DischargeCase.Path.NANNY_HOME,
                                   "王月嫂上门")
        case3 = services.acknowledge_transfer(case3.id, staff_id=self.nanny.id)
        self.assertTrue(case3.responsibility_transferred)


class HandoffPageAndTrackingTest(BaseDomainTest):
    def test_family_page_and_specialist_tracking(self):
        self.add_event("mother", "followup_task", "48小时内社区家访",
                       self.coordinator)
        packet = self.build_sign_publish("mother")
        delivery = services.deliver_packet(packet.id, self.contact.id)

        page = services.handoff_page(self.case.id)
        mother = page["current_effective_advice"]["mother"]
        self.assertEqual(mother["current_version"], 1)
        kinds = {s["kind"] for s in mother["sections"]}
        self.assertEqual(kinds, set(PacketSection.Kind.values))
        # 每个板块都标明谁确认、确认时间
        todo = next(s for s in mother["sections"]
                    if s["kind"] == PacketSection.Kind.TODO)
        self.assertEqual(todo["confirmed_by"], "赵专员")
        self.assertIsNotNone(todo["confirmed_at"])
        self.assertTrue(any(c["name"] == "爸爸"
                            for c in page["contact_channels"]))

        # 送达但未回执
        tracking = self._tracking()
        mp = next(p for p in tracking["packets"] if p["subject"] == "mother")
        self.assertEqual(mp["deliveries"][0]["receipt"], False)

        services.mark_viewed(delivery.id)
        tracking = self._tracking()
        mp = next(p for p in tracking["packets"] if p["subject"] == "mother")
        self.assertEqual(mp["deliveries"][0]["receipt"], True)
        self.assertIsNotNone(mp["deliveries"][0]["viewed_at"])

    def _tracking(self):
        from rest_framework.test import APIRequestFactory
        from handoff.views import CaseViewSet

        view = CaseViewSet.as_view({"get": "tracking"})
        request = APIRequestFactory().get("/")
        response = view(request, pk=self.case.id)
        response.render()
        return response.data


class APIFlowTest(BaseDomainTest):
    """通过 REST API 验证题目主故事线。"""

    def setUp(self):
        super().setUp()
        from rest_framework.test import APIClient
        self.client = APIClient()

    def test_full_flow_over_http(self):
        # 1) 登记事实：夜观/用品已确认，营养建议仍待确认
        ev_night = self.add_event("baby", "night_observation", "夜醒2次", self.nanny)
        self.add_event("baby", "supply_note", "奶瓶煮沸消毒", self.coordinator)
        diet = self.add_event("mother", "diet_advice", "催奶汤（草稿）",
                              self.dietitian, confirmed=False)

        # 未确认事实不能被非本角色确认
        r = self.client.post(f"/api/events/{diet.id}/confirm/",
                             {"staff_id": self.nanny.id}, format="json")
        self.assertEqual(r.status_code, 400)

        # 2) 构建 + 未签署直接发布 -> 400 且明确列出阻断原因
        r = self.client.post("/api/packets/build/",
                             {"case_id": self.case.id, "subject": "baby"},
                             format="json")
        self.assertEqual(r.status_code, 201)
        packet_id = r.data["id"]
        r = self.client.post(f"/api/packets/{packet_id}/publish/")
        self.assertEqual(r.status_code, 400)
        self.assertIn("publish_blocked", r.data)

        # 3) 各专业人员只签自己的板块；越权签署被拒
        sections = self.client.get(f"/api/packets/?case={self.case.id}").data
        baby = next(p for p in sections if p["subject"] == "baby")
        for section in baby["sections"]:
            r = self.client.post(f"/api/sections/{section['id']}/sign/",
                                 {"staff_id": self.dietitian.id}, format="json")
            self.assertEqual(r.status_code, 400)
        for section in baby["sections"]:
            if section["state"] == "draft":
                r = self.client.post(f"/api/sections/{section['id']}/sign/",
                                     {"staff_id": section["responsible"]},
                                     format="json")
                self.assertEqual(r.status_code, 200)

        # 4) 发布 v1，送达并回执
        r = self.client.post(f"/api/packets/{packet_id}/publish/")
        self.assertEqual(r.status_code, 200)
        # 产妇包：营养建议尚未确认，板块显式标为待处理但可发布（非空白）
        r = self.client.post("/api/packets/build/",
                             {"case_id": self.case.id, "subject": "mother"},
                             format="json")
        mother_id = r.data["id"]
        for section in r.data["sections"]:
            if section["state"] == "draft":
                self.client.post(f"/api/sections/{section['id']}/sign/",
                                 {"staff_id": section["responsible"]},
                                 format="json")
        r = self.client.post(f"/api/packets/{mother_id}/publish/")
        self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/api/packets/{packet_id}/deliver/",
                             {"contact_id": self.contact.id}, format="json")
        self.assertEqual(r.status_code, 201)
        delivery_id = r.data["id"]
        r = self.client.post(f"/api/deliveries/{delivery_id}/view/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.data["viewed_at"])

        # 5) 家庭提前一天离馆 -> v2 草稿 + 版本变化说明
        from datetime import datetime
        r = self.client.post(
            f"/api/cases/{self.case.id}/reschedule/",
            {"planned_discharge_at": (timezone.now() + timedelta(hours=12)).isoformat()},
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        drafts = self.client.get(f"/api/packets/?case={self.case.id}").data
        v2 = next(p for p in drafts if p["subject"] == "baby"
                  and p["status"] == "draft")
        self.assertEqual(v2["version"], 2)
        self.assertTrue(any("离馆时间" in c["text"] for c in v2["changes"]))

        # 6) 更正已发布事实 -> 旧签署失效 -> 重签发布 -> 旧查看人收到通知
        services.correct_event(ev_night.id, "夜醒3次（更正）", self.nanny.id)
        services.confirm_event(ev_night.id, self.nanny.id)
        r = self.client.post("/api/packets/build/",
                             {"case_id": self.case.id, "subject": "baby"},
                             format="json")
        rebuilt = r.data
        self.assertEqual(rebuilt["version"], 2)
        for section in rebuilt["sections"]:
            if section["state"] == "draft":
                r = self.client.post(f"/api/sections/{section['id']}/sign/",
                                     {"staff_id": section["responsible"]},
                                     format="json")
                self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/api/packets/{rebuilt['id']}/publish/")
        self.assertEqual(r.status_code, 200)
        notes = self.client.get(
            f"/api/notifications/?contact={self.contact.id}"
        ).data
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["from_version"], 1)

        # 7) 交接路径：提前离馆须家属确认，确认后责任转移
        r = self.client.post(
            f"/api/cases/{self.case.id}/transfer-path/",
            {"path": "early", "target_name": "自行回家"}, format="json",
        )
        self.assertEqual(r.status_code, 200)
        r = self.client.post(
            f"/api/cases/{self.case.id}/transfer-ack/", {}, format="json"
        )
        self.assertEqual(r.status_code, 400)  # 缺少家属确认人
        r = self.client.post(
            f"/api/cases/{self.case.id}/transfer-ack/",
            {"contact_id": self.contact.id}, format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["responsibility_transferred"])

        # 8) 家庭交接页分区呈现 + 专员追踪
        page = self.client.get(
            f"/api/cases/{self.case.id}/handoff-page/"
        ).data
        baby_page = page["current_effective_advice"]["baby"]
        self.assertEqual(baby_page["current_version"], 2)
        self.assertEqual(
            {s["kind"] for s in baby_page["sections"]},
            set(PacketSection.Kind.values),
        )
        self.assertTrue(page["contact_channels"])
        tracking = self.client.get(f"/api/cases/{self.case.id}/tracking/").data
        self.assertTrue(tracking["responsibility_transferred"])
        v2track = next(p for p in tracking["packets"]
                       if p["subject"] == "baby" and p["version"] == 2)
        self.assertTrue(
            all(set(d) >= {"contact", "delivered_at", "viewed_at", "receipt"}
                for d in v2track["deliveries"])
        )
        # 每部分都能追踪到确认人与状态
        self.assertTrue(
            all(set(s) >= {"responsible", "confirmed_by", "state"}
                for s in v2track["sections"])
        )

    def test_transfer_requires_receiver_role_over_http(self):
        self.add_event("mother", "night_observation", "夜眠6小时", self.nanny)
        self.add_event("baby", "night_observation", "夜醒2次", self.nanny)
        self.build_sign_publish("mother")
        self.build_sign_publish("baby")
        self.client.post(
            f"/api/cases/{self.case.id}/transfer-path/",
            {"path": "transfer", "target_name": "阳光康复中心"}, format="json",
        )
        # 月嫂不能替接收机构确认
        r = self.client.post(
            f"/api/cases/{self.case.id}/transfer-ack/",
            {"staff_id": self.nanny.id}, format="json",
        )
        self.assertEqual(r.status_code, 400)
        r = self.client.post(
            f"/api/cases/{self.case.id}/transfer-ack/",
            {"staff_id": self.receiver.id}, format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["responsibility_transferred"])
