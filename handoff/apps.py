from django.apps import AppConfig
from django.db.backends.signals import connection_created


def enable_sqlite_wal(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA busy_timeout=30000;")
            cursor.execute("PRAGMA foreign_keys=ON;")


class HandoffConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "handoff"
    verbose_name = "母婴离馆衔接"

    def ready(self):
        connection_created.connect(enable_sqlite_wal)
