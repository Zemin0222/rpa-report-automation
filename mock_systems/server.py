from __future__ import annotations

import csv
import hashlib
import io
import os
import time
import uuid
from datetime import date
from typing import Any
from fastapi import FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
import pymupdf
import uvicorn

app = FastAPI(title="RPA 演示模拟业务后台 (CRM + 广告投放 + 财务结算 + OA审批)")

CRM_USER = os.getenv("CRM_USERNAME", "ops_admin")
CRM_PASS = os.getenv("CRM_PASSWORD", "rpa_demo_pass")
ADS_USER = os.getenv("ADS_USERNAME", "ads_manager")
ADS_PASS = os.getenv("ADS_PASSWORD", "rpa_ads_pass")
FINANCE_USER = os.getenv("FINANCE_USERNAME", "fin_admin")
FINANCE_PASS = os.getenv("FINANCE_PASSWORD", "rpa_fin_pass")
OA_USER = os.getenv("OA_USERNAME", "oa_staff")
OA_PASS = os.getenv("OA_PASSWORD", "rpa_oa_pass")
EXPENSE_USER = os.getenv("EXPENSE_USERNAME", "fin_staff")
EXPENSE_PASS = os.getenv("EXPENSE_PASSWORD", "rpa_exp_pass")

_EXPORT_TASKS: dict[str, dict[str, Any]] = {}
_OA_SUBMISSIONS: dict[str, dict[str, Any]] = {}
_EXPENSE_SUBMISSIONS: dict[str, dict[str, Any]] = {}

CHANNELS = ["抖音信息流", "百度搜索", "小红书种草", "微信公众号", "自然流量"]


def _seed_factor(biz_date: str, salt: str) -> float:
    digest = hashlib.md5(f"{biz_date}:{salt}".encode("utf-8")).hexdigest()
    return 0.82 + (int(digest[:4], 16) % 38) / 100.0


def generate_crm_orders(biz_date: str) -> list[dict[str, Any]]:
    """Generate deterministic daily CRM orders across channels for a given date."""
    rows: list[dict[str, Any]] = []
    order_seq = 1001
    channel_specs = [
        ("抖音信息流", 14, 260.0, 0.08),
        ("百度搜索", 10, 340.0, 0.05),
        ("小红书种草", 6, 195.0, 0.22),  # 高退款率 + 偏低 ROI，用于演示异常告警
        ("微信公众号", 8, 410.0, 0.03),
        ("自然流量", 11, 290.0, 0.02),
    ]
    for channel, count, base_price, refund_ratio in channel_specs:
        factor = _seed_factor(biz_date, channel)
        actual_count = max(3, int(round(count * factor)))
        for idx in range(actual_count):
            amount = round(base_price * (0.75 + ((idx * 17) % 55) / 100.0), 2)
            is_refund = (idx / max(actual_count, 1)) < refund_ratio
            refund_amt = round(amount, 2) if is_refund else 0.0
            rows.append(
                {
                    "order_id": f"ORD-{biz_date.replace('-', '')}-{order_seq}",
                    "biz_date": biz_date,
                    "channel": channel,
                    "customer_tier": ["新客", "复购", "VIP"][idx % 3],
                    "order_amount": amount,
                    "refund_amount": refund_amt,
                    "order_status": "已退款" if is_refund else "已成交",
                }
            )
            order_seq += 1
    return rows


def generate_ads_campaigns(biz_date: str) -> list[dict[str, Any]]:
    """Generate deterministic multi-page ad campaign rows for a given date."""
    specs = [
        ("抖音信息流", "抖音-短视频引流计划A", 68000, 2150, 1280.0),
        ("抖音信息流", "抖音-直播间加热计划B", 42000, 1340, 820.0),
        ("百度搜索", "百度-品牌词精确投放", 24000, 1680, 960.0),
        ("百度搜索", "百度-行业长尾词计划", 31000, 1120, 690.0),
        ("小红书种草", "小红书-达人笔记推广", 52000, 980, 1450.0),  # 高消耗低产出，触发 ROI 告警
        ("小红书种草", "小红书-信息流测试组", 18000, 310, 480.0),
        ("微信公众号", "公众号-底部互选广告", 26000, 840, 620.0),
        ("微信公众号", "公众号-老客触达推文", 12000, 520, 240.0),
    ]
    rows: list[dict[str, Any]] = []
    for idx, (channel, campaign, impr, clicks, spend) in enumerate(specs, start=1):
        factor = _seed_factor(biz_date, campaign)
        rows.append(
            {
                "campaign_id": f"CMP-{idx:03d}",
                "biz_date": biz_date,
                "channel": channel,
                "campaign_name": campaign,
                "impressions": int(impr * factor),
                "clicks": int(clicks * factor),
                "ad_spend": round(spend * factor, 2),
            }
        )
    return rows


