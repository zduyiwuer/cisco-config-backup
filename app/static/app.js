/* 多厂商网络设备配置备份中心 - 前端逻辑 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = { devices: [], sites: [], editingId: null, editingSiteId: null, settings: {}, polling: null, importFile: null, me: null, users: [], vendors: {} };

/** 在线状态圆点 */
function onlineDot(dev) {
  if (dev.online === 1 || dev.online === true) {
    const lat = dev.probe_latency_ms;
    const latTxt = lat != null ? ` · ${lat}ms` : '';
    return `<span class="dot on" title="在线${esc(latTxt)}"><i></i>在线</span>`;
  }
  if (dev.online === 0 || dev.online === false) {
    const fails = dev.consecutive_fails > 1 ? ` · 连续失败 ${dev.consecutive_fails} 次` : '';
    return `<span class="dot off" title="离线${esc(fails)}"><i></i>离线</span>`;
  }
  return '<span class="dot unk"><i></i>未探测</span>';
}

function toast(msg, kind = '') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast ' + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), 3600);
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function statusPill(dev) {
  if (!dev.last_status) return '<span class="pill">未备份</span>';
  if (dev.last_status === 'success')
    return `<span class="pill ok">成功 · ${fmtTime(dev.last_backup_at).slice(5)}</span>`;
  return `<span class="pill err">失败 · ${fmtTime(dev.last_backup_at).slice(5)}</span>`;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (res.status === 401) {
    // 会话失效：回登录页
    window.location.href = '/login';
    throw new Error('未登录');
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ------------------------------------------------------------- 当前用户 */
const ROLE_NAMES = { admin: '管理员', operator: '操作员', viewer: '只读' };
const SOURCE_NAMES = { local: '本地', ad: '域账号' };

async function loadMe() {
  try {
    const r = await api('/api/auth/me');
    state.me = r.user;
  } catch (e) {
    return; // 未启用认证时接口可能不可用
  }
  const u = state.me;
  const initial = (u.display_name || u.username || '?').trim().charAt(0).toUpperCase();
  $('#user-avatar').textContent = initial;
  $('#user-name').textContent = u.display_name || u.username;
  $('#dd-name').textContent = u.display_name || u.username;
  const isAd = u.source === 'ad';
  $('#dd-role').textContent =
    `${ROLE_NAMES[u.role] || u.role}${isAd ? ' · 域账号' : ''}`;
  // 管理员才能管理用户与域配置
  if (u.role === 'admin') {
    $('#btn-users').classList.remove('hidden');
    $('#btn-ad-config').classList.remove('hidden');
  }
}

$('#user-trigger').addEventListener('click', (e) => {
  e.stopPropagation();
  $('#user-dropdown').classList.toggle('hidden');
});
document.addEventListener('click', () => $('#user-dropdown').classList.add('hidden'));
$('#user-dropdown').addEventListener('click', (e) => e.stopPropagation());

$('#btn-logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
  window.location.href = '/login';
});

/* --------------------------------------------------------- 修改密码 */
function checkStrength(pwd) {
  const el = $('#pwd-strength');
  if (!pwd) { el.textContent = ''; el.className = 'pwd-strength'; return; }
  let msg = '强度不足';
  let ok = false;
  if (pwd.length < 8) msg = `太短（当前 ${pwd.length} 位，至少 8 位）`;
  else if (!/[a-zA-Z]/.test(pwd)) msg = '需包含字母';
  else if (!/\d/.test(pwd)) msg = '需包含数字';
  else { msg = '强度合格'; ok = true; }
  el.textContent = msg;
  el.className = 'pwd-strength ' + (ok ? 'ok' : 'bad');
}
$('#p-new').addEventListener('input', (e) => checkStrength(e.target.value));

$('#btn-change-password').addEventListener('click', () => {
  ['#p-old', '#p-new', '#p-new2'].forEach((s) => ($(s).value = ''));
  $('#pwd-strength').textContent = '';
  $('#pwd-strength').className = 'pwd-strength';
  $('#pwd-msg').classList.add('hidden');
  $('#pwd-modal').classList.remove('hidden');
});

$('#btn-save-pwd').addEventListener('click', async () => {
  const oldPwd = $('#p-old').value;
  const newPwd = $('#p-new').value;
  const newPwd2 = $('#p-new2').value;
  if (!newPwd) return showMsg('#pwd-msg', '请输入新密码', 'err');
  if (newPwd !== newPwd2) return showMsg('#pwd-msg', '两次输入的新密码不一致', 'err');
  try {
    const r = await api('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPwd, new_password: newPwd }),
    });
    closeModals();
    toast(r.message || '密码已更新', 'ok');
  } catch (err) { showMsg('#pwd-msg', err.message, 'err'); }
});

/* --------------------------------------------------------- 用户管理 */
$('#btn-users').addEventListener('click', async () => {
  await loadUsers();
  $('#users-msg').classList.add('hidden');
  $('#users-modal').classList.remove('hidden');
});

async function loadUsers() {
  try {
    state.users = await api('/api/auth/users');
  } catch (e) {
    state.users = [];
  }
  const tb = $('#users-table tbody');
  tb.innerHTML = state.users.map((u) => {
    const rolePill = { admin: 'err', operator: 'ssh', viewer: '' }[u.role] || '';
    const statusPillHtml = u.status === 'active'
      ? '<span class="pill ok">正常</span>'
      : '<span class="pill err">已禁用</span>';
    const me = state.me && u.id === state.me.id;
    return `<tr data-uid="${u.id}">
      <td><div class="dev-name">${esc(u.display_name || u.username)}</div>
          <div class="dev-host">${esc(u.username)}${me ? ' · 当前登录' : ''}</div></td>
      <td><span class="pill">${SOURCE_NAMES[u.source] || u.source}</span></td>
      <td><select class="user-role" ${me ? 'disabled' : ''}>
        ${['admin', 'operator', 'viewer'].map((r) =>
          `<option value="${r}" ${u.role === r ? 'selected' : ''}>${ROLE_NAMES[r]}</option>`).join('')}
      </select></td>
      <td>${statusPillHtml}</td>
      <td class="mono">${u.last_login_at ? fmtTime(u.last_login_at).slice(5) : '从未'}</td>
      <td><div class="actions">
        <button class="btn small act-user-save">保存</button>
        <button class="btn small act-user-toggle">${u.status === 'active' ? '禁用' : '启用'}</button>
        <button class="btn small danger act-user-del" ${me ? 'disabled' : ''}>删除</button>
      </div></td></tr>`;
  }).join('');
}

