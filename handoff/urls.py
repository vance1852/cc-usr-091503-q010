from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("staff", views.StaffViewSet, basename="staff")
router.register("cases", views.CaseViewSet, basename="case")
router.register("contacts", views.ContactViewSet, basename="contact")
router.register("events", views.CareEventViewSet, basename="event")
router.register("packets", views.PacketViewSet, basename="packet")
router.register("deliveries", views.DeliveryViewSet, basename="delivery")
router.register("notifications", views.NotificationViewSet, basename="notification")

urlpatterns = [
    path("", include(router.urls)),
    path("sections/<int:pk>/sign/", views.SectionSignView.as_view(), name="section-sign"),
    path("anomalies/<int:pk>/resolve/", views.AnomalyResolveView.as_view(), name="anomaly-resolve"),
]
