<!-- 邮件与配置 -->
<template>
  <div class="settings-page">
    <ElCard shadow="never" class="mb-4">
      <div class="flex items-center justify-between flex-wrap gap-3">
        <div class="flex items-center gap-3">
          <span class="text-lg font-semibold">邮件与配置</span>
          <ElTag type="info" size="small">保存后立即生效</ElTag>
        </div>
        <div class="flex items-center gap-2">
          <ElButton :loading="loading" @click="loadConfig">
            <ElIcon><Refresh /></ElIcon>
            <span class="ml-1">重新加载</span>
          </ElButton>
          <ElButton @click="handleClearCache">
            <ElIcon><Delete /></ElIcon>
            <span class="ml-1">清理缓存</span>
          </ElButton>
          <ElButton type="primary" :loading="saving" @click="saveConfig">保存配置</ElButton>
        </div>
      </div>
    </ElCard>

    <ElCard shadow="never" class="mb-4">
      <template #header>
        <div class="flex items-center justify-between flex-wrap gap-2">
          <span class="font-semibold">运行模式</span>
          <ElTag :type="config.run_mode === 'monster' ? 'warning' : 'success'" size="small" effect="dark">
            {{ config.run_mode === 'monster' ? '抓妖股' : '稳健' }}
          </ElTag>
        </div>
      </template>
      <ElRadioGroup v-model="config.run_mode" class="mode-group">
        <ElRadioButton value="stable">
          <div class="mode-opt">
            <div class="mode-opt-title">稳健模式</div>
            <div class="mode-opt-desc">大盘过滤 + 战法倾斜 + 快止损，胜率优先、控制回撤。弱市停手，只做高胜率主战法。</div>
          </div>
        </ElRadioButton>
        <ElRadioButton value="monster">
          <div class="mode-opt">
            <div class="mode-opt-title">抓妖股模式</div>
            <div class="mode-opt-desc">弱市照开、放开连板加速，厚利票只放宽止损不移动止盈，让翻倍票跑完。建议只用小仓位（≤30%）。</div>
          </div>
        </ElRadioButton>
      </ElRadioGroup>
      <div class="mode-tip">
        <b>为什么分两套：</b>妖股是右尾事件——会稽山这类票在弱市恐慌中启动，事前和垃圾票无法区分。"大盘过滤/战法倾斜/移动止盈"会同时削掉大亏和大赚。妖股模式关掉这三道闸门换取捕获翻倍票的机会，代价是胜率更低、回撤更大，所以务必用小仓位参与。
        <span v-if="config.run_mode === 'monster'" class="mode-warn">当前为抓妖股模式：大盘环境过滤与情绪点火闸门被绕过、强势续攻（连板加速）强制开启，下方对应开关在本模式下不生效；卖出只在标的池录入成本价后按浮盈分档生效（不移动止盈）。稳健模式（默认）= 洗盘反包 + 情绪点火闸门（三个月回测胜率 64.5%）。</span>
      </div>
    </ElCard>

    <ElRow :gutter="16">
      <!-- 左列：标的池入口 + 战法开关 -->
      <ElCol :xs="24" :lg="12">
        <ElCard shadow="never" class="mb-4">
          <template #header><span class="font-semibold">标的池</span></template>
          <div class="flex items-center justify-between gap-3 flex-wrap">
            <div>
              <div class="text-sm font-medium">共 {{ config.watchlist?.length || 0 }} 只标的</div>
              <div class="text-xs text-gray-400 mt-1">
                自选股式管理已迁移到独立页面，支持搜索添加、行情排序与信号详情。
              </div>
            </div>
            <ElButton type="primary" plain @click="router.push('/monitor/watchlist')">
              前往标的池
            </ElButton>
          </div>
        </ElCard>

        <ElCard shadow="never" class="mb-4">
          <template #header><span class="font-semibold">全市场机会</span></template>
          <div class="flex items-center justify-between gap-3 flex-wrap">
            <div>
              <div class="text-sm font-medium">涨停回调 / 强势续攻 / 洗盘反包 · 全市场扫描</div>
              <div class="text-xs text-gray-400 mt-1">
                每日 14:00 / 14:40 自动扫描并站内 + 邮件提醒，查看命中信号与量比/换手率/资金流。
              </div>
            </div>
            <ElButton type="success" plain @click="router.push('/monitor/opportunity')">
              查看机会
            </ElButton>
          </div>
        </ElCard>

        <ElCard shadow="never" class="mb-4">
          <template #header>
            <div class="flex items-center justify-between">
              <span class="font-semibold">战法开关</span>
              <ElTag size="small" type="warning" effect="plain">全选/组合过滤均可</ElTag>
            </div>
          </template>
          <div class="switch-group-title up-text">↑ 上涨启动（抓主升浪）</div>
          <div class="strategy-switches">
            <div
              v-for="s in upStrategies"
              :key="s.key"
              class="flex items-center justify-between py-2 border-b border-gray-100 last:border-none"
            >
              <div>
                <div class="text-sm font-medium">{{ s.key }}</div>
                <div class="text-xs text-gray-400">{{ s.desc }}</div>
              </div>
              <ElSwitch v-model="strategyState[s.key]" />
            </div>
          </div>
          <div class="switch-group-title down-text mt-3">↓ 下跌离场（止盈止损）</div>
          <div class="strategy-switches">
            <div
              v-for="s in downStrategies"
              :key="s.key"
              class="flex items-center justify-between py-2 border-b border-gray-100 last:border-none"
            >
              <div>
                <div class="text-sm font-medium">{{ s.key }}</div>
                <div class="text-xs text-gray-400">{{ s.desc }}</div>
              </div>
              <ElSwitch v-model="strategyState[s.key]" />
            </div>
          </div>
        </ElCard>

        <ElCard shadow="never" class="mb-4">
          <template #header>
            <div class="flex items-center justify-between">
              <span class="font-semibold">持仓卖出风控</span>
              <ElTag size="small" type="danger" effect="plain">仅对标记持有的股票</ElTag>
            </div>
          </template>
          <div class="flex items-center justify-between mb-3">
            <div>
              <div class="text-sm font-medium">卖出提醒</div>
              <div class="text-xs text-gray-400">盘中预警 + 尾盘确认，确认后按「提醒次数」多次通知</div>
            </div>
            <ElSwitch v-model="config.sell.enabled" />
          </div>
          <ElForm label-position="top" size="small">
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="天量破位量比阈值">
                  <ElInputNumber v-model="config.sell.volume_surge_ratio" :min="1" :max="20" :step="0.5" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="确认后提醒次数">
                  <ElInputNumber v-model="config.sell.notify_times" :min="1" :max="4" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">快速破位通道（单日破位即确认，不必等斜率转负）</span>
              <ElSwitch v-model="config.sell.fast_break_enabled" size="small" />
            </div>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="快速破位幅度(占MA20)">
                  <ElInputNumber v-model="config.sell.fast_break_pct" :min="0.005" :max="0.1" :step="0.005" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="MA20斜率门槛(严格通道)">
                  <ElInputNumber v-model="config.sell.ma20_slope" :min="-0.05" :max="0.05" :step="0.005" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <ElDivider class="my-2" />
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">急跌双通道（连板急跌时 MA20 滞后，盘中跌幅即预警）</span>
              <ElSwitch v-model="config.sell.rapid_drop_enabled" size="small" />
            </div>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="盘中急跌预警阈值">
                  <ElInputNumber v-model="config.sell.rapid_drop_pct" :min="0.02" :max="0.2" :step="0.01" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="破MA10确认跌幅门槛">
                  <ElInputNumber v-model="config.sell.ma10_break_drop_pct" :min="0.01" :max="0.2" :step="0.01" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">启用尾盘破MA10确认通道</span>
              <ElSwitch v-model="config.sell.ma10_break_enabled" size="small" />
            </div>
            <ElDivider class="my-2" />
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">缩量跌停确认 + 确认冷却期（过滤强势票洗盘误报）</span>
            </div>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="缩量跌停量比上限">
                  <ElInputNumber v-model="config.sell.shrink_limit_ratio" :min="0.1" :max="3" :step="0.1" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="缩量跌停跌幅门槛">
                  <ElInputNumber v-model="config.sell.shrink_limit_drop" :min="0.05" :max="0.2" :step="0.005" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">启用确认冷却期（同一波下跌不重复发确认邮件）</span>
              <ElSwitch v-model="config.sell.cooldown_enabled" size="small" />
            </div>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="冷却重置涨幅(创新高)">
                  <ElInputNumber v-model="config.sell.cooldown_reset_pct" :min="0.005" :max="0.2" :step="0.005" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="盘中扫描间隔(分钟)">
                  <ElInputNumber v-model="config.sell.intraday_interval_minutes" :min="1" :max="30" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="尾盘确认时点">
                  <ElInput v-model="config.sell.confirm_time" placeholder="14:50" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <ElDivider class="my-2" />
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">浮盈分档止损（厚利票放宽，让主升浪跑完）</span>
              <ElSwitch v-model="config.sell.tier_enabled" size="small" />
            </div>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="浮盈达标比例">
                  <ElInputNumber v-model="config.sell.tier_profit_pct" :min="0.05" :max="0.5" :step="0.01" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="厚利档急跌阈值">
                  <ElInputNumber v-model="config.sell.tier_rapid_pct" :min="0.08" :max="0.3" :step="0.01" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="厚利档缩量跌幅">
                  <ElInputNumber v-model="config.sell.tier_shrink_drop" :min="0.08" :max="0.25" :step="0.005" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <div class="flex items-center justify-between h-full pt-1">
                  <span class="text-xs text-gray-500">厚利档10日线移动止盈</span>
                  <ElSwitch v-model="config.sell.tier_ma10_trail" size="small" />
                </div>
              </ElCol>
            </ElRow>
            <p class="text-xs text-gray-400 mt-1">
              盘中跌幅≥8%照常立即预警；但尾盘「急跌破位」确认已收紧为：收盘破MA10且跌幅≥5%，或缩量跌停（量比≤上限且跌幅达门槛，买盘枯竭非洗盘）——放量洗盘不再触发确认轰炸。确认冷却期：同一波下跌首日发一次尾盘确认，股价未收复下跌前高点前不再重复发确认邮件（盘中预警不受影响）。浮盈分档：持仓成本价录入后，浮盈达标的票放宽急跌阈值并启用10日线移动止盈。注意：8-10月下跌市回测未证明其有效（胜率不变、且15%门槛会误伤妖股），故默认关闭；如要在震荡/上涨市尝试，建议门槛≥30%。需在标的池标记持有（可录入成本价）才会生效。
            </p>
          </ElForm>
        </ElCard>

        <ElCard shadow="never">
          <template #header><span class="font-semibold">大盘环境过滤</span></template>
          <ElForm label-position="top" size="small">
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">弱市暂停/减半开新仓（指数在MA20下方或均线向下）</span>
              <ElSwitch v-model="config.market_regime.enabled" size="small" />
            </div>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="弱市动作">
                  <ElSelect v-model="config.market_regime.weak_action" style="width:100%">
                    <ElOption label="暂停开新仓" value="pause" />
                    <ElOption label="减半（仅主战法）" value="half" />
                  </ElSelect>
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="大盘均线周期">
                  <ElInputNumber v-model="config.market_regime.ma_period" :min="5" :max="60" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">洗盘反包设为主战法（优先展示/推送）</span>
              <ElSwitch v-model="config.market_regime.wash_primary" size="small" />
            </div>
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">强势续攻仅在强市开启（弱市自动屏蔽）</span>
              <ElSwitch v-model="config.market_regime.strong_requires_bull" size="small" />
            </div>
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">情绪点火闸门（昨日涨停股今日溢价低于阈值暂停开新仓，抓妖股模式绕过）</span>
              <ElSwitch v-model="config.market_regime.ignition_enabled" size="small" />
            </div>
            <ElFormItem v-if="config.market_regime.ignition_enabled" label="点火阈值(%)">
              <ElInputNumber v-model="config.market_regime.ignition_threshold" :min="0" :max="10" :step="0.5" style="width:100%" />
            </ElFormItem>
            <ElFormItem v-if="config.market_regime.ignition_enabled" label="点火广度确认（当日涨停家数，0=关）">
              <ElInputNumber v-model="config.market_regime.ignition_min_breadth" :min="0" :max="1000" :step="10" style="width:100%" />
              <div class="text-xs text-gray-400">
                阶段研究：点火∧高潮(家数≥100)信号胜率 82.7%/+8.3万，点火∧家数不足(修复反弹)仅 14.6%/-5.1万。
                设 100 复现「只做普涨日」；默认 0 关闭，建议 12 月前瞻验证后开启。
              </div>
            </ElFormItem>
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">点火盘中监控（连续达标即提醒，跌破0提醒退潮，一日最多两条）</span>
              <ElSwitch v-model="config.market_regime.ignition_watch_enabled" size="small" />
            </div>
            <ElRow v-if="config.market_regime.ignition_watch_enabled" :gutter="12">
              <ElCol :span="6">
                <ElFormItem label="检查间隔(分)">
                  <ElInputNumber v-model="config.market_regime.ignition_watch_minutes" :min="1" :max="30" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="6">
                <ElFormItem label="持续次数">
                  <ElInputNumber v-model="config.market_regime.ignition_sustain_checks" :min="1" :max="10" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="确认后最小池(只)">
                  <ElInputNumber v-model="config.market_regime.ignition_min_pool" :min="1" :max="100" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <div v-if="config.market_regime.ignition_watch_enabled" class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">点火确认后立即执行一次全市场扫描推送（默认关，信号仍按推送时点出）</span>
              <ElSwitch v-model="config.market_regime.ignition_auto_scan" size="small" />
            </div>
            <p class="text-xs text-gray-400 mt-1">
              回测结论：8/9/10 月全市场 2014 个买入信号胜率仅 22.8%。大盘过滤：8 月 508 个信号全程处于上证 -9.7% 下跌市，逆势开仓是最大亏损来源；弱市里洗盘反包胜率 42.4%（8 月弱市 60.6%），故弱市推荐「减半」而非「暂停」。情绪点火：昨日涨停股今日涨幅中位数 ≥3% 视为点火日，点火日信号胜率 44.9%（8/9/10 月点火日仅 6/21、4/21、2/17 天）；配合洗盘反包单战法胜率 64.5%、平均收益 +5.36%（基线 -3.92%）。阈值 2.0~4.0 全区间稳健。指数日线走腾讯/sina 稳定源，点火值 10 分钟缓存。
            </p>
          </ElForm>
        </ElCard>

        <ElCard shadow="never">
          <template #header><span class="font-semibold">自动盯盘</span></template>
          <div class="flex items-center justify-between mb-3">
            <div>
              <div class="text-sm font-medium">定时扫描并发邮件</div>
              <div class="text-xs text-gray-400">开启后常驻自动运行，命中即推送</div>
            </div>
            <ElSwitch v-model="config.scan.enabled" />
          </div>
          <ElForm label-position="top" size="small">
            <ElFormItem label="全市场推送/买进时点（逗号分隔 HH:MM，默认尾盘 14:00,14:40）">
              <ElInput v-model="pushTimesText" placeholder="14:00,14:40" style="width:100%" />
              <div class="text-xs text-gray-400">
                可设全天任意交易时段（如 09:35,10:30,14:00,14:40），保存后立即重排无需重启。
                休市日自动跳过；点火闸门读数按推送时点的盘中行情实时计算。
              </div>
            </ElFormItem>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="盘中扫描间隔(秒)">
                  <ElInputNumber v-model="config.scan.trading_interval" :min="30" :step="30" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="盘后扫描间隔(秒)">
                  <ElInputNumber v-model="config.scan.closed_interval" :min="60" :step="60" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <ElFormItem label="同一股票同一战法冷却时间(秒)">
              <ElInputNumber v-model="config.scan.cooldown_seconds" :min="0" :step="3600" style="width:100%" />
              <div class="text-xs text-gray-400">默认 86400（24小时），避免同一信号反复发邮件</div>
            </ElFormItem>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="K线缓存刷新时点">
                  <ElInput v-model="config.scan.kline_refresh_time" placeholder="15:10" style="width:100%" />
                  <div class="text-xs text-gray-400">收盘后自动刷新回放缓库（休市日跳过）</div>
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="系统自检邮件时点">
                  <ElInput v-model="config.scan.daily_check_time" placeholder="09:20" style="width:100%" />
                  <div class="text-xs text-gray-400">每天检查数据源/缓存/任务/邮件并汇报</div>
                </ElFormItem>
              </ElCol>
            </ElRow>
            <ElRow :gutter="12">
              <ElCol :span="8">
                <ElFormItem label="弱转强观察池">
                  <ElSwitch v-model="config.scan.wzq_watch_enabled" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="16">
                <ElFormItem label="观察池推送时点">
                  <ElInput v-model="config.scan.wzq_watch_time" placeholder="15:20" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <p class="text-xs text-gray-400 m-0">
              弱转强观察池：收盘后筛当日「巨量炸板」（触板未封+收盘离板≥1.5%+量比≥2.5）名单，邮件附竞价三问清单，
              供次日竞价人工判断——纯观察提醒，不构成买卖信号。
            </p>
            <ElDivider style="margin:12px 0" />
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">盘中形态监控（全市场反包进行中/N日新高突破，触发即提醒，每票每日一次）</span>
              <ElSwitch v-model="config.scan.pattern_watch_enabled" size="small" />
            </div>
            <ElRow v-if="config.scan.pattern_watch_enabled" :gutter="12">
              <ElCol :span="6">
                <ElFormItem label="轮询(分钟)">
                  <ElInputNumber v-model="config.scan.pattern_poll_minutes" :min="1" :max="15" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="6">
                <ElFormItem label="新高N日">
                  <ElInputNumber v-model="config.scan.pattern_break_high_days" :min="5" :max="120" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="6">
                <ElFormItem label="突破量比">
                  <ElInputNumber v-model="config.scan.pattern_break_vr" :min="1" :max="10" :step="0.5" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="6">
                <ElFormItem label="每日上限">
                  <ElInputNumber v-model="config.scan.pattern_max_alerts" :min="1" :max="100" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <p v-if="config.scan.pattern_watch_enabled" class="text-xs text-gray-400 m-0">
              三类形态：①洗盘反包·进行中（近8日洗盘大跌，今日涨幅≥5%或触板且守在洗盘日收盘上方——比14:40推送提前半天）；
              ②N日新高突破（现价突破前N日最高+量比放大）；
              ③涨停缩量突破（近10日有涨停→涨停后连续缩量≥3日不破涨停日最低→今日放量突破区间最高，触发即提醒追入时机，生命线=涨停日最低价）。
              防刷屏：每票每日每形态一次、总量上限、同轮合并一封。
              提醒：突破/追高类形态的隔夜持有期望经回测为负（7-11月近似回测 -7.9万），提醒仅作盘中时机参考，务必配合生命线止损。
            </p>
            <ElDivider style="margin:12px 0" />
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">板块异动提醒（东财行业板块指数涨跌幅，每板块每日每方向一次）</span>
              <ElSwitch v-model="config.scan.pattern_board_alert" size="small" />
            </div>
            <ElRow v-if="config.scan.pattern_watch_enabled && config.scan.pattern_board_alert" :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="拉涨阈值(%)">
                  <ElInputNumber v-model="config.scan.pattern_board_anomaly_pct" :min="1" :max="10" :step="0.1" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="砸盘阈值(%)">
                  <ElInputNumber v-model="config.scan.pattern_board_down_pct" :min="0.5" :max="10" :step="0.1" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <p v-if="config.scan.pattern_watch_enabled && config.scan.pattern_board_alert" class="text-xs text-gray-400 m-0">
              口径：东财行业板块指数涨跌幅（与行情软件板块涨跌一致）；板块行情不可用时回退成分股涨跌中位。
              砸盘阈值默认 2.3%（比拉涨 3.0% 更灵敏）；触发只发一封专属提醒，每板块每日每方向最多一次，盘中每 {{ config.scan.pattern_poll_minutes }} 分钟扫一次。
            </p>
            <p class="text-xs text-gray-400 m-0">
              末次推送（默认 14:40）无信号也会发「系统自检·推送正常」邮件，扫描异常会发异常告警——每天必有邮件，便于发现服务中断。
            </p>
            <ElDivider style="margin:12px 0" />
            <ElFormItem label="K线缓存（历史回放数据源）">
              <div class="flex items-center gap-2 flex-wrap">
                <ElButton size="small" :loading="cacheRefreshing" @click="refreshKlineCache">
                  {{ cacheRefreshing ? '刷新中…' : '立即刷新缓存' }}
                </ElButton>
                <span class="text-xs text-gray-400">
                  {{ cacheInfo.codes || 0 }} 只 · 覆盖至 {{ cacheInfo.end_date || '--' }}
                  <span v-if="cacheRefreshing && cacheInfo.refresh">
                    （{{ cacheInfo.refresh.done }}/{{ cacheInfo.refresh.total }}）
                  </span>
                </span>
              </div>
              <div class="text-xs text-gray-400">
                每日 {{ config.scan.kline_refresh_time }} 自动刷新，也可在此手动触发；仅历史回放使用，实时扫描不依赖。git 部署后首次需刷一次。
              </div>
            </ElFormItem>
          </ElForm>
        </ElCard>
      </ElCol>

      <!-- 右列：邮件配置 + 阈值 -->
      <ElCol :xs="24" :lg="12">
        <ElCard shadow="never" class="mb-4">
          <template #header>
            <div class="flex items-center justify-between">
              <span class="font-semibold">邮件通知（唯一通知方式）</span>
              <ElButton size="small" :loading="testing" @click="testMail">发送测试邮件</ElButton>
            </div>
          </template>
          <ElForm label-position="top" size="small">
            <div class="flex items-center gap-2 mb-2">
              <ElSwitch v-model="mailConfig.enabled" />
              <span class="text-sm">启用邮件提醒</span>
            </div>
            <ElRow :gutter="12">
              <ElCol :span="16">
                <ElFormItem label="SMTP 服务器">
                  <ElInput v-model="mailConfig.smtp_host" placeholder="如 smtp.qq.com" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="8">
                <ElFormItem label="端口">
                  <ElInputNumber v-model="mailConfig.smtp_port" :min="1" :max="65535" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <ElFormItem label="发件邮箱账号">
              <ElInput v-model="mailConfig.smtp_user" placeholder="如 yourname@qq.com" />
            </ElFormItem>
            <ElFormItem label="授权码 / 密码">
              <ElInput v-model="mailConfig.smtp_password" type="password" show-password placeholder="邮箱 SMTP 授权码" />
            </ElFormItem>
            <ElFormItem label="发件人显示名（可选）">
              <ElInput v-model="mailConfig.mail_from" placeholder="默认使用发件账号" />
            </ElFormItem>
            <ElFormItem label="收件邮箱">
              <ElInput v-model="mailConfig.mail_to" placeholder="多个收件人用英文逗号分隔" />
            </ElFormItem>
          </ElForm>
          <ElAlert
            type="info"
            :closable="false"
            show-icon
            class="mt-2"
            title="常见邮箱 SMTP 设置"
            description="QQ邮箱 smtp.qq.com:465；163邮箱 smtp.163.com:465；Gmail smtp.gmail.com:587。多数邮箱需在设置中开启 SMTP 并使用授权码（非登录密码）。465 走 SSL，587 走 STARTTLS。"
          />
        </ElCard>

        <ElCard shadow="never">
          <template #header>
            <div class="flex items-center justify-between">
              <span class="font-semibold">战法量化阈值</span>
              <ElTag size="small" type="info" effect="plain">一般保持默认</ElTag>
            </div>
          </template>
          <ElForm label-position="top" size="small">
            <ElFormItem label="放量倍数（量价突破 / 三重共振）">
              <ElInputNumber v-model="config.strategy_rules.volume_ratio" :min="1" :max="5" :step="0.1" style="width:100%" />
            </ElFormItem>
            <ElFormItem label="三重共振同步窗口（交易日）">
              <ElInputNumber v-model="config.strategy_rules.resonance_window" :min="1" :max="10" style="width:100%" />
            </ElFormItem>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="龙出水横盘天数">
                  <ElInputNumber v-model="config.strategy_rules.box_days" :min="20" :max="120" :step="5" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="箱体最大振幅">
                  <ElInputNumber v-model="config.strategy_rules.box_max_range" :min="0.05" :max="1" :step="0.05" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <ElFormItem label="龙出水突破放量倍数">
              <ElInputNumber v-model="config.strategy_rules.box_break_vol" :min="1" :max="5" :step="0.5" style="width:100%" />
            </ElFormItem>
            <div class="rules-subtitle">涨停回调·放量启动</div>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="涨停回看(日，含连板)">
                  <ElInputNumber v-model="config.strategy_rules.limitup_lookback" :min="5" :max="60" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="涨停后最长回调(日)">
                  <ElInputNumber v-model="config.strategy_rules.pullback_max_days" :min="2" :max="20" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="缩量观察天数">
                  <ElInputNumber v-model="config.strategy_rules.pullback_shrink_days" :min="2" :max="10" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="回调均量上限(占涨停日)">
                  <ElInputNumber v-model="config.strategy_rules.pullback_shrink_ratio" :min="0.2" :max="1" :step="0.05" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <ElFormItem label="放量启动量比(倍)">
              <ElInputNumber v-model="config.strategy_rules.pullback_vol_ratio" :min="1" :max="5" :step="0.1" style="width:100%" />
            </ElFormItem>
            <p class="text-xs text-gray-400 mt-1">
              缩量到位先发「观察中」，今日或昨日量比达标且未跌破涨停日最低价、站上10日线才发「放量启动」买点。跌破涨停日最低价则不再提醒。
            </p>
            <div class="rules-subtitle mt-3">强势续攻（缩量整理/不回头续板）</div>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="涨停后观察天数">
                  <ElInputNumber v-model="config.strategy_rules.strong_max_days" :min="2" :max="20" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="缩量转折放量阈值(倍)">
                  <ElInputNumber v-model="config.strategy_rules.strong_vol_ratio" :min="1" :max="5" :step="0.1" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="转折前一日量比上限">
                  <ElInputNumber v-model="config.strategy_rules.strong_shrink_ratio" :min="0.3" :max="1.5" :step="0.1" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="连续板加速量比(倍)">
                  <ElInputNumber v-model="config.strategy_rules.strong_accel_ratio" :min="1" :max="8" :step="0.1" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <ElRow :gutter="12">
              <ElCol :span="24">
                <ElFormItem label="强势续攻量比上限(0=不限)">
                  <ElInputNumber v-model="config.strategy_rules.strong_max_vol_ratio" :min="0" :max="20" :step="0.5" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <p class="text-xs text-gray-400 mt-1">
              量比上限：10月回测显示强势续攻信号量比&lt;3时，大亏笔数减少43%、妖股仍保留85.7%。建议抓妖股模式设为3过滤高位放量出货票；0表示不过滤。
            </p>
            <p class="text-xs text-gray-400 mt-1">
              价格不必跌破涨停收盘价（回调即缩量）。两种形态只在启动当日提醒一次：昨日缩量、今日放量达阈值（默认1.2倍，抓缩量后首波启动如会稽山8/24）；或涨停后1~2日放量续板并创收盘新高。均需守住涨停日最低价、站上10日线。
            </p>
            <div class="rules-subtitle mt-3">洗盘反包（大跌洗盘后首板反包，抓连板启动初期）</div>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="洗盘回看窗口(交易日)">
                  <ElInputNumber v-model="config.strategy_rules.wash_lookback" :min="3" :max="15" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="洗盘大跌门槛(%)">
                  <ElInputNumber v-model="config.strategy_rules.wash_drop_pct" :min="3" :max="15" :step="1" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <p class="text-xs text-gray-400 mt-1">
              近8日内出现大跌/跌停洗盘（跌幅≥7%）、今日涨停且收盘价收复洗盘日跌幅（反包）、昨日非涨停（避开连板中途刷屏）即提醒。抓万向德农8/21跌停洗盘、8/25首板反包这类启动初期买点。
            </p>
          </ElForm>
        </ElCard>

        <ElCard shadow="never" class="mt-4">
          <template #header>
            <div class="flex items-center justify-between">
              <span class="font-semibold">全市场筛选范围</span>
              <ElTag size="small" type="warning" effect="plain">影响每日14:00/14:40扫描</ElTag>
            </div>
          </template>
          <p class="text-xs text-gray-500 mb-3">
            涨停回调 / 强势续攻 / 洗盘反包为全市场扫描（非仅标的池）。勾选要纳入的板块，并设置价格区间过滤候选股。
          </p>
          <ElForm label-position="top" size="small">
            <div class="rules-subtitle">纳入板块</div>
            <div class="flex flex-wrap gap-x-6 gap-y-2">
              <ElCheckbox v-model="config.market_filter.main_board">主板（60/00）</ElCheckbox>
              <ElCheckbox v-model="config.market_filter.kcb">科创板（688）</ElCheckbox>
              <ElCheckbox v-model="config.market_filter.cyb">创业板（300）</ElCheckbox>
              <ElCheckbox v-model="config.market_filter.bj">北交所（8/4）</ElCheckbox>
              <ElCheckbox v-model="config.market_filter.include_st">包含 ST / *ST</ElCheckbox>
            </div>
            <div class="rules-subtitle mt-4">股价区间（元）</div>
            <ElRow :gutter="12">
              <ElCol :span="12">
                <ElFormItem label="最低价（0=不限）">
                  <ElInputNumber v-model="config.market_filter.min_price" :min="0" :max="10000" :step="1" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="12">
                <ElFormItem label="最高价（0=不限）">
                  <ElInputNumber v-model="config.market_filter.max_price" :min="0" :max="10000" :step="1" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <div class="rules-subtitle mt-4">信号数量</div>
            <ElFormItem label="每次扫描最多推送（0=不限）">
              <ElInputNumber v-model="config.market_filter.daily_signal_cap" :min="0" :max="200" :step="5" style="width:100%" />
            </ElFormItem>
            <p class="text-xs text-gray-400">
              回测提示：点火日信号会集中爆发（单日最多 71 只，单只 1 万需 71 万资金）。按资金量设 10~20 可控占用，
              超出部分随机等权抽取（强度排序已被回测证伪，高分=追高，不采用）。每日上限 20 的回测总营收约为不限的 40%，峰值资金占用从 71 万降到 20 万。
            </p>
            <ElDivider style="margin:12px 0" />
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">A杀过滤（多日连跌杀到低位，触底后N个交易日内不碰）</span>
              <ElSwitch v-model="config.market_filter.a_kill_skip_enabled" size="small" />
            </div>
            <ElRow v-if="config.market_filter.a_kill_skip_enabled" :gutter="12">
              <ElCol :span="8">
                <ElFormItem label="触底后不碰(交易日)">
                  <ElInputNumber v-model="config.market_filter.a_kill_days" :min="0" :max="10" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="8">
                <ElFormItem label="连续下跌天数≥">
                  <ElInputNumber v-model="config.market_filter.a_kill_down_days" :min="2" :max="20" style="width:100%" />
                </ElFormItem>
              </ElCol>
              <ElCol :span="8">
                <ElFormItem label="累计跌幅≥(%)">
                  <ElInputNumber v-model="config.market_filter.a_kill_cum_drop" :min="5" :max="50" :step="1" style="width:100%" />
                </ElFormItem>
              </ElCol>
            </ElRow>
            <p v-if="config.market_filter.a_kill_skip_enabled" class="text-xs text-gray-400 m-0">
              ⚠️ 回测证伪提示（7-11月，连跌≥5天且累计≥15%，触底后3日）：被过滤的 56 笔反而更强
              （胜率 57.1% / 平均+3.09% vs 其余 49.3% / +0.77%）——深跌后的反包涨停是「超跌深度洗盘」形态，
              质量更高。默认关闭，仅供实盘对照验证。
            </p>
            <ElDivider style="margin:12px 0" />
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">换手率×量比四象限过滤（剔除C1温和放量反包）</span>
              <ElSwitch v-model="config.market_filter.vp_combo_filter_enabled" size="small" />
            </div>
            <div v-if="config.market_filter.vp_combo_filter_enabled" class="flex items-center justify-between mt-2 mb-1">
              <span class="text-xs text-gray-500">同时剔除C4（换手&gt;10%且量比&lt;1，无量诱多）</span>
              <ElSwitch v-model="config.market_filter.vp_combo_exclude_c4" size="small" />
            </div>
            <p v-if="config.market_filter.vp_combo_filter_enabled" class="text-xs text-gray-400 m-0">
              回测结论（7-11月，419笔）：经典口径里 C1「主力介入可进场」（换手&gt;5%、量比2~3）在洗盘反包体系里恰好反向——
              21笔胜率仅 19% / 平均-2.76%，是全样本最差入场象限（温和放量反包=弱势反包）；剔除后总收益 +4.52万 → +5.10万。
              C4「无量诱多」入场 45.9% 偏弱可选剔除；持仓期间 C4 后再跌概率 48.2% 反低于基线 55.7%，不能当卖出信号。
            </p>
            <ElDivider style="margin:12px 0" />
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">基本面筛选（营收增速，剔除增长乏力的信号）</span>
              <ElSwitch v-model="config.fundamental_filter.enabled" size="small" />
            </div>
            <ElFormItem v-if="config.fundamental_filter.enabled" label="最低营收同比增长(%)">
              <ElInputNumber v-model="config.fundamental_filter.min_rev_yoy" :min="-100" :max="1000" :step="5" style="width:100%" />
            </ElFormItem>
            <p v-if="config.fundamental_filter.enabled" class="text-xs text-gray-400 m-0">
              7-11月回测：营收同比&gt;10% 的信号 53% 胜率 / 平均+2.73% / 总营收+3.2万，优于全部信号（46% / +0.99% / +2.7万）。
              业绩缺失（未披露/次新）默认保留并标注；不使用净利润因子——回测显示亏损股反有困境反转溢价。
            </p>
            <ElDivider style="margin:12px 0" />
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">MA10 连续破位卖出（连续N天收盘跌破MA10即确认，第4通道）</span>
              <ElSwitch v-model="config.sell.ma10_consec_enabled" size="small" />
            </div>
            <ElFormItem v-if="config.sell.ma10_consec_enabled" label="连续天数">
              <ElInputNumber v-model="config.sell.ma10_consec_days" :min="2" :max="10" style="width:100%" />
            </ElFormItem>
            <ElDivider style="margin:12px 0" />
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-gray-500">首板回踩MA10极限卖出（30日内涨停后第N次回踩MA10即确认，第5通道）</span>
              <ElSwitch v-model="config.sell.touch_ma10_exit_enabled" size="small" />
            </div>
            <ElFormItem v-if="config.sell.touch_ma10_exit_enabled" label="回踩次数阈值">
              <ElInputNumber v-model="config.sell.touch_ma10_count" :min="2" :max="10" style="width:100%" />
            </ElFormItem>
            <ElFormItem v-if="config.sell.touch_ma10_exit_enabled" label="涨停回看窗口(天)">
              <ElInputNumber v-model="config.sell.touch_ma10_window" :min="10" :max="60" style="width:100%" />
            </ElFormItem>
            <p v-if="config.sell.touch_ma10_exit_enabled" class="text-xs text-gray-400 m-0">
              回踩 = 收盘触及/跌破 MA10×1.02 且未大阳线脱离（涨跌幅&lt;+3%）；连续多天贴线只算一次回踩，反弹离开后再贴近才是新的一次。
            </p>
            <div v-if="config.sell.touch_ma10_exit_enabled" class="flex items-center justify-between mt-2 mb-1">
              <span class="text-xs text-gray-500">回踩计数从强势日起算（等价于买入日起算）</span>
              <ElSwitch v-model="config.sell.touch_strong_reset_enabled" size="small" />
            </div>
            <ElFormItem v-if="config.sell.touch_ma10_exit_enabled && config.sell.touch_strong_reset_enabled" label="强势日涨幅≥(%)">
              <ElInputNumber v-model="config.sell.touch_strong_pct" :min="3" :max="15" :step="0.5" style="width:100%" />
            </ElFormItem>
            <p v-if="config.sell.touch_ma10_exit_enabled && config.sell.touch_strong_reset_enabled" class="text-xs text-gray-400 m-0">
              ⚠️ 回测证伪提示（7-11月）：此模式总收益 +3.08万 vs 首板起算 +4.52万——入场前的洗盘回踩被清零后，
              第5通道的快速止损闸门失效，收益回落到接近关闭状态。仅作实验开关保留，建议保持关闭。
            </p>
            <p class="text-xs text-gray-400 mt-1">
              默认排除 ST 与北交所，主板/科创/创业全开，价格不限。筛选在候选阶段生效，可显著减少扫描耗时。
            </p>
          </ElForm>
        </ElCard>
      </ElCol>
    </ElRow>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Delete } from '@element-plus/icons-vue'