$('#users-table').addEventListener('click', async (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  const tr = e.target.closest('tr');
  const uid = Number(tr.dataset.uid);
  const user = state.users.find((u) => u.id === uid);
  if (!user) return;

  if (btn.classList.contains('act-user-save')) {
    const role = tr.querySelector('.user-role').value;
    try {
      await api(`/api/auth/users/${uid}`, { method: 'PATCH', body: JSON.stringify({ role }) });
      toast(`${user.username} 的角色已更新为${ROLE_NAMES[role]}`, 'ok');
      loadUsers();
    } catch (err) { toast(err.message, 'err'); }
  } else if (btn.classList.contains('act-user-toggle')) {
    const next = user.status === 'active' ? 'disabled' : 'active';
    try {
      await api(`/api/auth/users/${uid}`, { method: 'PATCH', body: JSON.stringify({ status: next }) });
      toast(next === 'disabled' ? `${user.username} 已禁用` : `${user.username} 已启用`, 'ok');
      loadUsers();
    } catch (err) { toast(err.message, 'err'); }
  } else if (btn.classList.contains('act-user-del')) {
    if (!confirm(`确定删除用户「${user.username}」？该用户的登录会话会同时失效。`)) return;
    try {
      await api(`/api/auth/users/${uid}`, { method: 'DELETE' });
      toast('用户已删除', 'ok');
      loadUsers();
    } catch (err) { toast(err.message, 'err'); }
  }
});

$('#btn-add-user').addEventListener('click', async () => {
  const username = $('#nu-username').value.trim();
  const password = $('#nu-password').value;
  const role = $('#nu-role').value;
  if (!username) return showMsg('#users-msg', '请输入用户名', 'err');
  if (!password) return showMsg('#users-msg', '请输入初始密码', 'err');
  try {
    await api('/api/auth/users', {
      method: 'POST',
      body: JSON.stringify({
        username, password, role, display_name: $('#nu-display').value.trim(),
      }),
    });
    ['#nu-username', '#nu-password', '#nu-display'].forEach((s) => ($(s).value = ''));
    $('#users-msg').classList.add('hidden');
    toast(`用户 ${username} 已创建`, 'ok');
    loadUsers();
  } catch (err) { showMsg('#users-msg', err.message, 'err'); }
});

/* --------------------------------------------------------- 域账号配置 */
$('#btn-ad-config').addEventListener('click', async () => {
  try {
    const s = await api('/api/settings');
    $('#ad-enabled').checked = !!s.ad_enabled;
    $('#ad-ssl').checked = s.ad_use_ssl !== false;
    $('#ad-ntlm').checked = !!s.ad_use_ntlm;
    $('#ad-host').value = s.ad_host || '';
    $('#ad-port').value = s.ad_port || 636;
    $('#ad-domain').value = s.ad_domain || '';
    $('#ad-base-dn').value = s.ad_base_dn || '';
    $('#ad-bind-dn').value = s.ad_bind_dn || '';
    $('#ad-bind-pw').value = '';
    $('#ad-bind-pw').placeholder = s.ad_bind_password_set ? '已设置，留空则不修改' : '服务账号密码';
    $('#ad-strategy').value = s.ad_auth_strategy || 'search';
    $('#ad-admin-group').value = s.ad_admin_group || '';
    $('#ad-default-role').value = s.ad_default_role || 'operator';
    $('#ad-ca').value = s.ad_ca_cert || '';
    $('#ad-plaintext').checked = !!s.ad_allow_plaintext;
    renderAdPreview();
  } catch (err) { toast(err.message, 'err'); }
  $('#ad-msg').classList.add('hidden');
  $('#ad-modal').classList.remove('hidden');
});

function renderAdPreview() {
  const ssl = $('#ad-ssl').checked;
  const plain = $('#ad-plaintext').checked;
  const el = $('#ad-preview');
  el.className = 'ad-preview';
  if (!ssl && !plain) {
    el.textContent = '⚠ 当前为明文 LDAP(389)，域账号密码将以明文传输。强烈建议启用 LDAPS。保存会被拒绝。';
    el.classList.add('err');
    return;
  }
  const lines = [];
  if (ssl) lines.push('✓ 启用 LDAPS(636)，密码加密传输');
  if ($('#ad-ntlm').checked) lines.push('· 认证机制 NTLM');
  if ($('#ad-strategy').value === 'direct') lines.push('· 绑定方式：直接绑定（不需服务账号）');
  else if ($('#ad-bind-dn').value) lines.push('· 绑定方式：搜索后绑定（需服务账号）');
  else lines.push('⚠ 未填写服务账号，将回退为直接绑定');
  if ($('#ad-admin-group').value) {
    lines.push(`· 属于「${$('#ad-admin-group').value.split(',')[0]}」组的用户 → 管理员`);
  }
  el.textContent = lines.join('\n') || '请填写 AD 服务器地址';
}

$('#ad-modal').addEventListener('input', renderAdPreview);
$('#ad-modal').addEventListener('change', renderAdPreview);

$('#btn-save-ad').addEventListener('click', async () => {
  const payload = {
    ad_enabled: $('#ad-enabled').checked,
    ad_use_ssl: $('#ad-ssl').checked,
    ad_port: Number($('#ad-port').value) || ($('#ad-ssl').checked ? 636 : 389),
    ad_use_ntlm: $('#ad-ntlm').checked,
    ad_host: $('#ad-host').value.trim(),
    ad_domain: $('#ad-domain').value.trim(),
    ad_base_dn: $('#ad-base-dn').value.trim(),
    ad_bind_dn: $('#ad-bind-dn').value.trim(),
    ad_auth_strategy: $('#ad-strategy').value,
    ad_admin_group: $('#ad-admin-group').value.trim(),
    ad_default_role: $('#ad-default-role').value,
    ad_ca_cert: $('#ad-ca').value.trim(),
    ad_allow_plaintext: $('#ad-plaintext').checked,
  };
  const pw = $('#ad-bind-pw').value;
  if (pw) payload.ad_bind_password = pw;
  try {
    await api('/api/auth/ad', { method: 'PUT', body: JSON.stringify(payload) });
    $('#ad-msg').classList.add('hidden');
    toast('域账号配置已保存', 'ok');
  } catch (err) { showMsg('#ad-msg', err.message, 'err'); }
});

$('#btn-test-ad').addEventListener('click', async () => {
  // 先保存再测试，保证测的是当前填写内容
  const pw = $('#ad-bind-pw').value;
  const payload = {
    ad_host: $('#ad-host').value.trim(),
    ad_port: Number($('#ad-port').value) || ($('#ad-ssl').checked ? 636 : 389),
    ad_use_ssl: $('#ad-ssl').checked,
    ad_domain: $('#ad-domain').value.trim(),
    ad_base_dn: $('#ad-base-dn').value.trim(),
    ad_bind_dn: $('#ad-bind-dn').value.trim(),
    ad_use_ntlm: $('#ad-ntlm').checked,
  };
  if (pw) payload.ad_bind_password = pw;
  const el = $('#ad-preview');
  el.textContent = '测试中…';
  try {
    await api('/api/auth/ad', { method: 'PUT', body: JSON.stringify(payload) });
    const r = await api('/api/auth/ad/test', { method: 'POST' });
    el.className = 'ad-preview ' + (r.ok ? 'ok' : 'err');
    el.textContent = (r.ok ? '✓ ' : '✗ ') + r.message +
      (r.naming_contexts && r.naming_contexts.length
        ? '\n命名上下文：' + r.naming_contexts.join(', ') : '');
  } catch (err) {
    el.className = 'ad-preview err';
    el.textContent = '✗ 测试失败：' + err.message;
  }
});

