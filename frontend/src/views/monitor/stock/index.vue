<!-- 个股异动 -->
<template>
  <div class="stock-monitor">
    <ElCard shadow="never" class="mb-4">
      <div class="flex items-center justify-between flex-wrap gap-3">
        <div class="flex items-center gap-3">
          <span class="text-lg font-semibold">个股异动</span>
          <ElTag type="info" size="small">价格 · 量能 · 资金 · 盘口</ElTag>
        </div>
        <div class="flex items-center gap-2">
          <ElButton :loading="loading" @click="loadAll">
            <ElIcon><Refresh /></ElIcon>
            <span class="ml-1">刷新</span>
          </ElButton>
        </div>
      </div>
    </ElCard>

    <ElTabs v-model="activeTab" type="border-card">
      <!-- 异动列表 -->
      <ElTabPane label="异动扫描" name="anomaly">
        <ElAlert
          v-if="!stockSignals.length"
          type="info"
          :closable="false"
          show-icon
          class="mb-3"
          title="当前无异动信号"
          description="异动基于涨跌幅/量比/换手率/涨速/振幅规则识别。非交易时段或行情源限流时列表可能为空，可稍后刷新。"
        />
        <ElTable :data="stockSignals" v-loading="loading" stripe size="small" height="480">
          <ElTableColumn prop="code" label="代码" width="100" fixed />
          <ElTableColumn prop="name" label="名称" width="120" />
          <ElTableColumn prop="price" label="最新价" width="100" align="right">
            <template #default="{ row }">{{ Number(row.price || 0).toFixed(2) }}</template>
          </ElTableColumn>
          <ElTableColumn prop="change_pct" label="涨跌幅" width="100" align="right" sortable>
            <template #default="{ row }">
              <span :class="row.change_pct > 0 ? 'text-red-500' : 'text-green-600'">
                {{ row.change_pct > 0 ? '+' : '' }}{{ row.change_pct }}%
              </span>
            </template>
          </ElTableColumn>
          <ElTableColumn prop="volume_ratio" label="量比" width="90" align="right" sortable />
          <ElTableColumn prop="turnover" label="换手率" width="100" align="right" sortable>
            <template #default="{ row }">{{ row.turnover }}%</template>
          </ElTableColumn>
          <ElTableColumn prop="amplitude" label="振幅" width="90" align="right">
            <template #default="{ row }">{{ row.amplitude }}%</template>
          </ElTableColumn>
          <ElTableColumn label="异动原因" min-width="240">
            <template #default="{ row }">
              <ElTag
                v-for="(r, i) in row.reasons"
                :key="i"
                :type="row.direction === 'up' ? 'danger' : 'success'"
                size="small"
                class="mr-1 mb-1"
              >{{ r }}</ElTag>
            </template>
          </ElTableColumn>
          <ElTableColumn prop="score" label="强度" width="90" align="right" sortable />
        </ElTable>
      </ElTabPane>

      <!-- 资金异动 -->
      <ElTabPane label="主力资金" name="fund">
        <ElTable :data="fundSignals" v-loading="loading" stripe size="small" height="480">
          <ElTableColumn prop="code" label="代码" width="100" />
          <ElTableColumn prop="name" label="名称" width="120" />
          <ElTableColumn prop="signal" label="资金信号" min-width="200">
            <template #default="{ row }">
              <span :class="row.direction === 'in' ? 'text-red-500' : 'text-green-600'">
                {{ row.signal }}
              </span>
            </template>
          </ElTableColumn>
          <ElTableColumn prop="net_flow" label="净额(元)" min-width="150" align="right" sortable>
            <template #default="{ row }">{{ formatMoney(row.net_flow) }}</template>
          </ElTableColumn>
          <ElTableColumn prop="net_pct" label="净占比" width="100" align="right">
            <template #default="{ row }">{{ row.net_pct ?? '-' }}</template>
          </ElTableColumn>
        </ElTable>
      </ElTabPane>

      <!-- 盘口异动 -->
      <ElTabPane label="盘口大单" name="changes">
        <ElTable :data="changes" v-loading="loading" stripe size="small" height="480">
          <ElTableColumn prop="时间" label="时间" width="110" />
          <ElTableColumn prop="代码" label="代码" width="100" />
          <ElTableColumn prop="名称" label="名称" width="120" />
          <ElTableColumn prop="板块" label="异动类型" width="120">
            <template #default="{ row }">
              <ElTag size="small" :type="String(row['板块']).includes('买入') ? 'danger' : 'success'">
                {{ row['板块'] }}
              </ElTag>
            </template>
          </ElTableColumn>
          <ElTableColumn prop="相关信息" label="相关信息" min-width="240" />
        </ElTable>
      </ElTabPane>

      <!-- 涨停股池 -->
      <ElTabPane label="涨停股池" name="limitup">
        <div class="mb-3 flex items-center gap-2">
          <span class="text-sm text-gray-500">交易日：</span>
          <ElDatePicker
            v-model="tradeDate"
            type="date"
            size="small"
            value-format="YYYYMMDD"
            placeholder="选择日期"
            style="width: 160px"
            @change="loadLimitUp"
          />
          <ElButton size="small" @click="loadLimitUp">查询</ElButton>
          <span class="text-xs text-gray-400">留空取最近交易日数据</span>
        </div>
        <ElTable :data="limitUp" v-loading="loading" stripe size="small" height="440">
          <ElTableColumn prop="代码" label="代码" width="100" />
          <ElTableColumn prop="名称" label="名称" width="120" />
          <ElTableColumn prop="最新价" label="最新价" width="100" align="right" />
          <ElTableColumn prop="涨跌幅" label="涨跌幅" width="100" align="right" />
          <ElTableColumn prop="封板资金" label="封板资金" width="130" align="right">
            <template #default="{ row }">{{ formatMoney(row['封板资金']) }}</template>
          </ElTableColumn>
          <ElTableColumn prop="连板数" label="连板数" width="90" align="right" />
          <ElTableColumn prop="所属行业" label="所属行业" min-width="140" />
        </ElTable>
      </ElTabPane>
    </ElTabs>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { monitorApi, type Signal } from '@/api/monitor'

