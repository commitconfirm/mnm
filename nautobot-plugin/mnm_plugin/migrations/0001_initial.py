"""Initial migration: Endpoint model.

Hand-crafted (rather than makemigrations-generated) because the
controller-side build environment doesn't run Django; the
migration ships pre-baked. ``makemigrations --check`` in CI
verifies this matches what Django would generate from
``models.py``.
"""

import uuid

import django.db.models.deletion
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("extras", "0001_initial_part_1"),
    ]

    operations = [
        migrations.CreateModel(
            name="Endpoint",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                ("mac_address", models.TextField()),
                ("current_switch", models.TextField()),
                ("current_port", models.TextField()),
                ("current_vlan", models.IntegerField()),
                ("active", models.BooleanField(default=True)),
                ("is_uplink", models.BooleanField(default=False)),
                ("current_ip", models.TextField(blank=True, null=True)),
                ("additional_ips", models.JSONField(blank=True, default=list)),
                ("mac_vendor", models.TextField(blank=True, null=True)),
                ("hostname", models.TextField(blank=True, null=True)),
                ("classification", models.TextField(blank=True, null=True)),
                (
                    "classification_confidence",
                    models.TextField(blank=True, null=True),
                ),
                ("classification_override", models.BooleanField(default=False)),
                ("dhcp_server", models.TextField(blank=True, null=True)),
                ("dhcp_lease_start", models.DateTimeField(blank=True, null=True)),
                ("dhcp_lease_expiry", models.DateTimeField(blank=True, null=True)),
                ("first_seen", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_seen", models.DateTimeField(default=django.utils.timezone.now)),
                ("data_source", models.TextField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "Endpoint",
                "verbose_name_plural": "Endpoints",
                "ordering": ("-last_seen", "mac_address"),
            },
        ),
        migrations.AddIndex(
            model_name="endpoint",
            index=models.Index(
                fields=["mac_address"], name="mnm_plugin__mac_add_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="endpoint",
            index=models.Index(fields=["active"], name="mnm_plugin__active_idx"),
        ),
        migrations.AddIndex(
            model_name="endpoint",
            index=models.Index(
                fields=["current_ip"], name="mnm_plugin__cur_ip_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="endpoint",
            index=models.Index(
                fields=["last_seen"], name="mnm_plugin__lastsn_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="endpoint",
            constraint=models.UniqueConstraint(
                fields=(
                    "mac_address",
                    "current_switch",
                    "current_port",
                    "current_vlan",
                ),
                name="mnm_endpoint_unique_location",
            ),
        ),
    ]