/* ------------------------------------------------------------- 设备列表 */
const VENDOR_LABELS = {
  cisco: 'Cisco', h3c: 'H3C', huawei: '华为', ruijie: '锐捷',
};

function vendorOf(dev) {
  return VENDOR_LABELS[dev.device_type] || dev.device_type || 'Cisco';
}

function vendorPill(dev) {
  const v = dev.device_type || 'cisco';
  const cls = { cisco: 'cisco', h3c: 'h3c', huawei: 'huawei', ruijie: 'ruijie' }[v] || '';
  return `<span class="pill vendor-pill ${cls}">${esc(vendorOf(dev))}</span>`;
}

async function loadDevices() {
  state.devices = await api('/api/devices');
  renderStats();
  renderDevices();
  fillDeviceSelects();
}

/** 厂商清单的兜底数据：接口不可用时前端仍能正确联动 */
const VENDOR_FALLBACK = {
  cisco: {
    label: 'Cisco IOS', default_port: 22, needs_enable: true,
    default_commands: ['show running-config', 'show cdp neighbors detail'],
  },
  h3c: {
    label: 'H3C Comware', default_port: 22, needs_enable: false,
    default_commands: ['display current-configuration', 'display lldp neighbor-information'],
  },
  huawei: {
    label: '华为 VRP', default_port: 22, needs_enable: false,
    default_commands: ['display current-configuration', 'display lldp neighbor brief'],
  },
  ruijie: {
    label: '锐捷 RGOS', default_port: 22, needs_enable: false,
    default_commands: ['show running-config', 'show lldp neighbors'],
  },
};

/** 载入厂商清单并填充下拉 */
async function loadVendors() {
  try {
    const list = await api('/api/vendors');
    if (Array.isArray(list) && list.length) {
      state.vendors = Object.fromEntries(list.map((v) => [v.id, v]));
      $('#f-vendor').innerHTML = list
        .map((v) => `<option value="${v.id}">${esc(v.label)}</option>`)
        .join('');
    } else {
      throw new Error('厂商清单为空');
    }
  } catch (e) {
    // 接口异常时用兜底数据，保证联动仍然可用
    state.vendors = { ...VENDOR_FALLBACK };
  }
  applyVendorDefaults();
}

/** 按当前厂商套用默认端口与命令 */
function applyVendorDefaults(force) {
  const vid = $('#f-vendor').value;
  const cfg = state.vendors[vid];
  if (!cfg) return;
  const portEl = $('#f-port');
  if (force || !portEl.value) portEl.value = cfg.default_port;

  const cmdsEl = $('#f-commands');
  const current = cmdsEl.value.split('\n').map((s) => s.trim()).filter(Boolean);
  // 强制填充，或当前内容还是某个厂商的默认值时才自动替换
  const isDefaultish = !current.length ||
    Object.values(state.vendors).some(
      (v) => v.default_commands.join('|') === current.join('|')
    );
  if (force || isDefaultish) {
    cmdsEl.value = cfg.default_commands.join('\n');
    if (force && current.length && !isDefaultish) {
      // 用户主动点的填充按钮，覆盖自定义命令时提示一下
      toast(`已按${cfg.label} 填充默认命令`, 'ok');
    }
  }

  // 仅 Cisco 需要 enable 密码，其余厂商置灰并提示
  const enEl = $('#f-enable');
  const label = enEl.closest('label');
  if (label) label.style.opacity = cfg.needs_enable ? '1' : '.45';
  enEl.placeholder = cfg.needs_enable
    ? '仅 Cisco 需要（enable 提权）'
    : `${cfg.label} 登录后即特权视图，留空即可`;
}

$('#f-vendor').addEventListener('change', () => applyVendorDefaults(true));
$('#btn-fill-default-cmds').addEventListener('click', () => {
  applyVendorDefaults(true);
});

function renderStats() {
  const total = state.devices.length;
  const ok = state.devices.filter((d) => d.last_status === 'success').length;
  const err = state.devices.filter((d) => d.last_status === 'failed').length;
  const sch = state.devices.filter((d) => d.schedule_enabled && d.enabled).length;
  const unassigned = state.devices.filter((d) => !d.site_id).length;
  const online = state.devices.filter((d) => d.online === 1).length;
  const offline = state.devices.filter((d) => d.online === 0).length;
  $('#stats').innerHTML = `
    <div class="stat"><div class="label">设备总数</div><div class="value">${total}</div></div>
    <div class="stat ok"><div class="label">在线 / 离线</div><div class="value">${online}<span style="font-size:15px;color:var(--err)"> / ${offline}</span></div></div>
    <div class="stat brand"><div class="label">门店数</div><div class="value">${state.sites.length}</div></div>
    <div class="stat err"><div class="label">最近失败</div><div class="value">${err}<span style="font-size:14px;color:var(--text-mute)"> / ${ok}</span></div></div>
    <div class="stat"><div class="label">定时/未归属</div><div class="value">${sch}<span style="font-size:15px;color:var(--text-mute)"> / ${unassigned}</span></div></div>`;
}