defineOptions({ name: 'StockAnomaly' })

const activeTab = ref('anomaly')
const loading = ref(false)
const stockSignals = ref<Signal[]>([])
const fundSignals = ref<Signal[]>([])
const changes = ref<any[]>([])
const limitUp = ref<any[]>([])
const tradeDate = ref('')

function formatMoney(v: any): string {
  const n = Number(v || 0)
  if (!n) return '-'
  if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + ' 亿'
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + ' 万'
  return n.toFixed(2)
}

async function loadAll() {
  loading.value = true
  try {
    // 全市场接口冷启动较慢，独立容错：单个接口失败/超时不影响其余数据展示
    const [stocks, funds, changesRes] = await Promise.allSettled([
      monitorApi.stockAnomalies(),
      monitorApi.fundAnomalies(),
      monitorApi.stockChanges(100)
    ])
    stockSignals.value = stocks.status === 'fulfilled' ? stocks.value : []
    fundSignals.value = funds.status === 'fulfilled' ? funds.value : []
    changes.value = changesRes.status === 'fulfilled' ? changesRes.value : []
    await loadLimitUp()
  } catch (error: any) {
    ElMessage.error(error?.message || '加载失败，请确认后端服务已启动')
  } finally {
    loading.value = false
  }
}

async function loadLimitUp() {
  try {
    const res = await monitorApi.limitUp(tradeDate.value || undefined)
    limitUp.value = res
  } catch {
    limitUp.value = []
  }
}

onMounted(loadAll)
</script>

<style scoped>
.stock-monitor { padding: 4px; }
</style>