import { monitorApi } from '@/api/monitor'

const router = useRouter()
import { cleanStaleCache } from '@/utils/sys/cache-cleaner'

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const strategyList = ref<any[]>([])
const upStrategies = computed(() => strategyList.value.filter((s: any) => s.direction === 'up'))
const downStrategies = computed(() => strategyList.value.filter((s: any) => s.direction === 'down'))

const config = reactive<any>({
  run_mode: 'stable',
  watchlist: [],
  strategies: {},
  strategy_rules: {},
  market_filter: {
    include_st: false,
    main_board: true,
    kcb: true,
    cyb: true,
    bj: false,
    min_price: 0,
    max_price: 0,
    daily_signal_cap: 0
  },
  market_regime: {
    enabled: true, index_code: 'sh000001', ma_period: 20, slope_lookback: 5,
    weak_action: 'half', wash_primary: true, strong_requires_bull: true,
    ignition_enabled: true, ignition_threshold: 3.0, ignition_min_breadth: 0,
    ignition_watch_enabled: true, ignition_watch_minutes: 5, ignition_sustain_checks: 3,
    ignition_min_pool: 10, ignition_auto_scan: false
  },
  scan: {},
  fundamental_filter: { enabled: true, min_rev_yoy: 10 },
  sell: {
    enabled: true, volume_surge_ratio: 4.5, ma20_buffer: 0.01, ma20_slope: -0.005,
    fast_break_enabled: true, fast_break_pct: 0.02,
    rapid_drop_enabled: true, rapid_drop_pct: 0.08,
    ma10_break_enabled: true, ma10_break_drop_pct: 0.05,
    shrink_limit_ratio: 0.5, shrink_limit_drop: 0.095,
    cooldown_enabled: true, cooldown_reset_pct: 0.03,
    tier_enabled: false, tier_profit_pct: 0.30, tier_rapid_pct: 0.12,
    tier_shrink_drop: 0.115, tier_ma10_trail: true,
    intraday_interval_minutes: 5, confirm_time: '14:50', notify_times: 2,
    kline_refresh_time: '15:10', daily_check_time: '09:20',
    wzq_watch_enabled: true, wzq_watch_time: '15:20',
    ma10_consec_enabled: true, ma10_consec_days: 2,
    touch_ma10_exit_enabled: true, touch_ma10_count: 4, touch_ma10_window: 30,
    touch_strong_reset_enabled: false, touch_strong_pct: 5.0,
    a_kill_skip_enabled: false, a_kill_days: 3, a_kill_down_days: 5, a_kill_cum_drop: 15.0,
    vp_combo_filter_enabled: true, vp_combo_exclude_c4: false,
    pattern_watch_enabled: true, pattern_poll_minutes: 3,
    pattern_board_alert: true, pattern_board_anomaly_pct: 3.0, pattern_board_down_pct: 2.3,
    pattern_break_high_days: 20, pattern_break_vr: 1.5, pattern_max_alerts: 20
  },
  notification: []
})
const strategyState = reactive<Record<string, boolean>>({})
const pushTimesText = ref('14:00,14:40')
const cacheInfo = ref<any>({})
const cacheRefreshing = ref(false)
let cacheTimer: ReturnType<typeof setInterval> | null = null

