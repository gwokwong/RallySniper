<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { monitorApi } from '@/api/monitor'
import type { SignalOutcomeRecord, SignalOutcomeSummary } from '@/api/monitor'

defineOptions({ name: 'SignalOutcomes' })

const summary = ref<SignalOutcomeSummary | null>(null)
const rows = ref<SignalOutcomeRecord[]>([])
const loading = ref(false)
const running = ref(false)
const statusFilter = ref<'all' | 'pending' | 'resolved' | 'no_data'>('all')

const loadData = async () => {
  loading.value = true
  try {
    const [s, list] = await Promise.all([
      monitorApi.signalOutcomesSummary(),
      monitorApi.signalOutcomesSignals(statusFilter.value, 100)
    ])
    summary.value = s
    rows.value = list.items || []
  } finally {
    loading.value = false
  }
}

const runResolve = async () => {
  running.value = true
  try {
    const s = await monitorApi.signalOutcomesRun()
    summary.value = s
    ElMessage.success('结果回填完成')
    await loadData()
  } catch {
    // 请求层已提示
  } finally {
    running.value = false
  }
}

const onFilterChange = () => loadData()

/** 盈亏着色：A 股规范 红=正 绿=负 */
const retColor = (v?: number | null) =>
  v === null || v === undefined ? '#999' : v >= 0 ? '#c62828' : '#2e7d32'
const fmtRet = (v?: number | null) =>
  v === null || v === undefined ? '--' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
const fmtPrice = (v?: number | null) => (v === null || v === undefined ? '--' : `¥${v}`)
const dirTag = (d: string) => (d === 'down' ? 'warning' : 'danger')
const dirLabel = (d: string) => (d === 'down' ? '卖出预警' : '买入信号')
const statusTag = (s?: string) =>
  ({ resolved: 'success', pending: 'warning', no_data: 'info' })[s || 'pending'] || 'info'
const statusLabel = (s?: string) =>
  ({ resolved: '已验证', pending: '待验证', no_data: '无数据' })[s || 'pending'] || s

/** 分 horizon 统计卡 */
const horizonCards = computed(() => {
  const hz = summary.value?.horizons || {}
  return [
    { key: 't1', label: 'T+1', stat: hz.t1 },
    { key: 't5', label: 'T+5', stat: hz.t5 },
    { key: 't10', label: 'T+10', stat: hz.t10 }
  ].map((x) => ({
    ...x,
    n: x.stat?.n || 0,
    winRate: x.stat?.win_rate ?? null,
    avg: x.stat?.avg ?? null
  }))
})

const countCards = computed(() => [
  { key: 'total', label: '累计信号', value: summary.value?.total ?? 0, color: '#409eff' },
  { key: 'resolved', label: '已验证', value: summary.value?.resolved ?? 0, color: '#c62828' },
  { key: 'pending', label: '待验证', value: summary.value?.pending ?? 0, color: '#b8860b' },
  { key: 'no_data', label: '无数据', value: summary.value?.no_data ?? 0, color: '#999' }
])

onMounted(loadData)
</script>

