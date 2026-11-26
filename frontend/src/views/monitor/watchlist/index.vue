<!-- 标的池管理（自选股式） -->
<template>
  <div class="watchlist-page">
    <ElCard shadow="never" class="mb-4">
      <div class="flex items-center justify-between flex-wrap gap-3">
        <div class="flex items-center gap-3">
          <span class="text-lg font-semibold">标的池</span>
          <ElTag type="danger" effect="dark" round>{{ rows.length }} 只</ElTag>
          <span class="text-xs text-gray-400">更新于 {{ updatedAt }}</span>
        </div>
        <div class="flex items-center gap-2">
          <ElButton :loading="loading" @click="loadData()">
            <ElIcon><Refresh /></ElIcon>
            <span class="ml-1">刷新行情</span>
          </ElButton>
          <ElButton type="primary" :loading="scanning" @click="handleScan">
            <ElIcon><Search /></ElIcon>
            <span class="ml-1">扫描信号</span>
          </ElButton>
        </div>
      </div>
    </ElCard>

    <ElCard shadow="never" class="mb-4">
      <template #header><span class="font-semibold">添加标的</span></template>
      <div class="flex items-center gap-2 flex-wrap">
        <ElAutocomplete
          v-model="keyword"
          :fetch-suggestions="querySearch"
          placeholder="输入股票代码或名称，如 600519 / 贵州茅台"
          clearable
          trigger-on-focus
          style="width: 320px"
          @select="handleSelect"
        >
          <template #default="{ item }">
            <div class="flex justify-between items-center">
              <span>{{ item.name }}</span>
              <span class="text-xs text-gray-400">{{ item.code }}</span>
            </div>
          </template>
        </ElAutocomplete>
        <ElButton type="primary" :loading="adding" @click="handleAdd">
          <ElIcon><Plus /></ElIcon>
          <span class="ml-1">添加</span>
        </ElButton>
        <span class="text-xs text-gray-400">
          支持代码或名称搜索；添加后立即生效，自动盯盘同步使用
        </span>
      </div>
    </ElCard>

    <ElCard shadow="never">
      <template #header>
        <div class="flex items-center justify-between flex-wrap gap-2">
          <ElRadioGroup v-model="filter" size="small">
            <ElRadioButton value="all">全部 {{ rows.length }}</ElRadioButton>
            <ElRadioButton value="up">↑ 上涨 {{ upTotal }}</ElRadioButton>
            <ElRadioButton value="down">↓ 下跌 {{ downTotal }}</ElRadioButton>
            <ElRadioButton value="none">无信号</ElRadioButton>
          </ElRadioGroup>
          <span class="text-xs text-gray-400">行情基于日K收盘数据，盘中仅供参考</span>
        </div>
      </template>

      <ElTable
        :data="filteredRows"
        v-loading="loading"
        stripe
        size="small"
        empty-text="标的池为空，先在上方添加股票"
      >
        <ElTableColumn type="index" label="#" width="50" />
        <ElTableColumn label="股票" min-width="180">
          <template #default="{ row }">
            <div class="stock-name font-medium" @click="openDetail(row)">
              {{ row.name }}
              <ElTag v-if="isHeld(row)" type="danger" size="small" effect="plain">持有</ElTag>
              <ElTag v-if="isHeld(row) && heldCosts[row.code] && row.price" type="danger" size="small" effect="plain">
                {{(row.price - heldCosts[row.code]) / heldCosts[row.code] * 100 >= 0 ? '+' : ''}}{{((row.price - heldCosts[row.code]) / heldCosts[row.code] * 100).toFixed(1)}}%
              </ElTag>
            </div>
            <div class="text-xs text-gray-400">{{ row.code }} · {{ row.date || '--' }}</div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="最新价" width="110" align="right">
          <template #default="{ row }">{{ row.price ?? '--' }}</template>
        </ElTableColumn>
        <ElTableColumn label="涨跌幅" width="110" align="right" prop="change_pct" sortable>
          <template #default="{ row }">
            <span :style="{ color: changeColor(row.change_pct) }">
              {{ formatChange(row.change_pct) }}
            </span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="量比" width="90" align="right" prop="volume_ratio" sortable>
          <template #default="{ row }">
            <span :style="{ color: volumeRatioColor(row.volume_ratio) }">
              {{ row.volume_ratio == null ? '--' : row.volume_ratio.toFixed(2) }}
            </span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="换手率" width="100" align="right" prop="turnover" sortable>
          <template #default="{ row }">
            {{ row.turnover == null ? '--' : `${row.turnover.toFixed(2)}%` }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="指标信号" width="130">
          <template #default="{ row }">
            <template v-if="(tdxMap[row.code] || []).length">
              <ElTag v-for="t in tdxMap[row.code]" :key="t.signal" size="small"
                     :type="t.signal === '鬼手抄底' ? 'danger' : (t.signal === '鬼手减仓' ? 'warning' : 'info')"
                     :effect="t.signal === '鬼手抄底' ? 'dark' : 'plain'" class="mr-1 mb-1">
                {{ t.signal === '鬼手抄底' ? '⭐抄底' : t.signal === '鬼手减仓' ? '⚠️减仓' : t.signal }}{{ t.days_ago > 0 ? `·${t.days_ago}日前` : '' }}
              </ElTag>
            </template>
            <span v-else class="text-xs text-gray-300">--</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="战法信号" min-width="240">
          <template #default="{ row }">
            <div v-if="row.signals.length" class="flex flex-wrap gap-1">
              <ElTag
                v-for="s in row.signals"
                :key="s.strategy"
                size="small"
                :type="s.direction === 'up' ? 'danger' : 'success'"
                effect="plain"
              >
                {{ s.direction === 'up' ? '↑' : '↓' }}{{ s.strategy }}
              </ElTag>
            </div>
            <span v-else class="text-xs text-gray-400">暂无命中</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="270" align="center">
          <template #default="{ row }">
            <ElButton link size="small" @click="openDetail(row)">详情</ElButton>
            <ElButton link size="small" :type="isHeld(row) ? 'warning' : 'success'" @click="toggleHeld(row)">
              {{ isHeld(row) ? '取消持有' : '标记持有' }}
            </ElButton>
            <ElButton v-if="isHeld(row)" link size="small" @click="editCost(row)">
              成本{{ heldCosts[row.code] ? ` ${heldCosts[row.code]}` : '' }}
            </ElButton>
            <ElButton link size="small" :disabled="isFirst(row)" @click="move(row, -1)">上移</ElButton>
            <ElButton link size="small" :disabled="isLast(row)" @click="move(row, 1)">下移</ElButton>
            <ElButton link size="small" type="danger" @click="remove(row)">移除</ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
      <p class="mt-3 text-xs text-gray-400">
        信号来自已启用的上涨/下跌战法。红涨绿跌；量比 = 当日成交量 / 前5日均量（盘中为近似值），红=放量、绿=缩量；换手率取最新日K数据。行情接口限流时价格显示“--”，不影响战法扫描。
      </p>
    </ElCard>

    <ElDialog
      v-model="detailVisible"
      :title="`${detailName}（${detailCode}）行情与战法`"
      width="min(1100px, 95vw)"
      top="4vh"
      destroy-on-close
    >
      <div class="detail-summary flex flex-wrap items-center gap-x-8 gap-y-2 mb-3">
        <div>
          <div class="text-xs text-gray-400">最新价</div>
          <div class="text-xl font-semibold" :style="{ color: changeColor(detailRow?.change_pct ?? null) }">
            {{ detailRow?.price ?? '--' }}
          </div>
        </div>
        <div>
          <div class="text-xs text-gray-400">涨跌幅</div>
          <div class="text-lg font-semibold" :style="{ color: changeColor(detailRow?.change_pct ?? null) }">
            {{ formatChange(detailRow?.change_pct ?? null) }}
          </div>
        </div>
        <div>
          <div class="text-xs text-gray-400">数据日期</div>
          <div class="text-sm">{{ detailRow?.date || '--' }}</div>
        </div>
        <div>
          <div class="text-xs text-gray-400">量比</div>
          <div class="text-sm font-medium" :style="{ color: volumeRatioColor(detailRow?.volume_ratio ?? null) }">
            {{ detailRow?.volume_ratio == null ? '--' : detailRow.volume_ratio.toFixed(2) }}
          </div>
        </div>
        <div>
          <div class="text-xs text-gray-400">换手率</div>
          <div class="text-sm font-medium">
            {{ detailRow?.turnover == null ? '--' : `${detailRow.turnover.toFixed(2)}%` }}
          </div>
        </div>
        <div>
          <div class="text-xs text-gray-400">命中战法</div>
          <div class="text-sm">{{ detailSignals.length }} 条</div>
        </div>
        <div class="ml-auto text-xs text-gray-400">红涨绿跌 · 均线 MA5 / MA10 / MA20</div>
      </div>

      <ElTabs v-model="detailTab" type="border-card">
        <ElTabPane label="日K线" name="daily">
          <div class="flex items-center gap-2 mb-2 flex-wrap">
            <span class="text-xs text-gray-400">回看</span>
            <ElRadioGroup v-model="dailyDays" size="small">
              <ElRadioButton :value="60">60日</ElRadioButton>
              <ElRadioButton :value="120">120日</ElRadioButton>
              <ElRadioButton :value="250">250日</ElRadioButton>
            </ElRadioGroup>
            <span class="text-xs text-gray-400 ml-auto">滚轮缩放 / 拖动底部滑块</span>
          </div>
          <StockChart
            v-if="detailTab === 'daily'"
            :rows="dailyRows"
            mode="daily"
            height="420px"
            :loading="chartLoading"
          />
        </ElTabPane>

        <ElTabPane label="日内K线" name="intraday">
          <div class="flex items-center gap-2 mb-2 flex-wrap">
            <span class="text-xs text-gray-400">周期</span>
            <ElRadioGroup v-model="intradayPeriod" size="small">
              <ElRadioButton value="1">1分钟</ElRadioButton>
              <ElRadioButton value="5">5分钟</ElRadioButton>
              <ElRadioButton value="15">15分钟</ElRadioButton>
              <ElRadioButton value="30">30分钟</ElRadioButton>
              <ElRadioButton value="60">60分钟</ElRadioButton>
            </ElRadioGroup>
            <span class="text-xs text-gray-400 ml-auto">
              {{ intradayDate ? `交易日 ${intradayDate}` : '数据源：新浪 / 东财' }}
            </span>
          </div>
          <StockChart
            v-if="detailTab === 'intraday'"
            :rows="intradayRows"
            mode="intraday"
            height="420px"
            :loading="intradayLoading"
          />
          <div
            v-if="!intradayLoading && !intradayRows.length"
            class="text-sm text-gray-400 py-10 text-center"
          >
            暂无分钟数据（非交易时段或数据源暂不可用），可稍后重试
          </div>
        </ElTabPane>

        <ElTabPane label="战法信号" name="signals">
          <ElTable
            :data="detailSignals"
            size="small"
            stripe
            v-loading="detailLoading"
            empty-text="该股票当前没有命中任何战法"
          >
            <ElTableColumn label="方向" width="90">
              <template #default="{ row }">
                <ElTag :type="row.direction === 'up' ? 'danger' : 'success'" size="small" effect="dark">
                  {{ row.direction === 'up' ? '↑ 上涨' : '↓ 下跌' }}
                </ElTag>
              </template>
            </ElTableColumn>
            <ElTableColumn label="战法" prop="strategy" width="120" />
            <ElTableColumn label="强度" prop="strength" width="70" align="center" />
            <ElTableColumn label="命中理由" min-width="480">
              <template #default="{ row }">
                <span class="text-xs">{{ (row.reasons || []).join(' · ') }}</span>
              </template>
            </ElTableColumn>
          </ElTable>
        </ElTabPane>
      </ElTabs>
      <template #footer>
        <ElButton @click="detailVisible = false">关闭</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Plus, Refresh } from '@element-plus/icons-vue'
