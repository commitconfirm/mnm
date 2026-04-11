/**
 * MNM Icon helpers — maps device classifications to Affinity SVG icons.
 *
 * Icons are from the Ecceman Affinity set (github.com/ecceman/affinity),
 * licensed under The Unlicense (public domain).
 *
 * Usage:
 *   deviceIcon('router')        → '<img class="device-icon" src="/static/icons/devices/router.svg" alt="router">'
 *   deviceIcon('switch', 'lg')  → same but with device-icon-lg class
 *   serviceIcon('grafana')      → '<img class="service-icon" src="/static/icons/services/grafana.svg" alt="Grafana">'
 */

var MNMIcons = (function() {
  var DEVICE_ICONS = {
    'router': 'router',
    'switch': 'switch',
    'firewall': 'firewall',
    'access_point': 'access_point',
    'server': 'server',
    'printer': 'printer',
    'phone': 'phone',
    'camera': 'camera',
    'virtual_machine': 'virtual_machine',
    'container': 'container',
    'hypervisor': 'hypervisor',
    'workstation': 'workstation',
    'web_service': 'web_service',
    'network_device': 'switch',
    'endpoint': 'endpoint',
    'unknown': 'unknown',
    '': 'unknown',
  };

  var SERVICE_NAMES = {
    'nautobot': 'Nautobot',
    'grafana': 'Grafana',
    'prometheus': 'Prometheus',
    'proxmox': 'Proxmox',
  };

  function deviceIcon(classification, size) {
    var cls = (classification || '').toLowerCase();
    var file = DEVICE_ICONS[cls] || 'unknown';
    var sizeClass = size === 'lg' ? ' device-icon-lg' : (size === 'sm' ? ' device-icon-sm' : '');
    return '<img class="device-icon' + sizeClass + '" src="/static/icons/devices/' + file + '.svg" alt="' + cls + '" title="' + cls + '">';
  }

  function serviceIcon(name) {
    var key = (name || '').toLowerCase();
    var label = SERVICE_NAMES[key] || name;
    return '<img class="service-icon" src="/static/icons/services/' + key + '.svg" alt="' + label + '">';
  }

  return {
    deviceIcon: deviceIcon,
    serviceIcon: serviceIcon,
    DEVICE_ICONS: DEVICE_ICONS,
  };
})();
