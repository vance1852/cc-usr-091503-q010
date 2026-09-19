import threading
from datetime import timedelta

from django.db import connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .exceptions import DomainConflictError
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
from .services import sign_section


def build_world():
    """标准场景：一户家庭、四名专业人员、各类时间线事实、待办、随访、联系人。"""
    now = timezone.now().replace(microsecond=0)
    case = Case.objects.create(
        family_name="张女士一家", mother_name="张女士", baby_name="小宝",
        expected_discharge_at=now + timedelta(days=5),
    )
    coordinator = Professional.objects.create(name="李专员", role=ProfessionalRole.COORDINATOR)
    matron = Professional.objects.create(name="王月嫂", role=ProfessionalRole.MATRON)
    nutritionist = Professional.objects.create(name="赵营养师", role=ProfessionalRole.NUTRITIONIST)
    nurse = Professional.objects.create(name="陈护士", role=ProfessionalRole.NURSE)

    def event(subject, category, content, confirmed_by=None):
        e = TimelineEvent.objects.create(
            case=case, subject=subject, category=category, content=content,
            occurred_at=now - timedelta(hours=6),
        )
        if confirmed_by:
            e.status = TimelineEvent.Status.CONFIRMED
            e.confirmed_by = confirmed_by
            e.confirmed_at = now - timedelta(hours=5)
            e.save()
        return e

    events = {
        "night_ok": event(Subject.BABY, TimelineEvent.Category.NIGHT_OBSERVATION,
                          "夜间每3小时喂养一次，睡眠安稳", matron),
        "night_pending": event(Subject.BABY, TimelineEvent.Category.NIGHT_OBSERVATION,
                               "凌晨体温偏高待复核"),
        "nutrition_ok": event(Subject.MOTHER, TimelineEvent.Category.NUTRITION,
                              "返家后两周内清淡饮食，忌生冷", nutritionist),
        "supply_ok": event(Subject.BABY, TimelineEvent.Category.SUPPLY,
                           "奶瓶每次使用后消毒，纸尿裤2-3小时更换", nurse),
    }
    todo = TodoItem.objects.create(
        case=case, subject=Subject.MOTHER, title="产后42天复查预约",
        due_at=case.expected_discharge_at + timedelta(days=7),
    )
    followup = FollowUpPlan.objects.create(
        case=case, subject=Subject.BABY, purpose="离馆后第3天电话随访",
        channel="phone", offset_days=3,
        scheduled_at=case.expected_discharge_at + timedelta(days=3),
    )
    ContactChannel.objects.create(
        case=case, label="中心24小时热线", kind="phone", value="400-000-0000",
        hours="全天",
    )
    contact_a = AuthorizedContact.objects.create(
        case=case, name="张女士", relation="本人", is_primary=True
    )
    contact_b = AuthorizedContact.objects.create(
        case=case, name="张先生", relation="配偶"
    )
    return {
        "case": case, "coordinator": coordinator, "matron": matron,
        "nutritionist": nutritionist, "nurse": nurse, "events": events,
        "todo": todo, "followup": followup,
        "contact_a": contact_a, "contact_b": contact_b, "now": now,
    }


ROLE_BY_CATEGORY = {
    "night_care": ProfessionalRole.MATRON,
    "nutrition": ProfessionalRole.NUTRITIONIST,
    "supplies": ProfessionalRole.NURSE,
    "todos": ProfessionalRole.COORDINATOR,
    "contacts": ProfessionalRole.COORDINATOR,
}


def sign_all_sections(client, version, world):
    for section in version.sections.all():
        if not section.needs_signoff:
            continue
        pro = Professional.objects.get(role=ROLE_BY_CATEGORY[section.category])
        resp = client.post(
            f"/api/summary-versions/{version.id}/sections/{section.id}/sign/",
            {"professional_id": pro.id}, format="json",
        )
        assert resp.status_code == 200, resp.content


def compose_and_publish(client, world):
    """走完整流程：生成 -> 签署 -> 发布，返回 {subject: version_id}。"""
    case, coordinator = world["case"], world["coordinator"]
    resp = client.post(f"/api/cases/{case.id}/compose/",
                       {"professional_id": coordinator.id}, format="json")
    assert resp.status_code == 201, resp.content
    ids = {}
    for subject, data in resp.json().items():
        version = SummaryVersion.objects.get(id=data["id"])
        sign_all_sections(client, version, world)
        resp2 = client.post(f"/api/summary-versions/{version.id}/publish/",
                            {"professional_id": coordinator.id}, format="json")
        assert resp2.status_code == 200, resp2.content
        ids[subject] = version.id
    return ids


