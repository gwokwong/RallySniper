<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Bell, Timer, Plus } from '@element-plus/icons-vue'
import { monitorApi, type MarketSignal } from '@/api/monitor'

defineOptions({ name: 'MarketOpportunity' })

const signals = ref<MarketSignal[]>([])
const snapshot = ref<any>({})
const status = ref<any>({})
const loading = ref(false)
const pushing = ref(false)
const adding = ref(false)
const activeTab = ref('all')
const watchCodes = ref<string[]>([])
const replayDate = ref('')
const replayLoading = ref(false)
const replayInfo = ref<any>(null)
const replayRows = computed(() => replayInfo.value?.signals || [])

async function queryReplay() {
  if (!replayDate.value) {
    ElMessage.warning('请先选择要回放的交易日')
    return
  }
  replayLoading.value = true
  try {
    const res = await monitorApi.marketReplay(replayDate.value)
    if (res?.success === false) {
      ElMessage.error(res?.error || '回放失败')
      return
    }
    replayInfo.value = res
  } catch { /* 请求层已提示 */ } finally {
    replayLoading.value = false
  }
}

function clearReplay() {
  replayInfo.value = null
}

// 日期输入框强制 autocomplete=new-password：Chrome 对 "off" 不生效，
// 会在单击日期框时误弹「使用已保存的密码」提示（已确认 Element Plus 内部写死 off）
function suppressPasswordHint() {
  nextTick(() => {
    document.querySelectorAll('.el-date-editor input').forEach((el) => {
      el.setAttribute('autocomplete', 'new-password')
      el.setAttribute('name', 'replay_date')
    })
  })
}
watch(replayDate, suppressPasswordHint)
onMounted(suppressPasswordHint)

const rows = computed(() =>
  signals.value.filter((s) => {
    if (activeTab.value === 'trigger') return s.stage === 'trigger'
    if (activeTab.value === 'watch') return s.stage === 'watch'
    return true
  })
)
const triggerCount = computed(() => signals.value.filter((s) => s.stage === 'trigger').length)
const watchCount = computed(() => signals.value.filter((s) => s.stage === 'watch').length)
const isWatched = (code: string) => watchCodes.value.includes(code)
const pendingCodes = computed(() =>
  rows.value.map((s) => s.code).filter((c) => !isWatched(c))
)

const changeColor = (v: number | null | undefined) =>
  v == null ? '#909399' : v >= 0 ? '#c62828' : '#2e7d32'
const fmtChange = (v: number | null | undefined) =>
  v == null ? '--' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
const fmtMoney = (v: number | null | undefined) => {
  if (v == null) return '--'
  const a = Math.abs(v)
  const s = v >= 0 ? '+' : '-'
  if (a >= 1e8) return `${s}${(a / 1e8).toFixed(2)}亿`
  if (a >= 1e4) return `${s}${(a / 1e4).toFixed(0)}万`
  return `${s}${a.toFixed(0)}`
}

async function loadData() {
  try {
    const [sigRes, statusRes, watchRes] = await Promise.all([
      monitorApi.marketSignals(),
      monitorApi.marketScanStatus(),
      monitorApi.watchlistCodes()
    ])
    signals.value = sigRes?.signals || []
    snapshot.value = sigRes?.snapshot || {}
    status.value = statusRes || {}
    watchCodes.value = watchRes?.codes || []
  } catch { /* 请求层已提示 */ }
}

async function scanNow(push = false) {
  if (push) pushing.value = true
  else loading.value = true
  try {
    const res = await monitorApi.marketScan(push)
    if (res?.success === false) {
      ElMessage.warning(res?.error || '扫描未完成')
      return
    }
    await loadData()
    const dateTip = res.scan_date_label ? `${res.scan_date_label} ` : ''
    if (push) {
      ElMessage.success(
        `${dateTip}推送完成：放量启动 ${res.trigger_count || 0} / 观察中 ${res.watch_count || 0}，邮件已发 ${res.pushed_channels || 0} 个渠道`
      )
    } else {
      ElMessage.success(`${dateTip}扫描完成：候选 ${res.candidates || 0} 只，命中 ${res.signal_count || 0} 只，耗时 ${res.duration || 0}s`)
    }
  } catch { /* 请求层已提示 */ } finally {
    loading.value = false
    pushing.value = false
  }
}

