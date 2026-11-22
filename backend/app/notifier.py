"""邮件通知模块（系统唯一通知渠道）。

标的池命中主升浪战法 -> SMTP 发送 HTML 邮件提醒。
使用标准库 smtplib / email，无额外 SDK 依赖。
支持 SSL(465) 与 STARTTLS(587) 两种加密方式。
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from .config import NotifierConfig


def _fmt_money(v: Any) -> str:
    """资金额格式化为万/亿。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "--"
    sign = "+" if n > 0 else ""
    if abs(n) >= 1e8:
        return f"{sign}{n / 1e8:.2f}亿"
    if abs(n) >= 1e4:
        return f"{sign}{n / 1e4:.0f}万"
    return f"{sign}{n:.0f}"


def build_email_html(title: str, content: str,
                     signals: list[dict[str, Any]]) -> str:
    """构造主升浪提醒的 HTML 邮件正文，按阶段着色。

    若信号带 volume_ratio/turnover/net_inflow 字段，则自动展示扩展列
    （量比、换手率、主力净流入），兼容不含这些字段的标的池告警。
    """
    extended = any("volume_ratio" in s or "net_inflow" in s for s in signals)
    td = "padding:8px;border:1px solid #e0e0e0"
    rows = ""
    for s in signals[:30]:
        change = s.get("change_pct", 0)
        color = "#c62828" if change >= 0 else "#2e7d32"  # 中国习惯：红涨绿跌
        stage = s.get("stage", "")
        label = s.get("stage_label", "")
        if not label:
            label = "危险信号" if s.get("direction") == "down" else s.get("strategy", "")
        if stage == "trigger":
            label_color, label_bg = "#c62828", "#fdecec"
        elif stage == "watch":
            label_color, label_bg = "#b8860b", "#fdf6e3"
        elif s.get("direction") == "down":
            label_color, label_bg = "#2e7d32", "#edf7ee"
        else:
            label_color, label_bg = "#666", "#f5f5f5"
        extra = ""
        if extended:
            vr = s.get("volume_ratio")
            to = s.get("turnover")
            net = s.get("net_inflow")
            net_color = "#c62828" if (net or 0) >= 0 else "#2e7d32"
            extra = (
                f"<td style='{td};text-align:right'>{vr if vr is not None else '--'}</td>"
                f"<td style='{td};text-align:right'>{f'{to:.2f}%' if to is not None else '--'}</td>"
                f"<td style='{td};text-align:right;color:{net_color}'>{_fmt_money(net) if net is not None else '--'}</td>"
            )
        rows += (
            "<tr>"
            f"<td style='{td};white-space:nowrap;color:{label_color};background:{label_bg};font-weight:600'>{label}</td>"
            f"<td style='{td};white-space:nowrap;font-weight:600'>{s.get('strategy', '')}</td>"
            f"<td style='{td}'>{s.get('code', '')}</td>"
            f"<td style='{td}'>{s.get('name', '')}</td>"
            f"<td style='{td};text-align:right'>{s.get('price', '')}</td>"
            f"<td style='{td};text-align:right;color:{color}'>{change:+.2f}%</td>"
            f"{extra}"
            f"<td style='{td}'>{' + '.join(s.get('reasons', []))}</td>"
            "</tr>"
        )
    if not rows:
        colspan = 10 if extended else 7
        rows = f"<tr><td colspan='{colspan}' style='padding:12px;border:1px solid #e0e0e0'>无信号</td></tr>"

    head_color = (
        "#c62828" if any(s.get("stage") == "trigger" for s in signals)
        else "#2e7d32" if any(s.get("direction") == "down" for s in signals)
        else "#b8860b"
    )
    extra_head = ""
    if extended:
        extra_head = (
            f"<th style='{td}'>量比</th>"
            f"<th style='{td}'>换手率</th>"
            f"<th style='{td}'>主力净流入</th>"
        )
    html = f"""<div style="font-family:-apple-system,'PingFang SC',Arial,sans-serif;color:#222;max-width:1080px">
      <h2 style="color:{head_color};margin-bottom:4px">{title}</h2>
      <p style="color:#666;font-size:13px">{content.replace(chr(10), '<br>')}</p>
      <table style="border-collapse:collapse;width:100%;font-size:13px">
        <thead><tr style="background:#fafafa">
          <th style="{td}">阶段</th>
          <th style="{td}">战法</th>
          <th style="{td}">代码</th>
          <th style="{td}">名称</th>
          <th style="{td}">现价</th>
          <th style="{td}">涨跌幅</th>
          {extra_head}
          <th style="{td}">命中理由</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#999;font-size:11px;margin-top:16px">
        本邮件由本地主升浪盯盘程序自动发送，基于公开行情数据与技术形态计算，仅供参考，不构成投资建议。
      </p>
    </div>"""
    return html


def send_to_channel(channel: NotifierConfig, title: str, content: str,
                    signals: list[dict[str, Any]] | None = None) -> tuple[bool, str]:
    """发送邮件，返回 (成功与否, 说明)。"""
    signals = signals or []
    if not channel.smtp_host or not channel.mail_to:
        return False, "邮件配置不完整（需填写 SMTP 服务器与收件人）"
    try:
        msg = EmailMessage()
        # 邮件头里的中文必须按 RFC 2047 编码，否则 smtplib 用 ascii 序列化会抛
        # "ascii codec can't encode characters"。EmailMessage 用 __setitem__ 赋值时
        # 会自动编码主题/显示名，但「显示名」与「邮箱地址」不能填反：
        # 早期版本把 mail_from（用户常填中文名）当成地址传给 formataddr 第二参数，
        # 导致中文地址无法编码。正确做法：显示名放第一参数，真实邮箱放第二参数。
        from_addr = channel.smtp_user or channel.mail_from
        # mail_from 作为显示名；若用户误填成邮箱地址，则回退到默认显示名避免歧义。
        display_name = channel.mail_from
        if display_name and "@" in display_name:
            display_name = ""
        display_name = display_name or "主升浪盯盘"
        msg["Subject"] = title
        msg["From"] = formataddr((display_name, from_addr))
        msg["To"] = channel.mail_to
        msg.set_content(content + "\n\n" + "\n".join(
            f"- [{s.get('stage_label') or s.get('strategy', '')}] "
            f"{s.get('code', '')} {s.get('name', '')} "
            f"{s.get('signal') or '；'.join(s.get('reasons', [])) or ''}"
            for s in signals[:30]
        ))
        msg.add_alternative(build_email_html(title, content, signals), subtype="html")

        if channel.smtp_port == 587:
            with smtplib.SMTP(channel.smtp_host, channel.smtp_port, timeout=15) as server:
                server.starttls()
                if channel.smtp_user:
                    server.login(channel.smtp_user, channel.smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(channel.smtp_host, channel.smtp_port, timeout=15) as server:
                if channel.smtp_user:
                    server.login(channel.smtp_user, channel.smtp_password)
                server.send_message(msg)
        return True, "邮件已发送"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def broadcast(channels: list[NotifierConfig], title: str, content: str,
              signals: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """广播到所有启用的邮件渠道。"""
    results: list[dict[str, Any]] = []
    for ch in channels:
        if not ch.enabled:
            continue
        ok, message = send_to_channel(ch, title, content, signals)
        results.append({
            "channel_id": ch.id,
            "channel_name": ch.name or "邮件通知",
            "type": "email",
            "success": ok,
            "message": message,
        })
    return results
