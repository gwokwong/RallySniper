<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Bell } from '@element-plus/icons-vue'
import { monitorApi, type YesterdayRow } from '@/api/monitor'

defineOptions({ name: 'YesterdayFollow' })

const rows = ref<YesterdayRow[]>([])
const snapshot = ref<any>({})
const category = ref('all')
const loading = ref(false)
const pushing = ref(false)

const categories = ['all', '连板', '炸板', '跌停', '低开低走', '下跌', '高开低走', '强势溢价', '平开震荡']
const visible = computed(() =>
  category.value === 'all' ? rows.value : rows.value.filter(r => r.category === category.value)
)
const summary = computed(() => snapshot.value?.summary || {})

const changeColor = (v: number | null | undefined) =>
  v == null ? '#909399' : v >= 0 ? '#c62828' : '#2e7d32'
const fmt = (v: number | null | undefined, suffix = '') =>
  v == null ? '--' : `${v > 0 ? '+' : ''}${v.toFixed(2)}${suffix}`
const tagType = (c: string) =>
  c === '连板' ? 'danger' : c === '炸板' || c === '跌停' ? 'warning'
    : (c === '下跌' || c === '低开低走' || c === '高开低走') ? 'success' : 'info'

async function loadData() {
  try {
    const res = await monitorApi.yesterdaySnapshot()
    rows.value = res?.rows || []
    snapshot.value = res?.snapshot || {}
  } catch { /* 请求层已提示 */ }
}

async function scan() {
  loading.value = true
  try {
    const res = await monitorApi.yesterdayScan()
    if (res?.success === false) { ElMessage.warning(res.error || '扫描失败'); return }
    await loadData()
    ElMessage.success(`已更新 ${res.base_label || ''} 涨停股 ${res.total || 0} 只，观察其在 ${res.observe_label || ''} 的表现`)
  } catch { /* 请求层已提示 */ } finally { loading.value = false }
}

async function push() {
  pushing.value = true
  try {
    const res = await monitorApi.yesterdayPush()
    if (res?.success === false) { ElMessage.warning(res.error || '简报生成失败'); return }
    await loadData()
    ElMessage.success(`简报已推送：站内通知 + ${res.pushed_channels || 0} 个邮件渠道`)
  } catch { /* 请求层已提示 */ } finally { pushing.value = false }
}

onMounted(loadData)
</script>

<template>
  <div class="p-4">
    <ElCard shadow="never" class="mb-4">
      <div class="flex items-center justify-between flex-wrap gap-3">
        <div>
          <div class="flex items-center gap-2">
            <span class="text-lg font-semibold">昨日涨停跟进</span>
            <ElTag type="danger" size="small" effect="dark">10:30 / 14:40 简报</ElTag>
          </div>
          <p class="text-sm text-gray-500 mt-2">
            今天看昨天：观察
            <b class="text-blue-600">{{ snapshot.base_label || snapshot.trade_date || '上一交易日' }}</b>
            的涨停股，在
            <b class="text-red-600">{{ snapshot.observe_label || '最近交易日' }}</b>
            的表现，按连板、炸板、高开低走等专业术语分类；同时标注所属行业板块与市场板块。
            <span v-if="snapshot.market_label && snapshot.market_label !== '交易中'" class="text-amber-600">
              当前{{ snapshot.market_label }}，表现数据为最近交易日收盘。
            </span>
          </p>
        </div>
        <div class="flex gap-2 flex-wrap">
          <ElButton :icon="Refresh" :loading="loading" @click="scan">立即刷新</ElButton>
          <ElButton type="primary" :icon="Bell" :loading="pushing" @click="push">生成并推送简报</ElButton>
        </div>
      </div>
    </ElCard>

    <ElRow :gutter="10" class="mb-4">
      <ElCol v-for="c in categories.filter(x => x !== 'all')" :key="c" :span="3">
        <ElCard shadow="never" class="text-center">
          <div class="text-xs text-gray-500">{{ c }}</div>
          <div class="text-lg font-semibold mt-1">{{ summary[c] || 0 }}</div>
        </ElCard>
      </ElCol>
    </ElRow>

    <ElCard shadow="never">
      <template #header>
        <div class="flex items-center justify-between flex-wrap gap-2">
          <span class="font-semibold">明细（{{ visible.length }} 只）</span>
          <ElSelect v-model="category" size="small" style="width: 150px">
            <ElOption v-for="c in categories" :key="c" :label="c === 'all' ? '全部分类' : c" :value="c" />
          </ElSelect>
        </div>
      </template>
      <ElTable :data="visible" stripe size="small" v-loading="loading" empty-text="暂无数据，点「立即刷新」拉取涨停池">
        <ElTableColumn label="分类" width="100">
          <template #default="{ row }">
            <ElTag :type="tagType(row.category)" size="small" effect="plain">{{ row.category }}</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="股票" min-width="150">
          <template #default="{ row }">
            <div class="font-medium">{{ row.name }}</div>
            <div class="text-xs text-gray-400">{{ row.code }}</div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="行业板块" min-width="130" prop="industry" />
        <ElTableColumn label="市场板块" width="100" prop="market_board" />
        <ElTableColumn label="昨连板" width="80" align="center">
          <template #default="{ row }">{{ row.prev_streak }}板</template>
        </ElTableColumn>
        <ElTableColumn label="现价" width="85" align="right" prop="price" />
        <ElTableColumn label="涨跌幅" width="95" align="right">
          <template #default="{ row }">
            <span :style="{ color: changeColor(row.change_pct) }">{{ fmt(row.change_pct, '%') }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="开盘" width="90" align="right">
          <template #default="{ row }">
            <span :style="{ color: changeColor(row.open_pct) }">{{ fmt(row.open_pct, '%') }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="最高/最低" width="120" align="right">
          <template #default="{ row }">{{ fmt(row.high_pct, '%') }} / {{ fmt(row.low_pct, '%') }}</template>
        </ElTableColumn>
        <ElTableColumn label="量比" width="75" align="right" prop="volume_ratio" />
        <ElTableColumn label="换手率" width="85" align="right">
          <template #default="{ row }">{{ row.turnover == null ? '--' : row.turnover + '%' }}</template>
        </ElTableColumn>
        <ElTableColumn label="解读" min-width="240">
          <template #default="{ row }"><span class="text-xs">{{ row.note }}</span></template>
        </ElTableColumn>
      </ElTable>
      <p class="text-xs text-gray-400 mt-3">
        数据来源：AKShare 涨停池（行业/连板数）+ 全市场实时行情；分类为技术形态描述，非买卖建议。红涨绿跌。
      </p>
    </ElCard>
  </div>
</template>
