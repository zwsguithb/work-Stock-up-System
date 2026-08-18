const $ = (id) => document.getElementById(id);

const state = {
  reportId: null,
  structure: [],      // [{style_code, batch_names, summary_columns, detail_groups}]
  styleCode: null,
  view: { type: 'summary' },
  cache: {},          // key -> {columns, rows}
};

function fmt(v) {
  if (v === null || v === undefined || v === '') return '';
  if (typeof v === 'number') {
    if (Number.isInteger(v)) return String(v);
    return v.toFixed(4).replace(/\.?0+$/, '');
  }
  return String(v);
}

function setStatus(id, msg, ok) {
  const el = $(id);
  if (!el) return;
  el.textContent = msg;
  el.className = 'status ' + (ok === undefined ? '' : ok ? 'ok' : 'err');
}

async function api(url, opts) {
  const r = await fetch(url, opts);
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
  if (!r.ok) throw new Error(data.detail || data.message || text.slice(0, 200));
  return data;
}

// ---------- 上传 ----------
async function upload(fileInputId, url, statusId) {
  const f = $(fileInputId).files[0];
  if (!f) { setStatus(statusId, '请先选择文件', false); return; }
  setStatus(statusId, '上传中…');
  const fd = new FormData();
  fd.append('file', f);
  try {
    const res = await api(url, { method: 'POST', body: fd });
    if (res.status === 'error') { setStatus(statusId, res.message || '导入失败', false); return; }
    const parts = [];
    if (res.styles !== undefined) parts.push(`款号 ${res.styles}`);
    if (res.accounts !== undefined) parts.push(`账号配置 ${res.accounts}`);
    if (res.imported_rows !== undefined) parts.push(`${res.imported_rows} 行`);
    if (res.batches !== undefined) parts.push(`批次 ${res.batches}`);
    setStatus(statusId, '成功：' + (parts.join('，') || 'ok'), true);
  } catch (e) {
    setStatus(statusId, '失败：' + e.message, false);
  }
}

// ---------- 报表 ----------
async function loadReports(selectId) {
  const res = await api('/api/reports');
  const sel = $('reportHistory');
  sel.innerHTML = '';
  if (!res.items.length) {
    sel.innerHTML = '<option value="">暂无出表记录</option>';
    return;
  }
  res.items.forEach(r => {
    const o = document.createElement('option');
    o.value = r.id;
    o.textContent = `#${r.id} ${r.name || '备货报表'} 基准日${r.baseline_date} [${r.status}]`;
    sel.appendChild(o);
  });
  sel.value = selectId || res.items[0].id;
  await loadReport(Number(sel.value));
}

async function loadReport(reportId) {
  state.reportId = reportId;
  state.cache = {};
  const [kpis, struct] = await Promise.all([
    api(`/api/reports/${reportId}/kpis`),
    api(`/api/reports/${reportId}/structure`),
  ]);
  $('kpiSku').textContent = kpis.sku_count;
  $('kpiStyle').textContent = kpis.style_count;
  $('kpiToOrder').textContent = kpis.skus_to_order;
  $('kpiTotalOrder').textContent = kpis.total_order_quantity;

  state.structure = struct.styles || [];
  if (!state.structure.length) {
    $('styleTabs').innerHTML = '<span class="subtitle">暂无数据，请先上传配置并出表</span>';
    $('viewTabs').innerHTML = '';
    renderTable({ columns: [], rows: [] });
    return;
  }
  if (!state.structure.find(s => s.style_code === state.styleCode)) {
    state.styleCode = state.structure[0].style_code;
  }
  renderStyleTabs();
  renderViewTabs();
  await loadView();
}

function renderStyleTabs() {
  const box = $('styleTabs');
  box.innerHTML = '';
  state.structure.forEach(s => {
    const d = document.createElement('div');
    d.className = 'tab' + (s.style_code === state.styleCode ? ' active' : '');
    d.textContent = s.style_code;
    d.onclick = async () => {
      state.styleCode = s.style_code;
      state.view = { type: 'summary' };
      $('exportStyleCode').value = s.style_code;
      renderStyleTabs(); renderViewTabs(); await loadView();
    };
    box.appendChild(d);
  });
}

