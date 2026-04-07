/**
 * MNM Preferences — slide-out panel for user settings.
 *
 * Settings stored in localStorage under 'mnm-preferences'.
 * Available preferences:
 * - theme: dark/light/system (integrates with existing theme switcher)
 * - defaultPageSize: 25/50/100/500/1000
 * - autoRefreshInterval: 15/30/60/0 (seconds, 0=off)
 *
 * Dispatches 'mnm-preferences-changed' custom event on save so pages
 * can react to preference changes without polling.
 */

const MNMPreferences = {
  _panelId: 'mnm-prefs-panel',
  _overlayId: 'mnm-prefs-overlay',

  defaults: {
    theme: 'dark',
    defaultPageSize: 100,
    autoRefreshInterval: 30,
  },

  /** Load all preferences, merging with defaults. */
  load() {
    try {
      const stored = JSON.parse(localStorage.getItem('mnm-preferences') || '{}');
      return Object.assign({}, this.defaults, stored);
    } catch (e) {
      return Object.assign({}, this.defaults);
    }
  },

  /** Save preferences and dispatch change event. */
  save(prefs) {
    localStorage.setItem('mnm-preferences', JSON.stringify(prefs));
    window.dispatchEvent(new CustomEvent('mnm-preferences-changed', { detail: prefs }));
  },

  /** Get a single preference value. */
  get(key) {
    return this.load()[key];
  },

  /** Initialize the gear icon and panel. Call once per page. */
  init() {
    // Wire up the gear button if it exists
    const btn = document.getElementById('prefs-btn');
    if (btn) {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        this.open();
      });
    }

    // Apply saved theme on load
    const prefs = this.load();
    if (prefs.theme) {
      document.documentElement.setAttribute('data-theme', prefs.theme);
      localStorage.setItem('mnm-theme', prefs.theme);
    }
  },

  /** Open the preferences panel. */
  open() {
    // Remove existing panel if any
    this.close();

    const prefs = this.load();

    // Create overlay
    const overlay = document.createElement('div');
    overlay.id = this._overlayId;
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:999;';
    overlay.addEventListener('click', () => this.close());

    // Create panel
    const panel = document.createElement('div');
    panel.id = this._panelId;
    panel.style.cssText = 'position:fixed;top:0;right:0;bottom:0;width:320px;background:var(--bg-surface);'
      + 'border-left:1px solid var(--border);z-index:1000;padding:24px;overflow-y:auto;'
      + 'box-shadow:-4px 0 24px rgba(0,0,0,0.3);';

    panel.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;">'
      + '<h2 style="margin:0;font-size:1rem;color:var(--text);">Preferences</h2>'
      + '<button id="prefs-close-btn" style="background:none;border:none;color:var(--text-muted);font-size:1.2rem;cursor:pointer;">&times;</button>'
      + '</div>'

      // Theme
      + '<div style="margin-bottom:20px;">'
      + '<label style="margin-bottom:8px;display:block;font-weight:600;">Theme</label>'
      + '<div style="display:flex;gap:12px;">'
      + this._radioBtn('pref-theme', 'dark', 'Dark', prefs.theme === 'dark')
      + this._radioBtn('pref-theme', 'light', 'Light', prefs.theme === 'light')
      + this._radioBtn('pref-theme', 'system', 'System', prefs.theme === 'system')
      + '</div></div>'

      // Default page size
      + '<div style="margin-bottom:20px;">'
      + '<label style="margin-bottom:8px;display:block;font-weight:600;">Default Page Size</label>'
      + '<select id="pref-pagesize" style="width:100%;">'
      + [25, 50, 100, 500, 1000].map(s =>
          '<option value="' + s + '"' + (prefs.defaultPageSize === s ? ' selected' : '') + '>' + s + '</option>'
        ).join('')
      + '</select></div>'

      // Auto-refresh interval
      + '<div style="margin-bottom:20px;">'
      + '<label style="margin-bottom:8px;display:block;font-weight:600;">Auto-Refresh Interval</label>'
      + '<select id="pref-refresh" style="width:100%;">'
      + '<option value="15"' + (prefs.autoRefreshInterval === 15 ? ' selected' : '') + '>15 seconds</option>'
      + '<option value="30"' + (prefs.autoRefreshInterval === 30 ? ' selected' : '') + '>30 seconds</option>'
      + '<option value="60"' + (prefs.autoRefreshInterval === 60 ? ' selected' : '') + '>60 seconds</option>'
      + '<option value="0"' + (prefs.autoRefreshInterval === 0 ? ' selected' : '') + '>Off</option>'
      + '</select></div>'

      // Save button
      + '<button id="prefs-save-btn" class="btn primary" style="width:100%;">Save</button>';

    document.body.appendChild(overlay);
    document.body.appendChild(panel);

    // Wire close
    document.getElementById('prefs-close-btn').addEventListener('click', () => this.close());

    // Wire save
    document.getElementById('prefs-save-btn').addEventListener('click', () => {
      const theme = panel.querySelector('input[name="pref-theme"]:checked').value;
      const defaultPageSize = parseInt(document.getElementById('pref-pagesize').value, 10);
      const autoRefreshInterval = parseInt(document.getElementById('pref-refresh').value, 10);

      const newPrefs = { theme, defaultPageSize, autoRefreshInterval };
      this.save(newPrefs);

      // Apply theme immediately
      document.documentElement.setAttribute('data-theme', theme);
      localStorage.setItem('mnm-theme', theme);
      // Sync theme switcher buttons if they exist
      document.querySelectorAll('.theme-switcher button').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-theme') === theme);
      });

      this.close();
    });
  },

  /** Close the preferences panel. */
  close() {
    const panel = document.getElementById(this._panelId);
    const overlay = document.getElementById(this._overlayId);
    if (panel) panel.remove();
    if (overlay) overlay.remove();
  },

  /** Helper to build a radio button label. */
  _radioBtn(name, value, label, checked) {
    return '<label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:0.85rem;color:var(--text);">'
      + '<input type="radio" name="' + name + '" value="' + value + '"' + (checked ? ' checked' : '') + '>'
      + label + '</label>';
  },
};