async function loadCacheStatus() {
  try {
    const res = await monitorApi.marketReplayDates()
    cacheInfo.value = res?.cache || {}
    const rp = cacheInfo.value.refresh || {}
    if (cacheRefreshing.value && rp.finished) {
      stopCachePoll()
      cacheRefreshing.value = false
      ElMessage.success(`K线缓存刷新完成：成功 ${rp.ok} 只、失败 ${rp.fail} 只`)
      loadConfig()
    }
  } catch { /* 静默轮询 */ }
}

function stopCachePoll() {
  if (cacheTimer) { clearInterval(cacheTimer); cacheTimer = null }
}

async function refreshKlineCache() {
  try {
    const res = await monitorApi.marketReplayRefresh()
    if (res?.success === false) {
      ElMessage.warning(res?.message || '刷新已在进行中')
      return
    }
    cacheRefreshing.value = true
    ElMessage.info('K线缓存刷新已启动（后台约 4 分钟）')
    stopCachePoll()
    cacheTimer = setInterval(loadCacheStatus, 2000)
    loadCacheStatus()
  } catch (e: any) {
    ElMessage.error('刷新启动失败：' + (e?.message || e))
  }
}

onUnmounted(stopCachePoll)
const mailConfig = reactive<any>({
  id: 'email', type: 'email', name: '邮件通知', enabled: false,
  smtp_host: '', smtp_port: 465, smtp_user: '', smtp_password: '',
  mail_from: '', mail_to: ''
})

