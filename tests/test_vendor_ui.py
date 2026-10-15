"""厂商联动回归测试。

重点防两类问题：
1. 后端静态资源版本号未按 mtime 注入，导致浏览器加载旧 JS（表现为联动失效）
2. 前端初始化顺序错误，覆盖掉厂商刚填好的命令
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = 0, 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


EXPECTED = {
    "cisco": ["show running-config", "show cdp neighbors detail"],
    "h3c": ["display current-configuration", "display lldp neighbor-information"],
    "huawei": ["display current-configuration", "display lldp neighbor brief"],
    "ruijie": ["show running-config", "show lldp neighbors"],
}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cisco_vendor_ui_"))
    from app import crypto, db, vendors

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.clear()
    db.init_db()
    db.update_settings({"auth_enabled": False})

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        print("\n=== 1. 应用名称已改为通用名===")
        r = client.get("/")
        check("首页可访问", r.status_code == 200)
        check("标题为多厂商网络设备配置备份中心", "<title>多厂商网络设备配置备份中心</title>" in r.text, r.text[:200])
        check("页面 H1 为多厂商网络设备配置备份中心", "<h1>多厂商网络设备配置备份中心</h1>" in r.text)
        check("标题不含厂商前缀", "Cisco 配置备份中心" not in r.text)
        check("副标题体现多厂商", "多厂商" in r.text)
        check("OpenAPI 标题已更新",
              client.get("/openapi.json").json()["info"]["title"] == "多厂商网络设备配置备份中心")

        print("\n=== 2. 静态资源版本号自动注入 ===")
        r = client.get("/")
        m_js = re.search(r"/static/app\.js\?v=(\d+)", r.text)
        m_css = re.search(r"/static/style\.css\?v=(\d+)", r.text)
        check("app.js 带版本号", m_js is not None, r.text[-300:])
        check("style.css 带版本号", m_css is not None)
        if m_js:
            expected = int((ROOT / "app/static/app.js").stat().st_mtime)
            check("版本号等于 app.js 的 mtime",
                  int(m_js.group(1)) == expected,
                  f"{m_js.group(1)} != {expected}")
        if m_css:
            expected = int((ROOT / "app/static/style.css").stat().st_mtime)
            check("版本号等于 style.css 的 mtime", int(m_css.group(1)) == expected)
        check("HTML 源码里没有手写版本号",
              "?v=2026" not in r.text, "仍有硬编码版本号")

        r2 = client.get("/login")
        check("登录页也注入版本号",
              re.search(r"/static/login\.js\?v=\d+", r2.text) is not None)
        check("登录页标题已改名", "登录 · 多厂商网络设备配置备份中心" in r2.text)

        print("\n=== 3. 前端联动逻辑静态检查 ===")
        js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        check("存在 applyVendorDefaults", "function applyVendorDefaults" in js)
        check("厂商下拉绑定 change事件",
              re.search(r"#f-vendor'\)\.addEventListener\('change'", js) is not None)
        check("存在厂商兜底数据", "VENDOR_FALLBACK" in js)
        check("兜底数据覆盖四家厂商",
              all(v in js for v in EXPECTED), "兜底数据不全")
        check("兜底命令与后端一致",
              all(
                  all(cmd in js for cmd in EXPECTED[v]) for v in EXPECTED
              ))
        # 初始化顺序：openDeviceModal 里 applyVendorDefaults 必须在字段赋值之后
        start = js.index("function openDeviceModal")
        # 用大括号配平截取函数体，避免依赖下一个标记的位置
        depth, i = 0, js.index("{", start)
        body_start = i
        while i < len(js):
            if js[i] == "{":
                depth += 1
            elif js[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        modal = js[body_start:i]
        check("openDeviceModal 里存在厂商套用", "applyVendorDefaults" in modal)
        check("命令字段赋值存在", "$('#f-commands').value" in modal)
        if "$('#f-commands').value" in modal and "applyVendorDefaults" in modal:
            last_cmds = modal.rindex("$('#f-commands').value")
            last_apply = modal.rindex("applyVendorDefaults")
            check("命令赋值在厂商套用之前（顺序正确）",
                  last_cmds < last_apply,
                  f"cmds@{last_cmds} apply@{last_apply}")
            # 初值应为空，交给 applyVendorDefaults 按厂商填充
            cmds_line = modal[last_cmds: modal.index("\n", last_cmds)]
            check("命令初值为空而非硬编码 Cisco",
                  ": ''" in cmds_line, cmds_line.strip()[:90])
        check("新增按钮等待厂商就绪", "await loadVendors()" in js)

        print("\n=== 4. 厂商接口供前端联动 ===")
        r = client.get("/api/vendors")
        check("接口可用", r.status_code == 200)
        items = {v["id"]: v for v in r.json()}
        check("返回 4 家厂商", len(items) == 4, str(list(items)))
        for vid, cmds in EXPECTED.items():
            check(f"{vid} 默认命令与前端一致",
                  items[vid]["default_commands"] == cmds,
                  str(items[vid]["default_commands"]))
            check(f"{vid} 兜底数据与接口一致",
                  items[vid]["default_commands"] == vendors.default_commands_for(vid))
        check("仅 cisco 需要 enable",
              items["cisco"]["needs_enable"] is True
              and all(not items[v]["needs_enable"] for v in ("h3c", "huawei", "ruijie")))

        print("\n=== 5. 新建设备按厂商落库 ===")
        for vid, cmds in EXPECTED.items():
            r = client.post("/api/devices", json={
                "name": f"ui-{vid}", "host": f"10.30.0.{list(EXPECTED).index(vid) + 1}",
                "protocol": "ssh", "username": "admin", "password": "pw",
                "device_type": vid,
            })
            if r.status_code != 201:
                check(f"{vid} 建建设备", False, r.text[:120])
                continue
            d = r.json()
            check(f"{vid} 厂商正确", d["device_type"] == vid, d["device_type"])
            check(f"{vid} 命令正确", d["commands"] == cmds, str(d["commands"]))
            check(f"{vid} 端口为 22", d["port"] == 22)

        print("\n=== 6. 编辑设备保留自身命令 ===")
        dev = db.get_device_by_name("ui-h3c")
        db.update_device(dev["id"], {
            "commands": ["display current-configuration", "display interface brief"]
        })
        r = client.patch(f"/api/devices/{dev['id']}", json={"site": "测试门店"})
        check("改门店不影响命令",
              r.status_code == 200
              and r.json()["commands"] == ["display current-configuration", "display interface brief"],
              str(r.json()["commands"]))
        r = client.patch(f"/api/devices/{dev['id']}", json={"device_type": "huawei"})
        check("自定义命令不被厂商切换覆盖",
              r.status_code == 200
              and r.json()["commands"] == ["display current-configuration", "display interface brief"],
              str(r.json()["commands"]))
        check("但厂商本身已更新", r.json()["device_type"] == "huawei")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())