async function addToWatchlist(code?: string) {
  const codes = code ? [code] : pendingCodes.value
  if (!codes.length) {
    ElMessage.info(code ? '该股票已在标的池中' : '当前结果已全部在标的池')
    return
  }
  adding.value = true
  try {
    const res = await monitorApi.watchlistAddBatch(codes)
    watchCodes.value = res?.codes || Array.from(new Set([...watchCodes.value, ...codes]))
    const added = res?.added?.length || 0
    if (added > 0) ElMessage.success(`已加入标的池 ${added} 只`)
    else ElMessage.info('这些股票已在标的池中')
  } catch { /* 请求层已提示 */ } finally {
    adding.value = false
  }
}

onMounted(loadData)
</script>

<template>
  <div class="p-4">
    <!-- 顶部说明 -->
    <ElCard shadow="never" class="mb-4">
      <div class="flex items-center justify-between flex-wrap gap-3">
        <div>
          <div class="flex items-center gap-2">
            <span class="text-lg font-semibold">全市场机会 · 洗盘反包（情绪点火闸门）</span>
            <ElTag type="danger" size="small" effect="dark">全市场扫描</ElTag>
          </div>
          <p class="text-sm text-gray-500 mt-2">
            每天 14:00 / 14:40 自动扫描近 20 日涨停股，跑「洗盘反包」主战法（大跌洗盘后首板反包）。
            情绪点火闸门：昨日涨停股今日涨幅中位数 ≥ 阈值（默认 3%）视为点火日，非点火日自动暂停开新仓
            ——三个月全市场回测胜率 22.8% → 64.5%、平均收益 +5.36%（阈值可在设置页调整，抓妖股模式绕过闸门）。
            <span class="block mt-1">
              数据交易日：<span class="text-blue-600 font-medium">{{ snapshot.scan_date_label || '待扫描' }}</span>
              <span v-if="snapshot.is_closed" class="text-amber-600">
                （当前{{ snapshot.market_label || '休市' }}，自动取最近一个交易日的数据）
              </span>
            </span>
          </p>
        </div>
        <div class="flex gap-2 flex-wrap">
          <ElButton :icon="Refresh" :loading="loading" @click="scanNow(false)">立即扫描</ElButton>
          <ElButton type="primary" :icon="Bell" :loading="pushing" @click="scanNow(true)">扫描并推送</ElButton>
          <ElButton
            type="success"
            plain
            :icon="Plus"
            :loading="adding"
            :disabled="!pendingCodes.length"
            @click="addToWatchlist()"
          >批量加池{{ pendingCodes.length ? `(${pendingCodes.length})` : '' }}</ElButton>
        </div>
      </div>
      <ElDivider style="margin:12px 0" />
      <div class="flex items-center flex-wrap gap-2">
        <span class="text-sm text-gray-500 font-medium">历史回放：</span>
        <ElDatePicker
          v-model="replayDate"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="选择任意交易日"
          :clearable="false"
          style="width:160px"
        />
        <ElButton type="primary" plain :loading="replayLoading" @click="queryReplay">回放查询</ElButton>
        <ElButton v-if="replayInfo" @click="clearReplay">返回实时</ElButton>
        <span class="text-xs text-gray-400">
          按当前策略与配置重放该交易日的扫描（收盘封板口径，无未来函数）。仅作历史查询展示，
          <span class="text-amber-600">不影响每日 14:00/14:40 的自动扫描与推送任务</span>，两者相互独立。
        </span>
      </div>
    </ElCard>

    <!-- 统计卡 -->
    <ElRow :gutter="12" class="mb-4">
      <ElCol :span="6">
        <ElCard shadow="never">
          <div class="text-sm text-gray-500">数据交易日</div>
          <div class="text-lg font-semibold mt-1">{{ snapshot.scan_date_label || '待扫描' }}</div>
          <div class="text-xs text-gray-400 mt-1">上次扫描 {{ snapshot.last_scan ? snapshot.last_scan.replace('T',' ') : '--' }}</div>
        </ElCard>
      </ElCol>
      <ElCol :span="6">
        <ElCard shadow="never"><div class="text-sm text-gray-500">候选 / 已扫</div><div class="text-lg font-semibold mt-1">{{ snapshot.candidates || 0 }} / {{ snapshot.scanned || 0 }}</div></ElCard>
      </ElCol>
      <ElCol :span="6">
        <ElCard shadow="never"><div class="text-sm text-gray-500">放量启动</div><div class="text-lg font-semibold mt-1 text-red-600">{{ triggerCount }}</div></ElCard>
      </ElCol>
      <ElCol :span="6">
        <ElCard shadow="never"><div class="text-sm text-gray-500">观察中</div><div class="text-lg font-semibold mt-1" style="color:#b8860b">{{ watchCount }}</div></ElCard>
      </ElCol>
    </ElRow>

    <!-- 推送时点状态 -->
    <ElCard shadow="never" class="mb-4">
      <div class="flex items-center flex-wrap gap-4 text-sm">
        <span class="font-semibold flex items-center gap-1"><Timer class="w-4 h-4" />定时推送</span>
        <ElTag :type="status.running ? 'success' : 'info'" size="small">
          {{ status.running ? '已启动' : '未启动' }}
        </ElTag>
        <span class="text-gray-500">时点：{{ (status.times || []).join(' / ') || '14:00 / 14:40' }}</span>
        <span v-if="snapshot.ignition?.median != null" class="text-gray-500 flex items-center gap-1">
          情绪点火：
          <ElTag :type="snapshot.ignition.ignited ? 'success' : 'danger'" size="small" effect="plain">
            {{ snapshot.ignition.ignited ? '已点火' : '未点火' }}
            {{ snapshot.ignition.median >= 0 ? '+' : '' }}{{ snapshot.ignition.median }}% / 阈值{{ snapshot.ignition.threshold }}%
          </ElTag>
          <ElTag v-if="snapshot.ignition.ignited && snapshot.ignition.today_zt != null"
                 :type="snapshot.ignition.size_tier === 'full' ? 'success' : 'warning'" size="small" effect="dark">
            {{ snapshot.ignition.size_tier === 'full' ? '全仓档' : '试错档' }}·今日涨停{{ snapshot.ignition.today_zt }}家
          </ElTag>
          <span class="text-gray-400 text-xs">{{ snapshot.ignition.prev_date }} 涨停股 {{ snapshot.ignition.n }} 只今日表现中位数</span>
        </span>
        <span v-if="status.jobs?.length" class="text-gray-400 text-xs">
          下次：{{ status.jobs[0].next_run || '--' }}
        </span>
        <span v-if="snapshot.filtered_out" class="text-gray-500">
          已按板块/价格淘汰 {{ snapshot.filtered_out }} 只
        </span>
      </div>
    </ElCard>

    <!-- 历史回放结果 -->
    <ElCard v-if="replayInfo" shadow="never" class="mb-4">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">
            历史回放 · {{ replayInfo.date }}
            <ElTag :type="replayInfo.gate.ignited ? 'success' : 'danger'" size="small" effect="dark" class="ml-2">
              {{ replayInfo.gate.ignited ? '已点火' : '未点火' }} {{ replayInfo.gate.premium }}%
            </ElTag>
            <ElTag v-if="replayInfo.gate.ignited" :type="replayInfo.gate.size_tier === 'full' ? 'success' : 'warning'"
                   size="small" class="ml-1">
              {{ replayInfo.gate.size_tier_label }} · 涨停{{ replayInfo.gate.n_zt }}家
            </ElTag>
          </span>
          <span class="text-xs text-gray-400">
            候选 {{ replayInfo.candidates }} 只 / 触发 {{ replayInfo.signal_count }} 只
            <ElButton link type="primary" size="small" @click="clearReplay">返回实时</ElButton>
          </span>
        </div>
      </template>
      <p v-if="replayInfo.blocked_reason" class="text-sm text-amber-600 m-0">
        {{ replayInfo.blocked_reason }} —— 当日按纪律不开仓，下表为空属正常。
      </p>
      <template v-else>
        <p class="text-sm m-0 mb-2">
          卖出配对（首确认）：胜率 <b class="good">{{ replayInfo.sell_stats.win }}%</b> ·
          平均收益 <b :class="replayInfo.sell_stats.avg >= 0 ? 'good' : 'neg'">{{ replayInfo.sell_stats.avg }}%</b> ·
          总营收 <b :class="replayInfo.sell_stats.total >= 0 ? 'good' : 'neg'">{{ replayInfo.sell_stats.total.toLocaleString() }} 元</b> ·
          已平仓 {{ replayInfo.sell_stats.sold }} / 持仓中 {{ replayInfo.sell_stats.open }}
        </p>
        <ElTable :data="replayRows" stripe size="small"
                 empty-text="当日点火成立但无反包形态触发">
          <ElTableColumn label="代码" width="90" prop="code" />
          <ElTableColumn label="名称" width="110" prop="name" />
          <ElTableColumn label="战法" width="105" prop="strategy" />
          <ElTableColumn label="买入价" width="85" prop="price" />
          <ElTableColumn label="卖出日" width="90" prop="sell_date" />
          <ElTableColumn label="卖出价" width="85" prop="sell_price" />
          <ElTableColumn label="收益" width="85">
            <template #default="{ row }">
              <span :class="row.r_sell >= 0 ? 'good' : 'neg'">{{ row.r_sell >= 0 ? '+' : '' }}{{ row.r_sell }}%</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="持有(自然/交易)" width="125">
            <template #default="{ row }">{{ row.hold_nat }} / {{ row.hold_trd }}</template>
          </ElTableColumn>
          <ElTableColumn label="触发理由" min-width="300">
            <template #default="{ row }">
              <span class="text-xs">{{ row.reason }}</span>
            </template>
          </ElTableColumn>
        </ElTable>
      </template>
      <p class="text-xs text-gray-400 m-0 mt-2">
        回放口径：收盘封板重建候选池，点火读数为收盘中位溢价（实盘为盘中快照+东财池含炸板，读数略有差异）；
        卖出=真实卖出引擎首次尾盘确认，未触发者按数据末日收盘浮动；结果已按当日配置缓存。
      </p>
    </ElCard>

    <!-- 信号表 -->
    <ElCard shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">命中信号（{{ rows.length }} 只）</span>
          <ElRadioGroup v-model="activeTab" size="small">
            <ElRadioButton value="all">全部</ElRadioButton>
            <ElRadioButton value="trigger">放量启动</ElRadioButton>
            <ElRadioButton value="watch">观察中</ElRadioButton>
          </ElRadioGroup>
        </div>
      </template>
      <ElTable :data="rows" stripe size="small" v-loading="loading" empty-text="当前无命中信号（可点「立即扫描」或调整筛选条件）">
        <ElTableColumn label="阶段" width="100">
          <template #default="{ row }">
            <ElTag :type="row.stage === 'trigger' ? 'danger' : 'warning'" size="small" effect="dark">
              {{ row.stage_label }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="战法" width="105">
          <template #default="{ row }">
            <ElTag :type="row.strategy === '强势续攻' ? 'success' : 'info'" size="small" effect="plain">
              {{ row.strategy || '涨停回调' }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="股票名称" min-width="130">
          <template #default="{ row }">
            <div class="font-medium">{{ row.name }}</div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="代码" prop="code" width="90" />
        <ElTableColumn label="现价" width="80" align="right">
          <template #default="{ row }">{{ row.price }}</template>
        </ElTableColumn>
        <ElTableColumn label="涨跌幅" width="90" align="right">
          <template #default="{ row }">
            <span :style="{ color: changeColor(row.change_pct) }">{{ fmtChange(row.change_pct) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="量比" width="80" align="right">
          <template #default="{ row }">
            <span :style="{ color: row.volume_ratio != null && row.volume_ratio >= 1.5 ? '#c62828' : '#606266' }">
              {{ row.volume_ratio ?? '--' }}
            </span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="换手率" width="85" align="right">
          <template #default="{ row }">{{ row.turnover != null ? row.turnover + '%' : '--' }}</template>
        </ElTableColumn>
        <ElTableColumn label="主力净流入" width="110" align="right">
          <template #default="{ row }">
            <span :style="{ color: changeColor(row.net_inflow) }">{{ fmtMoney(row.net_inflow) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="命中理由" min-width="320">
          <template #default="{ row }">
            <span class="text-xs">{{ (row.reasons || []).join(' · ') }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="强度" width="70" align="center">
          <template #default="{ row }">
            <ElTag size="small" :type="row.strength >= 6 ? 'danger' : 'warning'">{{ row.strength }}</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="110" align="center" fixed="right">
          <template #default="{ row }">
            <ElButton
              v-if="!isWatched(row.code)"
              link
              type="primary"
              size="small"
              :loading="adding"
              @click="addToWatchlist(row.code)"
            >加入标的池</ElButton>
            <ElTag v-else size="small" type="info" effect="plain">已在池</ElTag>
          </template>
        </ElTableColumn>
      </ElTable>
      <p class="text-xs text-gray-400 mt-3">
        数据说明：基于 AKShare 公开日 K 数据计算，盘中为近似值；量比 = 当日量 / 前 5 日均量；资金流来自同花顺。红涨绿跌。
      </p>
    </ElCard>
  </div>
</template>

<style scoped>
/* A股配色：盈利红、亏损绿（与系统红涨绿跌一致） */
.good {
  color: #d92b2b;
  font-weight: 600;
}
.neg {
  color: #0a8f3c;
  font-weight: 600;
}
</style>
