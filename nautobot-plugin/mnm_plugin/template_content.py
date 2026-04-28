"""Template content extensions registered with Nautobot.

Empty in E1. E5 (interface-detail extension) populates this with a
``TemplateExtension`` subclass that hooks into the
``dcim.interface`` detail page and renders the four MNM panels:

  - Endpoints currently on this port
  - Endpoints historically on this port
  - LLDP neighbors on this port
  - MAC entries on this port

Per E0 §3d + §7 Q5 the rendering posture is **inline panels**, not a
tab.
"""

template_extensions = []
