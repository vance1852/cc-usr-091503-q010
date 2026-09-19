from rest_framework import serializers

from .models import (
    Anomaly,
    AuthorizedContact,
    Case,
    ContactChannel,
    FollowUpPlan,
    Handover,
    Notification,
    Professional,
    SummarySection,
    SummaryVersion,
    TimelineEvent,
    TodoItem,
)


class CaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Case
        fields = [
            "id", "family_name", "mother_name", "baby_name",
            "expected_discharge_at", "actual_discharge_at", "status",
        ]


class ProfessionalSerializer(serializers.ModelSerializer):
    role_label = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = Professional
        fields = ["id", "name", "role", "role_label", "phone"]


class TimelineEventSerializer(serializers.ModelSerializer):
    confirmed_by_name = serializers.CharField(
        source="confirmed_by.name", read_only=True, default=None
    )

    class Meta:
        model = TimelineEvent
        fields = [
            "id", "case", "subject", "category", "content", "occurred_at",
            "status", "confirmed_by", "confirmed_by_name", "confirmed_at",
        ]
        read_only_fields = ["status", "confirmed_by", "confirmed_at"]


class AnomalySerializer(serializers.ModelSerializer):
    class Meta:
        model = Anomaly
        fields = [
            "id", "case", "subject", "description", "severity", "status",
            "raised_by", "resolution", "closed_by", "closed_at",
        ]
        read_only_fields = ["status", "resolution", "closed_by", "closed_at"]


class TodoItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = TodoItem
        fields = ["id", "case", "subject", "title", "detail", "due_at", "status"]
        read_only_fields = ["status"]


class FollowUpPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = FollowUpPlan
        fields = [
            "id", "case", "subject", "purpose", "channel",
            "offset_days", "scheduled_at", "status",
        ]
        read_only_fields = ["status"]


class ContactChannelSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactChannel
        fields = ["id", "case", "label", "kind", "value", "hours"]


class AuthorizedContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuthorizedContact
        fields = ["id", "case", "name", "relation", "phone", "is_primary"]


class SummarySectionSerializer(serializers.ModelSerializer):
    signed_by_name = serializers.CharField(
        source="signed_by.name", read_only=True, default=None
    )

    class Meta:
        model = SummarySection
        fields = [
            "id", "category", "required_role", "content",
            "signed_by", "signed_by_name", "signed_at",
        ]


class SummaryVersionSerializer(serializers.ModelSerializer):
    sections = SummarySectionSerializer(many=True, read_only=True)
    subject = serializers.CharField(source="summary.subject", read_only=True)

    class Meta:
        model = SummaryVersion
        fields = [
            "id", "subject", "version_no", "status", "change_note",
            "created_at", "published_at", "sections",
        ]


class HandoverSerializer(serializers.ModelSerializer):
    responsibility_transferred = serializers.BooleanField(read_only=True)

    class Meta:
        model = Handover
        fields = [
            "id", "case", "path_type", "status", "receiver_name", "receiver_org",
            "receiver_contact", "matron", "service_address", "scheduled_at",
            "initiated_by", "initiated_at", "receiver_confirmed_at",
            "responsibility_transferred", "notes",
        ]
        read_only_fields = ["status", "initiated_at", "receiver_confirmed_at"]


class NotificationSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.name", read_only=True)

    class Meta:
        model = Notification
        fields = ["id", "case", "contact", "contact_name", "version",
                  "kind", "message", "created_at"]


# ---- 动作请求 ----

class ProfessionalActionSerializer(serializers.Serializer):
    professional_id = serializers.IntegerField()


class ConfirmEventSerializer(serializers.Serializer):
    professional_id = serializers.IntegerField()


class ComposeSerializer(serializers.Serializer):
    professional_id = serializers.IntegerField()


class PublishSerializer(serializers.Serializer):
    professional_id = serializers.IntegerField()


class CorrectSerializer(serializers.Serializer):
    professional_id = serializers.IntegerField()
    change_note = serializers.CharField()
    section_updates = serializers.DictField(
        child=serializers.JSONField(), required=False, default=dict
    )


class HandoverCreateSerializer(serializers.Serializer):
    professional_id = serializers.IntegerField()
    path_type = serializers.ChoiceField(choices=Handover.PathType.choices)
    receiver_name = serializers.CharField()
    receiver_org = serializers.CharField(required=False, allow_blank=True, default="")
    receiver_contact = serializers.CharField(required=False, allow_blank=True, default="")
    matron_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    service_address = serializers.CharField(required=False, allow_blank=True, default="")
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class ReceiverConfirmSerializer(serializers.Serializer):
    receiver_name = serializers.CharField(required=False, allow_blank=True, default="")


class ChangeDischargeSerializer(serializers.Serializer):
    professional_id = serializers.IntegerField()
    new_time = serializers.DateTimeField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class CloseAnomalySerializer(serializers.Serializer):
    professional_id = serializers.IntegerField()
    resolution = serializers.CharField()
