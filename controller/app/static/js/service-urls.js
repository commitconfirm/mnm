/**
 * MNM Service URLs — single source of truth for external service links.
 *
 * Fetches URLs from /api/service-urls (which derives them from the request's
 * Host header + configurable ports). All pages should use MNMServiceURLs
 * instead of hardcoding ports like :8443 or :8080.
 *
 * Usage:
 *   await MNMServiceURLs.load();
 *   MNMServiceURLs.nautobot()          → "http://192.168.1.10:8443"
 *   MNMServiceURLs.grafana()           → "http://192.168.1.10:8080/grafana/"
 *   MNMServiceURLs.nautobotDevice(id)  → "http://192.168.1.10:8443/dcim/devices/{id}/"
 *   MNMServiceURLs.grafanaDashboard(uid) → "http://192.168.1.10:8080/grafana/d/{uid}"
 */

var MNMServiceURLs = (function() {
  var _urls = null;
  // Fallback: derive from current hostname + default ports (used before load completes)
  function _fallback() {
    var host = window.location.hostname;
    var proto = window.location.protocol;
    return {
      nautobot: proto + '//' + host + ':8443',
      grafana: proto + '//' + host + ':8080/grafana/',
      prometheus: proto + '//' + host + ':8080/prometheus/',
    };
  }

  async function load() {
    try {
      var r = await fetch('/api/service-urls');
      if (r.ok) _urls = await r.json();
    } catch (e) {
      // Use fallback
    }
    if (!_urls) _urls = _fallback();
    return _urls;
  }

  function get() {
    return _urls || _fallback();
  }

  function nautobot() { return get().nautobot; }
  function grafana() { return get().grafana; }
  function prometheus() { return get().prometheus; }

  function nautobotDevice(id) { return nautobot() + '/dcim/devices/' + id + '/'; }
  function grafanaDashboard(uid) { return grafana() + 'd/' + uid; }

  return {
    load: load,
    get: get,
    nautobot: nautobot,
    grafana: grafana,
    prometheus: prometheus,
    nautobotDevice: nautobotDevice,
    grafanaDashboard: grafanaDashboard,
  };
})();
