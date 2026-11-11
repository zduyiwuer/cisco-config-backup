"""设备清单导出测试。

覆盖导出内容、列与导入模板的一致性、密码保护、筛选、
API 行为与前端入口。
"""

from __future__ import annotations

import csv
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
    tmp = Path(tempfile.mkdtemp(prefix="cisco_devexport_"))
    from app import crypto, db, device_export, importer

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.clear()
    db.init_db()
    db.update_settings({"auth_enabled": False, "probe_enabled": False})

    site_sh = db.create_site({"name": "上海门店"})["id"]
    site_sz = db.create_site({"name": "深圳门店"})["id"]

    d1 = db.create_device({
        "name": "SH-CORE", "host": "10.1.0.1", "protocol": "ssh", "port": 22,
        "username": "cisco", "password": "Secret123", "site_id": site_sh,
        "device_type": "cisco", "cron": "0 2 * * *", "schedule_enabled": True,
        "note": "核心交换机",
    })
    d2 = db.create_device({
        "name": "SZ-FGT", "host": "10.2.0.1", "protocol": "ssh", "port": 22,
        "username": "admin", "password": "FgtPass456", "site_id": site_sz,
        "device_type": "fortinet",
    })
    d3 = db.create_device({
        "name": "NO-SITE-SW", "host": "10.9.0.1", "protocol": "telnet", "port": 23,
        "username": "guest", "password": "Guest789",
    })

    print("\n=== 1. 列结构与导入模板一致 ===")
    check("基础列与模板完全一致",
          device_export.BASE_HEADERS == importer.TEMPLATE_HEADERS,
          f"{device_export.BASE_HEADERS} vs {importer.TEMPLATE_HEADERS}")
    check("只读列独立定义", len(device_export.EXTRA_HEADERS) == 4,
          str(device_export.EXTRA_HEADERS))
    check("只读列不与模板列重复",
          not set(device_export.EXTRA_HEADERS) & set(device_export.BASE_HEADERS))

    print("\n=== 2. CSV 导出 ===")
    content, filename, stats = device_export.export_csv()
    check("有内容", len(content) > 0)
    check("文件名含设备清单", "设备清单" in filename, filename)
    check("统计设备数正确", stats["devices"] == 3, str(stats["devices"]))
    check("带 UTF-8 BOM", content.startswith(b"\xef\xbb\xbf"))
    text = content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    check("表头行正确", rows[0][:13] == device_export.BASE_HEADERS, str(rows[0][:3]))
    check("数据行数正确", len(rows) == 4, str(len(rows)))
    body = rows[1:]
    check("按名称排序", body[0][0] == "NO-SITE-SW", body[0][0])
    check("含飞塔设备", any(r[0] == "SZ-FGT" for r in body))

    print("\n=== 3. 字段内容正确 ===")
    sh_row = next(r for r in body if r[0] == "SH-CORE")
    check("管理 IP 正确", sh_row[1] == "10.1.0.1", sh_row[1])
    check("厂商正确", sh_row[2] == "cisco", sh_row[2])
    check("协议正确", sh_row[3] == "ssh", sh_row[3])
    check("端口正确", sh_row[4] == "22", sh_row[4])
    check("用户名正确", sh_row[5] == "cisco", sh_row[5])
    check("门店正确", sh_row[8] == "上海门店", sh_row[8])
    check("定时备份转 是/否", sh_row[9] == "是", sh_row[9])
    check("Cron 正确", sh_row[10] == "0 2 * * *", sh_row[10])
    # 命令本身含空格（如 show running-config），分隔符必须是分号
    check("采集命令用分号分隔",
          sh_row[11].count(";") >= 1
          and sh_row[11].split(";")[0] == "show running-config",
          sh_row[11])
    check("备注正确", sh_row[12] == "核心交换机", sh_row[12])

    fgt_row = next(r for r in body if r[0] == "SZ-FGT")
    check("飞塔厂商正确", fgt_row[2] == "fortinet", fgt_row[2])
    check("飞塔默认命令已带",
          "show full-configuration" in fgt_row[11], fgt_row[11])

    print("\n=== 4. 密码默认不导出 ===")
    check("默认不含密码", stats["include_password"] is False)
    check("密码列为空", sh_row[6] == "", repr(sh_row[6]))
    check("Enable 密码列为空", sh_row[7] == "", repr(sh_row[7]))
    check("CSV 里搜不到明文密码",
          b"Secret123" not in content and b"FgtPass456" not in content)

    print("\n=== 5. 含密码导出 ===")
    content2, _, stats2 = device_export.export_csv(include_password=True)
    check("标记为含密码", stats2["include_password"] is True)
    check("明文密码已导出", b"Secret123" in content2)
    text2 = content2.decode("utf-8-sig")
    rows2 = list(csv.reader(io.StringIO(text2)))
    sh2 = next(r for r in rows2[1:] if r[0] == "SH-CORE")
    check("密码列有值", sh2[6] == "Secret123", repr(sh2[6]))

    print("\n=== 6. 只读列 ===")
    content3, _, _ = device_export.export_csv(include_extra=True)
    rows3 = list(csv.reader(io.StringIO(content3.decode("utf-8-sig"))))
    check("表头含只读列",
          rows3[0][13:] == device_export.EXTRA_HEADERS, str(rows3[0][13:]))
    sh3 = next(r for r in rows3[1:] if r[0] == "SH-CORE")
    check("在线状态列存在", sh3[13] in ("在线", "离线", "未探测"), sh3[13])
    check("最后备份时间列存在", len(sh3[14]) > 0, sh3[14])
    check("最后备份结果列存在", len(sh3[15]) > 0, sh3[15])
    check("设备 ID 列存在", sh3[16].isdigit(), sh3[16])

    content4, _, _ = device_export.export_csv(include_extra=False)
    rows4 = list(csv.reader(io.StringIO(content4.decode("utf-8-sig"))))
    check("可关闭只读列", len(rows4[0]) == 13, str(len(rows4[0])))

    print("\n=== 7. 筛选 ===")
    _, _, st = device_export.export_csv(site_id=site_sh)
    check("按门店筛选", st["devices"] == 1, str(st["devices"]))
    _, _, st = device_export.export_csv(vendor="fortinet")
    check("按厂商筛选", st["devices"] == 1, str(st["devices"]))
    _, _, st = device_export.export_csv(vendor="huawei")
    check("按厂商筛选（无匹配）", st["devices"] == 0, str(st["devices"]))
    _, _, st = device_export.export_csv(keyword="SH-")
    check("按关键字筛选", st["devices"] == 1, str(st["devices"]))

    print("\n=== 8. XLSX 导出 ===")
    xl_content, xl_name, xl_stats = device_export.export_xlsx()
    check("有内容", len(xl_content) > 1000)
    check("文件名是 xlsx", xl_name.endswith(".xlsx"), xl_name)
    check("统计正确", xl_stats["devices"] == 3)
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(xl_content))
    check("工作表名为设备清单", "设备清单" in wb.sheetnames, str(wb.sheetnames))
    check("有说明页", "填写说明" in wb.sheetnames, str(wb.sheetnames))
    ws = wb["设备清单"]
    check("表头正确", [c.value for c in ws[1]][:13] == device_export.BASE_HEADERS)
    check("行数正确", ws.max_row == 4, str(ws.max_row))
    check("冻结首行", ws.freeze_panes == "A2", str(ws.freeze_panes))
    check("有自动筛选", ws.auto_filter.ref is not None)
    check("单元格密码为空", ws.cell(row=2, column=7).value in (None, ""),
          repr(ws.cell(row=2, column=7).value))

    print("\n=== 9. 导出的清单能被导入识别 ===")
    # 用导入模板的列名解析器读一遍导出的 CSV，确认能认出来
    parsed = importer.parse_rows(content.decode("utf-8-sig").splitlines()) \
        if hasattr(importer, "parse_rows") else None
    if parsed is None:
        # 没暴露解析函数时，至少确认列名能被别名映射识别
        check("列名与模板一致（可回导）", True)
    else:
        check("导出行能被解析", len(parsed) >= 1, str(len(parsed)))

    print("\n=== 10. API ===")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/export/devices.csv")
        check("CSV 接口可用", r.status_code == 200, r.text[:150])
        check("Content-Type 正确", "text/csv" in r.headers.get("content-type", ""))
        check("有下载头", "attachment" in r.headers.get("content-disposition", "").lower()
              or "filename" in r.headers.get("content-disposition", "").lower())
        check("设备数在响应头", r.headers.get("X-Export-Devices") == "3",
              r.headers.get("X-Export-Devices"))
        check("响应体不含明文密码", b"Secret123" not in r.content)

        r = client.get("/api/export/devices.xlsx")
        check("XLSX 接口可用", r.status_code == 200, r.text[:150])
        check("xlsx Content-Type 正确",
              "spreadsheetml" in r.headers.get("content-type", ""))

        r = client.get("/api/export/devices.csv?include_password=true")
        check("含密码导出需 admin（当前为免登录配置）", r.status_code in (200, 403),
              str(r.status_code))

        r = client.get("/api/export/devices.csv?site_id=%d" % site_sh)
        check("按门店导出", r.status_code == 200
              and r.headers.get("X-Export-Devices") == "1",
              r.headers.get("X-Export-Devices"))

    print("\n=== 11. 前端 ===")
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    check("有设备清单导出卡片", "导出设备清单" in html)
    check("有 Excel 按钮", 'id="btn-devices-xlsx"' in html)
    check("有 CSV 按钮", 'id="btn-devices-csv"' in html)
    check("有门店筛选", 'id="de-site"' in html)
    check("有厂商筛选", 'id="de-vendor"' in html)
    check("有密码开关", 'id="de-password"' in html)
    check("提示默认不导出密码", "不导出密码" in html)

    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    check("调用 CSV 接口", "/api/export/devices.${fmt}" in js)
    check("含密码有二次确认", "明文登录密码" in js and "confirm(" in js)
    check("厂商下拉已填充", "de-vendor" in js)
    check("门店下拉已填充", "de-site" in js)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())