import { monitorApi, type WatchlistRow } from '@/api/monitor'
import StockChart from './StockChart.vue'

const route = useRoute()
const rows = ref<WatchlistRow[]>([])
const loading = ref(false)
const scanning = ref(false)
const adding = ref(false)
const keyword = ref('')
const filter = ref<'all' | 'up' | 'down' | 'none'>('all')
const updatedAt = ref('--')
const heldCodes = ref<string[]>([])
const heldCosts = ref<Record<string, number>>({})

const detailVisible = ref(false)
const detailLoading = ref(false)
const detailCode = ref('')
const detailName = ref('')
const detailRow = ref<WatchlistRow | null>(null)
const detailSignals = ref<any[]>([])

const detailTab = ref<'daily' | 'intraday' | 'signals'>('daily')
const dailyRows = ref<any[]>([])
const dailyDays = ref(120)
const chartLoading = ref(false)
const intradayRows = ref<any[]>([])
const intradayPeriod = ref('5')
const intradayDate = ref('')
const intradayLoading = ref(false)
let intradayLoadedFor = ''

const upTotal = computed(() => rows.value.filter(r => r.up_count > 0).length)
const downTotal = computed(() => rows.value.filter(r => r.down_count > 0).length)
const filteredRows = computed(() => rows.value.filter(r => {
  if (filter.value === 'up') return r.up_count > 0
  if (filter.value === 'down') return r.down_count > 0
  if (filter.value === 'none') return !r.signals.length
  return true
}))

