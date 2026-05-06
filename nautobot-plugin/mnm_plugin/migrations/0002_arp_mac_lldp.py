"""E2 migration: ArpEntry + MacEntry + LldpNeighbor models.

Hand-written per the E1 lesson — controller build env doesn't
run Django, so we don't ``makemigrations`` from there. Field
types and defaults match ``models.py`` exactly. Indexes are
named explicitly to avoid Django auto-generated hash names.
"""

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mnm_plugin", "0001_initial"),
    ]

    operations = [
        # -----------------------------------------------------------------
        # ArpEntry
        # -----------------------------------------------------------------
        migrations.CreateModel(
            name="ArpEntry",
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
                ("ip", models.TextField()),
                ("mac", models.TextField()),
                ("interface", models.TextField(default="")),
                ("vrf", models.TextField(default="default")),
                ("collected_at", models.DateTimeField()),
            ],
            options={
                "verbose_name": "ARP entry",
                "verbose_name_plural": "ARP entries",
                "ordering": ("-collected_at", "node_name", "ip"),
            },
        ),
        migrations.AddIndex(
            model_name="arpentry",
            index=models.Index(
                fields=["node_name"],
                name="mnm_plugin_arp_node_name_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="arpentry",
            index=models.Index(
                fields=["ip"],
                name="mnm_plugin_arp_ip_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="arpentry",
            index=models.Index(
                fields=["mac"],
                name="mnm_plugin_arp_mac_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="arpentry",
            index=models.Index(
                fields=["collected_at"],
                name="mnm_plugin_arp_collected_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="arpentry",
            constraint=models.UniqueConstraint(
                fields=("node_name", "ip", "mac", "vrf"),
                name="mnm_arp_unique_node_ip_mac_vrf",
            ),
        ),
        # -----------------------------------------------------------------
        # MacEntry
        # -----------------------------------------------------------------
        migrations.CreateModel(
            name="MacEntry",
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
                ("mac", models.TextField()),
                ("interface", models.TextField(default="")),
                ("vlan", models.IntegerField(default=0)),
                ("entry_type", models.TextField(default="dynamic")),
                ("collected_at", models.DateTimeField()),
            ],
            options={
                "verbose_name": "MAC entry",
                "verbose_name_plural": "MAC entries",
                "ordering": ("-collected_at", "node_name", "mac"),
            },
        ),
        migrations.AddIndex(
            model_name="macentry",
            index=models.Index(
                fields=["node_name"],
                name="mnm_plugin_mac_node_name_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="macentry",
            index=models.Index(
                fields=["mac"],
                name="mnm_plugin_mac_mac_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="macentry",
            index=models.Index(
                fields=["vlan"],
                name="mnm_plugin_mac_vlan_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="macentry",
            index=models.Index(
                fields=["collected_at"],
                name="mnm_plugin_mac_collected_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="macentry",
            constraint=models.UniqueConstraint(
                fields=("node_name", "mac", "interface", "vlan"),
                name="mnm_mac_unique_node_mac_iface_vlan",
            ),
        ),
        # -----------------------------------------------------------------
        # LldpNeighbor
        # -----------------------------------------------------------------
        migrations.CreateModel(
            name="LldpNeighbor",
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
                ("local_interface", models.TextField()),
                ("remote_system_name", models.TextField(default="")),
                ("remote_port", models.TextField(default="")),
                ("remote_chassis_id", models.TextField(blank=True, null=True)),
                ("remote_management_ip", models.TextField(blank=True, null=True)),
                ("local_port_ifindex", models.IntegerField(blank=True, null=True)),
                ("local_port_name", models.TextField(blank=True, null=True)),
                (
                    "remote_chassis_id_subtype",
                    models.TextField(blank=True, null=True),
                ),
                (
                    "remote_port_id_subtype",
                    models.TextField(blank=True, null=True),
                ),
                (
                    "remote_system_description",
                    models.TextField(blank=True, null=True),
                ),
                ("collected_at", models.DateTimeField()),
            ],
            options={
                "verbose_name": "LLDP neighbor",
                "verbose_name_plural": "LLDP neighbors",
                "ordering": ("-collected_at", "node_name", "local_interface"),
            },
        ),
        migrations.AddIndex(
            model_name="lldpneighbor",
            index=models.Index(
                fields=["node_name"],
                name="mnm_plugin_lldp_node_name_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="lldpneighbor",
            index=models.Index(
                fields=["local_interface"],
                name="mnm_plugin_lldp_local_iface_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="lldpneighbor",
            index=models.Index(
                fields=["remote_system_name"],
                name="mnm_plugin_lldp_remote_sys_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="lldpneighbor",
            index=models.Index(
                fields=["collected_at"],
                name="mnm_plugin_lldp_collected_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="lldpneighbor",
            constraint=models.UniqueConstraint(
                fields=(
                    "node_name",
                    "local_interface",
                    "remote_system_name",
                    "remote_port",
                ),
                name="mnm_lldp_unique_node_iface_remote",
            ),
        ),
    ]