<template>
  <div class="p-4 flex flex-col gap-4">
    <!-- 头部 -->
    <el-card shadow="never">
      <div class="flex items-center justify-between flex-wrap gap-2">
        <div class="flex items-center gap-2">
          <span class="text-lg font-bold">信号验证</span>
          <el-tag type="info" size="small">实盘 trigger 信号 · T+1/5/10 自动回填</el-tag>
          <el-tag type="info" size="small">卖出预警按 r&lt;0 判定正确</el-tag>
        </div>
        <div class="flex gap-2">
          <el-button type="primary" :loading="running" @click="runResolve">立即回填</el-button>
          <el-button :loading="loading" @click="loadData">刷新</el-button>
        </div>
      </div>
    </el-card>

    <!-- 累计计数 -->
    <el-row :gutter="10">
      <el-col v-for="c in countCards" :key="c.key" :xs="12" :sm="6">
        <el-card shadow="never" class="text-center">
          <div class="text-xs text-gray-500">{{ c.label }}</div>
          <div class="text-lg font-semibold mt-1" :style="{ color: c.color }">{{ c.value }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 分窗口胜率 -->
    <el-row :gutter="10">
      <el-col v-for="h in horizonCards" :key="h.key" :xs="12" :sm="8">
        <el-card shadow="never" class="text-center">
          <div class="text-xs text-gray-500">{{ h.label }} 胜率（{{ h.n }} 笔已验证）</div>
          <div class="text-2xl font-bold mt-1" :style="{ color: h.winRate === null ? '#999' : h.winRate >= 50 ? '#c62828' : '#2e7d32' }">
            {{ h.winRate === null ? '--' : `${h.winRate}%` }}
          </div>
          <div class="text-xs text-gray-400 mt-1">平均 {{ fmtRet(h.avg) }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 分策略胜率 -->
    <el-card shadow="never">
      <template #header>
        <span class="font-bold">分策略胜率（按 信号×方向 分组）</span>
      </template>
      <el-table :data="summary?.by_strategy || []" size="small" stripe>
        <el-table-column prop="strategy" label="战法" min-width="120" />
        <el-table-column label="方向" width="90">
          <template #default="{ row }">
            <el-tag :type="dirTag(row.direction)" size="small">{{ dirLabel(row.direction) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="total" label="累计" width="70" align="center" />
        <el-table-column prop="resolved" label="已验证" width="80" align="center" />
        <el-table-column label="T+1 胜率/均值" align="center" min-width="120">
          <template #default="{ row }">
            <span v-if="row.t1.n">{{ row.t1.win_rate }}% / <span :style="{ color: retColor(row.t1.avg) }">{{ fmtRet(row.t1.avg) }}</span></span>
            <span v-else>--</span>
          </template>
        </el-table-column>
        <el-table-column label="T+5 胜率/均值" align="center" min-width="120">
          <template #default="{ row }">
            <span v-if="row.t5.n">{{ row.t5.win_rate }}% / <span :style="{ color: retColor(row.t5.avg) }">{{ fmtRet(row.t5.avg) }}</span></span>
            <span v-else>--</span>
          </template>
        </el-table-column>
        <el-table-column label="T+10 胜率/均值" align="center" min-width="120">
          <template #default="{ row }">
            <span v-if="row.t10.n">{{ row.t10.win_rate }}% / <span :style="{ color: retColor(row.t10.avg) }">{{ fmtRet(row.t10.avg) }}</span></span>
            <span v-else>--</span>
          </template>
        </el-table-column>
        <el-table-column label="T+10 累计" align="center" width="100">
          <template #default="{ row }">
            <span v-if="row.t10.n" :style="{ color: retColor(row.t10.sum_wan) }">{{ row.t10.sum_wan }}万</span>
            <span v-else>--</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 信号明细 -->
    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-bold">信号明细（最近 100 条）</span>
          <el-select v-model="statusFilter" size="small" style="width: 120px" @change="onFilterChange">
            <el-option label="全部" value="all" />
            <el-option label="待验证" value="pending" />
            <el-option label="已验证" value="resolved" />
            <el-option label="无数据" value="no_data" />
          </el-select>
        </div>
      </template>
      <el-table :data="rows" size="small" stripe :loading="loading">
        <el-table-column prop="tdate" label="信号日" width="100" />
        <el-table-column label="股票" min-width="130">
          <template #default="{ row }">
            {{ row.name }}<span class="text-gray-400">({{ row.code }})</span>
          </template>
        </el-table-column>
        <el-table-column prop="strategy" label="战法" min-width="110" />
        <el-table-column label="方向" width="90">
          <template #default="{ row }">
            <el-tag :type="dirTag(row.direction)" size="small">{{ dirLabel(row.direction) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="入场价" width="90" align="center">
          <template #default="{ row }">{{ fmtPrice(row.entry_price) }}</template>
        </el-table-column>
        <el-table-column label="当日涨幅" width="90" align="center">
          <template #default="{ row }">
            <span :style="{ color: retColor(row.change_pct) }">{{ fmtRet(row.change_pct) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="T+1" width="80" align="center">
          <template #default="{ row }">
            <span :style="{ color: retColor(row.outcome?.r_t1) }">{{ fmtRet(row.outcome?.r_t1) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="T+5" width="80" align="center">
          <template #default="{ row }">
            <span :style="{ color: retColor(row.outcome?.r_t5) }">{{ fmtRet(row.outcome?.r_t5) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="T+10" width="80" align="center">
          <template #default="{ row }">
            <span :style="{ color: retColor(row.outcome?.r_t10) }">{{ fmtRet(row.outcome?.r_t10) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.outcome?.status)" size="small">
              {{ statusLabel(row.outcome?.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="source" label="来源" width="100" align="center" />
      </el-table>
    </el-card>
  </div>
</template>