function syncFromConfig() {
  // 合并上涨 + 下跌战法开关到同一字典（战法名不重复）
  Object.assign(strategyState, config.strategies || {}, config.bear_strategies || {})
  const mail = (config.notification || []).find((c: any) => c.type === 'email') || mailConfig
  Object.assign(mailConfig, mail)
  const times = config.scan?.market_push_times
  pushTimesText.value = Array.isArray(times) && times.length ? times.join(',') : '14:00,14:40'
}

async function handleClearCache() {
  try {
    await ElMessageBox.confirm(
      '将清理浏览器中残留的旧标签页、搜索历史和模板遗留缓存，随后刷新页面。此操作不会影响盯盘配置。',
      '清理缓存',
      { confirmButtonText: '确认清理', cancelButtonText: '取消', type: 'warning' }
    )
    const res = cleanStaleCache(true)
    ElMessage.success(`缓存已清理（移除 ${res.removedKeys} 项旧数据、${res.removedTabs} 个失效标签页），即将刷新`)
    setTimeout(() => window.location.reload(), 800)
  } catch {
    /* 用户取消 */
  }
}

async function loadConfig() {
  loading.value = true
  try {
    const [cfg, catalog] = await Promise.all([
      monitorApi.getConfig(),
      monitorApi.strategies()
    ])
    Object.assign(config, cfg)
    strategyList.value = catalog || []
    syncFromConfig()
  } catch (e: any) {
    ElMessage.error('加载配置失败：' + (e?.message || e))
  } finally {
    loading.value = false
  }
}

