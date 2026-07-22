/* ================================================================
   SCRIBE — APP.JS
   Navigation, animated counters, Chart.js, search/filter,
   bilan annuel AJAX, upload drag-drop, scroll-to-top
   ================================================================ */

// Service Worker (PWA)
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/static/sw.js').catch(() => {});
}

document.addEventListener('DOMContentLoaded', () => {
  initSidebar();
  initCounters();
  initCharts();
  initUploadZones();
  initSearch();
  initScrollTop();
  initFlashAutoDismiss();
  initThemePicker();
  initNotifications();
  initOnboarding();
  initTrendsChart();
  initPdfExport();
  initOnboardingTutorial();

  // Auto-load bilan if years exist
  const bilanSelect = document.getElementById('bilanYear');
  if (bilanSelect && bilanSelect.value) {
    loadBilan(bilanSelect.value);
  }
});

/* ================================================================
   SIDEBAR NAVIGATION
   ================================================================ */
function initSidebar() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebarOverlay');
  const hamburger = document.getElementById('hamburgerBtn');
  const closeBtn = document.getElementById('sidebarClose');

  if (!sidebar) return;

  // Hamburger toggle
  if (hamburger) {
    hamburger.addEventListener('click', () => {
      sidebar.classList.add('open');
      if (overlay) overlay.classList.add('open');
    });
  }

  // Close sidebar
  function closeSidebar() {
    sidebar.classList.remove('open');
    if (overlay) overlay.classList.remove('open');
  }
  if (closeBtn) closeBtn.addEventListener('click', closeSidebar);
  if (overlay) overlay.addEventListener('click', closeSidebar);

  // Nav items — section switching
  const navItems = document.querySelectorAll('.nav-item[data-section]');
  const sections = document.querySelectorAll('.content-section');

  navItems.forEach(item => {
    item.addEventListener('click', (e) => {
      e.preventDefault();
      const targetId = item.getAttribute('data-section');

      // Update active nav
      navItems.forEach(n => n.classList.remove('active'));
      item.classList.add('active');

      // Show target section
      sections.forEach(s => {
        s.classList.remove('active');
        if (s.id === targetId) {
          s.classList.add('active');
        }
      });

      // Scroll to top of main
      const main = document.getElementById('mainContent');
      if (main) main.scrollTo({ top: 0, behavior: 'smooth' });

      // Close mobile sidebar
      closeSidebar();
    });
  });
}

/* ================================================================
   ANIMATED COUNTERS
   ================================================================ */
function initCounters() {
  const counters = document.querySelectorAll('[data-counter]');
  counters.forEach(el => {
    const target = parseFloat(el.getAttribute('data-counter')) || 0;
    animateCounter(el, target);
  });
}

function animateCounter(el, target, duration = 1200) {
  const start = 0;
  const startTime = performance.now();
  const isInteger = Number.isInteger(target) && Math.abs(target) < 10000;

  function step(now) {
    const elapsed = now - startTime;
    const progress = Math.min(elapsed / duration, 1);
    // Ease out cubic
    const eased = 1 - Math.pow(1 - progress, 3);
    const current = start + (target - start) * eased;

    if (isInteger) {
      el.textContent = Math.round(current).toLocaleString('fr-FR');
    } else {
      el.textContent = current.toLocaleString('fr-FR', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    }

    if (progress < 1) {
      requestAnimationFrame(step);
    }
  }
  requestAnimationFrame(step);
}

/* ================================================================
   CHARTS (Chart.js)
   ================================================================ */
function initCharts() {
  const data = window.SCRIBE_DATA;
  if (!data) return;

  const isDark = data.darkMode;
  const gridColor = isDark ? 'rgba(255,255,255,.08)' : 'rgba(0,0,0,.06)';
  const textColor = isDark ? '#a0a3b5' : '#5a5d70';

  // Chart.js global defaults
  Chart.defaults.color = textColor;
  Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
  Chart.defaults.font.size = 12;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.padding = 16;

  // Dashboard donut (small)
  buildDonut('dashboardDonut', data.catTotals, { cutout: '72%' });

  // Category donut (full)
  buildDonut('categoryDonut', data.catTotals, { cutout: '65%' });

  // Monthly trends chart
  buildMonthlyTrends(data, gridColor, textColor);
}

function buildDonut(canvasId, catTotals, opts) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !catTotals || Object.keys(catTotals).length === 0) return;

  const labels = Object.keys(catTotals);
  const values = labels.map(l => catTotals[l].amount);
  const colors = labels.map(l => catTotals[l].color);

  new Chart(canvas, {
    type: 'doughnut',
    data: {
      labels: labels,
      datasets: [{
        data: values,
        backgroundColor: colors,
        borderWidth: 2,
        borderColor: getComputedStyle(document.body).getPropertyValue('--bg-card').trim() || '#1c1e2e',
        hoverOffset: 6,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      cutout: opts.cutout || '65%',
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(0,0,0,.85)',
          titleColor: '#fff',
          bodyColor: '#ddd',
          padding: 10,
          cornerRadius: 8,
          displayColors: true,
          callbacks: {
            label: (ctx) => {
              const val = ctx.parsed;
              const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
              const pct = total > 0 ? ((val / total) * 100).toFixed(1) : 0;
              return ` ${ctx.label}: ${val.toLocaleString('fr-FR')} EUR (${pct}%)`;
            },
          },
        },
      },
    },
  });
}

