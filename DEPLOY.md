# 部署指南（服务器）

当前系统默认战法 = **洗盘反包 + 情绪点火闸门（中位数≥3%）**（2026-11-21 三个月全市场回测冠军方案：胜率 64.5%、平均收益 +5.36%、按单只 1 万元三个月总营收 +83,073 元）。

## 一键部署（推荐）

```bash
# 服务器上（需 Python 3.11+，推荐 3.13；有 node 则自动构建前端，没有则用仓库自带 dist）
./install.sh
```

**打包上传的正确姿势**（虚拟环境与缓存不可跨机器使用，排除后打包）：

```bash
# 本机打包（排除本机 .venv / __pycache__ / logs / .git；kline_cache 可留可不留，留着可省首次刷新）
tar --exclude='gupiao/backend/.venv' --exclude='gupiao/backend/app/__pycache__' \
    --exclude='gupiao/logs' --exclude='gupiao/.git' \
    -czf gupiao.tar.gz gupiao
# 服务器解压后：./install.sh（脚本会全新创建 .venv）
```

> install.sh 每次运行都会移除并重建 `.venv`——虚拟环境包含指向本机 Python 的绝对路径软链，mac 打包到 Linux 必然失效。

脚本自动完成：创建 venv → 安装后端依赖 → 构建前端（有 node 时）→ 启动服务 → 健康检查 → 打印访问地址。重复执行安全（旧进程自动替换）。

```bash
./start.sh   # 日常启动（幂等）
./stop.sh    # 停止
```

日志在 `logs/`（backend.log / frontend.log）。

## 服务形态

| 入口 | 地址 | 说明 |
|---|---|---|
| **系统页面（推荐）** | `http://服务器IP:8000/` | 后端直接托管 frontend/dist（hash 路由），单端口出 UI + API |
| 后端 API | `http://服务器IP:8000/api/health` | |
| 前端预览（可选） | `http://服务器IP:3006/` | 仅机器装有 node 时启动，/api 自动代理到 8000 |

单端口 8000 即完整系统：**服务器无需 nginx、无需 node**。如需 80 端口或域名，再补一段 nginx 反代即可：

```nginx
server {
    listen 80;
    location / { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; proxy_read_timeout 120s; }
}
```

## 常驻（systemd，防退出）

`install.sh` 用 nohup 启动，SSH 断开不影响；要开机自启/崩溃自拉用 systemd（`/etc/systemd/system/gupiao.service`）：

```ini
[Unit]
Description=Gupiao market scanner
After=network-online.target

[Service]
WorkingDirectory=/opt/gupiao/backend
ExecStart=/opt/gupiao/backend/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now gupiao
```

## 数据与安全

- `backend/data/config.json`：全部配置（战法开关、点火阈值、邮件 SMTP **含授权码**）。已随 git 跟踪并推送——**仓库必须保持私有**（GitHub 仓库 Settings → General → 底部 Change visibility 核实为 Private），推送到公共平台等于泄露邮箱授权码。已配置的邮件通道 `enabled=true`，信号命中自动发信。
- `backend/data/notifications.json` / `code_names.json`：站内通知与代码表，随 git 一起走。
- `backend/data/kline_cache/`（约 77MB 全市场日线）**已从 git 移除**（.gitignore）：git 部署的机器上回放功能需先执行 `curl -X POST http://127.0.0.1:8000/api/market/replay/refresh`（后台约 4 分钟）；实时扫描与推送**不依赖该缓存**。也可 scp 直接拷贝该目录跳过刷新。
- 新装环境无 config.json 时，代码默认值即为冠军方案（洗盘反包单战法 + 点火闸门 3.0 + 弱市 half + 强续量比上限 3.0），无需手工配置。
- 云服务器防火墙按需放行 8000（及 3006）。
- 本地领先远程 2 个提交（部署物料与清理），部署前 `git push github master` 同步。

## 部署后验收清单

1. `curl http://127.0.0.1:8000/api/health` → `status: ok`
2. 浏览器打开 `http://服务器IP:8000/` → 出现「主升浪捕手」界面
3. `GET /api/config` → `strategies` 仅 `洗盘反包=true`；`market_regime.ignition_enabled=true, ignition_threshold=3.0, weak_action=half`；`strategy_rules.strong_max_vol_ratio=3.0`
4. `POST /api/market/scan` → 交易时段返回信号或 `paused_by_ignition=true`（附 `ignition.median/threshold`）；休市日自动取最近交易日
5. 页面「全市场机会」顶部可见**情绪点火**标签（已点火/未点火 + 中位值）
6. 命中信号时收到站内通知 + 邮件（正文含【广度档位】与【买入理由明细】）；`GET /api/notifications` 有记录
7. 回放功能首次使用前刷新K线缓存：`curl -X POST http://127.0.0.1:8000/api/market/replay/refresh`（约 4 分钟后台完成）；「历史回放」选 8/4 应出 71 只、选 11/20 应显示未点火拦截

## 危险操作提醒

- **部署机不要执行 `git clean -fdx`**：会删掉未跟踪的 `backend/data/config.json`（邮件配置）、`kline_cache/`（回放缓存）与 `logs/`。
- **Python 版本**：依赖锁定了 pandas 3.1.1 等，要求 **3.11+**（推荐 3.13）；install.sh 已自动检查，低于 3.11 直接拒绝安装。

## 上线后建议

- 前两周只观察：记录每日点火读数（中位溢价）与实际信号收益。11 月前瞻验证显示正期望未复现（详见回测报告第七章），实盘口径与回测有约 2pp 差异，阈值 3.0 可能需要按实盘读数微调（如 2.5）。
- 资金规划：点火日信号可单日 71 只集中爆发（单只 1 万需 71 万）。按资金量在设置页「每次扫描最多推送」设 10~20（随机等权抽签，回测上限 20 约保留 40% 营收、峰值占用 20 万）。
- 抓妖股模式（monster）会绕过全部闸门并强制开启强势续攻，胜率会显著下降（回测 13.3%），仅作右尾博弈用，不要当默认模式。
