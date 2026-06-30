// Custom sidebar for skills and MCP servers - OPTIMIZED (no blocking ops)

function extractSkillsFromChat() {
  // Strategy 1: Check message elements (any element with "message" in class)
  let messages = document.querySelectorAll('[class*="message"]');
  if (messages.length === 0) {
    // Strategy 2: Check divs with role or aria attributes
    messages = document.querySelectorAll('div[role="article"], div[data-testid*="message"]');
  }
  if (messages.length === 0) {
    // Strategy 3: Check all divs (expensive but last resort)
    messages = Array.from(document.querySelectorAll('div')).slice(-50); // Last 50 divs
  }
  
  // Only trust the latest relevant message to avoid stale state from old history.
  const orderedMessages = Array.from(messages);
  for (let i = orderedMessages.length - 1; i >= 0; i -= 1) {
    const msg = orderedMessages[i];
    const text = (msg.textContent || msg.innerText || '').trim();
    if (!text) continue;

    // Explicit "no skills" message from backend should clear sidebar.
    if (text.toLowerCase().includes('no skills saved yet')) {
      return [];
    }
    
    // Look for "Saved skills:" pattern (case-insensitive)
    if (text.toLowerCase().includes('saved skills:')) {
      // Match from "Saved skills:" to end of line or next double newline
      const match = text.match(/Saved\s+skills:\s*([^\n]+)/i);
      if (match) {
        const skillSet = new Set();
        const skillsStr = match[1].trim();
        // Split by comma
        skillsStr.split(',').forEach(s => {
          const skill = s.trim();
          // Filter out garbage: must be alphanumeric + underscore, min 3 chars
          if (skill && /^[a-zA-Z0-9_]+$/.test(skill) && skill.length > 2) {
            skillSet.add(skill);
          }
        });
        return Array.from(skillSet).sort();
      }
    }
  }

  return [];
}

// ---------- Dashboard ----------

window.__dashboardData = window.__dashboardData || {};

function parseDashboardMessages() {
  // Strategy A: CSS class (Chainlit renders ```lang as code.language-lang)
  let codeBlocks = Array.from(
    document.querySelectorAll('code[class*="dashboard-data"], code[class*="dashboard_data"]')
  );

  // Strategy B: Text-content fallback — any <code> containing {"skill": ...,"outputs":...}
  if (codeBlocks.length === 0) {
    document.querySelectorAll('pre code, code').forEach(el => {
      const t = (el.textContent || '').trim();
      if (t.startsWith('{') && t.includes('"skill"') && t.includes('"outputs"')) {
        codeBlocks.push(el);
      }
    });
  }

  codeBlocks.forEach(el => {
    // Parse JSON and store in dashboard state
    try {
      const text = (el.textContent || '').trim();
      const data = JSON.parse(text);
      if (data && data.skill && data.outputs) {
        window.__dashboardData[data.skill] = {
          outputs: data.outputs,
          ts: data.ts || '',
          status: data.status || '',
        };
      }
    } catch (_) {}

    // Hide the entire message container.
    // Try Chainlit-known element types first, then walk up as fallback.
    const knownSelectors = [
      '[data-testid="step"]',
      '.step',
      'article',
      '[class*="step"]',
      '[class*="message-container"]',
      '[class*="chat-message"]',
    ];
    let hidden = false;
    for (const sel of knownSelectors) {
      const container = el.closest(sel);
      if (container) {
        container.style.setProperty('display', 'none', 'important');
        hidden = true;
        break;
      }
    }
    if (!hidden) {
      // Walk up until we find a sibling-bearing ancestor (the message row in the list)
      let node = el;
      for (let i = 0; i < 15; i++) {
        node = node.parentElement;
        if (!node || node === document.body) break;
        if (node.parentElement && node.parentElement.children.length > 1) {
          node.style.setProperty('display', 'none', 'important');
          break;
        }
      }
    }
  });
}

function formatTimestamp(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch (_) {
    return '';
  }
}

// ---- Markdown table detection and parsing ----

function isMarkdownTable(value) {
  if (typeof value !== 'string') return false;
  const tableLines = value.split('\n').filter(l => l.trim().startsWith('|'));
  return tableLines.length >= 3;
}

function parseMarkdownTable(text) {
  const lines = text.trim().split('\n')
    .map(l => l.trim())
    .filter(l => l.startsWith('|') && l.endsWith('|'));
  if (lines.length < 3) return null;

  function cells(line) {
    return line.slice(1, -1).split('|').map(c => c.trim());
  }
  function isSep(line) {
    return /^\|[\s|:|-]+\|$/.test(line);
  }

  // Find header + separator pair
  let headerIdx = -1;
  for (let i = 0; i < lines.length - 1; i++) {
    if (isSep(lines[i + 1])) { headerIdx = i; break; }
  }
  if (headerIdx < 0) return null;

  const headers = cells(lines[headerIdx]);
  const rows = lines.slice(headerIdx + 2).map(cells).filter(r => r.some(c => c));
  return { headers, rows };
}

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

