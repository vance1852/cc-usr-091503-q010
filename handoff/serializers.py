from rest_framework import serializers

from .models import (
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
    VersionChange,
)


class StaffSerializer(serializers.ModelSerializer):
    role_display = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = Staff
        fields = ["id", "name", "role", "role_display", "phone", "organization"]


class CareEventSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = CareEvent
        fields = [
            "id", "case", "subject", "category", "category_display",
            "content", "occurred_at", "recorded_by", "confirmed_by",
            "confirmed_at", "status", "status_display",
        ]
        read_only_fields = ["status", "confirmed_by", "confirmed_at"]


class ContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuthorizedContact
        fields = ["id", "case", "name", "relation", "channel"]


class CaseSerializer(serializers.ModelSerializer):
    path_display = serializers.CharField(source="get_path_display", read_only=True)
    transfer_status_display = serializers.CharField(
        source="get_transfer_status_display", read_only=True
    )
    responsibility_transferred = serializers.BooleanField(read_only=True)

    class Meta:
        model = DischargeCase
        fields = [
            "id", "mother_name", "baby_name", "planned_discharge_at",
            "path", "path_display", "transfer_status", "transfer_status_display",
            "target_name", "target_contact", "responsibility_transferred",
            "created_at",
        ]
        read_only_fields = [
            "transfer_status", "path", "target_name", "target_contact",
            "responsibility_transferred",
        ]


class ConfirmationSerializer(serializers.ModelSerializer):
    confirmer_name = serializers.CharField(source="confirmer.name", read_only=True)

    class Meta:
        model = SectionConfirmation
        fields = ["id", "confirmer", "confirmer_name", "signature", "confirmed_at"]
        read_only_fields = ["confirmer", "confirmed_at"]


class SectionSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    state_display = serializers.CharField(source="get_state_display", read_only=True)
    confirmation = ConfirmationSerializer(read_only=True)
    responsible_name = serializers.CharField(source="responsible.name", read_only=True)

    class Meta:
        model = PacketSection
        fields = [
            "id", "kind", "kind_display", "responsible", "responsible_name",
            "content", "state", "state_display", "confirmation",
        ]


class ChangeSerializer(serializers.ModelSerializer):
    class Meta:
        model = VersionChange
        fields = ["id", "text", "created_at"]


class AnomalySerializer(serializers.ModelSerializer):
    severity_display = serializers.CharField(source="get_severity_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Anomaly
        fields = [
            "id", "packet", "case", "section", "severity", "severity_display",
            "description", "status", "status_display", "created_at",
            "closed_at", "resolution",
        ]
        read_only_fields = ["status", "closed_at", "resolution"]


class PacketSerializer(serializers.ModelSerializer):
    subject_display = serializers.CharField(source="get_subject_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    sections = SectionSerializer(many=True, read_only=True)
    changes = ChangeSerializer(many=True, read_only=True)
    anomalies = AnomalySerializer(many=True, read_only=True)

    class Meta:
        model = HandoffPacket
        fields = [
            "id", "case", "subject", "subject_display", "version",
            "status", "status_display", "created_at", "published_at",
            "sections", "changes", "anomalies",
        ]


class DeliverySerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.name", read_only=True)

    class Meta:
        model = Delivery
        fields = ["id", "packet", "contact", "contact_name", "delivered_at", "viewed_at"]
        read_only_fields = ["delivered_at", "viewed_at"]


class NotificationSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.name", read_only=True)

    class Meta:
        model = Notification
        fields = [
            "id", "contact", "contact_name", "packet", "from_version",
            "message", "sent_at", "acknowledged_at",
        ]
        read_only_fields = ["sent_at", "acknowledged_at"]