function renderDevices(filter = '') {
  const kw = filter.trim().toLowerCase();
  const siteFilter = $('#device-site-filter').value;
  const onFilter = $('#device-online-filter').value;
  const rows = state.devices.filter((d) => {
    if (siteFilter === 'none' && d.site_id) return false;
    if (siteFilter && siteFilter !== 'none' && String(d.site_id) !== siteFilter) return false;
    if (onFilter === '1' && d.online !== 1) return false;
    if (onFilter === '0' && d.online !== 0) return false;
    if (onFilter === 'unk' && (d.online === 1 || d.online === 0)) return false;
    if (!kw) return true;
    return [d.name, d.host, d.site, d.note, vendorOf(d)]
      .some((x) => (x || '').toLowerCase().includes(kw));
  });
  const tb = $('#device-table tbody');
  $('#device-empty').classList.toggle('hidden', rows.length > 0);
  $('#device-table').classList.toggle('hidden', rows.length === 0);
  if (!rows.length) {
    $('#device-empty').textContent = state.devices.length
      ? '当前筛选条件下没有设备。'
      : '还没有设备，点击「新增设备」或「导入」批量添加。';
  }
  tb.innerHTML = rows
    .map((d) => {
      const cmds = (d.commands || []).slice(0, 2).join('<br>');
      const more = (d.commands || []).length > 2 ? `<br><span class="pill">+${d.commands.length - 2}</span>` : '';
      const sched = d.schedule_enabled && d.enabled
        ? `<span class="pill ssh" title="${esc(d.cron)}">${esc(d.cron)}</span>`
        : '<span class="pill">未启用</span>';
      const siteCell = d.site_id
        ? `<span class="pill ok">${esc(d.site)}</span>`
        : '<span class="pill">未归属</span>';
      const backupCell = d.last_backup_at
        ? `<div style="max-width:150px" title="${esc(d.last_message || '')}">
             <div>${fmtTime(d.last_backup_at).slice(5)}</div>
             <div>${statusPill(d)}</div></div>`
        : '<span class="pill">未备份</span>';
      return `<tr data-id="${d.id}">
        <td>${onlineDot(d)}</td>
        <td><div class="dev-name">${esc(d.name)}</div><div class="dev-host">${esc(d.host)}</div></td>
        <td>${vendorPill(d)}</td>
        <td>${siteCell}</td>
        <td><span class="pill ${d.protocol}">${d.protocol.toUpperCase()}</span>
            <div class="dev-host">:${d.port}</div></td>
        <td class="mono">${cmds}${more}</td>
        <td>${sched}</td>
        <td>${backupCell}</td>
        <td><div class="actions">
          <button class="btn small primary act-backup">备份</button>
          <button class="btn small act-test">测试</button>
          <button class="btn small act-edit">编辑</button>
          <button class="btn small act-pkg" title="打包下载该设备">打包</button>
          <button class="btn small danger act-del">删除</button>
        </div></td></tr>`;
    })
    .join('');
}

/* ------------------------------------------------------------- 事件委托 */
$('#device-table').addEventListener('click', async (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  const id = Number(e.target.closest('tr').dataset.id);
  const dev = state.devices.find((d) => d.id === id);
  if (btn.classList.contains('act-backup')) {
    btn.disabled = true; btn.textContent = '备份中…';
    try {
      const r = await api(`/api/devices/${id}/backup`, { method: 'POST' });
      if (r.status === 'success')
        toast(`${dev.name} 备份成功${r.changed ? '（配置有变化）' : '（配置无变化）'}`, 'ok');
      else toast(`${dev.name} 备份失败：${r.error}`, 'err');
    } catch (err) { toast(err.message, 'err'); }
    await refreshAll();
    btn.disabled = false; btn.textContent = '备份';
  } else if (btn.classList.contains('act-test')) {
    btn.disabled = true; btn.textContent = '测试…';
    try {
      const r = await api(`/api/devices/${id}/test`, { method: 'POST' });
      toast(r.ok ? `连接成功 (${r.elapsed_ms}ms) 提示符: ${r.prompt}` : `连接失败：${r.message}`, r.ok ? 'ok' : 'err');
    } catch (err) { toast(err.message, 'err'); }
    btn.disabled = false; btn.textContent = '测试';
  } else if (btn.classList.contains('act-edit')) {
    if (!Object.keys(state.vendors).length) await loadVendors();
    openDeviceModal(dev);
  } else if (btn.classList.contains('act-pkg')) {
    downloadPackage({ scope: 'device', device_ids: [id] }, `正在打包 ${dev.name}…`);
  } else if (btn.classList.contains('act-del')) {
    if (!confirm(`确定删除设备「${dev.name}」及其全部备份历史？`)) return;
    try { await api(`/api/devices/${id}`, { method: 'DELETE' }); toast('已删除', 'ok'); await refreshAll(); }
    catch (err) { toast(err.message, 'err'); }
  }
});

/* ------------------------------------------------------------- 在线监控 */
async function loadProbe() {
  let info;
  try {
    info = await api('/api/probe/status');
  } catch (e) {
    $('#probe-stats').innerHTML = '<div class="stat"><div class="label">加载失败</div><div class="value" style="font-size:15px">—</div></div>';
    return;
  }
  const devices = state.devices;
  const byId = Object.fromEntries(devices.map((d) => [d.id, d]));
  $('#probe-stats').innerHTML = `
    <div class="stat ok"><div class="label">在线设备</div><div class="value">${info.online}</div></div>
    <div class="stat err"><div class="label">离线设备</div><div class="value">${info.offline}</div></div>
    <div class="stat"><div class="label">未探测</div><div class="value">${info.unknown}</div></div>
    <div class="stat brand"><div class="label">探测间隔</div><div class="value">${info.probe_interval_minutes}<span style="font-size:14px">分钟</span></div></div>
    <div class="stat"><div class="label">探测状态</div><div class="value" style="font-size:17px">${info.probe_enabled ? '已启用' : '已关闭'}</div></div>`;

  $('#probe-next').textContent = info.probe_enabled
    ? `每 ${info.probe_interval_minutes} 分钟自动探测一次` +
      (info.next_probe_at ? ` · 下次 ${fmtTime(info.next_probe_at).slice(5)}` : '')
    : '定时探测已关闭，可在「设置」里开启';

  const rows = devices.slice().sort((a, b) => {
    if (a.online !== b.online) return (a.online === 0 ? -1 : 1);
    return a.name.localeCompare(b.name);
  });
  const tb = $('#probe-table tbody');
  $('#probe-empty').classList.toggle('hidden', rows.length > 0);
  $('#probe-table').classList.toggle('hidden', rows.length === 0);
  tb.innerHTML = rows.map((d) => {
    let lat = '<span class="latency">—</span>';
    if (d.probe_latency_ms != null && d.online === 1) {
      const ms = d.probe_latency_ms;
      const cls = ms > 1000 ? 'bad' : ms > 300 ? 'slow' : '';
      lat = `<span class="latency ${cls}">${ms} ms</span>`;
    }
    const fails = d.consecutive_fails > 0
      ? `<span class="pill err">${d.consecutive_fails} 次</span>`
      : '<span class="pill">0</span>';
    return `<tr>
      <td>${onlineDot(d)}</td>
      <td><div class="dev-name">${esc(d.name)}</div></td>
      <td>${d.site ? `<span class="pill">${esc(d.site)}</span>` : '<span class="pill">未归属</span>'}</td>
      <td class="mono">${esc(d.protocol)}://${esc(d.host)}:${d.port}</td>
      <td>${lat}</td>
      <td class="mono">${d.last_seen_at ? fmtTime(d.last_seen_at).slice(5) : '从未'}</td>
      <td>${fails}</td>
      <td style="max-width:220px;font-size:12px;color:var(--text-mute)">${esc((d.last_message || '').slice(0, 60))}</td>
    </tr>`;
  }).join('');
}

async function runProbeAll(btn) {
  const label = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '探测中…'; }
  try {
    const r = await api('/api/probe/run', {
      method: 'POST',
      body: JSON.stringify({}),
    });
    await refreshAll();
    await loadProbe();
    toast(`探测完成：${r.online}/${r.total} 在线`, r.offline ? 'err' : 'ok');
  } catch (e) {
    toast('探测失败：' + e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label || '立即探测全部'; }
  }
}