// Strip markdown links: [text](url) → text
function stripMdLinks(s) {
  return s.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
}

function renderMarkdownTable(text) {
  const parsed = parseMarkdownTable(text);
  if (!parsed) {
    return `<pre class="db-raw">${esc(text.slice(0, 300))}</pre>`;
  }
  const { headers, rows } = parsed;
  const ths = headers
    .map(h => `<th title="${esc(h)}">${truncate(h, 14)}</th>`)
    .join('');
  const trs = rows.map(row =>
    '<tr>' + row.map((c, i) => {
      if (i >= headers.length) return '';
      const clean = stripMdLinks(c);
      return `<td title="${esc(clean)}">${truncate(clean, 16)}</td>`;
    }).join('') + '</tr>'
  ).join('');
  const rowCount = rows.length;
  return `
    <div class="db-tbl-meta">${rowCount} row${rowCount !== 1 ? 's' : ''} &bull; scroll →</div>
    <div class="db-tbl-scroll">
      <table class="db-html-table">
        <thead><tr>${ths}</tr></thead>
        <tbody>${trs}</tbody>
      </table>
    </div>`;
}

function renderDashboard() {
  const panel = document.querySelector('#dashboard-panel');
  if (!panel) return;

  const entries = Object.entries(window.__dashboardData);
  if (entries.length === 0) {
    panel.innerHTML = '<p class="dashboard-empty">No data yet.<br>Add a skill to <code>startup_skills</code> in config.yaml or run a skill.</p>';
    return;
  }

  panel.innerHTML = entries.map(([skill, { outputs, ts, status }]) => {
    const time = ts ? `<span class="db-ts">${formatTimestamp(ts)}</span>` : '';
    const title = esc(skill.replace(/_/g, ' '));

    let bodyParts;
    if (status === 'loading') {
      bodyParts = '<div class="db-loading"><span class="db-spinner"></span> Loading…</div>';
    } else if (status === 'no-output' || (!status && Object.keys(outputs).length === 0)) {
      bodyParts = '<div class="db-no-output">No output captured</div>';
    } else {
      bodyParts = Object.entries(outputs).map(([key, val]) => {
        if (isMarkdownTable(val)) {
          return `<div class="db-output-label">${esc(key.replace(/_/g, ' '))}</div>${renderMarkdownTable(val)}`;
        }
        return `<div class="db-kv"><span class="db-key">${esc(key.replace(/_/g, ' '))}</span><span class="db-val">${esc(String(val))}</span></div>`;
      }).join('');
    }

    const hasTable = Object.values(outputs).some(isMarkdownTable);
    return `
      <div class="db-card${hasTable ? ' db-card-table' : ''}${status === 'loading' ? ' db-card-loading' : ''}">
        <div class="db-card-header">${title}${time}</div>
        <div class="db-card-body">${bodyParts}</div>
      </div>`;
  }).join('');
}

function syncDashboard() {
  parseDashboardMessages();
  renderDashboard();
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
  if (btn) { btn.textContent = '✕ Close'; btn.title = 'Collapse'; }
}

function collapseDashboard() {
  const dash = document.querySelector('.custom-dashboard');
  if (!dash) return;
  dash.classList.remove('db-expanded');
  const backdrop = document.querySelector('.db-backdrop');
  if (backdrop) backdrop.remove();
  const btn = dash.querySelector('.db-expand');
  if (btn) { btn.textContent = 'Expand ↗'; btn.title = 'Expand to center'; }
}

function createDashboard() {
  if (document.querySelector('.custom-dashboard')) return;

  const dash = document.createElement('div');
  dash.className = 'custom-dashboard';
  dash.innerHTML = `
    <div class="dashboard-header">
      <h3>Dashboard</h3>
      <div class="db-header-buttons">
        <button class="db-expand" title="Expand to center" onclick="expandDashboard()">Expand ↗</button>
        <button class="db-refresh" title="Refresh" onclick="syncDashboard()">↻</button>
      </div>
    </div>
    <div id="dashboard-panel"><p class="dashboard-empty">No data yet.<br>Add startup_skills to config.yaml or run a skill.</p></div>`;
  document.body.appendChild(dash);
}

// ---------- Skills sidebar (unchanged logic) ----------