function buildMonthlyTrends(data, gridColor, textColor) {
  const canvas = document.getElementById('monthlyTrendsChart');
  if (!canvas || !data.monthlyTrends || data.monthlyTrends.length === 0) return;

  // Build datasets per category, grouped by month
  const months = [...new Set(data.monthlyTrends.map(t => t.mois))].sort();
  const cats = [...new Set(data.monthlyTrends.map(t => t.auto_category))];

  const datasets = cats.map(cat => {
    const catInfo = data.categories[cat] || {};
    const monthMap = {};
    data.monthlyTrends
      .filter(t => t.auto_category === cat)
      .forEach(t => { monthMap[t.mois] = t.amount; });

    return {
      label: cat,
      data: months.map(m => monthMap[m] || 0),
      backgroundColor: catInfo.color || '#6b7280',
      borderRadius: 4,
      barPercentage: 0.85,
      categoryPercentage: 0.8,
    };
  });

  new Chart(canvas, {
    type: 'bar',
    data: { labels: months, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          stacked: true,
          grid: { display: false },
          ticks: { color: textColor, maxRotation: 45 },
        },
        y: {
          stacked: true,
          grid: { color: gridColor },
          ticks: {
            color: textColor,
            callback: v => v.toLocaleString('fr-FR') + ' €',
          },
        },
      },
      plugins: {
        legend: { position: 'bottom' },
        tooltip: {
          backgroundColor: 'rgba(0,0,0,.85)',
          cornerRadius: 8,
          callbacks: {
            label: ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y.toLocaleString('fr-FR')} EUR`,
          },
        },
      },
    },
  });
}

/* ================================================================
   UPLOAD DROP ZONES
   ================================================================ */
function initUploadZones() {
  setupDropZone('invoiceDropZone', 'invoiceFile', 'invoiceFileName', 'invoiceSubmitBtn');
  setupDropZone('payslipDropZone', 'payslipFile', 'payslipFileName', 'payslipSubmitBtn');
  setupDropZone('csvDropZone', 'csvFile', 'csvFileName', 'csvSubmitBtn');
}

function setupDropZone(zoneId, inputId, nameId, btnId) {
  const zone = document.getElementById(zoneId);
  const input = document.getElementById(inputId);
  const nameEl = document.getElementById(nameId);
  const btn = document.getElementById(btnId);

  if (!zone || !input) return;

  // Click to open file dialog
  zone.addEventListener('click', (e) => {
    if (e.target.tagName !== 'LABEL' && e.target.tagName !== 'INPUT') {
      input.click();
    }
  });

  // File selected
  input.addEventListener('change', () => {
    if (input.files.length > 0) {
      if (nameEl) nameEl.textContent = input.files[0].name;
      if (btn) btn.disabled = false;
    }
  });

  // Drag & drop
  zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => { zone.classList.remove('drag-over'); });
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    if (e.dataTransfer.files.length > 0) {
      input.files = e.dataTransfer.files;
      if (nameEl) nameEl.textContent = e.dataTransfer.files[0].name;
      if (btn) btn.disabled = false;
    }
  });
}

/* ================================================================
   SEARCH & FILTER TRANSACTIONS
   ================================================================ */
function initSearch() {
  const searchInput = document.getElementById('txSearch');
  const categoryFilter = document.getElementById('txCategoryFilter');
  if (!searchInput && !categoryFilter) return;

  function filterTable() {
    const query = searchInput ? searchInput.value.toLowerCase().trim() : '';
    const cat = categoryFilter ? categoryFilter.value : '';
    const rows = document.querySelectorAll('#txTableBody tr');
    let visible = 0;

    rows.forEach(row => {
      const label = row.getAttribute('data-label') || '';
      const rowCat = row.getAttribute('data-category') || '';
      const matchSearch = !query || label.includes(query);
      const matchCat = !cat || rowCat === cat;

      if (matchSearch && matchCat) {
        row.style.display = '';
        visible++;
      } else {
        row.style.display = 'none';
      }
    });

    const countEl = document.getElementById('txCount');
    if (countEl) countEl.textContent = `${visible} transaction(s)`;
  }

  if (searchInput) searchInput.addEventListener('input', filterTable);
  if (categoryFilter) {
    categoryFilter.addEventListener('change', filterTable);
    categoryFilter.addEventListener('input', filterTable);
  }

  // Run filter once on load (in case browser pre-filled the dropdown)
  filterTable();

  // Expose filter function globally for badge clicks
  window._txFilterTable = filterTable;
}

/* Filter by clicking a category badge */
function filterByCategory(catName) {
  const categoryFilter = document.getElementById('txCategoryFilter');
  if (!categoryFilter) return;

  // If already filtering this category, clear the filter
  if (categoryFilter.value === catName) {
    categoryFilter.value = '';
  } else {
    categoryFilter.value = catName;
  }

  // Trigger the filter
  if (window._txFilterTable) window._txFilterTable();
}

/* ================================================================
   SCROLL TO TOP
   ================================================================ */
function initScrollTop() {
  const btn = document.getElementById('scrollTopBtn');
  if (!btn) return;

  const main = document.getElementById('mainContent');
  const scrollTarget = main || window;

  function checkScroll() {
    const scrollY = main ? main.scrollTop : window.scrollY;
    btn.classList.toggle('visible', scrollY > 300);
  }

  (main || window).addEventListener('scroll', checkScroll, { passive: true });
  btn.addEventListener('click', () => {
    if (main) {
      main.scrollTo({ top: 0, behavior: 'smooth' });
    } else {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  });
}

/* ================================================================
   FLASH AUTO-DISMISS
   ================================================================ */
function initFlashAutoDismiss() {
  const flashes = document.querySelectorAll('.flash');
  flashes.forEach(flash => {
    setTimeout(() => {
      flash.style.transition = 'opacity .4s, transform .4s';
      flash.style.opacity = '0';
      flash.style.transform = 'translateY(-8px)';
      setTimeout(() => flash.remove(), 400);
    }, 6000);
  });
}

/* ================================================================
   THEME PICKER (live swatch click + preview)
   ================================================================ */
function initThemePicker() {
  const swatches = document.querySelectorAll('.theme-swatch');
  const modal = document.getElementById('settingsModal');

  // Set --swatch-color on each swatch from its actual background
  function applySwatchColor(swatch) {
    const colorEl = swatch.querySelector('.swatch-color');
    if (colorEl) {
      // Read from inline style attribute directly, or fall back to computed
      const raw = colorEl.getAttribute('style') || '';
      const match = raw.match(/background:\s*([^;]+)/i);
      const bg = match ? match[1].trim() : window.getComputedStyle(colorEl).backgroundColor;
      if (bg) swatch.style.setProperty('--swatch-color', bg);
    }
  }

  // Apply to all swatches on init (for the initially active one)
  swatches.forEach(applySwatchColor);

  swatches.forEach(s => {
    s.addEventListener('click', () => {
      swatches.forEach(sw => sw.classList.remove('active'));
      s.classList.add('active');

      // Live preview — apply accent colors immediately
      const root = document.documentElement;
      root.style.setProperty('--accent-mid', s.dataset.mid);
      root.style.setProperty('--accent-dark', s.dataset.dark);
      root.style.setProperty('--accent-light', s.dataset.light);
      root.style.setProperty('--accent-text', s.dataset.text);
      root.style.setProperty('--accent-pale', s.dataset.pale);
      root.style.setProperty('--accent-bg', s.dataset.bg);

      // Update gradients based on current mode
      const isDark = document.body.classList.contains('dark');
      if (isDark) {
        root.style.setProperty('--gradient1', s.dataset.g1);
        root.style.setProperty('--gradient2', s.dataset.g2);
        root.style.setProperty('--gradient3', s.dataset.g3);
      } else {
        const themeName = s.querySelector('input[name="accent_theme"]').value;
        const lightGrads = window.SCRIBE_DATA && window.SCRIBE_DATA.lightGrads;
        if (lightGrads && lightGrads[themeName]) {
          const lg = lightGrads[themeName];
          root.style.setProperty('--gradient1', lg[0]);
          root.style.setProperty('--gradient2', lg[1]);
          root.style.setProperty('--gradient3', lg[2]);
        }
      }

      // Make modal translucent so user sees the preview behind
      if (modal) {
        modal.classList.add('theme-preview');
        // Back to opaque after 4s
        clearTimeout(window._themePreviewTimer);
        window._themePreviewTimer = setTimeout(() => {
          modal.classList.remove('theme-preview');
        }, 4000);
      }
    });
  });
}

/* ================================================================
   DARK MODE TOGGLE (in settings modal)
   ================================================================ */
function toggleDarkPreview(checkbox) {
  const isDark = checkbox.checked;
  const hiddenInput = document.getElementById('darkModeInput');
  const label = document.getElementById('darkModeLabel');

  if (hiddenInput) hiddenInput.value = isDark ? 'true' : 'false';
  if (label) label.textContent = isDark ? 'Sombre' : 'Clair';

  // Live preview — switch body class
  document.body.classList.toggle('dark', isDark);
  document.body.classList.toggle('light', !isDark);
  document.documentElement.setAttribute('data-dark', isDark ? 'true' : 'false');

  // Update gradients for the current theme
  const activeSwatch = document.querySelector('.theme-swatch.active');
  if (activeSwatch) {
    const root = document.documentElement;
    if (isDark) {
      root.style.setProperty('--gradient1', activeSwatch.dataset.g1);
      root.style.setProperty('--gradient2', activeSwatch.dataset.g2);
      root.style.setProperty('--gradient3', activeSwatch.dataset.g3);
    } else {
      const themeName = activeSwatch.querySelector('input[name="accent_theme"]').value;
      const lightGrads = window.SCRIBE_DATA && window.SCRIBE_DATA.lightGrads;
      if (lightGrads && lightGrads[themeName]) {
        const lg = lightGrads[themeName];
        root.style.setProperty('--gradient1', lg[0]);
        root.style.setProperty('--gradient2', lg[1]);
        root.style.setProperty('--gradient3', lg[2]);
      }
    }
  }
}

/* ================================================================
   SETTINGS MODAL
   ================================================================ */
function openSettings() {
  const modal = document.getElementById('settingsModal');
  if (modal) modal.classList.add('open');
}

function closeSettings() {
  const modal = document.getElementById('settingsModal');
  if (modal) modal.classList.remove('open');
}

// Close modal on escape key
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeSettings();
});

// Close modal on backdrop click
const modalOverlay = document.getElementById('settingsModal');
if (modalOverlay) {
  modalOverlay.addEventListener('click', (e) => {
    if (e.target === modalOverlay) closeSettings();
  });
}

/* ================================================================
   BILAN ANNUEL (AJAX)
   ================================================================ */
function loadBilan(year) {
  const container = document.getElementById('bilanContent');
  if (!container) return;

  container.innerHTML = '<div class="bilan-loading" style="animation: pulse 1.5s infinite">Chargement du bilan ' + year + '...</div>';

  fetch('/api/bilan/' + year)
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        container.innerHTML = '<div class="bilan-loading">' + data.error + '</div>';
        return;
      }
      renderBilan(container, data);
    })
    .catch(err => {
      container.innerHTML = '<div class="bilan-loading">Erreur de chargement</div>';
      console.error(err);
    });
}

function renderBilan(container, data) {
  const isDark = window.SCRIBE_DATA ? window.SCRIBE_DATA.darkMode : true;

  let html = '<div class="bilan-stats">';
  html += bilanStat(data.total.toLocaleString('fr-FR') + ' EUR', 'Total depenses');
  html += bilanStat(data.count.toLocaleString('fr-FR'), 'Transactions');
  html += bilanStat(data.avg_month.toLocaleString('fr-FR') + ' EUR', 'Moyenne/mois');
  if (data.revenue > 0) {
    html += bilanStat(data.revenue.toLocaleString('fr-FR') + ' EUR', 'Revenus estimes');
    const epargnColor = data.epargne >= 0 ? 'var(--color-success)' : 'var(--color-danger)';
    html += bilanStat(
      '<span style="color:' + epargnColor + '">' + data.epargne.toLocaleString('fr-FR') + ' EUR</span>',
      'Epargne estimee'
    );
  }
  html += bilanStat(data.most_expensive_month, 'Mois le + cher');
  html += bilanStat(
    data.top_category_emoji + ' ' + data.top_category,
    'Categorie #1'
  );
  html += '</div>';

  // Monthly chart
  html += '<div class="bilan-chart-row">';
  html += '<div class="bilan-chart-wrapper"><canvas id="bilanMonthlyChart" height="250"></canvas></div>';
  if (data.top_categories && data.top_categories.length > 0) {
    html += '<div class="bilan-chart-wrapper"><canvas id="bilanCatChart" height="250"></canvas></div>';
  }
  html += '</div>';

  container.innerHTML = html;

  // Render monthly bar chart
  const monthlyCanvas = document.getElementById('bilanMonthlyChart');
  if (monthlyCanvas && data.monthly) {
    const textColor = isDark ? '#a0a3b5' : '#5a5d70';
    const gridColor = isDark ? 'rgba(255,255,255,.08)' : 'rgba(0,0,0,.06)';
    const accentMid = getComputedStyle(document.documentElement).getPropertyValue('--accent-mid').trim();

    new Chart(monthlyCanvas, {
      type: 'bar',
      data: {
        labels: data.monthly.map(m => m.month),
        datasets: [{
          label: 'Depenses',
          data: data.monthly.map(m => m.amount),
          backgroundColor: accentMid + 'cc',
          borderRadius: 6,
          barPercentage: 0.7,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: 'rgba(0,0,0,.85)',
            cornerRadius: 8,
            callbacks: {
              label: ctx => ctx.parsed.y.toLocaleString('fr-FR') + ' EUR',
            },
          },
        },
        scales: {
          x: { grid: { display: false }, ticks: { color: textColor, maxRotation: 45 } },
          y: {
            grid: { color: gridColor },
            ticks: { color: textColor, callback: v => v.toLocaleString('fr-FR') + ' €' },
          },
        },
      },
    });
  }

  // Render top categories donut
  const catCanvas = document.getElementById('bilanCatChart');
  if (catCanvas && data.top_categories && data.top_categories.length > 0) {
    const bgCard = getComputedStyle(document.body).getPropertyValue('--bg-card').trim();

    new Chart(catCanvas, {
      type: 'doughnut',
      data: {
        labels: data.top_categories.map(c => c.emoji + ' ' + c.name),
        datasets: [{
          data: data.top_categories.map(c => c.amount),
          backgroundColor: data.top_categories.map(c => c.color),
          borderWidth: 2,
          borderColor: bgCard || '#1c1e2e',
          hoverOffset: 6,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        cutout: '60%',
        plugins: {
          legend: { position: 'bottom' },
          tooltip: {
            backgroundColor: 'rgba(0,0,0,.85)',
            cornerRadius: 8,
            callbacks: {
              label: ctx => ` ${ctx.label}: ${ctx.parsed.toLocaleString('fr-FR')} EUR`,
            },
          },
        },
      },
    });
  }
}

function bilanStat(value, label) {
  return '<div class="bilan-stat"><div class="bilan-stat-value">' + value +
         '</div><div class="bilan-stat-label">' + label + '</div></div>';
}

/* ================================================================
   NOTIFICATION PANEL
   ================================================================ */
function initNotifications() {
  const bell = document.getElementById('notifBell');
  const panel = document.getElementById('notifPanel');
  const overlay = document.getElementById('notifOverlay');
  const closeBtn = document.getElementById('notifClose');

  if (!bell || !panel) return;

  function openNotifs() {
    panel.classList.add('open');
    if (overlay) overlay.classList.add('open');
    // Mark dot as read
    const dot = bell.querySelector('.notif-dot');
    if (dot) dot.remove();
    buildNotifList();
  }

  function closeNotifs() {
    panel.classList.remove('open');
    if (overlay) overlay.classList.remove('open');
  }

  bell.addEventListener('click', openNotifs);
  if (closeBtn) closeBtn.addEventListener('click', closeNotifs);
  if (overlay) overlay.addEventListener('click', closeNotifs);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && panel.classList.contains('open')) closeNotifs();
  });
}

function buildNotifList() {
  const list = document.getElementById('notifList');
  if (!list) return;

  // Build notifications from alerts data if available
  const alerts = document.querySelectorAll('.alert-card');
  const items = [];

  alerts.forEach(alert => {
    const title = alert.querySelector('.alert-title');
    const msg = alert.querySelector('.alert-message');
    const badge = alert.querySelector('.alert-severity-badge');
    let type = 'info';
    if (alert.classList.contains('alert-serious')) type = 'danger';
    else if (alert.classList.contains('alert-warning')) type = 'warning';

    items.push({
      title: title ? title.textContent : 'Alerte',
      desc: msg ? msg.textContent : '',
      type: type,
      time: "Aujourd'hui",
    });
  });

  // Add system notifications
  const statCards = document.querySelectorAll('.stat-card');
  statCards.forEach(card => {
    const val = card.querySelector('.stat-value');
    const label = card.querySelector('.stat-label');
    if (val && label && label.textContent.includes('Budget')) {
      const pct = parseFloat(val.textContent);
      if (pct > 80) {
        items.push({
          title: 'Budget presque atteint',
          desc: 'Tu as utilise ' + val.textContent + ' de ton budget mensuel.',
          type: pct > 100 ? 'danger' : 'warning',
          time: "Aujourd'hui",
        });
      }
    }
  });

  if (items.length === 0) {
    list.innerHTML = '<div class="notif-empty">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>' +
      '<div>Aucune notification</div>' +
      '<div style="font-size:.78rem;margin-top:.3rem">Tout va bien !</div></div>';
    return;
  }

  const iconSvgs = {
    warning: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    danger: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
  };

  list.innerHTML = items.map(n =>
    '<div class="notif-item unread">' +
      '<div class="notif-icon notif-' + n.type + '">' + (iconSvgs[n.type] || iconSvgs.info) + '</div>' +
      '<div class="notif-body">' +
        '<div class="notif-title">' + n.title + '</div>' +
        '<div class="notif-desc">' + n.desc + '</div>' +
        '<div class="notif-time">' + n.time + '</div>' +
      '</div>' +
    '</div>'
  ).join('');
}

/* ================================================================
   ONBOARDING GUIDED TOUR
   ================================================================ */
function initOnboarding() {
  // Only run for first-time users (check flag)
  if (!document.getElementById('sidebar')) return;
  try {
    if (window._scribeOnboardDone) return;
  } catch(e) {}

  // Check if onboarding flag is set in DOM
  const onboardFlag = document.getElementById('onboardFlag');
  if (!onboardFlag) return;

  window._scribeOnboardDone = true;

  const steps = [
    {
      target: '[data-section="section-dashboard"]',
      title: 'Tableau de bord',
      text: 'Vue d\'ensemble de tes finances : depenses du mois, graphiques et repartition par categorie.',
    },
    {
      target: '[data-section="section-factures"]',
      title: 'Analyse de factures',
      text: 'Importe tes factures (photo ou PDF) et l\'IA les analyse automatiquement pour detecter les hausses.',
    },
    {
      target: '[data-section="section-banque"]',
      title: 'Suivi bancaire',
      text: 'Importe ton releve CSV ou connecte ta banque pour categoriser automatiquement tes depenses.',
    },
    {
      target: '[data-section="section-budgets"]',
      title: 'Budgets par categorie',
      text: 'Definis un budget pour chaque categorie et suis ta progression en temps reel.',
    },
    {
      target: '[data-section="section-score"]',
      title: 'Score financier',
      text: 'Un score sur 100 qui evalue ta sante financiere avec des conseils personnalises.',
    },
    {
      target: '.sidebar-footer .nav-item:first-child',
      title: 'Parametres',
      text: 'Change ton theme, configure ta cle API Gemini et ajuste ton budget mensuel.',
    },
  ];

  let current = 0;

  function createOverlay() {
    const ov = document.createElement('div');
    ov.className = 'onboard-overlay';
    ov.id = 'onboardOverlay';
    document.body.appendChild(ov);
    return ov;
  }

  function showStep(idx) {
    // Remove previous
    const old = document.getElementById('onboardTooltip');
    if (old) old.remove();
    const oldHighlight = document.querySelector('.onboard-highlight');
    if (oldHighlight) oldHighlight.classList.remove('onboard-highlight');

    if (idx >= steps.length) {
      // Done
      const ov = document.getElementById('onboardOverlay');
      if (ov) { ov.style.opacity = '0'; setTimeout(() => ov.remove(), 300); }
      return;
    }

    const step = steps[idx];
    const target = document.querySelector(step.target);
    if (target) target.classList.add('onboard-highlight');

    const tooltip = document.createElement('div');
    tooltip.className = 'onboard-tooltip';
    tooltip.id = 'onboardTooltip';

    // Dots
    let dotsHtml = '<div class="onboard-dots">';
    for (let i = 0; i < steps.length; i++) {
      dotsHtml += '<div class="onboard-dot' + (i === idx ? ' active' : '') + '"></div>';
    }
    dotsHtml += '</div>';

    tooltip.innerHTML =
      '<h4>' + step.title + '</h4>' +
      '<p>' + step.text + '</p>' +
      '<div class="onboard-actions">' + dotsHtml +
        '<div class="onboard-btns">' +
          '<button class="onboard-skip" id="onboardSkip">Passer</button>' +
          '<button class="onboard-next" id="onboardNext">' +
            (idx === steps.length - 1 ? 'Terminer' : 'Suivant') +
          '</button>' +
        '</div>' +
      '</div>';

    document.body.appendChild(tooltip);

    // Position tooltip near target
    if (target) {
      const rect = target.getBoundingClientRect();
      const tooltipW = 320;
      let left = rect.right + 16;
      let top = rect.top;

      // If overflowing right, place below
      if (left + tooltipW > window.innerWidth) {
        left = Math.max(16, rect.left);
        top = rect.bottom + 12;
      }
      // If overflowing bottom
      if (top + 200 > window.innerHeight) {
        top = Math.max(16, rect.top - 200);
      }

      tooltip.style.left = left + 'px';
      tooltip.style.top = top + 'px';
    } else {
      tooltip.style.left = '50%';
      tooltip.style.top = '50%';
      tooltip.style.transform = 'translate(-50%,-50%)';
    }

    // Buttons
    document.getElementById('onboardSkip').addEventListener('click', () => {
      showStep(steps.length); // skip all
    });
    document.getElementById('onboardNext').addEventListener('click', () => {
      current++;
      showStep(current);
    });
  }

  // Start after a short delay
  setTimeout(() => {
    createOverlay();
    showStep(0);
  }, 800);
}

/* ================================================================
   SIMULATEUR D'IMPOT (AJAX)
   ================================================================ */
function simulerImpot() {
  const revenu = document.getElementById('taxRevenu');
  const parts = document.getElementById('taxParts');
  const container = document.getElementById('taxResult');
  if (!revenu || !parts || !container) return;

  const rev = parseFloat(revenu.value) || 0;
  const nbp = parseFloat(parts.value) || 1;

  if (rev <= 0) {
    container.style.display = 'block';
    container.innerHTML = '<div class="tax-result-main" style="background:var(--bg-input);border-color:var(--border-color)"><div class="tax-result-amount" style="color:var(--text-secondary)">0 EUR</div><div class="tax-result-sublabel">Pas d\'impot a payer</div></div>';
    return;
  }

  const formData = new FormData();
  formData.append('revenu', rev);
  formData.append('parts', nbp);

  fetch('/api/simuler-impot', { method: 'POST', body: formData })
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        container.innerHTML = '<div class="bilan-loading">' + data.error + '</div>';
        container.style.display = 'block';
        return;
      }
      renderTaxResult(container, data);
    })
    .catch(err => {
      container.innerHTML = '<div class="bilan-loading">Erreur de calcul</div>';
      container.style.display = 'block';
      console.error(err);
    });
}

function renderTaxResult(container, data) {
  const fmt = n => n.toLocaleString('fr-FR', { maximumFractionDigits: 0 });
  const monthly = Math.round(data.ir / 12);

  let html = '<div class="tax-result-header">';

  // Montant principal
  html += '<div class="tax-result-main">';
  html += '<div class="tax-result-amount">' + fmt(data.ir) + ' EUR</div>';
  html += '<div class="tax-result-sublabel">Impot sur le revenu estime</div>';
  html += '</div>';

  // Taux
  html += '<div class="tax-result-rates">';
  html += '<div class="tax-rate-box"><div class="tax-rate-value">' + data.taux_moyen + ' %</div><div class="tax-rate-label">Taux moyen</div></div>';
  html += '<div class="tax-rate-box"><div class="tax-rate-value">' + data.taux_marginal + ' %</div><div class="tax-rate-label">Taux marginal</div></div>';
  html += '</div>';
  html += '</div>';

  // Mensuel
  html += '<div class="tax-result-monthly">Soit environ <strong>' + fmt(monthly) + ' EUR/mois</strong> de prelevement a la source';
  if (data.nb_parts > 1) {
    html += ' (avec ' + data.nb_parts + ' parts)';
  }
  html += '</div>';

  // Detail par tranche
  if (data.tranches && data.tranches.length > 0) {
    html += '<details class="tax-tranches-detail"><summary style="cursor:pointer;color:var(--accent-mid);font-size:.88rem;margin-bottom:.5rem">Voir le detail par tranche</summary>';
    html += '<table class="data-table tax-tranches-table"><thead><tr><th>Tranche</th><th>Taux</th><th>Base imposable</th><th>Impot</th></tr></thead><tbody>';
    data.tranches.forEach(t => {
      const aStr = t.a === '∞' ? '∞' : fmt(t.a);
      html += '<tr>';
      html += '<td>' + fmt(t.de) + ' → ' + aStr + ' EUR</td>';
      html += '<td class="tax-rate">' + t.taux + ' %</td>';
      html += '<td class="amount">' + fmt(t.base) + ' EUR</td>';
      html += '<td class="amount">' + fmt(t.impot) + ' EUR</td>';
      html += '</tr>';
    });
    html += '</tbody></table>';
    if (data.nb_parts > 1) {
      html += '<div style="font-size:.82rem;color:var(--text-muted);margin-top:.4rem">Impot par part : ' + fmt(Math.round(data.ir / data.nb_parts)) + ' EUR × ' + data.nb_parts + ' parts = ' + fmt(data.ir) + ' EUR</div>';
    }
    html += '</details>';
  }

  // Abattement info
  html += '<div style="font-size:.82rem;color:var(--text-muted);margin-top:.6rem;padding-top:.6rem;border-top:1px solid var(--border-light)">';
  html += 'Revenu declare : ' + fmt(data.revenu_declare) + ' EUR';
  html += ' → Abattement 10 % : -' + fmt(data.abattement) + ' EUR';
  html += ' → Base de calcul : ' + fmt(data.revenu_net_imposable) + ' EUR';
  if (data.nb_parts > 1) {
    html += ' → Par part : ' + fmt(data.revenu_par_part) + ' EUR';
  }
  html += '</div>';

  container.innerHTML = html;
  container.style.display = 'block';
}

/* ================================================================
   TRENDS LINE CHART
   ================================================================ */
function initTrendsChart() {
  const canvas = document.getElementById('trendsLineChart');
  if (!canvas) return;

  // Get data from a hidden script tag
  const dataEl = document.getElementById('trendsChartData');
  if (!dataEl) return;

  try {
    const data = JSON.parse(dataEl.textContent);
    if (!data.labels || !data.values || data.labels.length < 2) return;

    const ctx = canvas.getContext('2d');
    const accentMid = getComputedStyle(document.documentElement).getPropertyValue('--accent-mid').trim() || '#10b981';

    new Chart(ctx, {
      type: 'line',
      data: {
        labels: data.labels,
        datasets: [{
          label: 'Depenses',
          data: data.values,
          borderColor: accentMid,
          backgroundColor: accentMid + '20',
          fill: true,
          tension: 0.4,
          pointRadius: 5,
          pointHoverRadius: 7,
          pointBackgroundColor: accentMid,
          pointBorderColor: '#fff',
          pointBorderWidth: 2,
          borderWidth: 2.5,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function(ctx) {
                return ctx.parsed.y.toLocaleString('fr-FR') + ' EUR';
              }
            }
          }
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: 'rgba(255,255,255,.5)', font: { size: 11 } }
          },
          y: {
            beginAtZero: true,
            grid: { color: 'rgba(255,255,255,.06)' },
            ticks: {
              color: 'rgba(255,255,255,.5)',
              font: { size: 11 },
              callback: function(v) { return v.toLocaleString('fr-FR') + ' EUR'; }
            }
          }
        }
      }
    });
  } catch (e) {
    console.warn('Trends chart error:', e);
  }
}

/* ================================================================
   PDF EXPORT — month selector
   ================================================================ */
function initPdfExport() {
  const select = document.getElementById('pdfMonthSelect');
  const btn = document.getElementById('pdfDownloadBtn');
  if (!select || !btn) return;

  select.addEventListener('change', () => {
    btn.href = '/api/export-pdf/' + select.value;
  });
}

/* ================================================================
   ONBOARDING TUTORIAL
   ================================================================ */
let onboardStep = 1;
function initOnboardingTutorial() {
  const tut = document.getElementById('onboardTutorial');
  if (!tut) return;
  // Show after a short delay
  setTimeout(() => tut.classList.add('visible'), 600);
}
function onboardNext() {
  if (onboardStep < 3) {
    onboardStep++;
    updateOnboardStep();
  } else {
    closeOnboarding();
  }
}
function onboardPrev() {
  if (onboardStep > 1) {
    onboardStep--;
    updateOnboardStep();
  }
}
function updateOnboardStep() {
  document.querySelectorAll('.onboard-step').forEach(el => {
    el.classList.toggle('active', parseInt(el.dataset.step) === onboardStep);
  });
  document.querySelectorAll('.onboard-dot').forEach(el => {
    el.classList.toggle('active', parseInt(el.dataset.dot) === onboardStep);
  });
  const prev = document.querySelector('.onboard-prev');
  const next = document.querySelector('.onboard-next');
  if (prev) prev.style.display = onboardStep > 1 ? '' : 'none';
  if (next) next.textContent = onboardStep === 3 ? 'C\'est parti !' : 'Suivant';
}
function closeOnboarding() {
  const tut = document.getElementById('onboardTutorial');
  if (tut) {
    tut.classList.remove('visible');
    setTimeout(() => tut.remove(), 400);
  }
}