$('#btn-probe-all').addEventListener('click', (e) => runProbeAll(e.target));
$('#btn-probe-now').addEventListener('click', (e) => runProbeAll(e.target));
$('#btn-probe-refresh').addEventListener('click', loadProbe);

/* ------------------------------------------------------------- 批量导入 */
function openImportModal() {
  state.importFile = null;
  const fileInput = $('#import-file');
  if (fileInput) fileInput.value = '';
  const fname = $('#drop-filename');
  if (fname) { fname.classList.add('hidden'); fname.textContent = ''; }
  ['#import-result', '#import-msg'].forEach((s) => {
    const el = $(s);
    if (el) el.classList.add('hidden');
  });
  const go = $('#btn-do-import');
  if (go) go.disabled = true;
  $('#import-modal').classList.remove('hidden');
}

$('#btn-import').addEventListener('click', openImportModal);

/** 选中文件后的统一处理 */
function pickImportFile(file) {
  if (!file) return;
  state.importFile = file;
  const el = $('#drop-filename');
  el.textContent = `${file.name}（${(file.size / 1024).toFixed(1)} KB）`;
  el.classList.remove('hidden');
  $('#btn-do-import').disabled = false;
  $('#import-result').classList.add('hidden');
  $('#import-msg').classList.add('hidden');
}

$('#drop-zone').addEventListener('click', () => $('#import-file').click());

$('#import-file').addEventListener('change', (e) => pickImportFile(e.target.files[0]));

['dragenter', 'dragover'].forEach((ev) =>
  $('#drop-zone').addEventListener(ev, (e) => {
    e.preventDefault();
    $('#drop-zone').classList.add('drag');
  })
);
['dragleave', 'drop'].forEach((ev) =>
  $('#drop-zone').addEventListener(ev, (e) => {
    e.preventDefault();
    $('#drop-zone').classList.remove('drag');
  })
);
$('#drop-zone').addEventListener('drop', (e) => {
  e.preventDefault();
  pickImportFile(e.dataTransfer.files[0]);
});

$('#btn-download-template').addEventListener('click', () => {
  window.location.href = '/api/import/template.xlsx';
});

$('#btn-download-template-csv').addEventListener('click', () => {
  window.location.href = '/api/import/template';
});

$('#btn-do-import').addEventListener('click', async (e) => {
  if (!state.importFile) return;
  const btn = e.target;
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '导入中…';
  const mode = document.querySelector('input[name="import-mode"]:checked').value;
  const defaultSite = $('#import-site').value.trim();
  try {
    const fd = new FormData();
    fd.append('file', state.importFile);
    const res = await fetch(
      `/api/import/devices?params=${encodeURIComponent(mode)}` +
      `&default_site=${encodeURIComponent(defaultSite)}`,
      { method: 'POST', body: fd }
    );
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);

    const s = data.summary;
    renderImportResult(data);
    toast(`导入完成：新增 ${s.created}、更新 ${s.updated}、跳过 ${s.skipped}、失败 ${s.failed}`,
      s.failed ? 'err' : 'ok');
    await refreshAll();
    await loadProbe();
    btn.textContent = '重新导入';
  } catch (err) {
    showMsg('#import-msg', err.message, 'err');
    btn.disabled = false;
    btn.textContent = label;
  }
});

function renderImportResult(data) {
  const s = data.summary;
  const box = $('#import-result');
  const label = { created: '新增', updated: '更新', skipped: '跳过', failed: '失败' };
  const cls = { created: 'ok', updated: 'upd', skipped: 'skip', failed: 'fail' };
  const rows = data.details.map((d) => `
    <tr class="${cls[d.status] || ''}">
      <td>行 ${d.row}</td>
      <td>${esc(d.name)}</td>
      <td>${label[d.status] || d.status}</td>
      <td>${esc(d.message)}</td>
    </tr>`).join('');
  box.innerHTML = `
    <div class="result-head">
      共 ${s.total} 行 · 新增 ${s.created} · 更新 ${s.updated} ·
      跳过 ${s.skipped} · <span style="color:var(--err)">失败 ${s.failed}</span>
    </div>
    <div class="result-body"><table>${rows}</table></div>`;
  box.classList.remove('hidden');
}

/* ------------------------------------------------------------- 门店管理 */
async function loadSites() {
  state.sites = await api('/api/sites');
  renderSites();
  fillSiteSelects();
}

function renderSites() {
  const grid = $('#site-grid');
  $('#site-empty').classList.toggle('hidden', state.sites.length > 0);
  if (!state.sites.length) {
    grid.innerHTML = '';
    return;
  }
  grid.innerHTML = state.sites.map((s) => `
    <div class="site-card" data-id="${s.id}">
      <div class="site-head">
        <h4>${esc(s.name)}${s.code ? `<span class="site-code">${esc(s.code)}</span>` : ''}</h4>
        <div class="actions">
          <button class="btn small act-site-edit">编辑</button>
          <button class="btn small danger act-site-del">删除</button>
        </div>
      </div>
      ${s.region || s.address ? `<div class="dev-host">${esc([s.region, s.address].filter(Boolean).join(' · '))}</div>` : ''}
      <div class="site-meta">
        <div><b>${s.device_count}</b>设备</div>
        <div class="ok"><b>${s.ok_count}</b>正常</div>
        <div class="err"><b>${s.failed_count}</b>异常</div>
        <div><b style="font-size:13px">${s.last_backup_at ? fmtTime(s.last_backup_at).slice(0, 16) : '—'}</b>最近备份</div>
      </div>
      <div class="site-foot">
        <button class="btn small primary act-site-backup">备份本店</button>
        <button class="btn small act-site-pkg">打包下载</button>
        <button class="btn small ghost act-site-view">查看设备</button>
      </div>
    </div>`).join('');
}

$('#site-grid').addEventListener('click', async (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  const card = e.target.closest('.site-card');
  const id = Number(card.dataset.id);
  const site = state.sites.find((s) => s.id === id);

  if (btn.classList.contains('act-site-edit')) {
    openSiteModal(site);
  } else if (btn.classList.contains('act-site-pkg')) {
    downloadPackage({ scope: 'site', site_id: id }, `正在打包「${site.name}」…`);
  } else if (btn.classList.contains('act-site-view')) {
    $('#device-site-filter').value = String(id);
    renderDevices();
    $$('.tab').find((t) => t.dataset.tab === 'devices').click();
  } else if (btn.classList.contains('act-site-backup')) {
    btn.disabled = true; btn.textContent = '提交中…';
    try {
      const r = await api(`/api/sites/${id}/backup`, { method: 'POST' });
      toast(`已提交「${site.name}」${r.queued} 台设备，正在后台备份…`, 'ok');
      pollBackups();
    } catch (err) { toast(err.message, 'err'); }
    btn.disabled = false; btn.textContent = '备份本店';
  } else if (btn.classList.contains('act-site-del')) {
    const others = state.sites.filter((s) => s.id !== id);
    const msg = site.device_count
      ? `门店「${site.name}」下有 ${site.device_count} 台设备。\n\n` +
        `点击「确定」把设备迁移到「${others.length ? esc(others[0].name) : '（无，设备将变为未归属）'}」；` +
        `如需直接删除请在弹窗中调整。\n\n确定继续吗？`
      : `确定删除门店「${site.name}」？`;
    if (!confirm(msg)) return;
    const reassign = others.length ? others[0].id : '';
    try {
      const r = await api(`/api/sites/${id}?reassign_to=${reassign}`, { method: 'DELETE' });
      toast(`门店已删除，${r.moved_devices} 台设备已迁移`, 'ok');
      await refreshAll();
    } catch (err) { toast(err.message, 'err'); }
  }
});

