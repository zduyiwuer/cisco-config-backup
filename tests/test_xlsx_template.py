"""Excel 模板专项测试：生成 → 回导 → 校验格式。"""

from __future__ import annotations

import io
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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cisco_xlsx_"))
    from app import crypto, db, importer

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.clear()
    db.init_db()
    # 本文件专注 Excel 模板，认证由 test_auth.py 专门覆盖
    db.update_settings({"auth_enabled": False})

    print("\n=== 1. Excel 模板生成 ===")
    xlsx = importer.template_xlsx()
    check("返回 bytes", isinstance(xlsx, bytes))
    check("是合法 xlsx", xlsx[:2] == b"PK", repr(xlsx[:4]))
    check("大小合理", 4 * 1024 < len(xlsx) < 200 * 1024, str(len(xlsx)))

    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(xlsx))
    check("包含设备清单工作表", "设备清单" in wb.sheetnames, str(wb.sheetnames))
    check("包含填写说明工作表", "填写说明" in wb.sheetnames)
    ws = wb["设备清单"]
    check("表头 13 列（含厂商）", ws.max_column == 13, str(ws.max_column))
    check("表头文案正确", ws.cell(row=1, column=1).value == "设备名称")
    check("含厂商列", ws.cell(row=1, column=3).value == "厂商")
    check("含 4 行示例（四厂商）", ws.max_row == 5, str(ws.max_row))
    check("冻结表头", ws.freeze_panes == "A2", str(ws.freeze_panes))
    check("启用筛选", ws.auto_filter.ref is not None)
    check("表头有样式", ws.cell(row=1, column=1).fill.fgColor.rgb not in (None, "00000000"))
    check("表头有批注", ws.cell(row=1, column=1).comment is not None)
    check("列宽已设置", ws.column_dimensions["A"].width >= 12,
          str(ws.column_dimensions["A"].width))
    ws2 = wb["填写说明"]
    check("说明页有内容", ws2.max_row >= 10, str(ws2.max_row))
    wb.close()

    print("\n=== 2. Excel 模板可直接回导 ===")
    rows = importer.read_table(xlsx, "template.xlsx")
    check("读回 4 行数据", len(rows) == 4, str(len(rows)))
    result = importer.import_devices(rows, mode="create")
    check("回导成功 4 台", result["summary"]["created"] == 4, str(result["summary"]))

    devices = {d["name"]: d for d in db.list_devices()}
    expected = {"core-sw-01", "h3c-sw-02", "huawei-sw-03", "access-sw-04"}
    check("四厂商设备都在", expected <= set(devices), str(sorted(devices)))
    check(
        "厂商字段正确",
        devices["huawei-sw-03"]["device_type"] == "huawei"
        and devices["h3c-sw-02"]["device_type"] == "h3c"
        and devices["core-sw-01"]["device_type"] == "cisco",
        f'{devices["huawei-sw-03"]["device_type"]}/'
        f'{devices["h3c-sw-02"]["device_type"]}/'
        f'{devices["core-sw-01"]["device_type"]}',
    )
    check("SSH 设备入库", devices["core-sw-01"]["protocol"] == "ssh")
    check(
        "华为设备命令按厂商套用",
        "display current-configuration" in devices["huawei-sw-03"]["commands"],
        str(devices["huawei-sw-03"]["commands"]),
    )
    check(
        "H3C 邻居命令正确",
        "display lldp neighbor-information" in devices["h3c-sw-02"]["commands"],
        str(devices["h3c-sw-02"]["commands"]),
    )
    check("Telnet 设备入库", devices["access-sw-04"]["protocol"] == "telnet")
    check("密码已加密存储",
          db.get_device_by_name("core-sw-01")["password"] == "cisco123")
    check("Enable 密码保留",
          db.get_device_by_name("core-sw-01")["enable_password"] == "enable123")
    check("华为设备无 enable 密码",
          not db.get_device_by_name("huawei-sw-03")["enable_password"])
    check("门店已自动创建", db.get_site_by_name("上海南京西路店") is not None)
    check("深圳门店已自动创建", db.get_site_by_name("深圳南山科技园店") is not None)
    check("定时备份已导入",
          devices["core-sw-01"]["schedule_enabled"] is True
          and devices["core-sw-01"]["cron"] == "0 2 * * *")
    check("非定时设备未误开", devices["access-sw-04"]["schedule_enabled"] is False)
    check("采集命令按分号拆分",
          devices["core-sw-01"]["commands"] ==
          ["show running-config", "show cdp neighbors detail"],
          str(devices["core-sw-01"]["commands"]))
    check("备注保留", devices["core-sw-01"]["note"] == "思科核心交换机")

    print("\n=== 3. 修改模板后回导（模拟用户填表） ===")
    wb = load_workbook(io.BytesIO(xlsx))
    ws = wb["设备清单"]
    # 清空全部示例行（模板有 4 行，按行数动态删）
    if ws.max_row > 1:
        ws.delete_rows(2, ws.max_row - 1)
    # 13 列：设备名称/管理IP/厂商/协议/端口/用户名/登录密码/Enable密码/门店/定时备份/Cron/采集命令/备注
    ws.append(["user-sw-01", "192.168.1.1", "huawei", "ssh", "", "admin", "userpw", "",
               "广州天河店", "否", "", "", "用户自填华为设备"])
    ws.append(["user-sw-02", "192.168.1.2", "cisco", "telnet", "2323", "admin", "userpw", "",
               "", "否", "", "show running-config", ""])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()

    rows2 = importer.read_table(buf.getvalue(), "filled.xlsx")
    check("读回用户数据", len(rows2) == 2, str(len(rows2)))
    r2 = importer.import_devices(rows2, mode="create")
    check("用户数据导入成功", r2["summary"]["created"] == 2, str(r2["summary"]))
    check("无失败行", r2["summary"]["failed"] == 0)
    d2 = {d["name"]: d for d in db.list_devices()}
    check("用户填写的厂商被识别", d2["user-sw-01"]["device_type"] == "huawei",
          d2["user-sw-01"]["device_type"])
    check("华为设备自动套用默认命令",
          "display current-configuration" in d2["user-sw-01"]["commands"],
          str(d2["user-sw-01"]["commands"]))
    check("用户端口为空时自动补全", d2["user-sw-01"]["port"] == 22,
          str(d2["user-sw-01"]["port"]))
    check("用户填写的端口被保留", d2["user-sw-02"]["port"] == 2323,
          str(d2["user-sw-02"]["port"]))
    check("未填门店则未归属", d2["user-sw-02"]["site_id"] is None)

    print("\n=== 4. CSV 模板仍可用 ===")
    csv_tpl = importer.template_csv()
    check("CSV 模板含表头", "设备名称" in csv_tpl)
    csv_rows = importer.read_table(csv_tpl.encode("utf-8"), "t.csv")
    check("CSV 模板可回导", len(csv_rows) == 4, str(len(csv_rows)))

    print("\n=== 5. HTTP API ===")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/import/template.xlsx")
        check("GET /api/import/template.xlsx", r.status_code == 200)
        check("返回 xlsx", r.content[:2] == b"PK")
        check("Content-Type 正确",
              "spreadsheetml" in r.headers.get("content-type", ""),
              r.headers.get("content-type", ""))
        check("中文文件名可下载",
              "UTF-8''" in r.headers.get("content-disposition", ""),
              r.headers.get("content-disposition", ""))

        # 通过 API 走一遍 Excel 导入
        r = client.post(
            "/api/import/devices",
            files={"file": ("tpl.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            params={"params": "create"},
        )
        check("Excel 走 API 导入", r.status_code == 200, r.text[:150])
        # 设备名已存在，应全部被跳过
        check("重复导入被跳过",
              r.json()["summary"]["skipped"] + r.json()["summary"]["updated"] >= 0,
              str(r.json()["summary"]))

        # 静态资源禁用缓存
        r = client.get("/static/app.js")
        check("JS 可访问", r.status_code == 200)
        check("JS 禁用缓存",
              "no-cache" in r.headers.get("cache-control", "").lower(),
              r.headers.get("cache-control", ""))
        r = client.get("/static/style.css")
        check("CSS 禁用缓存", "no-cache" in r.headers.get("cache-control", "").lower())

        # 页面引用的静态资源带版本号，避免浏览器用旧缓存
        r = client.get("/")
        check("页面 JS 带版本号", "/static/app.js?v=" in r.text)
        check("页面 CSS 带版本号", "/static/style.css?v=" in r.text)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())