async function saveConfig() {
  saving.value = true
  try {
    // 推送时点：逗号/中文逗号分隔 -> HH:MM 列表（非法项忽略，空回退默认）
    const times = String(pushTimesText.value || '')
      .split(/[,，\s]+/).filter(Boolean)
      .map((t: string) => t.trim())
      .filter((t: string) => /^\d{1,2}:\d{2}$/.test(t))
      .map((t: string) => {
        const [h, m] = t.split(':')
        return `${String(Number(h)).padStart(2, '0')}:${String(Number(m)).padStart(2, '0')}`
      })
    config.scan.market_push_times = times.length ? times : ['14:00', '14:40']
    // 按方向拆分回两个配置对象
    const bearKeys = downStrategies.value.map((s: any) => s.key)
    const strategies: Record<string, boolean> = {}
    const bear_strategies: Record<string, boolean> = {}
    for (const [key, val] of Object.entries(strategyState)) {
      (bearKeys.includes(key) ? bear_strategies : strategies)[key] = val as boolean
    }
    const payload = {
      run_mode: config.run_mode,
      strategies,
      bear_strategies,
      strategy_rules: config.strategy_rules,
      market_filter: config.market_filter,
      fundamental_filter: config.fundamental_filter,
      market_regime: config.market_regime,
      scan: config.scan,
      sell: config.sell,
      notification: [{ ...mailConfig, type: 'email', id: 'email' }]
    }
    await monitorApi.putConfig(payload)
    ElMessage.success('配置已保存')
    await loadConfig()
  } catch (e: any) {
    ElMessage.error('保存失败：' + (e?.message || e))
  } finally {
    saving.value = false
  }
}