class ComposeAndPublishTests(TestCase):
    def setUp(self):
        self.world = build_world()
        self.client = APIClient()

    def test_compose_only_includes_confirmed_facts(self):
        """已确认事实进入摘要；待确认事实不进入建议，但明确列出而非空白。"""
        world = self.world
        resp = self.client.post(
            f"/api/cases/{world['case'].id}/compose/",
            {"professional_id": world["coordinator"].id}, format="json")
        self.assertEqual(resp.status_code, 201)
        baby = resp.json()["baby"]
        night = next(s for s in baby["sections"] if s["category"] == "night_care")
        self.assertEqual(len(night["content"]["items"]), 1)
        self.assertIn("睡眠安稳", night["content"]["items"][0]["content"])
        pending = night["content"]["pending_confirmation"]
        self.assertEqual(len(pending), 1)
        self.assertIn("体温偏高", pending[0]["content"])

    def test_compose_requires_coordinator(self):
        world = self.world
        resp = self.client.post(
            f"/api/cases/{world['case'].id}/compose/",
            {"professional_id": world["matron"].id}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_event_confirm_role_enforced(self):
        """夜间观察只能由月嫂确认。"""
        world = self.world
        pending = world["events"]["night_pending"]
        resp = self.client.post(
            f"/api/timeline-events/{pending.id}/confirm/",
            {"professional_id": world["nutritionist"].id}, format="json")
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(
            f"/api/timeline-events/{pending.id}/confirm/",
            {"professional_id": world["matron"].id}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "confirmed")

    def test_publish_blocked_until_all_sections_signed(self):
        world = self.world
        resp = self.client.post(
            f"/api/cases/{world['case'].id}/compose/",
            {"professional_id": world["coordinator"].id}, format="json")
        version_id = resp.json()["mother"]["id"]
        resp = self.client.post(
            f"/api/summary-versions/{version_id}/publish/",
            {"professional_id": world["coordinator"].id}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("unsigned_sections", resp.json())

    def test_wrong_role_cannot_sign(self):
        world = self.world
        resp = self.client.post(
            f"/api/cases/{world['case'].id}/compose/",
            {"professional_id": world["coordinator"].id}, format="json")
        baby = resp.json()["baby"]
        night = next(s for s in baby["sections"] if s["category"] == "night_care")
        resp = self.client.post(
            f"/api/summary-versions/{baby['id']}/sections/{night['id']}/sign/",
            {"professional_id": world["nutritionist"].id}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_blocking_anomaly_blocks_publish_until_closed(self):
        """未闭环阻断级异常阻断发布；闭环后放行。"""
        world = self.world
        anomaly = Anomaly.objects.create(
            case=world["case"], subject=Subject.BABY,
            description="黄疸值偏高，需儿科复核",
            severity=Anomaly.Severity.BLOCKING, raised_by=world["nurse"],
        )
        resp = self.client.post(
            f"/api/cases/{world['case'].id}/compose/",
            {"professional_id": world["coordinator"].id}, format="json")
        baby_id = resp.json()["baby"]["id"]
        version = SummaryVersion.objects.get(id=baby_id)
        sign_all_sections(self.client, version, world)
        resp = self.client.post(
            f"/api/summary-versions/{baby_id}/publish/",
            {"professional_id": world["coordinator"].id}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["blocking_anomalies"][0]["anomaly_id"], anomaly.id)

        resp = self.client.post(
            f"/api/anomalies/{anomaly.id}/close/",
            {"professional_id": world["nurse"].id, "resolution": "儿科复核正常"},
            format="json")
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            f"/api/summary-versions/{baby_id}/publish/",
            {"professional_id": world["coordinator"].id}, format="json")
        self.assertEqual(resp.status_code, 200)

    def test_advisory_anomaly_listed_as_pending_not_hidden(self):
        """提示级未闭环异常不阻断发布，但在摘要中明确标为待处理。"""
        world = self.world
        Anomaly.objects.create(
            case=world["case"], subject=Subject.MOTHER,
            description="轻微水肿，建议返家后观察",
            severity=Anomaly.Severity.ADVISORY, raised_by=world["nurse"],
        )
        ids = compose_and_publish(self.client, world)
        version = SummaryVersion.objects.get(id=ids["mother"])
        section = version.sections.get(category="anomalies")
        self.assertEqual(len(section.content["open"]), 1)
        self.assertEqual(section.content["open"][0]["status_label"], "待处理")
        self.assertIn("待处理", section.content["note"])


class ConcurrentSignTests(TransactionTestCase):
    """并发签署：不同章节并行签署都成功；同一章节只允许一个签署人。"""

    def setUp(self):
        self.world = build_world()
        resp = APIClient().post(
            f"/api/cases/{self.world['case'].id}/compose/",
            {"professional_id": self.world["coordinator"].id}, format="json")
        assert resp.status_code == 201, resp.content
        self.baby_version = SummaryVersion.objects.get(id=resp.json()["baby"]["id"])

    def _run_concurrently(self, tasks):
        results, errors = {}, {}
        barrier = threading.Barrier(len(tasks))

        def make_run(key, fn):
            def run():
                try:
                    barrier.wait(timeout=10)
                    results[key] = fn()
                except Exception as exc:  # noqa: BLE001
                    errors[key] = exc
                finally:
                    connections.close_all()
            return run

        threads = [threading.Thread(target=make_run(k, f)) for k, f in tasks.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return results, errors

    def test_concurrent_sign_different_sections(self):
        night = self.baby_version.sections.get(category="night_care")
        supplies = self.baby_version.sections.get(category="supplies")
        results, errors = self._run_concurrently({
            "matron": lambda: sign_section(night.id, self.world["matron"]),
            "nurse": lambda: sign_section(supplies.id, self.world["nurse"]),
        })
        self.assertEqual(errors, {})
        self.assertEqual(set(results), {"matron", "nurse"})
        night.refresh_from_db()
        supplies.refresh_from_db()
        self.assertEqual(night.signed_by, self.world["matron"])
        self.assertEqual(supplies.signed_by, self.world["nurse"])

    def test_concurrent_sign_same_section_only_one_wins(self):
        matron2 = Professional.objects.create(name="刘月嫂", role=ProfessionalRole.MATRON)
        night = self.baby_version.sections.get(category="night_care")
        results, errors = self._run_concurrently({
            "matron1": lambda: sign_section(night.id, self.world["matron"]),
            "matron2": lambda: sign_section(night.id, matron2),
        })
        # 恰好一人成功，另一人收到冲突错误
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        loser = next(iter(errors.values()))
        self.assertIsInstance(loser, DomainConflictError)
        night.refresh_from_db()
        self.assertIsNotNone(night.signed_by)
        self.assertEqual(
            SummarySection.objects.filter(id=night.id).exclude(signed_by__isnull=True).count(),
            1,
        )


class DischargeChangeTests(TestCase):
    """离馆时间变更：随访按偏移重排、待办顺延、待确认交接更新、通知联系人。"""

    def setUp(self):
        self.world = build_world()
        self.client = APIClient()

    def test_change_discharge_reschedules_and_notifies(self):
        world = self.world
        case = world["case"]
        compose_and_publish(self.client, world)
        # 发起一个月嫂上门交接（待接收方确认）
        resp = self.client.post(
            f"/api/cases/{case.id}/handovers/",
            {"professional_id": world["coordinator"].id,
             "path_type": "home_service", "receiver_name": "王月嫂",
             "matron_id": world["matron"].id,
             "service_address": "幸福路1号"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)

        # 家庭决定提前一天离馆
        new_time = case.expected_discharge_at - timedelta(days=1)
        resp = self.client.post(
            f"/api/cases/{case.id}/change-discharge/",
            {"professional_id": world["coordinator"].id,
             "new_time": new_time.isoformat(), "reason": "家庭要求提前"},
            format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertEqual(data["rescheduled_followups"], 1)
        self.assertEqual(data["rescheduled_todos"], 1)
        self.assertEqual(data["handovers_updated"], 1)

        world["followup"].refresh_from_db()
        self.assertEqual(world["followup"].scheduled_at, new_time + timedelta(days=3))
        world["todo"].refresh_from_db()
        self.assertEqual(
            world["todo"].due_at,
            case.expected_discharge_at + timedelta(days=7) - timedelta(days=1),
        )
        handover = Handover.objects.get(case=case)
        self.assertEqual(handover.scheduled_at, new_time)

        notes = Notification.objects.filter(case=case, kind="discharge_changed")
        self.assertEqual(notes.count(), 2)  # 两位授权联系人都收到
        self.assertIn("提前", notes.first().message)

    def test_change_discharge_requires_coordinator(self):
        world = self.world
        new_time = world["case"].expected_discharge_at - timedelta(days=1)
        resp = self.client.post(
            f"/api/cases/{world['case'].id}/change-discharge/",
            {"professional_id": world["matron"].id, "new_time": new_time.isoformat()},
            format="json")
        self.assertEqual(resp.status_code, 403)


class CorrectionNotificationTests(TestCase):
    """发布后更正：生成新版本、旧版废止、只通知查看过旧版的授权联系人。"""

    def setUp(self):
        self.world = build_world()
        self.client = APIClient()
        self.ids = compose_and_publish(self.client, self.world)

    def _correct_and_publish_mother(self):
        world = self.world
        summary = Summary.objects.get(case=world["case"], subject="mother")
        resp = self.client.post(
            f"/api/summaries/{summary.id}/correct/",
            {"professional_id": world["coordinator"].id,
             "change_note": "饮食建议中忌口周期由两周更正为四周",
             "section_updates": {
                 "nutrition": {"items": [
                     {"content": "返家后四周内清淡饮食，忌生冷",
                      "confirmed_by": "赵营养师"}
                 ], "pending_confirmation": [], "note": ""}
             }}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        new_id = resp.json()["id"]
        new_version = SummaryVersion.objects.get(id=new_id)
        # 只有内容变化的 nutrition 章节需要重签
        unsigned = [s.category for s in new_version.sections.all()
                    if s.needs_signoff and not s.is_signed]
        self.assertEqual(unsigned, ["nutrition"])
        nutrition_section = new_version.sections.get(category="nutrition")
        resp = self.client.post(
            f"/api/summary-versions/{new_id}/sections/{nutrition_section.id}/sign/",
            {"professional_id": world["nutritionist"].id}, format="json")
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            f"/api/summary-versions/{new_id}/publish/",
            {"professional_id": world["coordinator"].id}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        return new_id

    def test_correction_notifies_only_old_version_viewers(self):
        world = self.world
        case = world["case"]
        # 张女士查看过 v1，张先生未查看
        resp = self.client.get(
            f"/api/cases/{case.id}/handover-page/",
            {"contact_id": world["contact_a"].id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            SummaryView.objects.filter(version_id=self.ids["mother"]).count(), 1)

        new_id = self._correct_and_publish_mother()

        old = SummaryVersion.objects.get(id=self.ids["mother"])
        self.assertEqual(old.status, "superseded")
        new = SummaryVersion.objects.get(id=new_id)
        self.assertEqual(new.version_no, old.version_no + 1)
        self.assertEqual(new.status, "published")

        notes = Notification.objects.filter(case=case, kind="correction_notice")
        self.assertEqual(notes.count(), 1)
        self.assertEqual(notes.first().contact, world["contact_a"])
        self.assertIn("v2", notes.first().message)

        # 新版本对两位联系人都登记了送达
        self.assertEqual(
            DeliveryReceipt.objects.filter(version=new).count(), 2)
        # 张女士查看旧版时已回执
        old_receipt = DeliveryReceipt.objects.get(
            version=old, contact=world["contact_a"])
        self.assertIsNotNone(old_receipt.acknowledged_at)

    def test_family_page_shows_current_and_version_changes(self):
        world = self.world
        case = world["case"]
        self._correct_and_publish_mother()
        resp = self.client.get(
            f"/api/cases/{case.id}/handover-page/",
            {"contact_id": world["contact_b"].id})
        self.assertEqual(resp.status_code, 200)
        page = resp.json()
        # 当前有效建议是新版本
        self.assertEqual(page["current_advice"]["mother"]["version_no"], 2)
        # 版本变化历史包含两个版本，且带更正说明
        mother_changes = [v for v in page["version_changes"] if v["subject"] == "mother"]
        self.assertEqual(len(mother_changes), 2)
        self.assertEqual(mother_changes[-1]["change_note"],
                         "饮食建议中忌口周期由两周更正为四周")
        # 待办与联系渠道齐全
        self.assertTrue(page["pending_items"]["todos"])
        self.assertTrue(page["contact_channels"])

    def test_family_page_rejects_unauthorized_contact(self):
        world = self.world
        other_case = Case.objects.create(
            family_name="另一家", mother_name="某", baby_name="某宝",
            expected_discharge_at=timezone.now())
        outsider = AuthorizedContact.objects.create(
            case=other_case, name="外人", relation="无")
        resp = self.client.get(
            f"/api/cases/{world['case'].id}/handover-page/",
            {"contact_id": outsider.id})
        self.assertEqual(resp.status_code, 403)

    def test_tracking_shows_signatures_and_receipts(self):
        world = self.world
        case = world["case"]
        self.client.get(f"/api/cases/{case.id}/handover-page/",
                        {"contact_id": world["contact_a"].id})
        resp = self.client.get(f"/api/cases/{case.id}/tracking/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        mother = next(s for s in data["summaries"] if s["subject"] == "mother")
        version = mother["versions"][0]
        signed = {s["category"]: s for s in version["sections"] if s["signed_by"]}
        self.assertEqual(signed["night_care"]["signed_by"], "王月嫂")
        self.assertEqual(signed["nutrition"]["signed_by"], "赵营养师")
        self.assertIsNotNone(signed["nutrition"]["signed_at"])
        receipts = {r["contact"]: r for r in version["receipts"]}
        self.assertIsNotNone(receipts["张女士"]["acknowledged_at"])
        self.assertIsNone(receipts["张先生"]["acknowledged_at"])
        self.assertIsNotNone(receipts["张先生"]["delivered_at"])


class HandoverPathTests(TestCase):
    """三种交接路径的校验与接收方确认后的责任转移。"""

    def setUp(self):
        self.world = build_world()
        self.client = APIClient()

    def _handover(self, payload):
        world = self.world
        return self.client.post(
            f"/api/cases/{world['case'].id}/handovers/",
            {"professional_id": world["coordinator"].id, **payload}, format="json")

    def test_handover_requires_published_summaries(self):
        resp = self._handover({"path_type": "home_service", "receiver_name": "王月嫂",
                               "matron_id": self.world["matron"].id,
                               "service_address": "幸福路1号"})
        self.assertEqual(resp.status_code, 409)

    def test_early_discharge_must_be_before_expected(self):
        world = self.world
        compose_and_publish(self.client, world)
        late = world["case"].expected_discharge_at + timedelta(days=1)
        resp = self._handover({"path_type": "early_discharge",
                               "receiver_name": "张女士",
                               "scheduled_at": late.isoformat()})
        self.assertEqual(resp.status_code, 400)
        early = world["case"].expected_discharge_at - timedelta(days=1)
        resp = self._handover({"path_type": "early_discharge",
                               "receiver_name": "张女士",
                               "scheduled_at": early.isoformat()})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertFalse(resp.json()["responsibility_transferred"])

    def test_transfer_requires_receiver_org(self):
        world = self.world
        compose_and_publish(self.client, world)
        resp = self._handover({"path_type": "transfer_facility",
                               "receiver_name": "刘医生"})
        self.assertEqual(resp.status_code, 400)
        resp = self._handover({"path_type": "transfer_facility",
                               "receiver_name": "刘医生",
                               "receiver_org": "市妇幼保健院"})
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_home_service_requires_matron_and_address(self):
        world = self.world
        compose_and_publish(self.client, world)
        resp = self._handover({"path_type": "home_service", "receiver_name": "王月嫂"})
        self.assertEqual(resp.status_code, 400)
        resp = self._handover({"path_type": "home_service", "receiver_name": "王月嫂",
                               "matron_id": world["matron"].id})
        self.assertEqual(resp.status_code, 400)
        resp = self._handover({"path_type": "home_service", "receiver_name": "王月嫂",
                               "matron_id": world["matron"].id,
                               "service_address": "幸福路1号"})
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_responsibility_transfers_only_after_receiver_confirms(self):
        world = self.world
        compose_and_publish(self.client, world)
        resp = self._handover({"path_type": "home_service", "receiver_name": "王月嫂",
                               "matron_id": world["matron"].id,
                               "service_address": "幸福路1号"})
        handover_id = resp.json()["id"]
        self.assertFalse(resp.json()["responsibility_transferred"])

        resp = self.client.post(
            f"/api/handovers/{handover_id}/receiver-confirm/",
            {"receiver_name": "王月嫂"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["responsibility_transferred"])
        self.assertIsNotNone(resp.json()["receiver_confirmed_at"])
        world["case"].refresh_from_db()
        self.assertEqual(world["case"].status, "discharged")

        # 重复确认被拒绝
        resp = self.client.post(
            f"/api/handovers/{handover_id}/receiver-confirm/",
            {"receiver_name": "王月嫂"}, format="json")
        self.assertEqual(resp.status_code, 409)

    def test_no_double_active_handover(self):
        world = self.world
        compose_and_publish(self.client, world)
        resp = self._handover({"path_type": "transfer_facility",
                               "receiver_name": "刘医生",
                               "receiver_org": "市妇幼保健院"})
        self.assertEqual(resp.status_code, 201)
        resp = self._handover({"path_type": "transfer_facility",
                               "receiver_name": "刘医生",
                               "receiver_org": "市妇幼保健院"})
        self.assertEqual(resp.status_code, 409)