const changeColor = (v: number | null) =>
  v == null ? '#909399' : v >= 0 ? '#c62828' : '#2e7d32'
const volumeRatioColor = (v: number | null) =>
  v == null ? '#909399' : v >= 1.5 ? '#c62828' : v < 0.7 ? '#2e7d32' : '#606266'
const formatChange = (v: number | null) =>
  v == null ? '--' : `${v >= 0 ? '+' : ''}${v}%`

const isHeld = (row: WatchlistRow) => heldCodes.value.includes(row.code)
const isFirst = (row: WatchlistRow) => rows.value[0]?.code === row.code
const isLast = (row: WatchlistRow) => rows.value[rows.value.length - 1]?.code === row.code

const tdxMap = ref<Record<string, string[]>>({})

async function loadTdx() {
  try {
    const res: any = await monitorApi.tdxLatest()
    tdxMap.value = res?.signals || {}
  } catch { /* 静默 */ }
}

async function loadData(showLoading = true) {
  if (showLoading) loading.value = true
  try {
    // 两个接口独立容错：持有接口慢/失败时，标的池表格仍能正常展示
    const [res, holdRes] = await Promise.allSettled([
      monitorApi.watchlist(), monitorApi.holdings()
    ])
    const watchData = res.status === 'fulfilled' ? res.value : null
    const holdData = holdRes.status === 'fulfilled' ? holdRes.value : null
    rows.value = watchData?.rows || []
    updatedAt.value = watchData?.updated_at || '--'
    heldCodes.value = holdData?.codes || []
    heldCosts.value = holdData?.costs || {}
  } catch {
    /* 请求层已提示 */
  } finally {
    loading.value = false
  }
}

