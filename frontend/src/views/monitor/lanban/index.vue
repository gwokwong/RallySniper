<template>
  <div class="p-4">
    <ElCard shadow="never" class="mb-4">
      <div class="flex items-center justify-between flex-wrap gap-3">
        <div>
          <div class="flex items-center gap-2">
            <span class="text-lg font-semibold">烂板接力</span>
            <ElTag type="danger" size="small" effect="dark">二进三换手板 · 次日高开秒板</ElTag>
          </div>
          <p class="text-sm text-gray-500 mt-2">
            形态：前日涨停 → 昨日<b>开盘无板+盘中触板+收盘封板+放量换手</b>（烂板/换手板）→ 今日接力确认价
            <b>{{ rows[0]?.limit_px || '--' }}（首只）</b> 封板即提醒。
            <br />九条纪律：①只做连板核心 ②情绪反转节点出手 ③放量换手板才有合力 ④无量上板不碰
            ⑤二进三选换手板次日秒板进 ⑥高位三次放量后低开必死 ⑦缩量不打/下午板不打/一字不打
            ⑧等待 ⑨跟随主力顺势。
            <span class="text-amber-600">回测：二进三+次日封住 5日均+8.65%（可控）/ 未封 -3.89%——封不住必须走。</span>
          </p>
        </div>
        <ElButton :icon="Refresh" :loading="loading" @click="loadData()">刷新</ElButton>
      </div>
    </ElCard>

    <ElCard shadow="never">
      <template #header><span class="font-semibold">接力候选（昨日烂板形态，数据日 {{ date || '--' }}）</span></template>
      <ElTable :data="rows" stripe size="small" v-loading="loading" empty-text="当前无烂板接力候选">
        <ElTableColumn label="名称" min-width="120">
          <template #default="{ row }"><span class="font-medium">{{ row.name }}</span></template>
        </ElTableColumn>
        <ElTableColumn label="代码" prop="code" width="90" />
        <ElTableColumn label="行业" width="110" prop="industry" />
        <ElTableColumn label="连板高度" width="100">
          <template #default="{ row }">
            <ElTag :type="row.streak === 2 ? 'danger' : (row.streak >= 4 ? 'warning' : 'info')" size="small" effect="dark">
              {{ row.streak === 2 ? '⭐二进三' : row.streak + '连板' }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="昨日收盘" width="90" align="right" prop="t1_close" />
        <ElTableColumn label="接力确认价" width="100" align="right">
          <template #default="{ row }"><b class="text-red-600">{{ row.limit_px }}</b></template>
        </ElTableColumn>
        <ElTableColumn label="放量倍数" width="90" align="right" prop="vol_ratio_1d" />
        <ElTableColumn label="换手%" width="85" align="right">
          <template #default="{ row }">{{ row.turnover_pct ?? '--' }}</template>
        </ElTableColumn>
        <ElTableColumn label="纪律提示" min-width="260">
          <template #default="{ row }">
            <span class="text-xs text-gray-500">
              {{ row.streak >= 4 ? '高位板：低开必死（规则6），仅在放量上攻时参与' : '高开秒板/上午封板参与；下午板不打；封不住次日走' }}
            </span>
          </template>
        </ElTableColumn>
      </ElTable>
    </ElCard>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElCard, ElTable, ElTableColumn, ElTag, ElButton } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { monitorApi } from '@/api/monitor'

const rows = ref<any[]>([])
const date = ref('')
const loading = ref(false)

async function loadData() {
  loading.value = true
  try {
    const res: any = await monitorApi.lanbanCandidates()
    rows.value = res?.candidates || []
    date.value = res?.date || ''
  } catch { /* 请求层已提示 */ } finally { loading.value = false }
}

onMounted(loadData)
</script>
