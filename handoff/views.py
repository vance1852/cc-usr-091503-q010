from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import (
    Anomaly,
    AuthorizedContact,
    CareEvent,
    DischargeCase,
    Delivery,
    HandoffPacket,
    Notification,
    Staff,
)
from .serializers import (
    AnomalySerializer,
    CareEventSerializer,
    CaseSerializer,
    ContactSerializer,
    DeliverySerializer,
    NotificationSerializer,
    PacketSerializer,
    StaffSerializer,
)


class StaffViewSet(viewsets.ModelViewSet):
    queryset = Staff.objects.all()
    serializer_class = StaffSerializer


class CaseViewSet(viewsets.ModelViewSet):
    queryset = DischargeCase.objects.all()
    serializer_class = CaseSerializer

    @action(detail=True, methods=["post"])
    def reschedule(self, request, pk=None):
        """离馆时间变更（含提前一天离馆）：已发布包不静默改动，生成更正草稿。"""
        from django.utils.dateparse import parse_datetime

        new_at = parse_datetime(request.data.get("planned_discharge_at", ""))
        if new_at is None:
            return Response(
                {"detail": "planned_discharge_at 需为 ISO 时间"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        case = services.reschedule(pk, new_at)
        return Response(CaseSerializer(case).data)

    @action(detail=True, methods=["post"], url_path="transfer-path")
    def set_path(self, request, pk=None):
        case = services.set_transfer_path(
            pk,
            request.data.get("path", ""),
            request.data.get("target_name", ""),
            request.data.get("target_contact", ""),
        )
        return Response(CaseSerializer(case).data)

    @action(detail=True, methods=["post"], url_path="transfer-ack")
    def acknowledge(self, request, pk=None):
        """接收方确认后责任才转移；三种路径确认人角色不同。"""
        case = services.acknowledge_transfer(
            pk,
            staff_id=request.data.get("staff_id"),
            contact_id=request.data.get("contact_id"),
        )
        return Response(CaseSerializer(case).data)

    @action(detail=True, methods=["get"], url_path="handoff-page")
    def handoff_page(self, request, pk=None):
        """家庭交接页：当前有效建议 / 待办 / 联系渠道 / 版本变化。"""
        return Response(services.handoff_page(pk))

    @action(detail=True, methods=["get"], url_path="tracking")
    def tracking(self, request, pk=None):
        """服务专员追踪：每部分由谁确认、何时送达、是否回执。"""
        case = get_object_or_404(DischargeCase, pk=pk)
        result = {"case_id": case.id, "packets": []}
        for packet in case.packets.order_by("subject", "-version"):
            sections = []
            for section in packet.sections.select_related(
                "responsible", "confirmation__confirmer"
            ):
                sections.append({
                    "kind": section.kind,
                    "kind_display": section.get_kind_display(),
                    "responsible": str(section.responsible),
                    "state": section.state,
                    "state_display": section.get_state_display(),
                    "confirmed_by": (
                        section.confirmation.confirmer.name
                        if hasattr(section, "confirmation") else None
                    ),
                    "confirmed_at": (
                        section.confirmation.confirmed_at
                        if hasattr(section, "confirmation") else None
                    ),
                })
            deliveries = [
                {
                    "contact": d.contact.name,
                    "delivered_at": d.delivered_at,
                    "viewed_at": d.viewed_at,
                    "receipt": d.viewed_at is not None,
                }
                for d in packet.deliveries.select_related("contact")
            ]
            result["packets"].append({
                "subject": packet.subject,
                "subject_display": packet.get_subject_display(),
                "version": packet.version,
                "status": packet.status,
                "published_at": packet.published_at,
                "sections": sections,
                "deliveries": deliveries,
            })
        result["open_anomalies"] = [
            {"severity": a.severity, "description": a.description}
            for a in case.anomalies.filter(status=Anomaly.Status.OPEN)
        ]
        result["responsibility_transferred"] = case.responsibility_transferred
        return Response(result)


class ContactViewSet(viewsets.ModelViewSet):
    serializer_class = ContactSerializer

    def get_queryset(self):
        return AuthorizedContact.objects.filter(
            case_id=self.kwargs.get("case_pk")
        ) if "case_pk" in self.kwargs else AuthorizedContact.objects.all()


class CareEventViewSet(viewsets.ModelViewSet):
    serializer_class = CareEventSerializer

    def get_queryset(self):
        qs = CareEvent.objects.all()
        case_id = self.request.query_params.get("case")
        return qs.filter(case_id=case_id) if case_id else qs

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        event = services.confirm_event(pk, request.data.get("staff_id"))
        return Response(CareEventSerializer(event).data)

    @action(detail=True, methods=["post"])
    def correct(self, request, pk=None):
        event = services.correct_event(
            pk, request.data.get("content", ""), request.data.get("staff_id")
        )
        return Response(CareEventSerializer(event).data)


class PacketViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PacketSerializer

    def get_queryset(self):
        qs = HandoffPacket.objects.prefetch_related(
            "sections__confirmation", "changes", "anomalies"
        )
        case_id = self.request.query_params.get("case")
        return qs.filter(case_id=case_id) if case_id else qs

    @action(detail=False, methods=["post"])
    def build(self, request):
        """从中心时间线挑选已确认事实，生成/刷新带版本交接包草稿。"""
        packet = services.build_packet(
            request.data.get("case_id"), request.data.get("subject")
        )
        return Response(PacketSerializer(packet).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        """发布闸门：未签署/阻断异常/空白一律明确阻断。"""
        packet = services.publish_packet(pk)
        return Response(PacketSerializer(packet).data)

    @action(detail=True, methods=["post"])
    def deliver(self, request, pk=None):
        delivery = services.deliver_packet(pk, request.data.get("contact_id"))
        return Response(DeliverySerializer(delivery).data, status=status.HTTP_201_CREATED)


class SectionSignView(APIView):
    def post(self, request, pk):
        confirmation = services.sign_section(
            pk, request.data.get("staff_id"), request.data.get("signature", "")
        )
        return Response({
            "section": pk,
            "confirmer": confirmation.confirmer.name,
            "confirmed_at": confirmation.confirmed_at,
        })


class AnomalyResolveView(APIView):
    def post(self, request, pk):
        anomaly = services.resolve_anomaly(
            pk, request.data.get("resolution", ""), request.data.get("staff_id")
        )
        return Response(AnomalySerializer(anomaly).data)


class DeliveryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Delivery.objects.all()
    serializer_class = DeliverySerializer

    @action(detail=True, methods=["post"], url_path="view")
    def mark_viewed(self, request, pk=None):
        delivery = services.mark_viewed(pk)
        return Response(DeliverySerializer(delivery).data)


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = NotificationSerializer

    def get_queryset(self):
        qs = Notification.objects.all()
        contact_id = self.request.query_params.get("contact")
        return qs.filter(contact_id=contact_id) if contact_id else qs

    @action(detail=True, methods=["post"], url_path="ack")
    def acknowledge(self, request, pk=None):
        from django.utils import timezone

        notification = get_object_or_404(Notification, pk=pk)
        if notification.acknowledged_at is None:
            notification.acknowledged_at = timezone.now()
            notification.save(update_fields=["acknowledged_at"])
        return Response(NotificationSerializer(notification).data)