function currentStyle() {
  return state.structure.find(s => s.style_code === state.styleCode);
}

function renderViewTabs() {
  const st = currentStyle();
  const box = $('viewTabs');
  box.innerHTML = '';
  if (!st) return;
  const tabs = [{ label: `${st.style_code} 备货汇总`, view: { type: 'summary' } }];
  (st.detail_groups || []).forEach(g => {
    tabs.push({ label: g.label, view: { type: 'detail', platform: g.platform, account: g.account } });
  });
  tabs.forEach(t => {
    const d = document.createElement('div');
    const active = JSON.stringify(t.view) === JSON.stringify(state.view);
    d.className = 'tab' + (active ? ' active' : '');
    d.textContent = t.label;
    d.onclick = async () => { state.view = t.view; renderViewTabs(); await loadView(); };
    box.appendChild(d);
  });
}

async function loadView() {
  const st = currentStyle();
  if (!st) return;
  const key = JSON.stringify([state.reportId, st.style_code, state.view]);
  if (!state.cache[key]) {
    if (state.view.type === 'summary') {
      const res = await api(`/api/reports/${state.reportId}/summary_table?style_code=${encodeURIComponent(st.style_code)}`);
      const s = (res.styles || [])[0] || { columns: [], rows: [] };
      state.cache[key] = { columns: s.columns, rows: s.rows };
    } else {
      let url = `/api/reports/${state.reportId}/detail?platform=${state.view.platform}&style_code=${encodeURIComponent(st.style_code)}`;
      if (state.view.account) url += `&account=${encodeURIComponent(state.view.account)}`;
      const res = await api(url);
      const cols = res.columns.map(c => c.label);
      const rows = res.items.map(it => res.columns.map(c => it[c.field]));
      state.cache[key] = { columns: cols, rows };
    }
  }
  renderTable(state.cache[key]);
}