_BASE_STYLE = """
<style>
  :root {
    --bg: #F5F4F0;
    --rail: #ECEAE4;
    --surface: #FDFCFA;
    --subtle: #F0EEE8;
    --ink: #14181F;
    --muted: #5A6270;
    --border: #DCD9D0;
    --border-strong: #C5C1B5;
    --accent: #0F52BA;
    --accent-hover: #0A3F91;
    --danger: #9E1C1C;
    --ok: #0B5D3B;
  }
  ::selection { background: #D6E4FF; color: #08306E; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif;
    line-height: 1.5;
    font-size: 14px;
    -webkit-font-smoothing: antialiased;
  }
  .topbar {
    background: var(--rail);
    border-bottom: 1px solid var(--border);
    color: var(--ink);
    padding: 13px 28px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .topbar h1 { margin: 0; font-size: 16px; font-weight: 700; letter-spacing: -0.01em; }
  .topbar span { color: var(--muted); font-size: 13px; }
  .topbar a { color: var(--accent); text-decoration: none; font-size: 13px; margin-left: 16px; }
  .container { max-width: 1120px; margin: 28px auto; padding: 0 20px; }
  .panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 22px 24px;
  }
  .login-box { max-width: 400px; margin: 72px auto; }
  label { display: block; font-size: 12.5px; font-weight: 600; margin-bottom: 6px; color: var(--muted); }
  input[type="text"], input[type="password"], input[type="date"] {
    width: 100%;
    padding: 8px 11px;
    border: 1px solid var(--border-strong);
    border-radius: 5px;
    font-size: 13.5px;
    background: #FFFFFF;
    color: var(--ink);
    margin-bottom: 14px;
  }
  input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .btn {
    display: inline-block;
    background: var(--accent);
    color: #FDFCFA;
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 8px 16px;
    font-size: 13.5px;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    transition: background 150ms cubic-bezier(0.22, 1, 0.36, 1);
  }
  .btn:hover { background: var(--accent-hover); }
  .btn-secondary {
    background: var(--surface);
    color: var(--ink);
    border: 1px solid var(--border-strong);
  }
  .btn-secondary:hover { background: var(--subtle); }
  .toolbar {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 16px;
    margin-bottom: 18px;
    flex-wrap: wrap;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 10px 12px; border-bottom: 1px solid var(--border); text-align: left; }
  th { background: var(--subtle); color: var(--muted); font-weight: 600; font-size: 12px; }
  tbody tr:hover { background: #F7F5F0; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .error { color: var(--danger); background: #FCE8E8; border: 1px solid #F5BABA; padding: 8px 12px; border-radius: 5px; font-size: 13px; margin-bottom: 12px; }
  .pager { margin-top: 16px; display: flex; gap: 10px; align-items: center; }
</style>
"""


def _is_crm_authed(request: Request) -> bool:
    return request.cookies.get("crm_session") == "valid_crm_token"


def _is_ads_authed(request: Request) -> bool:
    return request.cookies.get("ads_session") == "valid_ads_token"


def _is_finance_authed(request: Request) -> bool:
    return request.cookies.get("finance_session") == "valid_finance_token"


def _is_oa_authed(request: Request) -> bool:
    return request.cookies.get("oa_session") == "valid_oa_token"


def _is_expense_authed(request: Request) -> bool:
    return request.cookies.get("expense_session") == "valid_expense_token"


def generate_finance_settlements(biz_date: str) -> list[dict[str, Any]]:
    """Derive deterministic payment settlements from CRM orders with intentional discrepancies."""
    orders = generate_crm_orders(biz_date)
    settled: list[dict[str, Any]] = []
    for idx, ord_row in enumerate(orders):
        net = round(float(ord_row["order_amount"]) - float(ord_row["refund_amount"]), 2)
        if net <= 0:
            continue
        # Intentionally omit 1 valid order to simulate missing settlement (漏单)
        if idx == 4:
            continue
        settled_amt = net
        status = "SETTLED"
        # Intentionally introduce 2 short-payment discrepancies (短款/异常扣费)
        if idx == 1:
            settled_amt = round(max(0.0, net - 18.50), 2)
            status = "FEE_ADJUSTED"
        elif idx == 9:
            settled_amt = round(max(0.0, net - 12.00), 2)
            status = "DISPUTED"
        settled.append(
            {
                "order_id": ord_row["order_id"],
                "biz_date": biz_date,
                "settled_amount": settled_amt,
                "gateway_fee": round(settled_amt * 0.006, 2),
                "status": status,
            }
        )

    # Intentionally add 1 extra unmatched settlement (长款/未关联订单)
    settled.append(
        {
            "order_id": f"ORD-{biz_date.replace('-', '')}-9999",
            "biz_date": biz_date,
            "settled_amount": 328.00,
            "gateway_fee": 1.97,
            "status": "UNMATCHED",
        }
    )
    return settled


def _build_settlement_pdf(biz_date: str, rows: list[dict[str, Any]]) -> bytes:
    """Render a multi-page PDF statement with crisp table gridlines for pdfplumber parsing."""
    doc = pymupdf.open()
    headers = ["order_id", "biz_date", "settled_amount", "gateway_fee", "status"]
    x_edges = [40, 185, 275, 375, 455, 550]
    row_height = 22
    rows_per_page = 28

    for start in range(0, max(1, len(rows)), rows_per_page):
        chunk = rows[start : start + rows_per_page]
        page = doc.new_page(width=595, height=842)
        page.insert_text(
            (40, 38),
            f"Finance Settlement Statement - {biz_date} (Page {start // rows_per_page + 1})",
            fontsize=12,
        )
        all_rows = [headers] + [
            [
                str(r["order_id"]),
                str(r["biz_date"]),
                f"{float(r['settled_amount']):.2f}",
                f"{float(r['gateway_fee']):.2f}",
                str(r["status"]),
            ]
            for r in chunk
        ]
        y0 = 56
        for r_idx, r_vals in enumerate(all_rows):
            y = y0 + r_idx * row_height
            for c_idx, val in enumerate(r_vals):
                page.insert_text((x_edges[c_idx] + 5, y + 15), val, fontsize=8.5)
        for r_idx in range(len(all_rows) + 1):
            y = y0 + r_idx * row_height
            page.draw_line((x_edges[0], y), (x_edges[-1], y))
        for x in x_edges:
            page.draw_line((x, y0), (x, y0 + len(all_rows) * row_height))

    return doc.tobytes()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return f"""
    <html><head><title>RPA 演示靶场入口</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar"><h1>RPA 模拟业务后台靶场</h1></div>
      <div class="container">
        <div class="panel">
          <h2>可用模拟业务系统</h2>
          <p><a class="btn" href="/crm/login">进入 CRM 销售订单后台</a>
             <a class="btn btn-secondary" href="/ads/login">进入广告投放后台</a>
             <a class="btn btn-secondary" href="/finance/login">进入财务结算后台 (SPA)</a>
             <a class="btn btn-secondary" href="/oa/login">进入 OA 日报审批系统</a></p>
        </div>
      </div>
    </body></html>
    """


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/crm/login", response_class=HTMLResponse)
def crm_login_page(error: str = "") -> str:
    err_html = f'<div class="error">{error}</div>' if error else ""
    return f"""
    <html><head><title>CRM 登录</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar"><h1>CRM 销售订单系统</h1></div>
      <div class="container login-box">
        <div class="panel">
          <h2>账号登录</h2>
          {err_html}
          <form method="post" action="/crm/login">
            <label for="username">用户名</label>
            <input id="username" name="username" type="text" required />
            <label for="password">密码</label>
            <input id="password" name="password" type="password" required />
            <button id="crm-login-btn" class="btn" type="submit">登录系统</button>
          </form>
        </div>
      </div>
    </body></html>
    """