function openSiteModal(site) {
  state.editingSiteId = site ? site.id : null;
  $('#site-modal-title').textContent = site ? `编辑门店 · ${site.name}` : '新增门店';
  $('#fs-name').value = site ? site.name : '';
  $('#fs-code').value = site ? site.code : '';
  $('#fs-region').value = site ? site.region : '';
  $('#fs-address').value = site ? site.address : '';
  $('#fs-note').value = site ? site.note : '';
  $('#fs-sort').value = site ? site.sort_order : 100;
  $('#site-msg').classList.add('hidden');
  $('#site-modal').classList.remove('hidden');
}

$('#btn-add-site').addEventListener('click', () => openSiteModal(null));

$('#btn-save-site').addEventListener('click', async () => {
  const payload = {
    name: $('#fs-name').value.trim(),
    code: $('#fs-code').value.trim(),
    region: $('#fs-region').value.trim(),
    address: $('#fs-address').value.trim(),
    note: $('#fs-note').value.trim(),
    sort_order: Number($('#fs-sort').value) || 100,
  };
  if (!payload.name) return showMsg('#site-msg', '门店名称必填', 'err');
  try {
    if (state.editingSiteId) {
      await api(`/api/sites/${state.editingSiteId}`, { method: 'PATCH', body: JSON.stringify(payload) });
    } else {
      await api('/api/sites', { method: 'POST', body: JSON.stringify(payload) });
    }
    closeModals();
    toast('门店已保存', 'ok');
    await refreshAll();
  } catch (err) { showMsg('#site-msg', err.message, 'err'); }
});

function fillSiteSelects() {
  const opts = state.sites.map((s) => `<option value="${s.id}">${esc(s.name)}${s.code ? ' (' + esc(s.code) + ')' : ''}</option>`).join('');
  $('#f-site-id').innerHTML = '<option value="">未归属</option>' + opts;
  $('#e-site').innerHTML = '<option value="">请选择门店</option>' + opts;
  const cur = $('#device-site-filter').value;
  $('#device-site-filter').innerHTML =
    '<option value="">全部门店</option>' + opts + '<option value="none">仅未归属设备</option>';
  $('#device-site-filter').value = cur;
}

/* ------------------------------------------------------------- 打包下载 */
async function downloadPackage(payload, pendingMsg) {
  if (pendingMsg) toast(pendingMsg);
  try {
    const res = await fetch('/api/export/package', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      let d = res.statusText;
      try { d = (await res.json()).detail || d; } catch (e) { /* ignore */ }
      throw new Error(d);
    }
    const files = res.headers.get('X-Export-Files') || '?';
    const devices = res.headers.get('X-Export-Devices') || '?';
    const blob = await res.blob();
    if (blob.size < 100) throw new Error('没有可导出的备份文件，请先执行备份');

    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*=UTF-8''([^;]+)/);
    const name = m ? decodeURIComponent(m[1]) : 'cisco_backup.zip';
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`打包完成：${devices} 台设备 / ${files} 个文件（${(blob.size / 1024).toFixed(1)} KB）`, 'ok');
  } catch (err) { toast('打包失败：' + err.message, 'err'); }
}

function collectExportParams() {
  const scope = $('#e-scope').value;
  const payload = {
    scope,
    include_diff: $('#e-diff').checked,
  };
  if (scope === 'site') {
    const sid = $('#e-site').value;
    if (!sid) { toast('请选择门店', 'err'); return null; }
    payload.site_id = Number(sid);
  } else if (scope === 'device') {
    const did = $('#e-device').value;
    if (!did) { toast('请选择设备', 'err'); return null; }
    payload.device_ids = [Number(did)];
  }
  if ($('#e-limit').value) payload.limit_per_device = Number($('#e-limit').value);
  if ($('#e-since').value) payload.since = new Date($('#e-since').value).toISOString();
  if ($('#e-until').value) payload.until = new Date($('#e-until').value).toISOString();
  const cmds = Array.from($('#e-commands').selectedOptions).map((o) => o.value);
  if (cmds.length) payload.commands = cmds;
  return payload;
}

async function refreshExportPreview() {
  const params = collectExportParams();
  const el = $('#e-preview');
  if (!params) { el.textContent = '请先补全范围选择'; return; }
  el.textContent = '统计中…';
  try {
    const qs = new URLSearchParams({ scope: params.scope });
    if (params.site_id) qs.set('site_id', params.site_id);
    if (params.device_ids) qs.set('device_ids', params.device_ids.join(','));
    if (params.limit_per_device) qs.set('limit_per_device', params.limit_per_device);
    const r = await api('/api/export/preview?' + qs.toString());
    el.innerHTML = `预计包含 <b>${r.sites}</b> 个门店 · <b>${r.devices}</b> 台设备 ·
      <b>${r.backups}</b> 次备份 · <b>${r.files}</b> 个文件（约 ${(r.bytes / 1024).toFixed(1)} KB）`;
  } catch (err) {
    el.textContent = '预览失败：' + err.message;
  }
}

$('#btn-export').addEventListener('click', () => {
  const params = collectExportParams();
  if (params) downloadPackage(params, '正在打包，请稍候…');
});
$('#btn-export-refresh').addEventListener('click', refreshExportPreview);
$('#e-scope').addEventListener('change', () => {
  const scope = $('#e-scope').value;
  $('#e-site').closest('label').style.opacity = scope === 'site' ? '1' : '.5';
  $('#e-device').closest('label').style.opacity = scope === 'device' ? '1' : '.5';
  refreshExportPreview();
});
$('#e-site').addEventListener('change', refreshExportPreview);
$('#e-device').addEventListener('change', refreshExportPreview);
$('#btn-download-all').addEventListener('click', () => downloadPackage({ scope: 'all' }, '正在打包全部门店…'));
$('#btn-download-all-top').addEventListener('click', () => downloadPackage({ scope: 'all' }, '正在打包全部门店…'));
$('#device-site-filter').addEventListener('change', (e) => renderDevices($('#device-search').value));
$('#device-online-filter').addEventListener('change', (e) => renderDevices($('#device-search').value));

