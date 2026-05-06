"""E3 migration: Route + BgpNeighbor + Fingerprint models.

Hand-written per the E1+E2 lesson — controller build env doesn't
run Django, so we don't ``makemigrations`` from there. Field
types and defaults match ``models.py`` exactly. Indexes are
named explicitly to avoid Django auto-generated hash names.
"""

import uuid

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mnm_plugin", "0002_arp_mac_lldp"),
    ]

    operations = [
        # -----------------------------------------------------------------
        # Route
        # -----------------------------------------------------------------
        migrations.CreateModel(
            name="Route",
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
                ("node_name", models.TextField()),
                ("prefix", models.TextField()),
                ("next_hop", models.TextField(default="")),
                ("protocol", models.TextField(default="unknown")),
                ("vrf", models.TextField(default="default")),
                ("metric", models.IntegerField(blank=True, null=True)),
                ("preference", models.IntegerField(blank=True, null=True)),
                (
                    "outgoing_interface",
                    models.TextField(blank=True, null=True),
                ),
                ("active", models.BooleanField(default=True)),
                ("collected_at", models.DateTimeField()),
            ],
            options={
                "verbose_name": "Route",
                "verbose_name_plural": "Routes",
                "ordering": ("-collected_at", "node_name", "prefix"),
            },
        ),
        migrations.AddIndex(
            model_name="route",
            index=models.Index(
                fields=["node_name"],
                name="mnm_plugin_route_node_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="route",
            index=models.Index(
                fields=["prefix"],
                name="mnm_plugin_route_prefix_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="route",
            index=models.Index(
                fields=["protocol"],
                name="mnm_plugin_route_proto_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="route",
            index=models.Index(
                fields=["collected_at"],
                name="mnm_plugin_route_collected_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="route",
            constraint=models.UniqueConstraint(
                fields=("node_name", "prefix", "next_hop", "vrf"),
                name="mnm_route_unique_node_prefix_nh_vrf",
            ),
        ),
        # -----------------------------------------------------------------
        # BgpNeighbor
        # -----------------------------------------------------------------
        migrations.CreateModel(
            name="BgpNeighbor",
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
                ("node_name", models.TextField()),
                ("neighbor_ip", models.TextField()),
                ("remote_asn", models.IntegerField()),
                ("local_asn", models.IntegerField(blank=True, null=True)),
                ("state", models.TextField(default="Unknown")),
                (
                    "prefixes_received",
                    models.IntegerField(blank=True, null=True),
                ),
                (
                    "prefixes_sent",
                    models.IntegerField(blank=True, null=True),
                ),
                (
                    "uptime_seconds",
                    models.IntegerField(blank=True, null=True),
                ),
                ("vrf", models.TextField(default="default")),
                (
                    "address_family",
                    models.TextField(default="ipv4 unicast"),
                ),
                ("collected_at", models.DateTimeField()),
            ],
            options={
                "verbose_name": "BGP neighbor",
                "verbose_name_plural": "BGP neighbors",
                "ordering": ("-collected_at", "node_name", "neighbor_ip"),
            },
        ),
        migrations.AddIndex(
            model_name="bgpneighbor",
            index=models.Index(
                fields=["node_name"],
                name="mnm_plugin_bgp_node_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="bgpneighbor",
            index=models.Index(
                fields=["neighbor_ip"],
                name="mnm_plugin_bgp_ip_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="bgpneighbor",
            index=models.Index(
                fields=["state"],
                name="mnm_plugin_bgp_state_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="bgpneighbor",
            index=models.Index(
                fields=["collected_at"],
                name="mnm_plugin_bgp_collected_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="bgpneighbor",
            constraint=models.UniqueConstraint(
                fields=(
                    "node_name",
                    "neighbor_ip",
                    "vrf",
                    "address_family",
                ),
                name="mnm_bgp_unique_node_ip_vrf_af",
            ),
        ),
        # -----------------------------------------------------------------
        # Fingerprint
        # -----------------------------------------------------------------
        migrations.CreateModel(
            name="Fingerprint",
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
                ("target_mac", models.TextField()),
                ("signal_type", models.TextField()),
                ("signal_value", models.TextField()),
                (
                    "signal_metadata",
                    models.JSONField(blank=True, default=dict),
                ),
                (
                    "first_seen",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                (
                    "last_seen",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                ("seen_count", models.IntegerField(default=1)),
            ],
            options={
                "verbose_name": "Fingerprint",
                "verbose_name_plural": "Fingerprints",
                "ordering": ("-last_seen", "target_mac"),
            },
        ),
        migrations.AddIndex(
            model_name="fingerprint",
            index=models.Index(
                fields=["target_mac"],
                name="mnm_plugin_fp_mac_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="fingerprint",
            index=models.Index(
                fields=["signal_type"],
                name="mnm_plugin_fp_type_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="fingerprint",
            index=models.Index(
                fields=["signal_value"],
                name="mnm_plugin_fp_value_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="fingerprint",
            index=models.Index(
                fields=["last_seen"],
                name="mnm_plugin_fp_lastsn_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="fingerprint",
            constraint=models.UniqueConstraint(
                fields=("target_mac", "signal_type", "signal_value"),
                name="mnm_fingerprint_unique_mac_type_value",
            ),
        ),
    ]
