"""Filter forms.

E1 ships ``EndpointFilterForm``. E2 adds ``ArpEntryFilterForm``,
``MacEntryFilterForm``, ``LldpNeighborFilterForm``. E6 replaces
all four with the expression-mode + saved-presets UI in E0 §4.
"""

from django import forms

from nautobot.apps.forms import NautobotFilterForm

from mnm_plugin import models


class EndpointFilterForm(NautobotFilterForm):
    model = models.Endpoint

    q = forms.CharField(required=False, label="Search")
    mac_address = forms.CharField(required=False)
    current_ip = forms.CharField(required=False)
    current_switch = forms.CharField(required=False)
    current_port = forms.CharField(required=False)
    current_vlan = forms.IntegerField(required=False)
    active = forms.NullBooleanField(required=False)


class ArpEntryFilterForm(NautobotFilterForm):
    model = models.ArpEntry

    q = forms.CharField(required=False, label="Search")
    node_name = forms.CharField(required=False)
    ip = forms.CharField(required=False)
    mac = forms.CharField(required=False)
    interface = forms.CharField(required=False)
    vrf = forms.CharField(required=False)


class MacEntryFilterForm(NautobotFilterForm):
    model = models.MacEntry

    q = forms.CharField(required=False, label="Search")
    node_name = forms.CharField(required=False)
    mac = forms.CharField(required=False)
    interface = forms.CharField(required=False)
    vlan = forms.IntegerField(required=False)
    entry_type = forms.ChoiceField(
        required=False,
        choices=[
            ("", "---------"),
            ("static", "Static"),
            ("dynamic", "Dynamic"),
        ],
    )


class LldpNeighborFilterForm(NautobotFilterForm):
    model = models.LldpNeighbor

    q = forms.CharField(required=False, label="Search")
    node_name = forms.CharField(required=False)
    local_interface = forms.CharField(required=False)
    remote_system_name = forms.CharField(required=False)
    remote_port = forms.CharField(required=False)
    remote_chassis_id = forms.CharField(required=False)


class RouteFilterForm(NautobotFilterForm):
    model = models.Route

    q = forms.CharField(required=False, label="Search")
    node_name = forms.CharField(required=False)
    prefix = forms.CharField(required=False)
    next_hop = forms.CharField(required=False)
    protocol = forms.ChoiceField(
        required=False,
        choices=[
            ("", "---------"),
            ("bgp", "BGP"),
            ("ospf", "OSPF"),
            ("ospf3", "OSPFv3"),
            ("isis", "IS-IS"),
            ("static", "Static"),
            ("connected", "Connected"),
            ("direct", "Direct"),
            ("local", "Local"),
            ("rip", "RIP"),
            ("eigrp", "EIGRP"),
        ],
    )
    vrf = forms.CharField(required=False)
    active = forms.NullBooleanField(required=False)


class BgpNeighborFilterForm(NautobotFilterForm):
    model = models.BgpNeighbor

    q = forms.CharField(required=False, label="Search")
    node_name = forms.CharField(required=False)
    neighbor_ip = forms.CharField(required=False)
    remote_asn = forms.IntegerField(required=False)
    state = forms.ChoiceField(
        required=False,
        choices=[
            ("", "---------"),
            ("Established", "Established"),
            ("Up", "Up"),
            ("Idle", "Idle"),
            ("Active", "Active"),
            ("Connect", "Connect"),
            ("OpenSent", "OpenSent"),
            ("OpenConfirm", "OpenConfirm"),
            ("Down", "Down"),
            ("Unknown", "Unknown"),
        ],
    )
    vrf = forms.CharField(required=False)
    address_family = forms.CharField(required=False)


class FingerprintFilterForm(NautobotFilterForm):
    model = models.Fingerprint

    q = forms.CharField(required=False, label="Search")
    target_mac = forms.CharField(required=False)
    signal_type = forms.ChoiceField(
        required=False,
        choices=[
            ("", "---------"),
            ("ssh_hostkey", "SSH Host Key"),
            ("tls_cert", "TLS Cert"),
            ("snmpv3_engineid", "SNMPv3 EngineID"),
            ("mdns", "mDNS"),
            ("netbios", "NetBIOS"),
            ("ssdp", "SSDP"),
        ],
    )
    signal_value = forms.CharField(required=False)