async function querySearch(query: string, cb: (items: any[]) => void) {
  const q = query.trim()
  if (!q) {
    cb([])
    return
  }
  try {
    const list = await monitorApi.watchlistSearch(q, 12)
    cb((list || []).map(item => ({ ...item, value: `${item.code} ${item.name}` })))
  } catch {
    cb([])
  }
}

function handleSelect(item: any) {
  keyword.value = item.code
}

async function handleAdd() {
  const text = keyword.value.trim()
  if (!text) {
    ElMessage.warning('请输入股票代码或名称')
    return
  }
  adding.value = true
  try {
    let code = text.replace(/\D/g, '').slice(-6)
    if (code.length !== 6) {
      const found = await monitorApi.watchlistSearch(text, 1)
      code = found?.[0]?.code || ''
    }
    if (code.length !== 6) {
      ElMessage.warning('未找到对应的 A 股股票')
      return
    }
    await monitorApi.watchlistAdd(code)
    ElMessage.success('已加入标的池')
    keyword.value = ''
    await loadData(false)
  } catch {
    /* 请求层已提示 */
  } finally {
    adding.value = false
  }
}

async function remove(row: WatchlistRow) {
  try {
    await ElMessageBox.confirm(
      `确认从标的池移除 ${row.name}（${row.code}）？`,
      '移除标的',
      { confirmButtonText: '移除', cancelButtonText: '取消', type: 'warning' }
    )
    await monitorApi.watchlistRemove(row.code)
    ElMessage.success('已移除')
    await loadData(false)
  } catch {
    /* 用户取消或请求失败 */
  }
}

async function toggleHeld(row: WatchlistRow) {
  try {
    if (isHeld(row)) {
      await monitorApi.holdingsRemove(row.code)
      ElMessage.success('已取消持有标记')
    } else {
      // 录入成本价（可选）：浮盈分档止损依赖成本价判断厚利档
      let cost: number | undefined
      try {
        const { value } = await ElMessageBox.prompt(
          `为 ${row.name || row.code} 录入买入成本价（用于浮盈分档止损；留空则按基准档风控）`,
          '标记持有',
          {
            confirmButtonText: '确认标记', cancelButtonText: '留空标记',
            inputPattern: /^\s*$|^\d+(\.\d+)?$/,
            inputErrorMessage: '请输入有效数字（可留空）',
            inputPlaceholder: '例如 20.21',
          } as any
        )
        if (value && Number(value) > 0) cost = Number(value)
      } catch (action: any) {
        // 留空标记：用户点的是 cancel 类按钮，仍标记但不带成本
        if (action !== 'cancel' && action !== 'close') throw action
      }
      await monitorApi.holdingsAdd(row.code, cost)
      ElMessage.success('已标记持有，卖出风控将对该股生效' + (cost ? `（成本 ${cost}）` : ''))
    }
    await loadData(false)
  } catch {
    /* 请求层已提示 */
  }
}