function renderTable(data) {
  const thead = $('dataTable').querySelector('thead');
  const tbody = $('dataTable').querySelector('tbody');
  const kw = ($('searchSku').value || '').trim().toLowerCase();
  thead.innerHTML = '';
  tbody.innerHTML = '';
  if (!data.columns.length) {
    $('tableInfo').textContent = '';
    return;
  }
  const tr = document.createElement('tr');
  data.columns.forEach(c => {
    const th = document.createElement('th');
    th.textContent = c;
    tr.appendChild(th);
  });
  thead.appendChild(tr);

  const rows = kw ? data.rows.filter(r => String(r[0] || '').toLowerCase().includes(kw)) : data.rows;
  rows.forEach(r => {
    const tr = document.createElement('tr');
    r.forEach((v, i) => {
      const td = document.createElement('td');
      td.textContent = fmt(v);
      if (typeof v === 'number' && v < 0) td.className = 'negative';
      if (i === 0) td.style.fontWeight = '600';
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  $('tableInfo').textContent = `共 ${rows.length} 行 / ${data.columns.length} 列`
    + (kw ? `（已按「${kw}」筛选，总 ${data.rows.length} 行）` : '');
}

// ---------- 出表 ----------
async function createAndGenerate(withSync) {
  const bd = $('baselineDate').value;
  if (!bd) { alert('请选择基准日'); return; }
  const btn = withSync ? $('btnSyncGenerate') : $('btnGenerateOnly');
  const orig = btn.textContent;
  btn.disabled = true;
  try {
    if (withSync) {
      btn.textContent = '正在从领星拉取…';
      const fd = new FormData();
      fd.append('baseline_date', bd);
      const sync = await api('/api/lingxing/sync', { method: 'POST', body: fd });
      if (sync.status === 'error') {
        if (!confirm('领星拉取失败：' + (sync.message || '') + '\n\n是否用已上传的兜底数据继续出表？')) {
          return;
        }
      } else {
        $('sourceInfo').textContent = `领星已同步：销量 ${sync.sales_rows || 0} 行，库存 ${sync.inventory_rows || 0} 行`;
      }
    }
    btn.textContent = '正在计算…';
    const fd1 = new FormData();
    fd1.append('name', $('reportName').value || `备货报表 ${bd}`);
    fd1.append('baseline_date', bd);
    const created = await api('/api/reports', { method: 'POST', body: fd1 });

    const fd2 = new FormData();
    fd2.append('baseline_date', bd);
    const gen = await api(`/api/reports/${created.report_id}/generate`, { method: 'POST', body: fd2 });
    if (gen.status === 'error') { alert('出表失败：' + gen.message); return; }
    await loadReports(created.report_id);
    $('reportHistory').value = created.report_id;
  } catch (e) {
    alert('出表失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

// ---------- 配置 ----------
async function openConfig() {
  try {
    const [cfg, lx] = await Promise.all([api('/api/config'), api('/api/lingxing/status')]);
    $('cfgDivisor').value = cfg.sellable_days_divisor || 'denoised';
    $('cfgRounding').value = cfg.sellable_days_rounding || 'ceil';
    const sids = lx.sids || {};
    $('cfgAppId').value = lx.app_id_set ? (sids.lingxing_app_id || '已配置') : '';
    $('cfgSidsAmazon').value = sids.lingxing_sids_amazon || '';
    $('cfgSidsWalmart').value = sids.lingxing_sids_walmart || '';
    $('cfgSidsTemu').value = sids.lingxing_sids_temu || '';
    $('cfgSidsOther').value = sids.lingxing_sids_other || '';
  } catch (e) { /* 忽略 */ }
  $('configModal').classList.add('open');
}

async function saveConfig() {
  try {
    await api('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sellable_days_divisor: $('cfgDivisor').value,
        sellable_days_rounding: $('cfgRounding').value,
      }),
    });
    const lxPayload = {};
    const appId = $('cfgAppId').value.trim();
    const secret = $('cfgAppSecret').value.trim();
    if (appId && appId !== '已配置') lxPayload.lingxing_app_id = appId;
    if (secret) lxPayload.lingxing_app_secret = secret;
    ['amazon', 'walmart', 'temu', 'other'].forEach(k => {
      const v = $('cfgSids' + k.charAt(0).toUpperCase() + k.slice(1)).value.trim();
      if (v) lxPayload['lingxing_sids_' + k] = v;
    });
    if (Object.keys(lxPayload).length) {
      await api('/api/lingxing/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(lxPayload),
      });
    }
    $('configModal').classList.remove('open');
    alert('已保存。口径变更需重新出表（或点「仅重算」）才会生效。');
  } catch (e) { alert('保存失败：' + e.message); }
}

// ---------- 绑定 ----------
function init() {
  const d = new Date();
  $('baselineDate').value = d.toISOString().slice(0, 10);

  $('btnUploadStyles').onclick = () => upload('fileStyles', '/api/styles/upload', 'statusStyles');
  $('btnUploadUnproduced').onclick = () => upload('fileUnproduced', '/api/unproduced/upload', 'statusUnproduced');
  $('btnUploadSales').onclick = () => upload('fileSales', '/api/sales/upload', 'statusSales');
  $('btnUploadInventory').onclick = () => upload('fileInventory', '/api/inventory/upload', 'statusInventory');

  $('btnSyncGenerate').onclick = () => createAndGenerate(true);
  $('btnGenerateOnly').onclick = () => createAndGenerate(false);

  $('btnConfig').onclick = openConfig;
  $('btnCloseConfig').onclick = () => $('configModal').classList.remove('open');
  $('btnSaveConfig').onclick = saveConfig;

  $('btnExport').onclick = () => {
    if (!state.reportId) { alert('请先出表'); return; }
    window.location = `/api/reports/${state.reportId}/export`;
  };

  $('btnExportByStyle').onclick = () => {
    if (!state.reportId) { alert('请先出表'); return; }
    const sc = $('exportStyleCode').value.trim();
    let url = `/api/reports/${state.reportId}/export`;
    if (sc) url += `?style_code=${encodeURIComponent(sc)}`;
    window.location = url;
  };

  $('reportHistory').onchange = (e) => {
    if (e.target.value) loadReport(Number(e.target.value));
  };

  let timer;
  $('searchSku').oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(() => loadView(), 200);
  };

  loadReports().catch(() => {});
}

init();
