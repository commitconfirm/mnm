/* mnm-plugin filter bar (E6).
 *
 * Wires up:
 *   - Saved-filter chip clicks (Layer 2): toggle ?preset_<name>=true
 *     in the URL and reload.
 *   - Per-column input behaviour (Layer 1) is handled by the inline
 *     <form method="get">; this script just wires the "Clear" button.
 *   - Expression-mode toggle (Layer 3): show/hide the DSL input row.
 *   - Expression-mode Apply: navigate to the same URL with ?q=<DSL>.
 *   - Expression-mode Clear: drop the q param and reload.
 *   - Export buttons: navigate to the export URL with current query.
 *
 * Vanilla JS, no jQuery. Same posture as the controller's frontend.
 *
 * Idempotent: safe to include multiple times. The init guard at the
 * bottom no-ops on re-run.
 */

(function () {
  "use strict";

  if (window.__mnmFilterBarInit) return;
  window.__mnmFilterBarInit = true;

  function getCurrentParams() {
    return new URLSearchParams(window.location.search);
  }

  function navigateWithParams(params) {
    var url = window.location.pathname + "?" + params.toString();
    window.location.href = url;
  }

  // -- chip click: toggle ?preset_<name>=true
  document.addEventListener("click", function (event) {
    var chip = event.target.closest(".mnm-filter-chip");
    if (!chip) return;
    event.preventDefault();
    var preset = chip.getAttribute("data-preset");
    var active = chip.getAttribute("data-active") === "true";
    var params = getCurrentParams();
    if (active) {
      params.delete(preset);
    } else {
      params.set(preset, "true");
    }
    navigateWithParams(params);
  });

  // -- expression-mode toggle: show/hide the DSL input
  document.addEventListener("click", function (event) {
    var btn = event.target.closest(".mnm-filter-expression-toggle");
    if (!btn) return;
    event.preventDefault();
    var bar = btn.closest(".mnm-filter-bar");
    if (!bar) return;
    var input = bar.querySelector(".mnm-filter-expression-input");
    if (!input) return;
    if (input.style.display === "none" || input.style.display === "") {
      input.style.display = "block";
      btn.textContent = "▼ Hide expression mode";
    } else {
      input.style.display = "none";
      btn.textContent = "▶ Expression mode (DSL)…";
    }
  });

  // -- expression-mode Apply: set ?q=<value> and reload
  document.addEventListener("click", function (event) {
    var btn = event.target.closest(".mnm-filter-expression-apply");
    if (!btn) return;
    event.preventDefault();
    var bar = btn.closest(".mnm-filter-bar");
    if (!bar) return;
    var input = bar.querySelector(".mnm-filter-expression-q");
    if (!input) return;
    var params = getCurrentParams();
    var value = input.value.trim();
    if (value) {
      params.set("q", value);
    } else {
      params.delete("q");
    }
    navigateWithParams(params);
  });

  // -- expression-mode Apply on Enter
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter") return;
    var input = event.target.closest(".mnm-filter-expression-q");
    if (!input) return;
    event.preventDefault();
    var params = getCurrentParams();
    var value = input.value.trim();
    if (value) {
      params.set("q", value);
    } else {
      params.delete("q");
    }
    navigateWithParams(params);
  });

  // -- expression-mode Clear: drop q and reload
  document.addEventListener("click", function (event) {
    var btn = event.target.closest(".mnm-filter-expression-clear");
    if (!btn) return;
    event.preventDefault();
    var params = getCurrentParams();
    params.delete("q");
    navigateWithParams(params);
  });

  // -- column-filter Clear: drop all column params (keep presets/q)
  document.addEventListener("click", function (event) {
    var btn = event.target.closest(".mnm-filter-clear");
    if (!btn) return;
    event.preventDefault();
    var bar = btn.closest(".mnm-filter-bar");
    if (!bar) return;
    var params = getCurrentParams();
    var inputs = bar.querySelectorAll(".mnm-filter-column");
    inputs.forEach(function (inp) {
      params.delete(inp.name);
    });
    navigateWithParams(params);
  });

  // -- export buttons: navigate to export URL with current query
  function buildExportUrl(extension) {
    var bar = document.querySelector(".mnm-filter-bar");
    if (!bar) return null;
    var key = bar.getAttribute("data-export-key");
    if (!key) return null;
    var params = getCurrentParams();
    var path = window.location.pathname.replace(/\/+$/, "");
    // path looks like /plugins/mnm/<slug>; replace trailing slug with
    // <slug>/export.<ext>. Be defensive: accept .../<slug>/ as well.
    var parts = path.split("/");
    // Drop empty trailing segment from the split if the URL ended in /
    while (parts.length && parts[parts.length - 1] === "") parts.pop();
    // Last segment should be the slug; if not, fall back to data-export-key.
    parts[parts.length - 1] = key;
    var basePath = parts.join("/");
    var qs = params.toString();
    return basePath + "/export." + extension + (qs ? "?" + qs : "");
  }

  document.addEventListener("click", function (event) {
    var csvBtn = event.target.closest(".mnm-export-csv");
    if (csvBtn) {
      event.preventDefault();
      var url = buildExportUrl("csv");
      if (url) window.location.href = url;
      return;
    }
    var jsonBtn = event.target.closest(".mnm-export-json");
    if (jsonBtn) {
      event.preventDefault();
      var url2 = buildExportUrl("json");
      if (url2) window.location.href = url2;
    }
  });

})();
