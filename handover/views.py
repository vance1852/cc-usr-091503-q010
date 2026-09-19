from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import services
from .exceptions import DomainValidationError
from .models import (
    Anomaly,
    AuthorizedContact,
    Case,
    ContactChannel,
    FollowUpPlan,
    Handover,
    Notification,
    Professional,
    Summary,
    SummaryVersion,
    TimelineEvent,
    TodoItem,
)
from .serializers import (
    AnomalySerializer,
    AuthorizedContactSerializer,
    CaseSerializer,
    ChangeDischargeSerializer,
    CloseAnomalySerializer,
    ComposeSerializer,
    ConfirmEventSerializer,
    ContactChannelSerializer,
    CorrectSerializer,
    FollowUpPlanSerializer,
    HandoverCreateSerializer,
    HandoverSerializer,
    NotificationSerializer,
    ProfessionalActionSerializer,
    ProfessionalSerializer,
    PublishSerializer,
    ReceiverConfirmSerializer,
    SummaryVersionSerializer,
    TimelineEventSerializer,
    TodoItemSerializer,
)


class CaseViewSet(viewsets.GenericViewSet, mixins.CreateModelMixin,
                  mixins.ListModelMixin, mixins.RetrieveModelMixin):
    queryset = Case.objects.all()
    serializer_class = CaseSerializer

    @action(detail=True, methods=["post"])
    def timeline_events(self, request, pk=None):
        """录入时间线事实（初始为待确认）。"""
        case = self.get_object()
        serializer = TimelineEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(case=case)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def anomalies(self, request, pk=None):
        case = self.get_object()
        serializer = AnomalySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(case=case)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def todos(self, request, pk=None):
        case = self.get_object()
        serializer = TodoItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(case=case)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def followups(self, request, pk=None):
        case = self.get_object()
        serializer = FollowUpPlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(case=case)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def contact_channels(self, request, pk=None):
        case = self.get_object()
        serializer = ContactChannelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(case=case)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def contacts(self, request, pk=None):
        """登记授权联系人。"""
        case = self.get_object()
        serializer = AuthorizedContactSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(case=case)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def compose(self, request, pk=None):
        """从已确认时间线事实生成产妇/婴儿两份摘要草稿。"""
        case = self.get_object()
        serializer = ComposeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        coordinator = services.get_professional(serializer.validated_data["professional_id"])
        versions = services.compose_summaries(case, coordinator)
        return Response({
            subject: SummaryVersionSerializer(v).data
            for subject, v in versions.items()
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def handovers(self, request, pk=None):
        """发起交接（提前离馆 / 转往其他机构 / 月嫂上门服务）。"""
        case = self.get_object()
        serializer = HandoverCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        actor = services.get_professional(data["professional_id"])
        matron = (services.get_professional(data["matron_id"])
                  if data.get("matron_id") else None)
        handover = services.initiate_handover(
            case, actor,
            path_type=data["path_type"],
            receiver_name=data["receiver_name"],
            receiver_org=data["receiver_org"],
            receiver_contact=data["receiver_contact"],
            matron=matron,
            service_address=data["service_address"],
            scheduled_at=data.get("scheduled_at"),
            notes=data["notes"],
        )
        return Response(HandoverSerializer(handover).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="change-discharge")
    def change_discharge(self, request, pk=None):
        """变更预计离馆时间：重排随访/待办，更新待确认交接，通知联系人。"""
        case = self.get_object()
        serializer = ChangeDischargeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        actor = services.get_professional(data["professional_id"])
        report = services.change_discharge(
            case, data["new_time"], actor, reason=data["reason"]
        )
        return Response({
            "old": report["old"],
            "new": report["new"],
            "rescheduled_followups": report["rescheduled_followups"],
            "rescheduled_todos": report["rescheduled_todos"],
            "handovers_updated": report["handovers_updated"],
        })

    @action(detail=True, methods=["get"], url_path="handover-page")
    def handover_page(self, request, pk=None):
        """家庭交接页：当前有效建议、待办、联系渠道、版本变化。"""
        case = self.get_object()
        contact_id = request.query_params.get("contact_id")
        if not contact_id:
            raise DomainValidationError("缺少 contact_id 参数")
        page = services.family_handover_page(case, contact_id)
        return Response(page)

    @action(detail=True, methods=["get"])
    def tracking(self, request, pk=None):
        """服务专员追踪：章节签署人/时间、送达与回执、交接进展。"""
        case = self.get_object()
        return Response(services.tracking_view(case))


class ProfessionalViewSet(viewsets.GenericViewSet, mixins.CreateModelMixin,
                          mixins.ListModelMixin):
    queryset = Professional.objects.all()
    serializer_class = ProfessionalSerializer


class TimelineEventViewSet(viewsets.GenericViewSet):
    queryset = TimelineEvent.objects.all()

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """专业人员确认自己负责类别的事实。"""
        serializer = ConfirmEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        professional = services.get_professional(
            serializer.validated_data["professional_id"]
        )
        event = services.confirm_timeline_event(self.get_object(), professional)
        return Response(TimelineEventSerializer(event).data)


class AnomalyViewSet(viewsets.GenericViewSet):
    queryset = Anomaly.objects.all()

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        """闭环异常。"""
        serializer = CloseAnomalySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        professional = services.get_professional(
            serializer.validated_data["professional_id"]
        )
        anomaly = self.get_object()
        if anomaly.status == Anomaly.Status.CLOSED:
            raise DomainValidationError("该异常已闭环")
        anomaly.status = Anomaly.Status.CLOSED
        anomaly.resolution = serializer.validated_data["resolution"]
        anomaly.closed_by = professional
        anomaly.closed_at = timezone.now()
        anomaly.save(update_fields=["status", "resolution", "closed_by", "closed_at"])
        return Response(AnomalySerializer(anomaly).data)


class SummaryVersionViewSet(viewsets.GenericViewSet, mixins.RetrieveModelMixin):
    queryset = SummaryVersion.objects.all()
    serializer_class = SummaryVersionSerializer

    @action(detail=True, methods=["post"], url_path=r"sections/(?P<section_id>\d+)/sign")
    def sign_section(self, request, pk=None, section_id=None):
        """专业人员签署自己负责的章节（并发安全）。"""
        serializer = ProfessionalActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        professional = services.get_professional(
            serializer.validated_data["professional_id"]
        )
        version = self.get_object()
        section = get_object_or_404(version.sections, pk=section_id)
        section = services.sign_section(section.id, professional)
        return Response({
            "section_id": section.id,
            "category": section.category,
            "signed_by": professional.name,
            "signed_at": section.signed_at,
        })

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        """发布版本：全部章节签署 + 无未闭环阻断异常。"""
        serializer = PublishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        publisher = services.get_professional(
            serializer.validated_data["professional_id"]
        )
        version = services.publish_version(self.get_object(), publisher)
        return Response(SummaryVersionSerializer(version).data)


class SummaryViewSet(viewsets.GenericViewSet):
    queryset = Summary.objects.all()

    @action(detail=True, methods=["post"])
    def correct(self, request, pk=None):
        """对已发布摘要更正：生成新版本草稿，变化章节需重新签署。"""
        serializer = CorrectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        editor = services.get_professional(serializer.validated_data["professional_id"])
        new_version = services.correct_summary(
            self.get_object(),
            editor,
            change_note=serializer.validated_data["change_note"],
            section_updates=serializer.validated_data["section_updates"],
        )
        return Response(SummaryVersionSerializer(new_version).data,
                        status=status.HTTP_201_CREATED)


class HandoverViewSet(viewsets.GenericViewSet, mixins.RetrieveModelMixin):
    queryset = Handover.objects.all()
    serializer_class = HandoverSerializer

    @action(detail=True, methods=["post"], url_path="receiver-confirm")
    def receiver_confirm(self, request, pk=None):
        """接收方确认，完成责任转移。"""
        serializer = ReceiverConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        handover = services.receiver_confirm(
            self.get_object().id, serializer.validated_data["receiver_name"]
        )
        return Response(HandoverSerializer(handover).data)


class NotificationViewSet(viewsets.GenericViewSet, mixins.ListModelMixin):
    serializer_class = NotificationSerializer

    def get_queryset(self):
        qs = Notification.objects.all()
        case_id = self.request.query_params.get("case")
        contact_id = self.request.query_params.get("contact")
        if case_id:
            qs = qs.filter(case_id=case_id)
        if contact_id:
            qs = qs.filter(contact_id=contact_id)
        return qs
