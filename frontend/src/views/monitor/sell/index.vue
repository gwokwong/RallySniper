<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Timer } from '@element-plus/icons-vue'
import { monitorApi, type SellSignal } from '@/api/monitor'

defineOptions({ name: 'SellRiskControl' })

const signals = ref<SellSignal[]>([])
const status = ref<any>({})
const holdings = ref<any[]>([])
const rules = ref<any>({
  enabled: true, volume_surge_ratio: 5, ma20_buffer: 0.01,
  ma20_slope: -0.005, intraday_interval_minutes: 5, confirm_time: '14:50'
})
const saving = ref(false)
const scanning = ref(false)

const ruleText = computed(() =>
  `天量破位：量比≥${rules.value.volume_surge_ratio} 且阴线破前低/MA5；` +
  `20日线：斜率≤${rules.value.ma20_slope}、跌破>${(rules.value.ma20_buffer * 100).toFixed(1)}%且连续2日确认`
)

const changeColor = (v: number) => v >= 0 ? '#c62828' : '#2e7d32'

async function loadData() {
  try {
    const [sigRes, statusRes, holdRes, cfgRes] = await Promise.all([
      monitorApi.sellSignals(), monitorApi.sellStatus(),
      monitorApi.holdings(), monitorApi.getConfig()
    ])
    signals.value = sigRes?.signals || []
    status.value = statusRes || {}
    holdings.value = holdRes?.rows || []
    if (cfgRes?.sell) rules.value = { ...rules.value, ...cfgRes.sell }
  } catch { /* 请求层已提示 */ }
}

async function saveRules() {
  saving.value = true
  try {
    await monitorApi.putConfig({ sell: rules.value })
    await loadData()
    ElMessage.success('卖出规则已保存并生效')
  } catch { /* 请求层已提示 */ } finally { saving.value = false }
}

async function scan(confirm = false) {
  scanning.value = true
  try {
    const res = await monitorApi.sellScan(confirm)
    if (res?.success === false) { ElMessage.warning(res.error || '扫描失败'); return }
    await loadData()
    ElMessage.info(res.new_count ? `新增 ${res.new_count} 条信号` : '本次无新增信号')
  } catch { /* 请求层已提示 */ } finally { scanning.value = false }
}

async function unreverse(code: string) {
  try {
    await monitorApi.holdingsRemove(code)
    await loadData()
    ElMessage.success('已取消持有标记')
  } catch { /* 请求层已提示 */ }
}

onMounted(loadData)
</script>