function updateSkillsList(skills) {
  window.currentSkills = skills;
  const list = document.querySelector('#skills-list');
  if (!list) return;
  
  list.innerHTML = '';
  if (skills && skills.length > 0) {
    skills.forEach(skill => {
      const li = document.createElement('li');
      li.textContent = skill;
      li.title = `Click to run ${skill}`;
      list.appendChild(li);
    });
  } else {
    const li = document.createElement('li');
    li.className = 'sidebar-empty';
    li.textContent = 'No skills recorded yet';
    list.appendChild(li);
  }
}

function sendSkillCommand(skillName) {
  const input = document.querySelector('textarea, input[type="text"]');
  if (!input) return;

  const cmd = `run ${skillName}`;
  input.focus();

  // Use native setter so React/Chainlit state is updated.
  const proto = input.tagName === 'TEXTAREA'
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
  if (descriptor && descriptor.set) {
    descriptor.set.call(input, cmd);
  } else {
    input.value = cmd;
  }

  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));

  // Prefer clicking submit button so the same path as manual send is used.
  const submitBtn = document.querySelector('button[type="submit"], button[aria-label*="Send" i]');
  if (submitBtn && !submitBtn.disabled) {
    submitBtn.click();
    return;
  }

  // Fallback to Enter key events.
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
  input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true }));
}

function attachSidebarListeners() {
  const list = document.querySelector('#skills-list');
  if (!list) return;
  list.addEventListener('click', (e) => {
    const li = e.target.closest('li');
    if (li && !li.classList.contains('sidebar-empty')) {
      sendSkillCommand(li.textContent.trim());
    }
  });
}

function createSidebar() {
  if (document.querySelector('.custom-sidebar')) return;
  
  const sidebar = document.createElement('div');
  sidebar.className = 'custom-sidebar';
  sidebar.innerHTML = `<div class="sidebar-section"><h3>Skills</h3><ul id="skills-list"><li class="sidebar-empty">No skills recorded yet</li></ul></div>`;
  document.body.insertBefore(sidebar, document.body.firstChild);
  attachSidebarListeners();
  
  const skills = extractSkillsFromChat();
  updateSkillsList(skills);
}

// ---------- Init ----------

createSidebar();
createDashboard();

// Console helper for debugging in browser devtools: window.dtDebug()
window.dtDebug = function() {
  console.group('Digital Twin Debug');
  console.log('Dashboard data:', window.__dashboardData);
  console.log('Dashboard panel:', document.querySelector('.custom-dashboard'));
  console.log('Sidebar panel:', document.querySelector('.custom-sidebar'));
  console.log('dashboard-data code blocks:', document.querySelectorAll('code[class*="dashboard"]').length);
  const allCode = Array.from(document.querySelectorAll('code'))
    .filter(el => (el.textContent || '').includes('"skill"'));
  console.log('Code blocks containing "skill":', allCode.length, allCode);
  console.groupEnd();
};

// Aggressive sync - try to extract skills with retries
let syncAttempts = 0;
function syncSkills() {
  const skills = extractSkillsFromChat();
  updateSkillsList(skills);
  if (skills.length > 0) {
    syncAttempts = 0; // Reset on success
  } else if (syncAttempts < 5) {
    syncAttempts++;
    // Retry in 500ms if not found
    setTimeout(syncSkills, 500);
  }
}

// Initial sync after page settles
setTimeout(syncSkills, 300);
setTimeout(syncDashboard, 600);
setTimeout(syncDashboard, 2000);  // retry after slow loads

// Periodic re-scan — catches messages that arrived before the observer was ready
setInterval(syncDashboard, 3000);

// Observer for new messages — updates both skills sidebar and dashboard
let lastKnownSkillCount = 0;
let lastDashboardKeys = '';
const observer = new MutationObserver(() => {
  const skills = extractSkillsFromChat();
  if (skills.length !== lastKnownSkillCount) {
    lastKnownSkillCount = skills.length;
    updateSkillsList(skills);
  }
  // Always re-parse so new dashboard-data messages are hidden and rendered
  parseDashboardMessages();
  const newKeys = Object.keys(window.__dashboardData).sort().join(',');
  if (newKeys !== lastDashboardKeys) {
    lastDashboardKeys = newKeys;
    renderDashboard();
  }
});

setTimeout(() => {
  const mainContainer = document.querySelector('main') || document.body;
  observer.observe(mainContainer, { childList: true, subtree: true });
}, 500);

window.updateSkills = updateSkillsList;
window.currentSkills = [];
window.syncDashboard = syncDashboard;
window.expandDashboard = expandDashboard;
window.collapseDashboard = collapseDashboard;

// Close expanded dashboard with Escape key
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') collapseDashboard();
});
