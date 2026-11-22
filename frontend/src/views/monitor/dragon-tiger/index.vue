<!-- 龙虎榜 -->
<template>
  <div class="lhb-monitor">
    <ElCard shadow="never" class="mb-4">
      <div class="flex items-center justify-between flex-wrap gap-3">
        <div class="flex items-center gap-3">
          <span class="text-lg font-semibold">龙虎榜</span>
          <ElTag type="warning" size="small">机构与游资动向</ElTag>
        </div>
        <div class="flex items-center gap-2 flex-wrap">
          <ElDatePicker
            v-model="dateRange"
            type="daterange"
            size="small"
            range-separator="至"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            value-format="YYYYMMDD"
            style="width: 260px"
          />
          <ElButton :loading="loading" @click="loadData">
            <ElIcon><Refresh /></ElIcon>
            <span class="ml-1">刷新</span>
          </ElButton>
        </div>
      </div>
      <div class="mt-3 text-xs text-gray-400">
        数据来源：东方财富龙虎榜。净额为上榜席位买入减卖出，红色表示净买入、绿色表示净卖出。数据仅供盯盘参考，不构成投资建议。
      </div>
    </ElCard>

    <ElRow :gutter="16" class="mb-4">
      <ElCol :xs="24" :sm="8" v-for="card in summaryCards" :key="card.label">
        <ElCard shadow="never" class="text-center">
          <div class="text-sm text-gray-500">{{ card.label }}</div>
          <div class="text-xl font-bold mt-1" :style="{ color: card.color }">{{ card.value }}</div>
        </ElCard>
      </ElCol>
    </ElRow>

    <ElCard shadow="never">
      <ElTable :data="records" v-loading="loading" stripe size="small" height="560">
        <ElTableColumn prop="code" label="代码" width="100" fixed />
        <ElTableColumn prop="name" label="名称" width="120" />
        <ElTableColumn prop="net_buy" label="净买额(元)" width="150" align="right" sortable>
          <template #default="{ row }">
            <span :class="row.net_buy > 0 ? 'text-red-500' : 'text-green-600'">
              {{ formatMoney(row.net_buy) }}
            </span>
          </template>
        </ElTableColumn>
        <ElTableColumn prop="signal" label="信号" width="160">
          <template #default="{ row }">
            <ElTag :type="row.direction === 'in' ? 'danger' : 'success'" size="small">
              {{ row.signal }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="上榜原因" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">{{ row.reason || '-' }}</template>
        </ElTableColumn>
        <ElTableColumn label="净买占比" width="120" align="right">
          <template #default="{ row }">
            {{ row.raw?.['净买额占总成交比'] ? Number(row.raw['净买额占总成交比']).toFixed(2) + '%' : '-' }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="换手率" width="100" align="right">
          <template #default="{ row }">
            {{ row.raw?.['换手率'] ? Number(row.raw['换手率']).toFixed(2) + '%' : '-' }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="上榜日" width="120">
          <template #default="{ row }">{{ row.raw?.['上榜日'] || '-' }}</template>
        </ElTableColumn>
      </ElTable>
    </ElCard>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { monitorApi, type Signal } from '@/api/monitor'

defineOptions({ name: 'DragonTiger' })

const loading = ref(false)
const records = ref<Signal[]>([])
const dateRange = ref<string[]>([])

const summaryCards = computed(() => {
  const netIn = records.value.filter(r => r.net_buy > 0)
  const netOut = records.value.filter(r => r.net_buy < 0)
  const totalIn = netIn.reduce((s, r) => s + r.net_buy, 0)
  return [
    { label: '上榜个股', value: records.value.length, color: '#303133' },
    { label: '净买入家数', value: netIn.length, color: '#f56c6c' },
    { label: '净买入合计', value: formatMoney(totalIn), color: '#e6a23c' }
  ]
})

function formatMoney(v: any): string {
  const n = Number(v || 0)
  if (!n) return '-'
  const sign = n > 0 ? '+' : '-'
  const a = Math.abs(n)
  if (a >= 1e8) return sign + (a / 1e8).toFixed(2) + ' 亿'
  if (a >= 1e4) return sign + (a / 1e4).toFixed(2) + ' 万'
  return sign + a.toFixed(2)
}

async function loadData() {
  loading.value = true
  try {
    const start = dateRange.value?.[0]
    const end = dateRange.value?.[1]
    records.value = await monitorApi.dragonTiger(start, end)
    if (!records.value.length) {
      ElMessage.info('所选日期区间无龙虎榜数据')
    }
  } catch (error: any) {
    ElMessage.error(error?.message || '加载失败，请确认后端服务已启动')
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.lhb-monitor { padding: 4px; }
</style>