<template>
  <div class="p-4">
    <ElCard shadow="never" class="mb-4">
      <div class="flex items-center justify-between flex-wrap gap-3">
        <div>
          <div class="flex items-center gap-2">
            <span class="text-lg font-semibold">持仓风控 · 卖出提醒</span>
            <ElTag type="danger" size="small" effect="dark">仅标记持有后生效</ElTag>
          </div>
          <p class="text-sm text-gray-500 mt-2">
            盘中每 {{ status.rules?.intraday_interval_minutes || 5 }} 分钟预警，
            {{ status.rules?.confirm_time || '14:50' }} 尾盘按近收盘价确认。信号：天量破位 + 20日线确认破位。
          </p>
        </div>
        <div class="flex gap-2 flex-wrap">
          <ElButton :icon="Refresh" :loading="scanning" @click="scan(false)">扫描预警</ElButton>
          <ElButton :loading="scanning" @click="scan(true)">尾盘确认</ElButton>
        </div>
      </div>
    </ElCard>

    <ElRow :gutter="12" class="mb-4">
      <ElCol :span="8">
        <ElCard shadow="never">
          <div class="text-sm text-gray-500">规则开关</div>
          <div class="text-lg font-semibold mt-1" :style="{ color: status.enabled ? '#c62828' : '#909399' }">
            {{ status.enabled ? '已开启' : '已关闭' }}
          </div>
        </ElCard>
      </ElCol>
      <ElCol :span="8">
        <ElCard shadow="never">
          <div class="text-sm text-gray-500">持有股票</div>
          <div class="text-lg font-semibold mt-1">{{ holdings.length }} 只</div>
        </ElCard>
      </ElCol>
      <ElCol :span="8">
        <ElCard shadow="never">
          <div class="text-sm text-gray-500">累计信号</div>
          <div class="text-lg font-semibold mt-1">{{ status.signal_count || 0 }} 条</div>
        </ElCard>
      </ElCol>
    </ElRow>

    <ElCard shadow="never" class="mb-4">
      <template #header><span class="font-semibold">规则参数</span></template>
      <ElForm label-position="top" size="small">
        <ElRow :gutter="12">
          <ElCol :span="6">
            <ElFormItem label="启用卖出提醒">
              <ElSwitch v-model="rules.enabled" />
            </ElFormItem>
          </ElCol>
          <ElCol :span="6">
            <ElFormItem label="天量量比阈值">
              <ElInputNumber v-model="rules.volume_surge_ratio" :min="1" :max="20" :step="0.5" style="width:100%" />
            </ElFormItem>
          </ElCol>
          <ElCol :span="6">
            <ElFormItem label="MA20跌破缓冲">
              <ElInputNumber v-model="rules.ma20_buffer" :min="0" :max="0.1" :step="0.005" style="width:100%" />
            </ElFormItem>
          </ElCol>
          <ElCol :span="6">
            <ElFormItem label="MA20斜率门槛">
              <ElInputNumber v-model="rules.ma20_slope" :min="-0.05" :max="0.05" :step="0.005" style="width:100%" />
            </ElFormItem>
          </ElCol>
        </ElRow>
        <ElRow :gutter="12">
          <ElCol :span="6">
            <ElFormItem label="盘中扫描间隔(分)">
              <ElInputNumber v-model="rules.intraday_interval_minutes" :min="1" :max="30" style="width:100%" />
            </ElFormItem>
          </ElCol>
          <ElCol :span="6">
            <ElFormItem label="尾盘确认时点">
              <ElInput v-model="rules.confirm_time" placeholder="14:50" />
            </ElFormItem>
          </ElCol>
          <ElCol :span="12" class="flex items-end">
            <ElButton type="primary" :loading="saving" @click="saveRules">保存规则</ElButton>
          </ElCol>
        </ElRow>
      </ElForm>
      <p class="text-xs text-gray-400 mt-1">{{ ruleText }}</p>
    </ElCard>

    <ElCard shadow="never" class="mb-4">
      <template #header><span class="font-semibold">已标记持有</span></template>
      <div v-if="!holdings.length" class="text-sm text-gray-400 py-3">
        暂无持有股票。请在「标的池」页面点击「标记持有」，只有被标记的股票才会触发卖出提醒。
      </div>
      <div v-else class="flex flex-wrap gap-2">
        <ElTag v-for="h in holdings" :key="h.code" type="danger" effect="plain" closable @close="unreverse(h.code)">
          {{ h.name }}（{{ h.code }}）{{ h.change_pct == null ? '' : (h.change_pct >= 0 ? '+' : '') + h.change_pct.toFixed(2) + '%' }}
        </ElTag>
      </div>
    </ElCard>

    <ElCard shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="font-semibold">风险信号（{{ signals.length }}）</span>
          <span class="text-xs text-gray-400 flex items-center gap-1"><Timer class="w-4 h-4" />盘中预警 / 尾盘确认分级推送</span>
        </div>
      </template>
      <ElTable :data="signals" stripe size="small" empty-text="暂无风险信号">
        <ElTableColumn label="级别" width="85">
          <template #default="{ row }">
            <ElTag :type="row.level === '确认' ? 'danger' : 'warning'" size="small" effect="dark">{{ row.level }}</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="时间" width="110">
          <template #default="{ row }">{{ row.date }} {{ row.time }}</template>
        </ElTableColumn>
        <ElTableColumn label="股票" min-width="150">
          <template #default="{ row }">{{ row.name }}（{{ row.code }}）</template>
        </ElTableColumn>
        <ElTableColumn label="信号" width="130" prop="strategy" />
        <ElTableColumn label="现价" width="85" align="right" prop="price" />
        <ElTableColumn label="涨跌幅" width="90" align="right">
          <template #default="{ row }"><span :style="{ color: changeColor(row.change_pct) }">{{ row.change_pct }}%</span></template>
        </ElTableColumn>
        <ElTableColumn label="量比" width="75" align="right" prop="volume_ratio" />
        <ElTableColumn label="触发依据" min-width="360">
          <template #default="{ row }"><span class="text-xs">{{ (row.reasons || []).join(' · ') }}</span></template>
        </ElTableColumn>
      </ElTable>
      <p class="text-xs text-gray-400 mt-3">
        预警为盘中触碰或量能异常，确认按尾盘近收盘价与连续2日口径；是否执行请自行判断，系统不做自动交易。
      </p>
    </ElCard>
  </div>
</template>
