<!-- 板块异常 -->
<template>
  <div class="sector-monitor">
    <ElCard shadow="never" class="mb-4">
      <div class="flex items-center justify-between flex-wrap gap-3">
        <div class="flex items-center gap-3">
          <span class="text-lg font-semibold">板块异常</span>
          <ElTag type="info" size="small">行业 · 概念</ElTag>
        </div>
        <ElButton :loading="loading" @click="loadAll">
          <ElIcon><Refresh /></ElIcon>
          <span class="ml-1">刷新</span>
        </ElButton>
      </div>
    </ElCard>

    <ElRow :gutter="16">
      <ElCol :xs="24" :lg="12">
        <ElCard shadow="never">
          <template #header>
            <div class="flex items-center justify-between">
              <span class="font-semibold">
                <ElIcon class="mr-1"><Warning /></ElIcon>
                异常板块
              </span>
              <ElTag size="small" type="danger">{{ anomalies.length }} 个</ElTag>
            </div>
          </template>
          <ElTable :data="anomalies" v-loading="loading" stripe size="small" height="560">
            <ElTableColumn label="板块" min-width="150">
              <template #default="{ row }">
                <div class="flex items-center gap-2">
                  <ElTag size="small" :type="row.board_type === '行业' ? 'primary' : 'warning'">
                    {{ row.board_type }}
                  </ElTag>
                  <span>{{ row.board_name }}</span>
                </div>
              </template>
            </ElTableColumn>
            <ElTableColumn prop="change_pct" label="涨跌幅" width="100" align="right" sortable>
              <template #default="{ row }">
                <span :class="row.change_pct > 0 ? 'text-red-500' : 'text-green-600'">
                  {{ row.change_pct > 0 ? '+' : '' }}{{ row.change_pct }}%
                </span>
              </template>
            </ElTableColumn>
            <ElTableColumn prop="turnover" label="换手率" width="90" align="right">
              <template #default="{ row }">{{ row.turnover }}%</template>
            </ElTableColumn>
            <ElTableColumn label="涨跌家数" width="120" align="center">
              <template #default="{ row }">
                <span class="text-red-500">{{ row.up_count }}</span>
                /
                <span class="text-green-600">{{ row.down_count }}</span>
              </template>
            </ElTableColumn>
            <ElTableColumn label="领涨股" min-width="140">
              <template #default="{ row }">
                <div>
                  <div>{{ row.leader || '-' }}</div>
                  <div v-if="row.leader_pct" class="text-xs text-red-500">{{ row.leader_pct }}%</div>
                </div>
              </template>
            </ElTableColumn>
          </ElTable>
        </ElCard>
      </ElCol>

      <ElCol :xs="24" :lg="12">
        <ElCard shadow="never">
          <template #header>
            <div class="flex items-center justify-between">
              <ElRadioGroup v-model="boardType" size="small" @change="loadBoards">
                <ElRadioButton value="行业">行业板块</ElRadioButton>
                <ElRadioButton value="概念">概念板块</ElRadioButton>
              </ElRadioGroup>
              <span class="text-xs text-gray-400">按涨跌幅排序</span>
            </div>
          </template>
          <ElTable :data="boards" v-loading="loading" stripe size="small" height="560">
            <ElTableColumn label="板块名称" min-width="150">
              <template #default="{ row }">
                <div class="flex items-center gap-2">
                  <span>{{ row.board_name }}</span>
                  <ElTag v-if="row.abnormal" size="small" type="danger" effect="dark">异动</ElTag>
                </div>
              </template>
            </ElTableColumn>
            <ElTableColumn prop="change_pct" label="涨跌幅" width="100" align="right" sortable>
              <template #default="{ row }">
                <span :class="row.change_pct > 0 ? 'text-red-500' : 'text-green-600'">
                  {{ row.change_pct > 0 ? '+' : '' }}{{ row.change_pct }}%
                </span>
              </template>
            </ElTableColumn>
            <ElTableColumn prop="turnover" label="换手" width="90" align="right">
              <template #default="{ row }">{{ row.turnover }}%</template>
            </ElTableColumn>
            <ElTableColumn prop="leader" label="领涨股" min-width="120" />
          </ElTable>
        </ElCard>
      </ElCol>
    </ElRow>

    <ElAlert
      v-if="!anomalies.length && !loading"
      type="info"
      :closable="false"
      show-icon
      class="mt-4"
      title="暂无异常板块"
      description="板块涨跌幅超过配置阈值（默认 ±3%）时才会出现在左侧列表。右侧展示全部板块行情，红色为涨、绿色为跌。"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Warning } from '@element-plus/icons-vue'
import { monitorApi, type Signal } from '@/api/monitor'

defineOptions({ name: 'SectorAnomaly' })

const loading = ref(false)
const anomalies = ref<Signal[]>([])
const boards = ref<any[]>([])
const boardType = ref('行业')

async function loadAll() {
  loading.value = true
  try {
    const [anomalyRes] = await Promise.all([
      monitorApi.sectorAnomalies(),
      loadBoards()
    ])
    anomalies.value = anomalyRes
  } catch (error: any) {
    ElMessage.error(error?.message || '加载失败，请确认后端服务已启动')
  } finally {
    loading.value = false
  }
}

async function loadBoards() {
  try {
    boards.value = await monitorApi.sectors(boardType.value)
  } catch {
    boards.value = []
  }
}

onMounted(loadAll)
</script>

<style scoped>
.sector-monitor { padding: 4px; }
</style>