async function refreshAll() {
  await Promise.all([loadSites(), loadDevices()]);
  loadHistory();
  loadProbe();
}

/* ------------------------------------------------------------- 设备弹窗 */
function openDeviceModal(dev) {
  state.editingId = dev ? dev.id : null;
  $('#device-modal-title').textContent = dev ? `编辑设备 · ${dev.name}` : '新增设备';
  $('#f-name').value = dev ? dev.name : '';
  $('#f-host').value = dev ? dev.host : '';
  $('#f-protocol').value = dev ? dev.protocol : 'ssh';
  $('#f-vendor').value = dev ? (dev.device_type || 'cisco') : 'cisco';

  // 先把编辑态的字段填好，最后再统一套用厂商默认值，
  // 否则后续赋值会把厂商联动刚填好的内容覆盖掉
  $('#f-port').value = dev ? dev.port : '';
  $('#f-username').value = dev ? dev.username : '';
  $('#f-password').value = '';
  $('#f-enable').value = '';
  $('#f-site-id').value = dev && dev.site_id ? String(dev.site_id) : '';
  $('#f-note').value = dev ? dev.note : '';
  $('#f-commands').value = dev ? (dev.commands || []).join('\n') : '';
  $('#f-enabled').checked = dev ? dev.enabled : true;
  $('#f-scheduled').checked = dev ? dev.schedule_enabled : false;
  $('#f-cron').value = dev ? dev.cron : '0 2 * * *';
  $('#device-msg').classList.add('hidden');

  // 新增设备时按厂商填充默认命令；编辑时保留设备自身的命令
  applyVendorDefaults(!dev);
  $('#device-modal').classList.remove('hidden');
}

$('#f-protocol').addEventListener('change', (e) => {
  // 端口为空时按协议补默认值（22 / 23）
  if (!$('#f-port').value) {
    $('#f-port').value = e.target.value === 'telnet' ? 23 : 22;
  }
});

$('#btn-save-device').addEventListener('click', async () => {
  const payload = {
    name: $('#f-name').value.trim(),
    host: $('#f-host').value.trim(),
    protocol: $('#f-protocol').value,
    device_type: $('#f-vendor').value,
    port: Number($('#f-port').value) || ($('#f-protocol').value === 'telnet' ? 23 : 22),
    username: $('#f-username').value.trim(),
    password: $('#f-password').value,
    enable_password: $('#f-enable').value,
    site_id: $('#f-site-id').value ? Number($('#f-site-id').value) : null,
    note: $('#f-note').value.trim(),
    commands: $('#f-commands').value.split('\n').map((s) => s.trim()).filter(Boolean),
    enabled: $('#f-enabled').checked,
    schedule_enabled: $('#f-scheduled').checked,
    cron: $('#f-cron').value.trim() || '0 2 * * *',
  };
  if (!payload.name || !payload.host) {
    return showMsg('#device-msg', '设备名称和管理 IP 必填', 'err');
  }
  if (!payload.commands.length) return showMsg('#device-msg', '至少配置一条采集命令', 'err');
  try {
    if (state.editingId) {
      if (!payload.password) { delete payload.password; delete payload.enable_password; }
      await api(`/api/devices/${state.editingId}`, { method: 'PATCH', body: JSON.stringify(payload) });
    } else {
      if (!payload.password) return showMsg('#device-msg', '新增设备必须填写登录密码', 'err');
      await api('/api/devices', { method: 'POST', body: JSON.stringify(payload) });
    }
    closeModals();
    toast('保存成功', 'ok');
    await refreshAll();
  } catch (err) { showMsg('#device-msg', err.message, 'err'); }
});

function showMsg(sel, text, kind) {
  const el = $(sel);
  el.textContent = text;
  el.className = 'modal-msg ' + kind;
  el.classList.remove('hidden');
}

/* ----------------------------------------------------------------- 历史 */
async function loadHistory() {
  const did = $('#history-device').value;
  const rows = await api(`/api/backups?limit=200${did ? `&device_id=${did}` : ''}`);
  const tb = $('#history-table tbody');
  $('#history-empty').classList.toggle('hidden', rows.length > 0);
  $('#history-table').classList.toggle('hidden', rows.length === 0);
  tb.innerHTML = rows
    .map((b) => {
      const res = b.status === 'success'
        ? '<span class="pill ok">成功</span>'
        : `<span class="pill err" title="${esc(b.error || '')}">失败</span>`;
      return `<tr>
        <td class="mono">${fmtTime(b.started_at)}</td>
        <td><div class="dev-name">${esc(b.device_name)}</div><div class="dev-host">${esc(b.host)}</div></td>
        <td><span class="pill">${b.trigger === 'schedule' ? '定时' : '手动'}</span></td>
        <td>${res}</td>
        <td>${b.status === 'success' ? (b.changed ? '<span class="pill new">有变化</span>' : '<span class="pill">无变化</span>') : '—'}</td>
        <td class="mono">${b.duration_ms != null ? b.duration_ms + ' ms' : '—'}</td>
        <td><button class="btn small act-view" data-id="${b.id}">详情</button></td>
      </tr>`;
    })
    .join('');
}

$('#history-table').addEventListener('click', async (e) => {
  const btn = e.target.closest('.act-view');
  if (!btn) return;
  await openDetail(Number(btn.dataset.id));
});

async function openDetail(id) {
  const b = await api(`/api/backups/${id}`);
  $('#detail-title').textContent = `备份详情 · ${b.device_name} · ${fmtTime(b.started_at)}`;
  $('#btn-download-zip').href = `/api/backup/${b.id}/download`;
  const meta = `
    <div class="detail-meta">
      <div><div class="k">状态</div><div class="v">${b.status === 'success' ? '成功' : '失败'}</div></div>
      <div><div class="k">触发方式</div><div class="v">${b.trigger === 'schedule' ? '定时' : '手动'}</div></div>
      <div><div class="k">耗时</div><div class="v">${b.duration_ms ?? '—'} ms</div></div>
      <div><div class="k">配置变化</div><div class="v">${b.changed ? '有变化' : '无变化'}</div></div>
      ${b.os_version ? `<div><div class="k">IOS 版本</div><div class="v">${esc(b.os_version)}</div></div>` : ''}
    </div>`;
  const err = b.error ? `<div class="modal-msg err">${esc(b.error)}</div>` : '';
  const blocks = [];
  for (const f of b.files) {
    const file = await api(`/api/files/${f.id}`);
    let diffHtml = '';
    if (f.diff) {
      try {
        const d = await api(`/api/files/${f.id}/diff`);
        diffHtml = `<div class="file-head"><span class="cmd">与上一版本的差异 (+${d.added} / -${d.removed})</span></div>
          <pre class="diff">${d.diff_text.split('\n').map((l) => {
            const c = l.startsWith('+++') || l.startsWith('---') || l.startsWith('@@') ? 'h'
              : l.startsWith('+') ? 'a' : l.startsWith('-') ? 'd' : 'c';
            return `<span class="${c}">${esc(l)}</span>`;
          }).join('')}</pre>`;
      } catch (e) { /* 无 diff 记录 */ }
    }
    blocks.push(`<div class="file-block">
      <div class="file-head">
        <span class="cmd">${esc(f.command)}</span>
        <span>${f.size} B · ${f.is_new ? '<span class="pill new">新版本</span>' : '<span class="pill">与上一版本一致</span>'}
        &nbsp;<a class="btn small" href="/api/files/${f.id}/raw" target="_blank">原文</a></span>
      </div>${diffHtml}
      <pre class="config">${esc(file.content)}</pre></div>`);
  }
  $('#detail-body').innerHTML = meta + err + (blocks.join('') || '<div class="empty">没有采集到内容</div>');
  $('#detail-modal').classList.remove('hidden');
}

