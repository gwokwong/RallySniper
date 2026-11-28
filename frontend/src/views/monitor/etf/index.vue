<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { monitorApi } from '@/api/monitor'
import type { EtfPoolItem, EtfSignal, EtfSearchItem, EtfScanItem } from '@/api/monitor'

defineOptions({ name: 'EtfMonitor' })

const pool = ref<EtfPoolItem[]>([])
const signals = ref<EtfSignal[]>([])
const scanItems = ref<EtfScanItem[]>([])
const scanSignals = ref<EtfScanSignal[]>([])
const scanDate = ref('--')
const loading = ref(false)
const scanning = ref(false)
const searchQ = ref('')
const searchResults = ref<EtfSearchItem[]>([])
const searching = ref(false)
const hotlist = ref<{ pct: EtfSearchItem[]; volume: EtfSearchItem[]; n: number } | null>(null)
const activeTab = ref('pool')

const loadData = async () => {
  loading.value = true
  try {
    const [st, sigs] = await Promise.all([
      monitorApi.etfStatus(),
      monitorApi.etfSignals(50)
    ])
    pool.value = st.pool || []
    signals.value = sigs.items || []
    scanItems.value = (st.last_scan?.items || []) as EtfScanItem[]
    scanSignals.value = (st.last_scan?.signals || []) as EtfScanSignal[]
    scanDate.value = st.last_scan?.date || '--'
  } finally {
    loading.value = false
  }
}

const runScan = async () => {
  scanning.value = true
  try {
    await monitorApi.etfScan()
    ElMessage.success('扫描完成（信号收盘确认、次日执行）')
    await loadData()
  } finally {
    scanning.value = false
  }
}

const runRefresh = async () => {
  scanning.value = true
  try {
    await monitorApi.etfRefresh()
    ElMessage.success('K线缓存已刷新')
    await loadData()
  } finally {
    scanning.value = false
  }
}

const doSearch = async () => {
  if (!searchQ.value.trim()) return
  searching.value = true
  try {
    const r = await monitorApi.etfSearch(searchQ.value.trim())
    searchResults.value = r.items || []
  } finally {
    searching.value = false
  }
}

const addToPool = async (row: EtfSearchItem) => {
  await monitorApi.etfPoolAdd(row.code, row.name)
  ElMessage.success(`${row.name || row.code} 已加入池子`)
  await loadData()
}

const toggleHold = async (row: EtfPoolItem) => {
  await monitorApi.etfHoldingsToggle(row.code)
  ElMessage.success(row.in_position ? `${row.name} 已取消持仓标记` : `${row.name} 已标记持仓——跌破中轨将盘中预警`)
  await loadData()
}

const removeFromPool = async (row: EtfPoolItem) => {
  await ElMessageBox.confirm(`确认把 ${row.name || row.code} 移出监控池？`, '提示', { type: 'warning' })
  await monitorApi.etfPoolRemove(row.code)
  ElMessage.success('已移出')
  await loadData()
}

const loadHotlist = async () => {
  hotlist.value = await monitorApi.etfHotlist(15)
}

const onTabChange = (tab: string) => {
  if (tab === 'hot') loadHotlist()
}

const inPosCount = computed(() => pool.value.filter((p) => p.in_position).length)

/** 最近一次扫描中，各 ETF 命中的战法（含 watch 级） */
const hitsByCode = computed<Record<string, string[]>>(() => {
  const m: Record<string, string[]> = {}
  for (const s of scanSignals.value) {
    (m[s.code] = m[s.code] || []).push(s.stage_label)
  }
  return m
})
const lastScanTitle = computed(() => {
  const ls = (pool.value.length, signals.value[0])
  return ls ? `${ls.tdate}` : '--'
})

const retColor = (v?: number | null) =>
  v === null || v === undefined ? '#999' : v >= 0 ? '#c62828' : '#2e7d32'
const fmtRet = (v?: number | null) =>
  v === null || v === undefined ? '--' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
