from django.contrib import admin

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
    Summary,
    SummarySection,
    SummaryVersion,
    SummaryView,
    TimelineEvent,
    TodoItem,
)

for model in (
    Case, Professional, TimelineEvent, Anomaly, Summary, SummaryVersion,
    SummarySection, TodoItem, FollowUpPlan, ContactChannel, Handover,
    AuthorizedContact, SummaryView, DeliveryReceipt, Notification,
):
    admin.site.register(model)