/* ----------------------------------------------------------------- 邻居 */
async function loadNeighbors() {
  const did = $('#neighbor-device').value;
  const card = $('#neighbor-card');
  if (!did) {
    card.innerHTML = '<div class="empty">选择一个设备查看它的 CDP 邻居。</div>';
    return;
  }
  const data = await api(`/api/devices/${did}/neighbors`);
  if (!data.neighbors.length) {
    card.innerHTML = `<div class="empty">设备「${esc(data.device)}」最近一次备份中没有 CDP 邻居信息。</div>`;
    return;
  }
  card.innerHTML = `<div class="nb-grid">${data.neighbors.map((n) => `
    <div class="nb">
      <h4>${esc(n.device_id) || '(未知设备)'}</h4>
      <div class="row"><span>管理 IP</span><b>${esc(n.ip) || '—'}</b></div>
      <div class="row"><span>平台</span><b>${esc(n.platform) || '—'}</b></div>
      <div class="row"><span>本端接口</span><b>${esc(n.local_interface) || '—'}</b></div>
      <div class="row"><span>对端接口</span><b>${esc(n.remote_interface) || '—'}</b></div>
    </div>`).join('')}</div>`;
}

function fillDeviceSelects() {
  const opts = '<option value="">全部设备</option>' +
    state.devices.map((d) => `<option value="${d.id}">${esc(d.name)}${d.site ? ' · ' + esc(d.site) : ''}</option>`).join('');
  $('#history-device').innerHTML = opts;
  $('#neighbor-device').innerHTML = opts;
  const curExport = $('#e-device').value;
  $('#e-device').innerHTML = '<option value="">请选择设备</option>' +
    state.devices.map((d) => `<option value="${d.id}">${esc(d.site ? d.site + ' / ' : '')}${esc(d.name)}</option>`).join('');
  $('#e-device').value = curExport;
}

/* ----------------------------------------------------------------- 设置 */
async function openSettings() {
  const s = await api('/api/settings');
  state.settings = s;
  $('#s-concurrency').value = s.concurrency;
  $('#s-connect').value = s.connect_timeout;
  $('#s-command').value = s.command_timeout;
  $('#s-keep').value = s.keep_versions;
  $('#s-paging').checked = s.paging_fix;
  $('#s-probe').checked = s.probe_enabled !== false;
  $('#s-probe-interval').value = s.probe_interval_minutes || 5;
  $('#s-probe-timeout').value = s.probe_timeout || 5;
  $('#s-commands').value = (s.default_commands || []).join('\n');
  $('#settings-msg').classList.add('hidden');
  $('#settings-modal').classList.remove('hidden');
}

$('#btn-save-settings').addEventListener('click', async () => {
  try {
    await api('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({
        concurrency: Number($('#s-concurrency').value),
        connect_timeout: Number($('#s-connect').value),
        command_timeout: Number($('#s-command').value),
        keep_versions: Number($('#s-keep').value),
        paging_fix: $('#s-paging').checked,
        probe_enabled: $('#s-probe').checked,
        probe_interval_minutes: Number($('#s-probe-interval').value) || 5,
        probe_timeout: Number($('#s-probe-timeout').value) || 5,
        default_commands: $('#s-commands').value.split('\n').map((s) => s.trim()).filter(Boolean),
      }),
    });
    closeModals(); toast('设置已保存', 'ok');
  } catch (err) { showMsg('#settings-msg', err.message, 'err'); }
});

/* ------------------------------------------------------------- 全局交互 */
$('#btn-add').addEventListener('click', async () => {
  // 厂商清单尚未就绪时先等待，避免打开弹窗时命令没按厂商填充
  if (!Object.keys(state.vendors).length) await loadVendors();
  openDeviceModal(null);
});
$('#btn-settings').addEventListener('click', openSettings);
$('#btn-refresh-history').addEventListener('click', loadHistory);
$('#btn-backup-all').addEventListener('click', async () => {
  if (!state.devices.length) return toast('请先添加设备', 'err');
  if (!confirm(`开始对 ${state.devices.length} 台设备执行备份？`)) return;
  try {
    const r = await api('/api/backups', { method: 'POST', body: JSON.stringify({}) });
    toast(`已提交 ${r.queued} 台设备，正在后台采集…`, 'ok');
    pollBackups();
  } catch (err) { toast(err.message, 'err'); }
});
$('#device-search').addEventListener('input', (e) => renderDevices(e.target.value));
$('#history-device').addEventListener('change', loadHistory);
$('#neighbor-device').addEventListener('change', loadNeighbors);

$$('.tab').forEach((t) => t.addEventListener('click', () => {
  $$('.tab').forEach((x) => x.classList.remove('active'));
  $$('.tab-panel').forEach((x) => x.classList.remove('active'));
  t.classList.add('active');
  $('#tab-' + t.dataset.tab).classList.add('active');
}));

function closeModals() { $$('.modal').forEach((m) => m.classList.add('hidden')); }
$$('[data-close]').forEach((b) => b.addEventListener('click', closeModals));
$$('.modal').forEach((m) => m.addEventListener('click', (e) => { if (e.target === m) closeModals(); }));
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModals(); });

function pollBackups() {
  clearInterval(state.polling);
  state.polling = setInterval(async () => {
    try {
      const st = await api('/api/status');
      if (!st.running.length) {
        clearInterval(state.polling);
        await refreshAll();
        toast('批量备份完成', 'ok');
      }
    } catch (e) { clearInterval(state.polling); }
  }, 2000);
}

loadMe();
loadVendors();
loadSites();
loadDevices();
loadHistory();
loadProbe();
// 页面可见时每 30 秒静默刷新一次设备状态
setInterval(() => {
  if (!document.hidden && $('#detail-modal').classList.contains('hidden')) refreshAll();
}, 30000);