@app.post("/crm/login")
def crm_login_submit(username: str = Form(...), password: str = Form(...)) -> Response:
    if username == CRM_USER and password == CRM_PASS:
        resp = RedirectResponse(url="/crm/orders", status_code=303)
        resp.set_cookie(key="crm_session", value="valid_crm_token", httponly=True)
        return resp
    return RedirectResponse(url="/crm/login?error=用户名或密码错误", status_code=303)


@app.get("/crm/orders", response_class=HTMLResponse)
def crm_orders_page(request: Request, date_str: str = Query("", alias="date")) -> Response:
    if not _is_crm_authed(request):
        return RedirectResponse(url="/crm/login", status_code=303)

    biz_date = date_str or date.today().isoformat()
    orders = generate_crm_orders(biz_date)
    rows_html = "".join(
        f"<tr>"
        f"<td>{r['order_id']}</td>"
        f"<td>{r['biz_date']}</td>"
        f"<td>{r['channel']}</td>"
        f"<td>{r['customer_tier']}</td>"
        f"<td class='num'>{r['order_amount']:.2f}</td>"
        f"<td class='num'>{r['refund_amount']:.2f}</td>"
        f"<td>{r['order_status']}</td>"
        f"</tr>"
        for r in orders[:15]
    )
    html = f"""
    <html><head><title>CRM 订单明细</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar">
        <h1>CRM 销售订单后台</h1>
        <div><span>当前用户: {CRM_USER}</span><a href="/ads/orders-link"></a></div>
      </div>
      <div class="container">
        <div class="panel">
          <div class="toolbar">
            <form method="get" action="/crm/orders" style="display:flex;gap:12px;align-items:flex-end;">
              <div>
                <label for="biz-date">业务日期</label>
                <input id="biz-date" name="date" type="date" value="{biz_date}" style="margin-bottom:0;" />
              </div>
              <button id="crm-filter-btn" class="btn btn-secondary" type="submit">查询日期</button>
            </form>
            <a id="export-csv-btn" class="btn" href="/crm/export?date={biz_date}">导出当日订单 CSV</a>
          </div>
          <p style="color:#5b6577;font-size:13px;">共 {len(orders)} 条订单记录（页面展示前 15 条，导出包含全量）。</p>
          <table id="crm-orders-table">
            <thead>
              <tr>
                <th>订单号</th><th>日期</th><th>来源渠道</th><th>客户层级</th>
                <th class="num">订单金额</th><th class="num">退款金额</th><th>状态</th>
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>
      </div>
    </body></html>
    """
    return HTMLResponse(html)


@app.get("/crm/export")
def crm_export_csv(request: Request, date_str: str = Query("", alias="date")) -> Response:
    if not _is_crm_authed(request):
        return RedirectResponse(url="/crm/login", status_code=303)

    biz_date = date_str or date.today().isoformat()
    orders = generate_crm_orders(biz_date)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=[
            "order_id",
            "biz_date",
            "channel",
            "customer_tier",
            "order_amount",
            "refund_amount",
            "order_status",
        ],
    )
    writer.writeheader()
    writer.writerows(orders)
    csv_bytes = buf.getvalue().encode("utf-8-sig")
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="crm_orders_{biz_date}.csv"'},
    )


@app.get("/ads/login", response_class=HTMLResponse)
def ads_login_page(error: str = "") -> str:
    err_html = f'<div class="error">{error}</div>' if error else ""
    return f"""
    <html><head><title>广告投放后台登录</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar"><h1>广告投放数据后台</h1></div>
      <div class="container login-box">
        <div class="panel">
          <h2>投放管理员登录</h2>
          {err_html}
          <form method="post" action="/ads/login">
            <label for="username">账号</label>
            <input id="username" name="username" type="text" required />
            <label for="password">密码</label>
            <input id="password" name="password" type="password" required />
            <button id="ads-login-btn" class="btn" type="submit">登录后台</button>
          </form>
        </div>
      </div>
    </body></html>
    """


@app.post("/ads/login")
def ads_login_submit(username: str = Form(...), password: str = Form(...)) -> Response:
    if username == ADS_USER and password == ADS_PASS:
        resp = RedirectResponse(url="/ads/campaigns", status_code=303)
        resp.set_cookie(key="ads_session", value="valid_ads_token", httponly=True)
        return resp
    return RedirectResponse(url="/ads/login?error=账号或密码错误", status_code=303)


