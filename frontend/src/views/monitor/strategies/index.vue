<!-- 主升浪战法说明 -->
<template>
  <div class="strategies-page">
    <ElCard shadow="never" class="mb-4">
      <div class="flex items-center gap-3">
        <span class="text-lg font-semibold">战法说明</span>
        <ElTag type="info" size="small">聚焦两类信号 · 友好 + 危险 · 11 种战法可独立开关</ElTag>
      </div>
      <p class="text-sm text-gray-500 mt-2">
        默认只盯核心信号：友好信号看「涨停回调·放量启动」，危险信号看「双线死叉 / 生命线破位」。其余战法保留在此，需要时在配置页手动开启。
      </p>
    </ElCard>

    <!-- 上涨战法 -->
    <div class="section-title up-title">
      <ElIcon :size="18"><TrendCharts /></ElIcon>
      <span>上涨启动战法（抓主升浪）</span>
    </div>
    <ElRow :gutter="16">
      <ElCol :xs="24" :lg="12" v-for="(s, i) in upStrategies" :key="s.key">
        <ElCard shadow="never" class="mb-4 strategy-card">
          <template #header>
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="strategy-no up-no">{{ i + 1 }}</span>
                <span class="font-semibold">{{ s.key }}</span>
                <ElTag :type="s.enabled ? 'success' : 'info'" size="small" effect="plain">
                  {{ s.enabled ? '已启用' : '未启用' }}
                </ElTag>
              </div>
              <ElTag size="small" type="danger" effect="plain">{{ s.desc }}</ElTag>
            </div>
          </template>
          <p class="text-sm text-gray-600 leading-6">{{ s.detail }}</p>
        </ElCard>
      </ElCol>
    </ElRow>

    <!-- 下跌战法 -->
    <div class="section-title down-title">
      <ElIcon :size="18"><ArrowDown /></ElIcon>
      <span>下跌离场战法（及时止盈止损）</span>
    </div>
    <ElRow :gutter="16">
      <ElCol :xs="24" :lg="12" v-for="(s, i) in downStrategies" :key="s.key">
        <ElCard shadow="never" class="mb-4 strategy-card">
          <template #header>
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="strategy-no down-no">{{ i + 1 }}</span>
                <span class="font-semibold">{{ s.key }}</span>
                <ElTag :type="s.enabled ? 'success' : 'info'" size="small" effect="plain">
                  {{ s.enabled ? '已启用' : '未启用' }}
                </ElTag>
              </div>
              <ElTag size="small" type="success" effect="plain">{{ s.desc }}</ElTag>
            </div>
          </template>
          <p class="text-sm text-gray-600 leading-6">{{ s.detail }}</p>
        </ElCard>
      </ElCol>
    </ElRow>

    <ElCard shadow="never">
      <template #header><span class="font-semibold">使用建议</span></template>
      <ul class="text-sm text-gray-600 leading-7 list-disc pl-5">
        <li><b>核心友好信号</b>：涨停回调分两步——缩量到位先提醒「观察中」，等今日或昨日重新放量、且守住涨停日最低价并站上 10 日线，才提醒「放量启动」买点；中途跌破涨停日最低价则不再跟踪。阈值可在配置页调整。</li>
        <li><b>其余上涨战法</b>：量价突破最直接；三重共振误报少；四特征共振最严格；龙出水抓横盘突破；三线底部启动抓反转。默认关闭，想抓其他形态时再开启。</li>
        <li><b>危险信号</b>：默认开启双线死叉与生命线破位。多战法同时命中说明趋势走坏概率更高；其余三种默认关闭，可按需开启。</li>
        <li>信号按「股票 + 战法 + 阶段」做 24 小时冷却：观察中与放量启动会分别提醒，不会互相吞掉；想更频繁可调小冷却。</li>
        <li class="text-danger">技术形态仅为辅助工具，不构成投资建议。请结合基本面、市场环境与个人风险承受能力独立决策。</li>
      </ul>
    </ElCard>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { TrendCharts, ArrowDown } from '@element-plus/icons-vue'
import { monitorApi } from '@/api/monitor'

const catalog = ref<any[]>([])
const upStrategies = computed(() => catalog.value.filter(s => s.direction === 'up'))
const downStrategies = computed(() => catalog.value.filter(s => s.direction === 'down'))

async function load() {
  try {
    catalog.value = await monitorApi.strategies()
  } catch { /* ignore */ }
}
onMounted(load)
</script>

<style scoped>
.strategy-no {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; border-radius: 6px; color: #fff; font-size: 13px; font-weight: bold;
}
.up-no { background: #c62828; }
.down-no { background: #2e7d32; }
.strategy-card { min-height: 150px; }
.section-title {
  display: flex; align-items: center; gap: 8px;
  font-size: 16px; font-weight: 600; margin: 18px 0 12px; padding-left: 4px;
}
.up-title { color: #c62828; border-left: 3px solid #c62828; padding-left: 10px; }
.down-title { color: #2e7d32; border-left: 3px solid #2e7d32; padding-left: 10px; }
.text-danger { color: #c62828; }
</style>