async function editCost(row: WatchlistRow) {
  try {
    const { value } = await ElMessageBox.prompt(
      `修改 ${row.name || row.code} 的买入成本价（留空则清除）`,
      '持仓成本价',
      {
        confirmButtonText: '保存', cancelButtonText: '取消',
        inputPattern: /^\s*$|^\d+(\.\d+)?$/,
        inputErrorMessage: '请输入有效数字（可留空）',
        inputValue: String(heldCosts.value[row.code] ?? ''),
      } as any
    )
    const cost = value && Number(value) > 0 ? Number(value) : null
    await monitorApi.holdingsCost(row.code, cost)
    ElMessage.success(cost ? `成本价已更新为 ${cost}` : '成本价已清除')
    await loadData(false)
  } catch {
    /* 用户取消或请求失败 */
  }
}

async function move(row: WatchlistRow, delta: number) {
  const from = rows.value.findIndex(r => r.code === row.code)
  const to = from + delta
  if (from < 0 || to < 0 || to >= rows.value.length) return
  const codes = rows.value.map(r => r.code)
  ;[codes[from], codes[to]] = [codes[to], codes[from]]
  try {
    await monitorApi.watchlistReorder(codes)
    await loadData(false)
  } catch {
    /* 请求层已提示 */
  }
}

async function openDetail(row: WatchlistRow) {
  detailCode.value = row.code
  detailName.value = row.name
  detailRow.value = row
  detailSignals.value = []
  dailyRows.value = []
  intradayRows.value = []
  intradayDate.value = ''
  intradayLoadedFor = ''
  detailTab.value = 'daily'
  detailVisible.value = true
  detailLoading.value = true
  try {
    const res = await monitorApi.mainwaveStock(row.code)
    detailSignals.value = res?.signals || []
  } catch {
    /* 请求层已提示 */
  } finally {
    detailLoading.value = false
  }
  await loadDailyK()
}

async function loadDailyK() {
  if (!detailCode.value) return
  chartLoading.value = true
  try {
    const res = await monitorApi.stockKline(detailCode.value, dailyDays.value)
    dailyRows.value = res?.rows || []
  } catch {
    dailyRows.value = []
  } finally {
    chartLoading.value = false
  }
}

async function loadIntraday() {
  if (!detailCode.value) return
  intradayLoading.value = true
  try {
    const res = await monitorApi.stockIntraday(detailCode.value, intradayPeriod.value)
    intradayRows.value = res?.rows || []
    intradayDate.value = res?.date || ''
    intradayLoadedFor = `${detailCode.value}:${intradayPeriod.value}`
    if (!intradayRows.value.length) ElMessage.info('暂无分钟数据，可稍后重试')
  } catch {
    intradayRows.value = []
  } finally {
    intradayLoading.value = false
  }
}

watch(detailTab, (tab) => {
  if (tab !== 'intraday' || !detailVisible.value) return
  const key = `${detailCode.value}:${intradayPeriod.value}`
  if (intradayLoadedFor !== key) loadIntraday()
})
watch(dailyDays, () => {
  if (detailVisible.value) loadDailyK()
})
watch(intradayPeriod, () => {
  intradayLoadedFor = ''
  if (detailTab.value === 'intraday') loadIntraday()
})

async function handleScan() {
  scanning.value = true
  try {
    const res = await monitorApi.scan()
    const n = res?.fresh_count ?? res?.signal_count ?? 0
    if (n > 0) ElMessage.success(`扫描完成：${n} 条新信号`)
    else ElMessage.info('扫描完成：暂无新信号')
    await loadData(false)
  } catch {
    /* 请求层已提示 */
  } finally {
    scanning.value = false
  }
}

onMounted(async () => {
  await loadData()
  loadTdx()
  // 支持深链：#/monitor/watchlist?code=600519&tab=intraday 直接打开个股详情
  const q = String(route.query.code || '')
  if (!q) return
  const hit = rows.value.find(r => r.code === q)
  if (!hit) return
  await openDetail(hit)
  const tab = String(route.query.tab || '')
  if (tab === 'intraday' || tab === 'signals') detailTab.value = tab
})
</script>

<style scoped>
.stock-name {
  cursor: pointer;
}
.stock-name:hover {
  color: #409eff;
}
</style>