@app.get("/ads/campaigns", response_class=HTMLResponse)
def ads_campaigns_page(
    request: Request,
    date_str: str = Query("", alias="date"),
    page: int = Query(1, ge=1),
) -> Response:
    if not _is_ads_authed(request):
        return RedirectResponse(url="/ads/login", status_code=303)

    biz_date = date_str or date.today().isoformat()
    all_rows = generate_ads_campaigns(biz_date)
    page_size = 4
    total_pages = max(1, (len(all_rows) + page_size - 1) // page_size)
    page = min(page, total_pages)
    slice_rows = all_rows[(page - 1) * page_size : page * page_size]

    rows_html = "".join(
        f"<tr>"
        f"<td>{r['campaign_id']}</td>"
        f"<td>{r['biz_date']}</td>"
        f"<td>{r['channel']}</td>"
        f"<td>{r['campaign_name']}</td>"
        f"<td class='num'>{r['impressions']}</td>"
        f"<td class='num'>{r['clicks']}</td>"
        f"<td class='num'>{r['ad_spend']:.2f}</td>"
        f"</tr>"
        for r in slice_rows
    )

    next_link = (
        f'<a id="next-page" class="btn btn-secondary" href="/ads/campaigns?date={biz_date}&page={page + 1}">下一页</a>'
        if page < total_pages
        else '<span id="no-more-pages" style="color:#5b6577;font-size:13px;">已到末页</span>'
    )

    html = f"""
    <html><head><title>广告投放消耗明细</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar">
        <h1>广告投放数据后台</h1>
        <div><span>当前账号: {ADS_USER}</span></div>
      </div>
      <div class="container">
        <div class="panel">
          <div class="toolbar">
            <form method="get" action="/ads/campaigns" style="display:flex;gap:12px;align-items:flex-end;">
              <div>
                <label for="ads-date">投放日期</label>
                <input id="ads-date" name="date" type="date" value="{biz_date}" style="margin-bottom:0;" />
              </div>
              <button id="ads-filter-btn" class="btn" type="submit">查询投放数据</button>
            </form>
            <div style="font-size:13px;color:#5b6577;">第 <span id="current-page">{page}</span> / <span id="total-pages">{total_pages}</span> 页</div>
          </div>
          <table id="ads-table">
            <thead>
              <tr>
                <th>计划ID</th><th>日期</th><th>渠道</th><th>计划名称</th>
                <th class="num">曝光量</th><th class="num">点击量</th><th class="num">消耗(元)</th>
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
          <div class="pager">
            {next_link}
          </div>
        </div>
      </div>
    </body></html>
    """
    return HTMLResponse(html)


@app.get("/finance/login", response_class=HTMLResponse)
def finance_login_page(error: str = "") -> str:
    err_html = f'<div class="error">{error}</div>' if error else ""
    return f"""
    <html><head><title>财务结算后台登录</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar"><h1>财务支付结算后台 (SPA)</h1></div>
      <div class="container login-box">
        <div class="panel">
          <h2>财务账号登录</h2>
          {err_html}
          <form method="post" action="/finance/login">
            <label for="username">财务账号</label>
            <input id="username" name="username" type="text" required />
            <label for="password">密码</label>
            <input id="password" name="password" type="password" required />
            <button id="finance-login-btn" class="btn" type="submit">登录结算后台</button>
          </form>
        </div>
      </div>
    </body></html>
    """


@app.post("/finance/login")
def finance_login_submit(username: str = Form(...), password: str = Form(...)) -> Response:
    if username == FINANCE_USER and password == FINANCE_PASS:
        resp = RedirectResponse(url="/finance/settlements", status_code=303)
        resp.set_cookie(key="finance_session", value="valid_finance_token", httponly=True)
        return resp
    return RedirectResponse(url="/finance/login?error=财务账号或密码错误", status_code=303)


@app.get("/finance/settlements", response_class=HTMLResponse)
def finance_settlements_page(request: Request, date_str: str = Query("", alias="date")) -> Response:
    if not _is_finance_authed(request):
        return RedirectResponse(url="/finance/login", status_code=303)

    biz_date = date_str or date.today().isoformat()
    html = f"""
    <html><head><title>财务结算明细 (SPA)</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar">
        <h1>财务支付结算后台 (前后端分离 SPA)</h1>
        <div><span>当前财务员: {FINANCE_USER}</span></div>
      </div>
      <div class="container">
        <div class="panel">
          <div class="toolbar">
            <form method="get" action="/finance/settlements" style="display:flex;gap:12px;align-items:flex-end;">
              <div>
                <label for="fin-date">结算日期</label>
                <input id="fin-date" name="date" type="date" value="{biz_date}" style="margin-bottom:0;" />
              </div>
              <button id="fin-filter-btn" class="btn btn-secondary" type="submit">查询结算</button>
            </form>
            <div style="display:flex;gap:10px;align-items:center;">
              <button id="start-export-btn" class="btn" type="button">生成对账单 PDF</button>
              <span id="export-status" style="font-size:13px;color:#5b6577;"></span>
              <a id="download-pdf-btn" class="btn" style="display:none;background:#047857;" href="#">下载 PDF 对账单</a>
            </div>
          </div>
          <p id="spa-summary" style="color:#5b6577;font-size:13px;">正在通过 XHR 接口加载结算数据...</p>
          <table id="finance-table">
            <thead>
              <tr>
                <th>关联订单号</th><th>结算日期</th><th class="num">实收结算金额</th>
                <th class="num">通道手续费</th><th>状态</th>
              </tr>
            </thead>
            <tbody id="finance-tbody"></tbody>
          </table>
        </div>
      </div>
      <script>
        const bizDate = "{biz_date}";
        fetch(`/api/finance/settlements?date=${{encodeURIComponent(bizDate)}}`)
          .then(r => r.json())
          .then(data => {{
            const tbody = document.getElementById("finance-tbody");
            document.getElementById("spa-summary").textContent = `异步接口返回 ${{data.total}} 条结算记录。`;
            tbody.innerHTML = data.items.slice(0, 15).map(item => `
              <tr>
                <td>${{item.order_id}}</td>
                <td>${{item.biz_date}}</td>
                <td class="num">${{Number(item.settled_amount).toFixed(2)}}</td>
                <td class="num">${{Number(item.gateway_fee).toFixed(2)}}</td>
                <td>${{item.status}}</td>
              </tr>
            `).join("");
          }});

        document.getElementById("start-export-btn").addEventListener("click", async () => {{
          const statusEl = document.getElementById("export-status");
          const dlBtn = document.getElementById("download-pdf-btn");
          dlBtn.style.display = "none";
          statusEl.textContent = "导出任务排队生成中...";
          const res = await fetch(`/api/finance/export?date=${{encodeURIComponent(bizDate)}}`, {{ method: "POST" }});
          const created = await res.json();
          const timer = setInterval(async () => {{
            const poll = await fetch(`/api/finance/export/${{created.task_id}}`);
            const state = await poll.json();
            if (state.status === "READY") {{
              clearInterval(timer);
              statusEl.textContent = "PDF 对账单已就绪";
              dlBtn.href = state.download_url;
              dlBtn.style.display = "inline-block";
            }}
          }}, 250);
        }});
      </script>
    </body></html>
    """
    return HTMLResponse(html)


@app.get("/api/finance/settlements")
def api_finance_settlements(request: Request, date_str: str = Query("", alias="date")) -> Response:
    if not _is_finance_authed(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    biz_date = date_str or date.today().isoformat()
    items = generate_finance_settlements(biz_date)
    return JSONResponse({"biz_date": biz_date, "total": len(items), "items": items})


@app.post("/api/finance/export")
def api_finance_export_create(request: Request, date_str: str = Query("", alias="date")) -> Response:
    if not _is_finance_authed(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    biz_date = date_str or date.today().isoformat()
    task_id = uuid.uuid4().hex[:12]
    _EXPORT_TASKS[task_id] = {
        "biz_date": biz_date,
        "ready_at": time.monotonic() + 0.6,
    }
    return JSONResponse({"task_id": task_id, "status": "PENDING"})


@app.get("/api/finance/export/{task_id}")
def api_finance_export_status(request: Request, task_id: str) -> Response:
    if not _is_finance_authed(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    task = _EXPORT_TASKS.get(task_id)
    if not task:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    is_ready = time.monotonic() >= task["ready_at"]
    return JSONResponse(
        {
            "task_id": task_id,
            "status": "READY" if is_ready else "PENDING",
            "download_url": f"/api/finance/export/{task_id}/download" if is_ready else None,
        }
    )


@app.get("/api/finance/export/{task_id}/download")
def api_finance_export_download(request: Request, task_id: str) -> Response:
    if not _is_finance_authed(request):
        return RedirectResponse(url="/finance/login", status_code=303)
    task = _EXPORT_TASKS.get(task_id)
    if not task:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    biz_date = task["biz_date"]
    pdf_bytes = _build_settlement_pdf(biz_date, generate_finance_settlements(biz_date))
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="finance_settlement_{biz_date}.pdf"'},
    )


@app.get("/oa/login", response_class=HTMLResponse)
def oa_login_page(error: str = "") -> str:
    err_html = f'<div class="error">{error}</div>' if error else ""
    return f"""
    <html><head><title>OA 日报审批系统登录</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar"><h1>企业 OA 经营日报审批系统</h1></div>
      <div class="container login-box">
        <div class="panel">
          <h2>员工账号登录</h2>
          {err_html}
          <form method="post" action="/oa/login">
            <label for="username">OA 账号</label>
            <input id="username" name="username" type="text" required />
            <label for="password">密码</label>
            <input id="password" name="password" type="password" required />
            <button id="oa-login-btn" class="btn" type="submit">登录 OA 系统</button>
          </form>
        </div>
      </div>
    </body></html>
    """


@app.post("/oa/login")
def oa_login_submit(username: str = Form(...), password: str = Form(...)) -> Response:
    if username == OA_USER and password == OA_PASS:
        resp = RedirectResponse(url="/oa/submit", status_code=303)
        resp.set_cookie(key="oa_session", value="valid_oa_token", httponly=True)
        return resp
    return RedirectResponse(url="/oa/login?error=OA 账号或密码错误", status_code=303)


@app.get("/oa/submit", response_class=HTMLResponse)
def oa_submit_page(request: Request, date_str: str = Query("", alias="date")) -> Response:
    if not _is_oa_authed(request):
        return RedirectResponse(url="/oa/login", status_code=303)
    biz_date = date_str or date.today().isoformat()
    return HTMLResponse(
        f"""
    <html><head><title>OA 日报与异常报备提交</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar">
        <h1>企业 OA 经营日报与异常报备填报单</h1>
        <div><span>填报人: {OA_USER}</span><a href="/oa/submissions">历史提交记录</a></div>
      </div>
      <div class="container" style="max-width:760px;">
        <div class="panel">
          <h2 style="margin-top:0;">经营日报审批填报</h2>
          <form method="post" action="/oa/submit" enctype="multipart/form-data">
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">
              <div>
                <label for="oa-biz-date">报告业务日期</label>
                <input id="oa-biz-date" name="biz_date" type="date" value="{biz_date}" required />
              </div>
              <div>
                <label for="oa-total-orders">成交总订单数</label>
                <input id="oa-total-orders" name="total_orders" type="text" placeholder="例如: 46" required />
              </div>
              <div>
                <label for="oa-net-sales">全渠道净销售额 (元)</label>
                <input id="oa-net-sales" name="net_sales" type="text" placeholder="例如: 12753.80" required />
              </div>
              <div>
                <label for="oa-overall-roi">整体投产比 ROI</label>
                <input id="oa-overall-roi" name="overall_roi" type="text" placeholder="例如: 1.93" required />
              </div>
            </div>
            <label for="oa-risk-level">风险与异常等级</label>
            <select id="oa-risk-level" name="risk_level" style="width:100%;padding:8px 11px;border:1px solid var(--border-strong);border-radius:5px;margin-bottom:14px;background:#fff;">
              <option value="正常">正常（指标均达标）</option>
              <option value="关注">关注（存在渠道异常或对账差异）</option>
              <option value="高危异常">高危异常（需财务/运营立即介入）</option>
            </select>
            <label for="oa-summary-note">经营摘要与异常对账说明</label>
            <textarea id="oa-summary-note" name="summary_note" rows="5" style="width:100%;padding:9px 11px;border:1px solid var(--border-strong);border-radius:5px;margin-bottom:14px;font-family:inherit;" required></textarea>
            <label for="oa-report-file">上传日报 Excel 附件 (.xlsx)</label>
            <input id="oa-report-file" name="report_file" type="file" accept=".xlsx,.csv" style="margin-bottom:18px;display:block;" required />
            <button id="oa-submit-btn" class="btn" type="submit">提交 OA 日报审批单</button>
          </form>
        </div>
      </div>
    </body></html>
    """
    )


@app.post("/oa/submit")
async def oa_submit_form(
    request: Request,
    biz_date: str = Form(...),
    total_orders: str = Form(...),
    net_sales: str = Form(...),
    overall_roi: str = Form(...),
    risk_level: str = Form(...),
    summary_note: str = Form(...),
    report_file: UploadFile = File(...),
) -> Response:
    if not _is_oa_authed(request):
        return RedirectResponse(url="/oa/login", status_code=303)
    file_bytes = await report_file.read()
    ticket_id = f"OA-{biz_date.replace('-', '')}-{len(_OA_SUBMISSIONS) + 1:03d}"
    _OA_SUBMISSIONS[ticket_id] = {
        "ticket_id": ticket_id,
        "biz_date": biz_date,
        "total_orders": total_orders,
        "net_sales": net_sales,
        "overall_roi": overall_roi,
        "risk_level": risk_level,
        "summary_note": summary_note,
        "filename": report_file.filename or "report.xlsx",
        "file_bytes": len(file_bytes),
    }
    return RedirectResponse(url=f"/oa/receipt/{ticket_id}", status_code=303)


@app.get("/oa/receipt/{ticket_id}", response_class=HTMLResponse)
def oa_receipt_page(request: Request, ticket_id: str) -> Response:
    if not _is_oa_authed(request):
        return RedirectResponse(url="/oa/login", status_code=303)
    rec = _OA_SUBMISSIONS.get(ticket_id)
    if not rec:
        return HTMLResponse("<h3>未找到对应审批单</h3>", status_code=404)
    return HTMLResponse(
        f"""
    <html><head><title>OA 审批回执 {ticket_id}</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar">
        <h1>企业 OA 经营日报审批回执</h1>
        <div><a href="/oa/submit">继续填报</a><a href="/oa/submissions">全部审批记录</a></div>
      </div>
      <div class="container" style="max-width:720px;">
        <div class="panel">
          <div style="display:inline-block;background:#E6F4ED;color:#0B5D3B;border:1px solid #B5DFCA;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;margin-bottom:10px;">审批单已提交成功</div>
          <h2 style="margin:4px 0 14px 0;">单号：<span id="oa-ticket-id">{rec['ticket_id']}</span></h2>
          <table>
            <tbody>
              <tr><th>业务日期</th><td>{rec['biz_date']}</td></tr>
              <tr><th>成交订单数</th><td>{rec['total_orders']} 笔</td></tr>
              <tr><th>净销售额</th><td>¥{rec['net_sales']}</td></tr>
              <tr><th>整体 ROI</th><td>{rec['overall_roi']}</td></tr>
              <tr><th>风险等级</th><td>{rec['risk_level']}</td></tr>
              <tr><th>上传附件</th><td>{rec['filename']} ({rec['file_bytes']} bytes)</td></tr>
              <tr><th>摘要说明</th><td style="white-space:pre-wrap;">{rec['summary_note']}</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </body></html>
    """
    )


@app.get("/oa/submissions", response_class=HTMLResponse)
def oa_submissions_list(request: Request) -> Response:
    if not _is_oa_authed(request):
        return RedirectResponse(url="/oa/login", status_code=303)
    rows = "".join(
        f"<tr><td><a href='/oa/receipt/{r['ticket_id']}'>{r['ticket_id']}</a></td>"
        f"<td>{r['biz_date']}</td><td>{r['total_orders']}</td><td>¥{r['net_sales']}</td>"
        f"<td>{r['overall_roi']}</td><td>{r['risk_level']}</td><td>{r['filename']}</td></tr>"
        for r in reversed(list(_OA_SUBMISSIONS.values()))
    ) or "<tr><td colspan='7'>暂无审批提交记录</td></tr>"
    return HTMLResponse(
        f"""
    <html><head><title>OA 审批单列表</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar"><h1>OA 日报审批记录</h1><div><a href="/oa/submit">新建填报</a></div></div>
      <div class="container"><div class="panel">
        <table><thead><tr><th>审批单号</th><th>日期</th><th>订单数</th><th>净销售额</th><th>ROI</th><th>风险等级</th><th>附件</th></tr></thead><tbody>{rows}</tbody></table>
      </div></div>
    </body></html>
    """
    )


@app.get("/expense/login", response_class=HTMLResponse)
def expense_login_page(error: str = "") -> str:
    err_html = f'<div class="error">{error}</div>' if error else ""
    return f"""
    <html><head><title>企业费用报销系统登录</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar"><h1>企业财务费用报销平台</h1></div>
      <div class="container login-box">
        <div class="panel">
          <h2>员工报销系统登录</h2>
          {err_html}
          <form method="post" action="/expense/login">
            <label for="username">财务/员工账号</label>
            <input id="username" name="username" type="text" required />
            <label for="password">登录密码</label>
            <input id="password" name="password" type="password" required />
            <button id="expense-login-btn" class="btn" type="submit">登录报销平台</button>
          </form>
        </div>
      </div>
    </body></html>
    """


@app.post("/expense/login")
def expense_login_submit(username: str = Form(...), password: str = Form(...)) -> Response:
    if username == EXPENSE_USER and password == EXPENSE_PASS:
        resp = RedirectResponse(url="/expense/submit", status_code=303)
        resp.set_cookie(key="expense_session", value="valid_expense_token", httponly=True)
        return resp
    return RedirectResponse(url="/expense/login?error=报销系统账号或密码错误", status_code=303)


@app.get("/expense/submit", response_class=HTMLResponse)
def expense_submit_page(request: Request, date_str: str = Query("", alias="date")) -> Response:
    if not _is_expense_authed(request):
        return RedirectResponse(url="/expense/login", status_code=303)
    biz_date = date_str or date.today().isoformat()
    return HTMLResponse(
        f"""
    <html><head><title>自动报销填单申请</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar">
        <h1>企业财务费用报销平台 · 费用报销单填报</h1>
        <div><span>当前员工: {EXPENSE_USER}</span><a href="/expense/submissions">历史报销单</a></div>
      </div>
      <div class="container" style="max-width:980px;">
        <div class="panel">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
            <h2 style="margin:0;">新建费用报销申请单</h2>
            <div style="font-size:15px;font-weight:700;color:var(--accent);">申请报销合计：<span id="exp-total-display">¥0.00</span></div>
          </div>
          <form method="post" action="/expense/submit" enctype="multipart/form-data">
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px;">
              <div>
                <label for="exp-biz-date">报销结算日期</label>
                <input id="exp-biz-date" name="biz_date" type="date" value="{biz_date}" required />
              </div>
              <div>
                <label for="exp-applicant">申报人姓名</label>
                <input id="exp-applicant" name="applicant" type="text" value="林小默" required />
              </div>
              <div>
                <label for="exp-department">所属部门 / 成本中心</label>
                <input id="exp-department" name="department" type="text" value="运营交付中心" required />
              </div>
            </div>
            <label for="exp-remark">报销说明与AI合规审查附注</label>
            <textarea id="exp-remark" name="remark" rows="2" style="width:100%;padding:8px 11px;border:1px solid var(--border-strong);border-radius:5px;margin-bottom:16px;font-family:inherit;"></textarea>

            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
              <strong style="font-size:14px;">逐张发票报销明细（已通过风控拦截筛选）</strong>
              <button id="exp-add-row-btn" class="btn btn-secondary" type="button">+ 新增报销明细行</button>
            </div>
            <table>
              <thead>
                <tr>
                  <th style="width:110px;">费用类别</th>
                  <th style="width:140px;">发票号码</th>
                  <th>销售方名称</th>
                  <th style="width:110px;">含税金额(元)</th>
                  <th style="width:220px;">电子发票原件上传</th>
                </tr>
              </thead>
              <tbody id="exp-rows">
                <tr data-row-idx="0">
                  <td>
                    <select id="exp-cat-0" name="categories" style="width:100%;padding:6px 8px;border:1px solid var(--border-strong);border-radius:5px;background:#fff;">
                      <option value="交通">交通</option>
                      <option value="住宿">住宿</option>
                      <option value="餐饮">餐饮</option>
                      <option value="办公">办公</option>
                      <option value="通信">通信</option>
                    </select>
                  </td>
                  <td><input id="exp-inv-0" name="invoice_numbers" type="text" required style="margin-bottom:0;" /></td>
                  <td><input id="exp-vendor-0" name="vendors" type="text" required style="margin-bottom:0;" /></td>
                  <td><input id="exp-amt-0" name="amounts" type="text" class="exp-amt-input" required style="margin-bottom:0;" /></td>
                  <td><input id="exp-file-0" name="files" type="file" accept=".pdf,.png,.jpg,.jpeg" required style="margin-bottom:0;" /></td>
                </tr>
              </tbody>
            </table>
            <div style="margin-top:18px;display:flex;justify-content:flex-end;">
              <button id="exp-submit-btn" class="btn" type="submit">提交报销审批单</button>
            </div>
          </form>
        </div>
      </div>
      <script>
        function recalcExpTotal() {{
          let sum = 0;
          document.querySelectorAll(".exp-amt-input").forEach(el => {{
            const v = parseFloat(el.value || "0");
            if (!isNaN(v)) sum += v;
          }});
          document.getElementById("exp-total-display").textContent = "¥" + sum.toFixed(2);
        }}
        document.addEventListener("input", e => {{
          if (e.target.classList.contains("exp-amt-input")) recalcExpTotal();
        }});
        document.getElementById("exp-add-row-btn").addEventListener("click", () => {{
          const tbody = document.getElementById("exp-rows");
          const idx = tbody.querySelectorAll("tr").length;
          const tr = document.createElement("tr");
          tr.dataset.rowIdx = String(idx);
          tr.innerHTML = `
            <td>
              <select id="exp-cat-${{idx}}" name="categories" style="width:100%;padding:6px 8px;border:1px solid var(--border-strong);border-radius:5px;background:#fff;">
                <option value="交通">交通</option>
                <option value="住宿">住宿</option>
                <option value="餐饮">餐饮</option>
                <option value="办公">办公</option>
                <option value="通信">通信</option>
              </select>
            </td>
            <td><input id="exp-inv-${{idx}}" name="invoice_numbers" type="text" required style="margin-bottom:0;" /></td>
            <td><input id="exp-vendor-${{idx}}" name="vendors" type="text" required style="margin-bottom:0;" /></td>
            <td><input id="exp-amt-${{idx}}" name="amounts" type="text" class="exp-amt-input" required style="margin-bottom:0;" /></td>
            <td><input id="exp-file-${{idx}}" name="files" type="file" accept=".pdf,.png,.jpg,.jpeg" required style="margin-bottom:0;" /></td>
          `;
          tbody.appendChild(tr);
        }});
      </script>
    </body></html>
    """
    )


@app.post("/expense/submit")
async def expense_submit_form(
    request: Request,
    biz_date: str = Form(...),
    applicant: str = Form(...),
    department: str = Form(...),
    remark: str = Form(""),
    categories: list[str] = Form(...),
    invoice_numbers: list[str] = Form(...),
    vendors: list[str] = Form(...),
    amounts: list[str] = Form(...),
    files: list[UploadFile] = File(...),
) -> Response:
    if not _is_expense_authed(request):
        return RedirectResponse(url="/expense/login", status_code=303)

    items: list[dict[str, Any]] = []
    total_amt = 0.0
    for idx in range(len(invoice_numbers)):
        amt_val = float(amounts[idx]) if idx < len(amounts) else 0.0
        total_amt += amt_val
        up = files[idx] if idx < len(files) else None
        raw = await up.read() if up else b""
        items.append(
            {
                "category": categories[idx] if idx < len(categories) else "办公",
                "invoice_number": invoice_numbers[idx],
                "vendor": vendors[idx] if idx < len(vendors) else "-",
                "amount": round(amt_val, 2),
                "filename": up.filename if up else "invoice.pdf",
                "file_bytes": len(raw),
            }
        )

    ticket_id = f"EXP-{biz_date.replace('-', '')}-{len(_EXPENSE_SUBMISSIONS) + 1:03d}"
    _EXPENSE_SUBMISSIONS[ticket_id] = {
        "ticket_id": ticket_id,
        "biz_date": biz_date,
        "applicant": applicant,
        "department": department,
        "remark": remark,
        "total_amount": round(total_amt, 2),
        "items": items,
    }
    return RedirectResponse(url=f"/expense/receipt/{ticket_id}", status_code=303)


@app.get("/expense/receipt/{ticket_id}", response_class=HTMLResponse)
def expense_receipt_page(request: Request, ticket_id: str) -> Response:
    if not _is_expense_authed(request):
        return RedirectResponse(url="/expense/login", status_code=303)
    rec = _EXPENSE_SUBMISSIONS.get(ticket_id)
    if not rec:
        return HTMLResponse("<h3>未找到对应报销单</h3>", status_code=404)
    rows_html = "".join(
        f"<tr><td>{it['category']}</td><td>{it['invoice_number']}</td><td>{it['vendor']}</td>"
        f"<td class='num'>¥{it['amount']:.2f}</td><td>{it['filename']} ({it['file_bytes']} bytes)</td></tr>"
        for it in rec["items"]
    )
    return HTMLResponse(
        f"""
    <html><head><title>报销回执 {ticket_id}</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar">
        <h1>企业财务费用报销平台 · 审批回执</h1>
        <div><a href="/expense/submit">继续报销</a><a href="/expense/submissions">全部报销记录</a></div>
      </div>
      <div class="container" style="max-width:860px;">
        <div class="panel">
          <div style="display:inline-block;background:#E6F4ED;color:#0B5D3B;border:1px solid #B5DFCA;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;margin-bottom:10px;">报销单已提交财务审批</div>
          <h2 style="margin:4px 0 14px 0;">报销单号：<span id="exp-ticket-id">{rec['ticket_id']}</span></h2>
          <p style="color:var(--muted);margin-top:0;">申报人：{rec['applicant']}（{rec['department']}） · 申报日期：{rec['biz_date']} · 申请总额：<strong>¥{rec['total_amount']:.2f}</strong></p>
          <table>
            <thead><tr><th>类别</th><th>发票号码</th><th>销售方</th><th class="num">含税金额</th><th>电子发票附件</th></tr></thead>
            <tbody>{rows_html}</tbody>
          </table>
          <p style="margin-top:14px;font-size:13px;color:var(--muted);">附注：{rec['remark'] or '-'}</p>
        </div>
      </div>
    </body></html>
    """
    )


@app.get("/expense/submissions", response_class=HTMLResponse)
def expense_submissions_list(request: Request) -> Response:
    if not _is_expense_authed(request):
        return RedirectResponse(url="/expense/login", status_code=303)
    rows = "".join(
        f"<tr><td><a href='/expense/receipt/{r['ticket_id']}'>{r['ticket_id']}</a></td>"
        f"<td>{r['biz_date']}</td><td>{r['applicant']}</td><td>{len(r['items'])} 张</td>"
        f"<td class='num'>¥{r['total_amount']:.2f}</td></tr>"
        for r in reversed(list(_EXPENSE_SUBMISSIONS.values()))
    ) or "<tr><td colspan='5'>暂无报销提交记录</td></tr>"
    return HTMLResponse(
        f"""
    <html><head><title>历史报销记录</title>{_BASE_STYLE}</head>
    <body>
      <div class="topbar"><h1>企业财务报销记录</h1><div><a href="/expense/submit">新建报销单</a></div></div>
      <div class="container"><div class="panel">
        <table><thead><tr><th>报销单号</th><th>日期</th><th>申报人</th><th>票据数</th><th class="num">申报总额</th></tr></thead><tbody>{rows}</tbody></table>
      </div></div>
    </body></html>
    """
    )


def run_mock_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    uvicorn.run(app, host=host, port=port, log_level="info")
