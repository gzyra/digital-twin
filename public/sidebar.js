// Digital-Twin custom sidebar + dashboard + KPI strip
// -------------------------------------------------------
// All fenced-block parsing uses data-dt-hidden to mark already-processed
// elements — this is the ONLY guard needed to break the
// "hide → MutationObserver → hide again" feedback loop.
// JS is single-threaded, so no extra re-entrance flags are required.

// ---------- Utility ----------

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
function truncate(s, max) {
  if (s.length <= max) return esc(s);
  return esc(s.slice(0, max)) + '&hellip;';
}
function stripMdLinks(s) {
  return s.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
}
function formatTimestamp(ts) {
  if (!ts) return '';
  try { return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
  catch (_) { return ''; }
}

// ---------- Core: find + consume fenced-block messages ----------
// Finds all unprocessed <code> elements whose parsed JSON matches testFn,
// marks them (and their message container) with data-dt-hidden so the
// MutationObserver does not re-process them, and returns parsed payloads.

function consumeCodeBlocks(testFn) {
  const results = [];
  document.querySelectorAll('code:not([data-dt-hidden])').forEach(el => {
    const raw = (el.textContent || '').trim();
    if (!raw.startsWith('{')) return;
    let parsed;
    try { parsed = JSON.parse(raw); } catch (_) { return; }
    if (!testFn(parsed)) return;

    // Mark code element BEFORE touching style (prevents re-entry via observer)
    el.setAttribute('data-dt-hidden', '1');

    // Hide the surrounding message container
    const containerSelectors = [
      '[data-testid="step"]', '.step', 'article',
      '[class*="step"]', '[class*="message-container"]', '[class*="chat-message"]',
    ];
    let hidden = false;
    for (const sel of containerSelectors) {
      const c = el.closest(sel);
      if (c && !c.hasAttribute('data-dt-hidden')) {
        c.setAttribute('data-dt-hidden', '1');
        c.style.setProperty('display', 'none', 'important');
        hidden = true; break;
      }
    }
    if (!hidden) {
      let node = el;
      for (let i = 0; i < 15; i++) {
        node = node.parentElement;
        if (!node || node === document.body) break;
        if (node.parentElement && node.parentElement.children.length > 1) {
          if (!node.hasAttribute('data-dt-hidden')) {
            node.setAttribute('data-dt-hidden', '1');
            node.style.setProperty('display', 'none', 'important');
          }
          break;
        }
      }
    }
    results.push(parsed);
  });
  return results;
}

// ---------- Skills sidebar ----------

window.__skillsData = window.__skillsData || [];

function parseSkillsMessages() {
  const blocks = consumeCodeBlocks(d => Array.isArray(d.skills));
  if (blocks.length > 0) {
    window.__skillsData = blocks[blocks.length - 1].skills || [];
    renderSkills();
  }
}

function renderSkills() {
  const list = document.querySelector('#skills-list');
  if (!list) return;
  const skills = window.__skillsData;
  if (skills.length === 0) {
    list.innerHTML = '<li class="sidebar-empty">No skills recorded yet</li>';
    return;
  }
  list.innerHTML = skills.map(s =>
    `<li title="Click to run ${esc(s)}">${esc(s)}</li>`
  ).join('');
}

function sendSkillCommand(skillName) {
  const input = document.querySelector('textarea, input[type="text"]');
  if (!input) return;
  const cmd = 'run ' + skillName;
  input.focus();
  const proto = input.tagName === 'TEXTAREA'
    ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
  if (descriptor && descriptor.set) { descriptor.set.call(input, cmd); } else { input.value = cmd; }
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  const btn = document.querySelector('button[type="submit"], button[aria-label*="Send" i]');
  if (btn && !btn.disabled) { btn.click(); return; }
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
  input.dispatchEvent(new KeyboardEvent('keyup',   { key: 'Enter', code: 'Enter', bubbles: true }));
}

function createSidebar() {
  if (document.querySelector('.custom-sidebar')) return;
  const sidebar = document.createElement('div');
  sidebar.className = 'custom-sidebar';
  sidebar.innerHTML = '<div class="sidebar-section"><h3>Skills</h3><ul id="skills-list"><li class="sidebar-empty">Loading\u2026</li></ul></div>';
  document.body.insertBefore(sidebar, document.body.firstChild);
  sidebar.addEventListener('click', e => {
    const li = e.target.closest('li');
    if (li && !li.classList.contains('sidebar-empty')) sendSkillCommand(li.textContent.trim());
  });
}

// ---------- Dashboard ----------

window.__dashboardData = window.__dashboardData || {};

function parseDashboardMessages() {
  const blocks = consumeCodeBlocks(d => d.skill !== undefined && d.outputs !== undefined);
  blocks.forEach(d => {
    window.__dashboardData[d.skill] = { outputs: d.outputs, ts: d.ts || '', status: d.status || '' };
  });
  if (blocks.length > 0) renderDashboard();
}

function isMarkdownTable(value) {
  if (typeof value !== 'string') return false;
  return value.split('\n').filter(l => l.trim().startsWith('|')).length >= 3;
}

function parseMarkdownTable(text) {
  const lines = text.trim().split('\n').map(l => l.trim()).filter(l => l.startsWith('|') && l.endsWith('|'));
  if (lines.length < 3) return null;
  const cells = l => l.slice(1, -1).split('|').map(c => c.trim());
  const isSep = l => /^\|[\s|:|-]+\|$/.test(l);
  let hi = -1;
  for (let i = 0; i < lines.length - 1; i++) { if (isSep(lines[i + 1])) { hi = i; break; } }
  if (hi < 0) return null;
  return { headers: cells(lines[hi]), rows: lines.slice(hi + 2).map(cells).filter(r => r.some(c => c)) };
}

function renderMarkdownTable(text) {
  const p = parseMarkdownTable(text);
  if (!p) return '<pre class="db-raw">' + esc(text.slice(0, 300)) + '</pre>';
  const ths = p.headers.map(h => '<th title="' + esc(h) + '">' + truncate(h, 14) + '</th>').join('');
  const trs = p.rows.map(row =>
    '<tr>' + row.map((c, i) => {
      if (i >= p.headers.length) return '';
      const clean = stripMdLinks(c);
      return '<td title="' + esc(clean) + '">' + truncate(clean, 16) + '</td>';
    }).join('') + '</tr>'
  ).join('');
  return '<div class="db-tbl-meta">' + p.rows.length + ' row' + (p.rows.length !== 1 ? 's' : '') + ' &bull; scroll &rarr;</div>' +
    '<div class="db-tbl-scroll"><table class="db-html-table"><thead><tr>' + ths + '</tr></thead><tbody>' + trs + '</tbody></table></div>';
}

function renderDashboard() {
  const panel = document.querySelector('#dashboard-panel');
  if (!panel) return;
  const entries = Object.entries(window.__dashboardData);
  if (entries.length === 0) {
    panel.innerHTML = '<p class="dashboard-empty">No data yet.<br>Add startup_skills to config.yaml or run a skill.</p>';
    return;
  }
  panel.innerHTML = entries.map(function(entry) {
    const skill = entry[0], d = entry[1];
    const time = d.ts ? '<span class="db-ts">' + formatTimestamp(d.ts) + '</span>' : '';
    const title = esc(skill.replace(/_/g, ' '));
    let body;
    if (d.status === 'loading') {
      body = '<div class="db-loading"><span class="db-spinner"></span> Loading\u2026</div>';
    } else if (d.status === 'no-output' || (!d.status && Object.keys(d.outputs).length === 0)) {
      body = '<div class="db-no-output">No output captured</div>';
    } else {
      body = Object.entries(d.outputs).map(function(kv) {
        const key = kv[0], val = kv[1];
        if (isMarkdownTable(val))
          return '<div class="db-output-label">' + esc(key.replace(/_/g, ' ')) + '</div>' + renderMarkdownTable(val);
        return '<div class="db-kv"><span class="db-key">' + esc(key.replace(/_/g, ' ')) + '</span><span class="db-val">' + esc(String(val)) + '</span></div>';
      }).join('');
    }
    const hasTable = Object.values(d.outputs).some(isMarkdownTable);
    return '<div class="db-card' + (hasTable ? ' db-card-table' : '') + (d.status === 'loading' ? ' db-card-loading' : '') + '">' +
      '<div class="db-card-header">' + title + time + '</div>' +
      '<div class="db-card-body">' + body + '</div></div>';
  }).join('');
}

function expandDashboard() {
  const dash = document.querySelector('.custom-dashboard');
  if (!dash) return;
  if (dash.classList.contains('db-expanded')) { collapseDashboard(); return; }
  const backdrop = document.createElement('div');
  backdrop.className = 'db-backdrop';
  backdrop.addEventListener('click', collapseDashboard);
  document.body.appendChild(backdrop);
  dash.classList.add('db-expanded');
  const btn = dash.querySelector('.db-expand');
  if (btn) { btn.textContent = '\u2715 Close'; btn.title = 'Collapse'; }
}

function collapseDashboard() {
  const dash = document.querySelector('.custom-dashboard');
  if (!dash) return;
  dash.classList.remove('db-expanded');
  const b = document.querySelector('.db-backdrop');
  if (b) b.remove();
  const btn = dash.querySelector('.db-expand');
  if (btn) { btn.textContent = 'Expand \u2197'; btn.title = 'Expand to center'; }
}

function createDashboard() {
  if (document.querySelector('.custom-dashboard')) return;
  const dash = document.createElement('div');
  dash.className = 'custom-dashboard';
  dash.innerHTML = '<div class="dashboard-header"><h3>Dashboard</h3><div class="db-header-buttons">' +
    '<button class="db-expand" title="Expand to center" onclick="expandDashboard()">Expand \u2197</button>' +
    '<button class="db-refresh" title="Refresh" onclick="syncAll()">\u21bb</button>' +
    '</div></div><div id="dashboard-panel"><p class="dashboard-empty">No data yet.<br>Add startup_skills to config.yaml or run a skill.</p></div>';
  document.body.appendChild(dash);
}

// ---------- KPI hero strip ----------

window.__kpiData = window.__kpiData || [];

function parseKpiMessages() {
  const blocks = consumeCodeBlocks(d => Array.isArray(d.kpis));
  if (blocks.length > 0) {
    window.__kpiData = blocks[blocks.length - 1].kpis || [];
    renderKpis();
  }
}

function renderKpis() {
  const strip = document.querySelector('#kpi-strip');
  if (!strip) return;
  const kpis = window.__kpiData || [];
  if (kpis.length === 0) {
    strip.style.display = 'none';
    document.body.classList.remove('dt-has-kpis');
    return;
  }
  strip.style.display = 'flex';
  document.body.classList.add('dt-has-kpis');
  strip.innerHTML = kpis.map(k =>
    '<div class="kpi-card"><div class="kpi-value">' + esc(String(k.value)) + '</div><div class="kpi-label">' + esc(String(k.label)) + '</div></div>'
  ).join('');
}

function createKpiStrip() {
  if (document.querySelector('#kpi-strip')) return;
  const strip = document.createElement('div');
  strip.id = 'kpi-strip';
  strip.style.display = 'none';
  document.body.appendChild(strip);
}

// ---------- Tips card (lower-right) ----------

var DT_TIPS = [
  'Type <b>/goal &lt;objective&gt;</b> to have the agent plan &amp; chain skills automatically.',
  'Click any skill in the left panel to run it instantly.',
  'Use <b>/run skill 73595369</b> to pass a DID inline \u2014 no prompts.',
  'A skill\u2019s <i>gives</i> can feed another skill\u2019s <i>needs</i> \u2014 that\u2019s how chaining works.',
  'Type <b>/memory</b> to see cached results and their age.',
  'Type <b>/audit</b> to review the last skill executions.',
];

function createTips() {
  if (document.querySelector('#dt-tips')) return;
  if (localStorage.getItem('dtTipsHidden') === '1') return;
  var idx = Math.floor(Math.random() * DT_TIPS.length);
  var card = document.createElement('div');
  card.id = 'dt-tips';
  card.innerHTML = '<div class="dt-tips-head"><span class="dt-tips-title">\ud83d\udca1 Tip</span><div>' +
    '<button class="dt-tips-next" title="Next tip">\u21bb</button>' +
    '<button class="dt-tips-close" title="Hide tips">\u2715</button>' +
    '</div></div><div class="dt-tips-body">' + DT_TIPS[idx] + '</div>';
  document.body.appendChild(card);
  var body = card.querySelector('.dt-tips-body');
  card.querySelector('.dt-tips-next').addEventListener('click', function() {
    idx = (idx + 1) % DT_TIPS.length;
    body.innerHTML = DT_TIPS[idx];
  });
  card.querySelector('.dt-tips-close').addEventListener('click', function() {
    localStorage.setItem('dtTipsHidden', '1');
    card.remove();
  });
}

// ---------- Master sync ----------

function syncAll() {
  parseSkillsMessages();
  parseDashboardMessages();
  parseKpiMessages();
}

// ---------- Init ----------

createSidebar();
createDashboard();
createKpiStrip();
createTips();

// Initial parse with staggered retries (React hydration can take 1-2 s)
setTimeout(syncAll, 400);
setTimeout(syncAll, 1500);
setTimeout(syncAll, 4000);

// Periodic safety-net; MutationObserver handles real-time updates
setInterval(syncAll, 10000);

// MutationObserver: react to new messages arriving in DOM
setTimeout(function() {
  var root = document.querySelector('main') || document.body;
  var observer = new MutationObserver(function() { syncAll(); });
  observer.observe(root, { childList: true, subtree: true });
}, 500);

// ---------- Public API ----------
window.syncAll = syncAll;
window.expandDashboard = expandDashboard;
window.collapseDashboard = collapseDashboard;

window.dtDebug = function() {
  console.group('Digital Twin Debug');
  console.log('Skills:', window.__skillsData);
  console.log('Dashboard:', window.__dashboardData);
  console.log('KPIs:', window.__kpiData);
  console.log('Hidden elements:', document.querySelectorAll('[data-dt-hidden]').length);
  console.groupEnd();
};

document.addEventListener('keydown', function(e) { if (e.key === 'Escape') collapseDashboard(); });
