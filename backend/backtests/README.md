# 回测脚本目录

所有关键结论的回测脚本固化于此，防止口径漂移。运行用 backend/.venv/bin/python。

## 口径约定（重要——修改任何脚本前先读）

1. **放量判定**：量 ≥ 1.5 × **前5日均量**（`rolling(5).mean().shift(1)`，不含当日）——shifted 是"量比"的正确口径，非 shifted 会得出完全不同的结论（2026-11-28 教训）
2. **触板判定**：盘中高点 ≥ 昨收 × (1 + 涨停阈值 - 0.3pp)——注意统一为**百分比**（曾因小数/百分比单位错位导致炸板率恒空的 bug）
3. **封板判定**：pct ≥ 阈值(主板9.8/创业科创19.5) 且 close ≥ high×0.995
4. **烂板接力入场**：接力日封板价入场（秒板近似）——与实盘的偏差见福达案例审计（理想 vs 现实 -40%）
5. **回放缓存**：replay.replay(date) 按(日期,配置指纹)缓存，配置变更后指纹变会全量重算；指纹当前 v5（含动量死区）
6. **样本卫生**：数据最后一日的入场无前向数据（r≈0），统计时必须剔除，否则稀释胜率（2026-11-28 教训：524 vs 280 的假象）

## 脚本清单

| 脚本 | 结论 | 数据依赖 |
|---|---|---|
| test_regression_20261125.py（在 tests/） | 因子回归 18/18 | 无（自含 stub）|
| factor_screen.py | 6因子筛选（炸板率-0.428最强） | replay 缓存 + industry_map |
| backtest_ch5_ab.py | 第5通道 A/B（+1.58万改善） | ch5_ab_result.json（生成于脚本）|
| backtest_vr_turnover.py | 四象限（C1剔除+1.58万） | lanban_nov2026_final.json + kline_cache |
| lanban_relay_nov_report_20261127.py | 烂板接力11月报告（62.5%/+6.94%） | lanban_nov2026_final.json |

## 关键结论存档（6-11月）

- 动量死区：市场5日动量-5~0%阴跌区入场 24.7%/-1.77%（89笔-1.58万）→ 闸门已实现（momentum_dead_zone_enabled）
- 高置信档：板块≥4∧高度≥7 → 86.6%（67笔）→ 已实现为置信档位（12月验证期）
- 个股动量：前20日涨幅 -0.274 → 追涨证伪，勿回头
EOF
ls -la /Users/xxwei/myspace/gupiao/backend/backtests/ | head -10
__zcode_status=$?
if [ "$__zcode_status" -eq 0 ]; then pwd -P > '/var/folders/1x/0r8rl8957q31yh0n9trs09nr0000gn/T/zcode-22428981-aa02-456b-bbb4-7e9c2330dfde-cwd'; fi
exit "$__zcode_status"