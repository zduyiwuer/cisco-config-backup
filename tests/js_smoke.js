/**
 * 在 Node 里用最小 DOM 桩执行 app.js，抓出真实的运行时错误。
 * 重点验证：导入弹窗相关的绑定能否正常挂载。
 */
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
const js = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');

// 收集 HTML 中所有 id
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));

const listeners = new Map();
const errors = [];

function makeEl(id) {
  const el = {
    id,
    className: '',
    textContent: '',
    innerHTML: '',
    value: '',
    checked: false,
    disabled: false,
    style: {},
    dataset: {},
    files: [],
    closestSelector: null,
    classList: {
      add: (...c) => {},
      remove: (...c) => {},
      toggle: () => {},
      contains: () => false,
    },
    addEventListener: (ev, fn) => {
      if (!listeners.has(id)) listeners.set(id, {});
      listeners.get(id)[ev] = fn;
    },
    removeEventListener: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
    appendChild: () => {},
    remove: () => {},
    setAttribute: () => {},
    getAttribute: () => null,
    focus: () => {},
    click() {},
    reset() {},
  };
  return el;
}

const elements = new Map();
for (const id of ids) elements.set(id, makeEl(id));

global.document = {
  querySelector: (sel) => {
    const m = sel.match(/^#(.+)$/);
    if (m) {
      if (!elements.has(m[1])) {
        errors.push(`querySelector 未找到元素: ${sel}`);
      }
      return elements.get(m[1]) || null;
    }
    return null;
  },
  querySelectorAll: (sel) => {
    const m = sel.match(/^\.(.+)$/);
    if (m) {
      // 匹配 class
      const out = [];
      for (const [, attrs] of html.matchAll(/class="([^"]*)"/g)) {
        if (attrs.split(/\s+/).includes(m[1])) {
          out.push(makeEl('class-' + m[1]));
        }
      }
      return out;
    }
    return [];
  },
  createElement: () => makeEl('created'),
  body: { appendChild: () => {} },
  addEventListener: () => {},
  hidden: false,
};
global.window = { location: { href: '' } };
global.fetch = async () => ({ ok: true, json: async () => ({}), text: async () => '', headers: { get: () => null } });
global.FormData = class { append() {} };
global.Blob = class {};
global.URL = { createObjectURL: () => '', revokeObjectURL: () => {} };
global.setInterval = () => {};
global.setTimeout = (fn) => { try { fn(); } catch (e) { errors.push('setTimeout 回调: ' + e.message); } return 0; };
global.clearTimeout = () => {};
global.confirm = () => true;
global.alert = () => {};

try {
  new Function(js)();
  console.log('脚本执行完成（无顶层异常）');
} catch (e) {
  console.log('顶层异常:', e.message, '\n', e.stack.split('\n').slice(0, 4).join('\n'));
}

console.log('\n捕获错误:', errors.length ? errors : '无');
console.log('\n已注册监听的元素:', [...listeners.keys()].sort().join(', '));

// 关键校验：导入相关绑定是否挂上
const need = ['btn-import', 'drop-zone', 'import-file', 'btn-do-import',
  'btn-download-template', 'btn-download-template-csv'];
const missingBind = need.filter((id) => !(listeners.get(id) && (listeners.get(id).click || listeners.get(id).change)));
console.log('\n导入相关绑定:', missingBind.length ? '❌ 缺失 ' + missingBind : '✅ 全部挂载');

// 校验 JS 引用的每个 id 都真实存在于 HTML（静态防御）
const allIds = new Set([...html.matchAll(/\$\('#([a-zA-Z0-9_-]+)'\)/g)].map((m) => m[1]));
const missingIds = [...allIds].filter((id) => !ids.has(id));
console.log('JS 引用但不存在的元素:', missingIds.length ? '❌ ' + missingIds : '✅ 无');

// 模拟点击导入按钮
const importFn = listeners.get('btn-import') && listeners.get('btn-import').click;
if (importFn) {
  try {
    importFn();
    console.log('点击「导入」按钮: ✅ 无异常');
  } catch (e) {
    console.log('点击「导入」按钮: ❌', e.message);
  }
}