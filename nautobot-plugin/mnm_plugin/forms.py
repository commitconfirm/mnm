"""Filter forms.

E1 ships a minimal ``EndpointFilterForm`` exposing the indexed
fields. E6 (filter framework) replaces this with the full
expression-mode + saved-presets UI specified in E0 §4.
"""

from django import forms

from nautobot.apps.forms import NautobotFilterForm

from mnm_plugin import models


class EndpointFilterForm(NautobotFilterForm):
    model = models.Endpoint

    q = forms.CharField(
        required=False,
        label="Search",
    )
    mac_address = forms.CharField(required=False)
    current_ip = forms.CharField(required=False)
    current_switch = forms.CharField(required=False)
    current_port = forms.CharField(required=False)
    current_vlan = forms.IntegerField(required=False)
    active = forms.NullBooleanField(required=False)
