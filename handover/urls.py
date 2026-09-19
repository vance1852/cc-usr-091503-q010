from rest_framework.routers import DefaultRouter

from .views import (
    AnomalyViewSet,
    CaseViewSet,
    HandoverViewSet,
    NotificationViewSet,
    ProfessionalViewSet,
    SummaryVersionViewSet,
    SummaryViewSet,
    TimelineEventViewSet,
)

router = DefaultRouter()
router.register("cases", CaseViewSet, basename="case")
router.register("professionals", ProfessionalViewSet, basename="professional")
router.register("timeline-events", TimelineEventViewSet, basename="timeline-event")
router.register("anomalies", AnomalyViewSet, basename="anomaly")
router.register("summary-versions", SummaryVersionViewSet, basename="summary-version")
router.register("summaries", SummaryViewSet, basename="summary")
router.register("handovers", HandoverViewSet, basename="handover")
router.register("notifications", NotificationViewSet, basename="notification")

urlpatterns = router.urls