const outTag = (o?: string) =>
  ({ resolved: 'success', pending: 'warning', no_data: 'info' })[o || 'pending'] || 'info'
const outLabel = (o?: string) =>
  ({ resolved: '已验证', pending: '待验证', no_data: '无数据' })[o || 'pending'] || '--'

onMounted(loadData)
</script>

<template>
  <div class="p-4 flex flex-col gap-4">
    <el-card shadow="never">
      <div class="flex items-center justify-between flex-wrap gap-2">
        <div class="flex items-center gap-2">
          <span class="text-lg font-bold">ETF 监控</span>
          <el-tag type="success" size="small">S4 波动收缩突破（回测唯一稳健）</el-tag>
          <el-tag type="info" size="small">T+1 · 收盘确认 · 次日执行</el-tag>
        </div>
        <div class="flex gap-2">
          <el-button type="primary" :loading="scanning" @click="runScan">立即扫描</el-button>
          <el-button :loading="scanning" @click="runRefresh">刷新缓存</el-button>
        </div>
      </div>
    </el-card>

    <!-- 池子 -->
    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-bold">
            监控池（{{ pool.length }} 只 · 纸面持仓 {{ inPosCount }}）
            <el-tag size="small" type="info" class="ml-2">最近信号 {{ lastScanTitle }}</el-tag>
          </span>
        </div>
      </template>
      <el-table :data="pool" size="small" stripe :loading="loading">
        <el-table-column prop="code" label="代码" width="90" />
        <el-table-column prop="name" label="名称" min-width="130" />
        <el-table-column label="现价" width="90" align="center">
          <template #default="{ row }">{{ row.price ?? '--' }}</template>
        </el-table-column>
        <el-table-column prop="date" label="数据日期" width="110" align="center" />
        <el-table-column label="纸面持仓" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="row.in_position ? 'danger' : 'info'" size="small">
              {{ row.in_position ? '持有' : '空仓' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="成本" width="90" align="center">
          <template #default="{ row }">{{ row.entry_price ?? '--' }}</template>
        </el-table-column>
        <el-table-column label="浮盈" width="90" align="center">
          <template #default="{ row }">
            <span :style="{ color: retColor(row.pnl_pct) }">{{ fmtRet(row.pnl_pct) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="生命线(中轨)" width="110" align="center">
          <template #default="{ row }">{{ row.life_line ?? '--' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.flags?.freeze" type="warning" size="small">❄️冰点</el-tag>
            <el-tag v-else-if="row.flags?.squeeze" type="primary" size="small">收缩中</el-tag>
            <span v-else class="text-gray-400">--</span>
          </template>
        </el-table-column>
        <el-table-column label="战法命中" min-width="150">
          <template #default="{ row }">
            <template v-if="(hitsByCode[row.code] || []).length">
              <el-tag v-for="t in (hitsByCode[row.code] || []).slice(0, 3)" :key="t" size="small"
                      :type="t.includes('买入') || t.includes('翻多') ? 'danger' : t.includes('冰点') ? 'warning' : 'info'"
                      class="mr-1">{{ t }}</el-tag>
            </template>
            <span v-else class="text-gray-400">--</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" align="center">
          <template #default="{ row }">
            <el-button link type="danger" size="small" @click="removeFromPool(row)">移出</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 今日扫描（含 watch 级：冰点/收缩观察，不进验证闭环） -->
    <el-card shadow="never">
      <template #header>
        <span class="font-bold">今日扫描（{{ scanDate }} · 站内+邮件同步推送）</span>
      </template>
      <div v-if="!scanItems.length" class="text-gray-400 text-sm">暂无扫描记录——每日 15:20 自动扫描，或点右上角「立即扫描」</div>
      <template v-else>
        <div class="flex flex-wrap gap-2 mb-3">
          <el-tag v-for="it in scanItems" :key="it.code" size="small"
                  :type="it.flags?.freeze ? 'warning' : it.flags?.squeeze ? 'primary' : 'info'">
            {{ it.name }} {{ it.chg >= 0 ? '+' : '' }}{{ it.chg.toFixed(1) }}%
            {{ it.flags?.freeze ? '❄️冰点' : it.flags?.squeeze ? '收缩中' : '' }}
          </el-tag>
        </div>
        <el-table v-if="scanSignals.length" :data="scanSignals" size="small" stripe>
          <el-table-column prop="stage_label" label="级别" width="110" align="center">
            <template #default="{ row }">
              <el-tag :type="row.stage === 'trigger' ? 'danger' : 'warning'" size="small">{{ row.stage_label }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="标的" min-width="140">
            <template #default="{ row }">{{ row.name }}<span class="text-gray-400">({{ row.code }})</span></template>
          </el-table-column>
          <el-table-column prop="price" label="信号价" width="90" align="center" />
          <el-table-column label="理由" min-width="260">
            <template #default="{ row }">
              <span class="text-xs text-gray-500">{{ (row.reasons || []).join('；') }}</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-else class="text-gray-400 text-sm">今日无买卖/观察信号</div>
      </template>
    </el-card>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- 信号 -->
      <el-tab-pane label="信号与验证" name="signals">
        <el-card shadow="never">
          <el-table :data="signals" size="small" stripe>
            <el-table-column prop="tdate" label="信号日" width="100" />
            <el-table-column label="标的" min-width="140">
              <template #default="{ row }">{{ row.name }}<span class="text-gray-400">({{ row.code }})</span></template>
            </el-table-column>
            <el-table-column label="方向" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="row.direction === 'down' ? 'warning' : 'danger'" size="small">
                  {{ row.direction === 'down' ? '卖出' : '买入' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="信号价" width="90" align="center">
              <template #default="{ row }">{{ row.entry_price ?? '--' }}</template>
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
                <el-tag :type="outTag(row.outcome?.status)" size="small">{{ outLabel(row.outcome?.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="理由" min-width="200">
              <template #default="{ row }">
                <span class="text-xs text-gray-500">{{ (row.reasons || []).join('；') }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- 搜索添加 -->
      <el-tab-pane label="搜索添加" name="search">
        <el-card shadow="never">
          <div class="flex gap-2 mb-3">
            <el-input v-model="searchQ" placeholder="输入 ETF 代码或名称关键字，如 半导体 / 588" clearable
                      style="width: 320px" @keyup.enter="doSearch" />
            <el-button type="primary" :loading="searching" @click="doSearch">搜索</el-button>
          </div>
          <el-table :data="searchResults" size="small" stripe max-height="420">
            <el-table-column prop="code" label="代码" width="100" />
            <el-table-column prop="name" label="名称" min-width="160" />
            <el-table-column prop="price" label="现价" width="90" align="center" />
            <el-table-column label="当日涨幅" width="100" align="center">
              <template #default="{ row }">
                <span :style="{ color: retColor(row.pct) }">{{ fmtRet(row.pct) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="100" align="center">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="addToPool(row)">加入池子</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- 全市场热榜 -->
      <el-tab-pane label="全市场热榜" name="hot">
        <el-card shadow="never">
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <div class="font-bold mb-2 text-sm">当日涨幅榜</div>
              <el-table :data="hotlist?.pct || []" size="small" stripe max-height="420">
                <el-table-column prop="code" label="代码" width="100" />
                <el-table-column prop="name" label="名称" min-width="150" />
                <el-table-column label="涨幅" width="90" align="center">
                  <template #default="{ row }">
                    <span :style="{ color: retColor(row.pct) }">{{ fmtRet(row.pct) }}</span>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            <div>
              <div class="font-bold mb-2 text-sm">成交量榜</div>
              <el-table :data="hotlist?.volume || []" size="small" stripe max-height="420">
                <el-table-column prop="code" label="代码" width="100" />
                <el-table-column prop="name" label="名称" min-width="150" />
                <el-table-column label="现价" width="90" align="center">
                  <template #default="{ row }">{{ row.price ?? '--' }}</template>
                </el-table-column>
              </el-table>
            </div>
          </div>
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>
