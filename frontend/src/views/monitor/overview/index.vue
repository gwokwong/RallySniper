<!-- 主升浪盯盘总览：友好信号 / 危险信号 -->
<template>
  <div class="wave-overview">
    <ElCard class="mb-4" shadow="never">
      <div class="flex items-center justify-between flex-wrap gap-3">
        <div class="flex items-center gap-3">
          <span class="text-lg font-semibold">主升浪捕手</span>
          <ElTag :type="marketTagType" effect="dark" round>
            {{ market.label }}
          </ElTag>
          <span class="text-sm text-gray-500">{{ market.date }} {{ market.time }}</span>
        </div>
        <div class="flex items-center gap-2">
          <ElButton :loading="loading" @click="loadData">
            <ElIcon><Refresh /></ElIcon>
            <span class="ml-1">刷新</span>
          </ElButton>
          <ElButton type="primary" :loading="scanning" @click="handleScan">
            <ElIcon><Search /></ElIcon>
            <span class="ml-1">立即扫描并发邮件</span>
          </ElButton>
        </div>
      </div>
      <div class="mt-3 flex flex-wrap gap-3">
        <ElAlert
          v-if="!monitor.email_configured"
          type="warning"
          :closable="false"
          show-icon
          title="尚未配置邮件"
          description="请到「邮件与配置」页填写 SMTP 并启用，命中信号后才能收到提醒邮件。"
        />
        <ElAlert
          v-else
          type="success"
          :closable="false"
          show-icon
          :title="monitor.running ? '自动盯盘运行中' : '邮件已配置（自动盯盘未开启）'"
          :description="monitor.running
            ? `每 ${monitor.interval} 秒扫描一次标的池，命中信号自动发邮件（同一信号24小时内不重复）。`
            : '到「邮件与配置」页开启自动盯盘，实现无人值守。'"
        />
      </div>
    </ElCard>

    <ElRow :gutter="16" class="mb-4">
      <ElCol :xs="12" :sm="6" v-for="card in statCards" :key="card.key">
        <ElCard shadow="never" class="stat-card">
          <div class="flex items-center justify-between">
            <div>
              <div class="text-sm text-gray-500">{{ card.label }}</div>
              <div class="text-2xl font-bold mt-1" :style="{ color: card.color }">
                {{ card.value }}
              </div>
            </div>
            <div class="stat-icon" :style="{ background: card.color + '18', color: card.color }">
              <ElIcon :size="22"><component :is="card.icon" /></ElIcon>
            </div>
          </div>
        </ElCard>
      </ElCol>
    </ElRow>

    <ElCard shadow="never" class="mb-4">
      <template #header>
        <div class="flex items-center justify-between flex-wrap gap-2">
          <div class="flex items-center gap-2">
            <span class="font-semibold text-lg">友好信号 · 该关注</span>
            <ElTag size="small" type="danger" effect="dark" v-if="friendlyRows.length">
              {{ friendlyRows.length }} 只
            </ElTag>
          </div>
          <ElRadioGroup v-model="friendlyFilter" size="small">
            <ElRadioButton value="all">全部</ElRadioButton>
            <ElRadioButton value="trigger">仅放量启动</ElRadioButton>
            <ElRadioButton value="watch">仅观察中</ElRadioButton>
          </ElRadioGroup>
        </div>
      </template>
      <ElTable
        :data="friendlyRows"
        v-loading="loading"
        stripe
        size="small"
        empty-text="暂无友好信号：等待首板后缩量洗盘的标的出现"
      >
        <ElTableColumn label="股票名称" width="140">
          <template #default="{ row }">
            <span class="font-medium">{{ row.name || '—' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="股票代码" prop="code" width="110" />
        <ElTableColumn label="阶段" width="110">
          <template #default="{ row }">
            <ElTag v-if="row.hasTrigger" type="danger" size="small" effect="dark">放量启动</ElTag>
            <ElTag v-else type="warning" size="small" effect="plain">观察中</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="现价" width="90" align="right">
          <template #default="{ row }">{{ row.price }}</template>
        </ElTableColumn>
        <ElTableColumn label="涨跌幅" width="100" align="right">
          <template #default="{ row }">
            <span :style="{ color: row.change_pct >= 0 ? '#c62828' : '#2e7d32' }">
              {{ row.change_pct >= 0 ? '+' : '' }}{{ row.change_pct }}%
            </span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="信号理由" min-width="420">
          <template #default="{ row }">
            <div class="reason-list">
              <div v-for="(s, i) in row.signals" :key="i" class="reason-line">
                <span class="reason-name" :class="s.stage === 'trigger' ? 'up' : 'watch'">
                  {{ s.stage_label || s.strategy }}
                </span>
                <span class="text-xs">{{ (s.reasons || []).join(' · ') }}</span>
              </div>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="强度" width="80" align="center">
          <template #default="{ row }">
            <ElTag size="small" :type="row.maxStrength >= 6 ? 'danger' : 'warning'">
              {{ row.maxStrength }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="信号日" prop="date" width="110" />
      </ElTable>
    </ElCard>

    <ElCard shadow="never">
      <template #header>
        <div class="flex items-center gap-2">
          <span class="font-semibold text-lg">危险信号 · 该离场</span>
          <ElTag size="small" type="success" effect="dark" v-if="dangerRows.length">
            {{ dangerRows.length }} 只
          </ElTag>
          <span class="text-xs text-gray-400 ml-2">跌破生命线或均线死叉，趋势走坏</span>
        </div>
      </template>
      <ElTable
        :data="dangerRows"
        v-loading="loading"
        stripe
        size="small"
        empty-text="暂无危险信号：标的池趋势正常"
      >
        <ElTableColumn label="股票名称" width="140">
          <template #default="{ row }">
            <span class="font-medium">{{ row.name || '—' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="股票代码" prop="code" width="110" />
        <ElTableColumn label="危险战法" width="130">
          <template #default="{ row }">
            <div class="flex flex-col gap-1">
              <ElTag v-for="(s, i) in row.signals.slice(0, 3)" :key="i" type="success" size="small" effect="plain">
                {{ s.strategy }}
              </ElTag>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="现价" width="90" align="right">
          <template #default="{ row }">{{ row.price }}</template>
        </ElTableColumn>
        <ElTableColumn label="涨跌幅" width="100" align="right">
          <template #default="{ row }">
            <span :style="{ color: row.change_pct >= 0 ? '#c62828' : '#2e7d32' }">
              {{ row.change_pct >= 0 ? '+' : '' }}{{ row.change_pct }}%
            </span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="离场理由" min-width="420">
          <template #default="{ row }">
            <div class="reason-list">
              <div v-for="(s, i) in row.signals" :key="i" class="reason-line">
                <span class="reason-name down">↓ {{ s.strategy }}</span>
                <span class="text-xs">{{ (s.reasons || []).join(' · ') }}</span>
              </div>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="强度" width="80" align="center">
          <template #default="{ row }">
            <ElTag size="small" type="success">{{ row.maxStrength }}</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="信号日" prop="date" width="110" />
      </ElTable>
      <div class="mt-3 text-xs text-gray-400">
        <p>
          放量启动：首板涨停后缩量洗盘、守住涨停日最低价，且今日或昨日重新放量未破位（买点）。
          观察中：缩量到位但尚未放量，等待确认。危险信号：趋势走坏，注意离场。
        </p>
        <p class="mt-1">基于 AKShare 公开日 K 数据计算，收盘后最准确，盘中为近似值；仅供参考，不构成投资建议。</p>
      </div>
    </ElCard>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Search, Aim, TrendCharts, View, Warning } from '@element-plus/icons-vue'
import { monitorApi } from '@/api/monitor'

const loading = ref(false)
const scanning = ref(false)
const market = ref<any>({ label: '--', date: '', time: '' })
const monitor = ref<any>({})
const signals = ref<any[]>([])
const watchlistSize = ref(0)
const friendlyFilter = ref<'all' | 'trigger' | 'watch'>('all')

const groupByCode = (list: any[]) => {
  const map = new Map<string, any>()
  for (const s of list) {
    let row = map.get(s.code)
    if (!row) {
      row = {
        code: s.code,
        name: s.name || s.code,
        price: s.price,
        change_pct: s.change_pct,
        date: s.date,
        signals: [],
        maxStrength: 0,
        hasTrigger: false
      }
      map.set(s.code, row)
    }
    row.signals.push(s)
    if (s.stage === 'trigger') row.hasTrigger = true
    row.maxStrength = Math.max(row.maxStrength, Number(s.strength) || 0)
  }
  return Array.from(map.values()).sort((a, b) => b.maxStrength - a.maxStrength)
}

const friendlySignals = computed(() => signals.value.filter(s => s.direction === 'up' && s.stage))
const dangerSignals = computed(() => signals.value.filter(s => s.direction === 'down'))
const friendlyRows = computed(() =>
  groupByCode(
    friendlyFilter.value === 'all'
      ? friendlySignals.value
      : friendlySignals.value.filter(s => s.stage === friendlyFilter.value)
  )
)
const dangerRows = computed(() => groupByCode(dangerSignals.value))

const triggerCount = computed(() =>
  new Set(friendlySignals.value.filter(s => s.stage === 'trigger').map(s => s.code)).size
)
const watchCount = computed(() => {
  const triggerCodes = new Set(friendlySignals.value.filter(s => s.stage === 'trigger').map(s => s.code))
  return new Set(
    friendlySignals.value.filter(s => s.stage === 'watch' && !triggerCodes.has(s.code)).map(s => s.code)
  ).size
})
const dangerCount = computed(() => dangerRows.value.length)

const statCards = computed(() => [
  { key: 'watchlist', label: '标的池', value: watchlistSize.value, color: '#409eff', icon: Aim },
  { key: 'trigger', label: '放量启动', value: triggerCount.value, color: '#c62828', icon: TrendCharts },
  { key: 'watch', label: '观察中', value: watchCount.value, color: '#e6a23c', icon: View },
  { key: 'danger', label: '危险信号', value: dangerCount.value, color: '#2e7d32', icon: Warning }
])

const marketTagType = computed(() => {
  const map: Record<string, string> = {
    trading: 'success', lunch: 'warning', pre_open: 'info',
    after_hours: 'info', closed: 'info'
  }
  return map[market.value.status] || 'info'
})

async function loadData() {
  loading.value = true
  try {
    const overview = await monitorApi.mainwaveOverview()
    market.value = overview.market || market.value
    monitor.value = overview.monitor || {}
    watchlistSize.value = overview.watchlist_size || 0
    const full = await monitorApi.mainwaveScan(false)
    signals.value = full.signals || []
  } catch (e: any) {
    ElMessage.error('加载失败：' + (e?.message || e))
  } finally {
    loading.value = false
  }
}

async function handleScan() {
  scanning.value = true
  try {
    const res = await monitorApi.scan()
    const n = res.fresh_count ?? res.signal_count ?? 0
    const pushed = res.pushed_channels || 0
    if (pushed > 0) {
      ElMessage.success(`扫描完成：${n} 条新信号，邮件已发送`)
    } else if (n > 0) {
      ElMessage.warning(`扫描完成：${n} 条信号（处于冷却期或未配置邮件，未重复发送）`)
    } else {
      ElMessage.info('扫描完成：暂无信号命中')
    }
    await loadData()
  } catch (e: any) {
    ElMessage.error('扫描失败：' + (e?.message || e))
  } finally {
    scanning.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.stat-icon {
  width: 46px;
  height: 46px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.reason-line {
  padding: 3px 0;
  border-bottom: 1px dashed #eee;
}
.reason-line:last-child {
  border-bottom: none;
}
.reason-name {
  font-weight: 600;
  margin-right: 6px;
  white-space: nowrap;
}
.reason-name.up {
  color: #c62828;
}
.reason-name.watch {
  color: #b8860b;
}
.reason-name.down {
  color: #2e7d32;
}
</style>
