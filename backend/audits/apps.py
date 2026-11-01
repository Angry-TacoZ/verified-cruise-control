"""Audit engine app: durable jobs, executions, observations, findings."""

from django.apps import AppConfig


class AuditsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "audits"
