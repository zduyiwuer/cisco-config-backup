/* 登录页逻辑 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let mode = 'local';
let adInfo = { ad_enabled: false, allow_password_login: true, ad_domain: '' };

function showError(msg) {
  const el = $('#login-error');
  el.textContent = msg;
  el.classList.remove('hidden');
}

function hideError() {
  $('#login-error').classList.add('hidden');
}

function setMode(next) {
  mode = next;
  $$('.login-tab').forEach((t) => t.classList.toggle('active', t.dataset.mode === next));
  hideError();

  const isAd = next === 'ad';
  $('#label-user').textContent = isAd ? '域账号' : '用户名';
  $('#username').placeholder = isAd
    ? (adInfo.ad_domain ? `如zhangsan@${adInfo.ad_domain}` : '如 domain\\zhangsan')
    : '请输入用户名';
  //域账号允许空密码（域可能是空密码策略），本地账号必须填
  $('#password').required = !isAd;
  $('#login-btn').textContent = isAd ? '域账号登录' : '登 录';
  $('#username').focus();
}

async function init() {
  try {
    const res = await fetch('/api/auth/ad-status');
    adInfo = await res.json();
  } catch (e) {
    // 接口异常时保持本地登录可用
  }

  if (adInfo.ad_enabled && adInfo.allow_password_login) {
    $('#tab-ad').classList.remove('hidden');
  } else if (adInfo.ad_enabled) {
    // 只允许 AD：直接切到域账号模式，隐藏标签
    setMode('ad');
    $('#login-tabs').classList.add('hidden');
  }
  if (!adInfo.allow_password_login) {
    $('#login-tabs').classList.add('hidden');
  }
  if (adInfo.ad_enabled && adInfo.ad_domain) {
    $('#ad-hint').textContent = `域：${adInfo.ad_domain}`;
    if (mode === 'ad') $('#ad-hint').classList.remove('hidden');
  }
  $('#login-foot-text').textContent = adInfo.ad_enabled
    ? '支持域账号与本地账号登录'
    : '交换机配置备份与版本管理';
}

$$('.login-tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    setMode(tab.dataset.mode);
    if (mode === 'ad' && adInfo.ad_domain) $('#ad-hint').classList.remove('hidden');
    else $('#ad-hint').classList.add('hidden');
  });
});

$('#pwd-toggle').addEventListener('click', () => {
  const input = $('#password');
  const isPwd = input.type === 'password';
  input.type = isPwd ? 'text' : 'password';
  $('#pwd-toggle').style.opacity = isPwd ? '1' : '';
  input.focus();
});

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  hideError();

  const username = $('#username').value.trim();
  const password = $('#password').value;
  if (!username) return showError('请输入用户名');
  if (!password && mode === 'local') return showError('请输入密码');

  const btn = $('#login-btn');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '登录中…';

  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, mode }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showError(data.detail || '登录失败，请重试');
      $('#password').value = '';
      $('#password').focus();
      return;
    }
    window.location.href = '/';
  } catch (err) {
    showError('网络错误：' + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
});

init();