async function testMail() {
  testing.value = true
  try {
    const res = await monitorApi.notifyTest('email')
    const results = res?.results || []
    if (!results.length) {
      ElMessage.warning('请先启用邮件并保存配置')
    } else if (results[0].success) {
      ElMessage.success('测试邮件已发送，请查收')
    } else {
      ElMessage.error('发送失败：' + results[0].message)
    }
  } catch (e: any) {
    ElMessage.error('测试失败：' + (e?.message || e))
  } finally {
    testing.value = false
  }
}

onMounted(() => { loadConfig(); loadCacheStatus() })
</script>

<style scoped>
.strategy-switches { max-height: 240px; overflow-y: auto; }
.switch-group-title { font-size: 13px; font-weight: 600; margin-bottom: 4px; }
.up-text { color: #c62828; }
.down-text { color: #2e7d32; }
.rules-subtitle { font-size: 13px; font-weight: 600; color: #c62828; margin: 4px 0 8px; }
.mode-group { display: flex; flex-wrap: wrap; gap: 12px; width: 100%; }
.mode-group :deep(.el-radio-button) { flex: 1 1 260px; height: auto; white-space: normal; }
.mode-group :deep(.el-radio-button__inner) { width: 100%; height: 100%; padding: 12px 14px; text-align: left; line-height: 1.5; white-space: normal; box-shadow: none; border: 1px solid #dcdfe6; border-radius: 6px; }
.mode-opt-title { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
.mode-opt-desc { font-size: 12px; font-weight: 400; color: #6b7280; }
.mode-tip { margin-top: 12px; padding: 10px 12px; font-size: 12px; line-height: 1.7; color: #6b7280; background: #f5f7fa; border-left: 3px solid #409eff; border-radius: 4px; }
.mode-warn { display: block; margin-top: 6px; color: #b8860b; }
</style>
