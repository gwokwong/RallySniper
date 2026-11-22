<!-- 个股K线图：日K（含MA5/10/20）或日内分钟K -->
<template>
  <div
    ref="chartRef"
    class="w-full stock-chart"
    :style="{ height: props.height }"
    v-loading="props.loading"
  />
</template>

<script setup lang="ts">
import { watch, onMounted, nextTick } from 'vue'
import type { EChartsOption } from '@/plugins/echarts'
import { useChart } from '@/hooks/core/useChart'

const props = withDefaults(
  defineProps<{
    rows: any[]
    mode?: 'daily' | 'intraday'
    height?: string
    loading?: boolean
  }>(),
  { rows: () => [], mode: 'daily', height: '420px', loading: false }
)

const UP = '#c62828'
const DOWN = '#2e7d32'
const MA_COLORS = ['#f5a623', '#409eff', '#9b59b6']
const MA_KEYS = ['ma5', 'ma10', 'ma20']

const {
  chartRef,
  updateChart,
  getAxisLineStyle,
  getAxisLabelStyle,
  getSplitLineStyle,
  getTooltipStyle
} = useChart()

function formatVol(v: number | null | undefined): string {
  if (v == null) return '-'
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (v >= 1e4) return `${(v / 1e4).toFixed(1)}万`
  return String(v)
}

function buildOption(): EChartsOption {
  const rows = props.rows || []
  const isDaily = props.mode === 'daily'
  const labels = rows.map((r: any) => (isDaily ? String(r.date).slice(5) : String(r.time)))
  const candles = rows.map((r: any) => [r.open, r.close, r.low, r.high])
  const vols = rows.map((r: any) => ({
    value: r.volume || 0,
    itemStyle: { color: r.close >= r.open ? UP : DOWN }
  }))

  const series: any[] = [
    {
      name: 'K线',
      type: 'candlestick',
      data: candles,
      itemStyle: {
        color: UP,
        color0: DOWN,
        borderColor: UP,
        borderColor0: DOWN,
        borderWidth: 1
      },
      xAxisIndex: 0,
      yAxisIndex: 0
    }
  ]
  if (isDaily) {
    MA_KEYS.forEach((key, i) => {
      series.push({
        name: key.toUpperCase(),
        type: 'line',
        data: rows.map((r: any) => r[key] ?? null),
        smooth: true,
        symbol: 'none',
        connectNulls: false,
        lineStyle: { color: MA_COLORS[i], width: 1.2 },
        itemStyle: { color: MA_COLORS[i] },
        xAxisIndex: 0,
        yAxisIndex: 0
      })
    })
  }
  series.push({
    name: '成交量',
    type: 'bar',
    data: vols,
    xAxisIndex: 1,
    yAxisIndex: 1,
    barWidth: '60%'
  })

  const zoomStart = isDaily && rows.length > 70 ? 55 : 0
  return {
    animation: false,
    legend: isDaily
      ? {
          data: ['MA5', 'MA10', 'MA20'],
          top: 4,
          left: 10,
          itemWidth: 14,
          itemHeight: 8,
          textStyle: { fontSize: 11 }
        }
      : { show: false },
    tooltip: getTooltipStyle('axis', {
      axisPointer: { type: 'cross' },
      formatter: (params: any[]) => {
        if (!params?.length) return ''
        const r = rows[params[0].dataIndex]
        if (!r) return ''
        const pct = r.open ? ((r.close - r.open) / r.open) * 100 : 0
        const c = r.close >= r.open ? UP : DOWN
        const lines = [
          `<div style="font-weight:600;margin-bottom:4px">${isDaily ? r.date : `${r.date || ''} ${r.time}`}</div>`,
          `开盘 ${r.open}　收盘 <span style="color:${c}">${r.close}</span>`,
          `最高 ${r.high}　最低 ${r.low}`,
          `涨跌 <span style="color:${c}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</span>`,
          `成交量 ${formatVol(r.volume)}`
        ]
        if (isDaily) {
          lines.push(
            `量比 ${r.volume_ratio == null ? '-' : r.volume_ratio.toFixed(2)}　换手 ${r.turnover == null ? '-' : `${r.turnover.toFixed(2)}%`}`
          )
          lines.push(`MA5 ${r.ma5 ?? '-'}　MA10 ${r.ma10 ?? '-'}　MA20 ${r.ma20 ?? '-'}`)
        }
        return lines.join('<br/>')
      }
    }),
    grid: [
      { left: 12, right: 16, top: 34, height: '56%', containLabel: true },
      { left: 12, right: 16, top: '74%', height: '14%', containLabel: true }
    ],
    xAxis: [
      {
        type: 'category',
        data: labels,
        gridIndex: 0,
        boundaryGap: true,
        axisLine: getAxisLineStyle(),
        axisTick: { show: false },
        axisLabel: { show: false },
        splitLine: { show: false }
      },
      {
        type: 'category',
        data: labels,
        gridIndex: 1,
        boundaryGap: true,
        axisLine: getAxisLineStyle(),
        axisTick: { show: false },
        axisLabel: {
          ...getAxisLabelStyle(),
          interval: Math.max(0, Math.floor(labels.length / 6))
        },
        splitLine: { show: false }
      }
    ],
    yAxis: [
      {
        type: 'value',
        scale: true,
        gridIndex: 0,
        position: 'left',
        axisLine: getAxisLineStyle(),
        axisLabel: getAxisLabelStyle(),
        splitLine: getSplitLineStyle()
      },
      {
        type: 'value',
        scale: true,
        gridIndex: 1,
        position: 'left',
        axisLine: getAxisLineStyle(),
        axisLabel: { ...getAxisLabelStyle(), formatter: (v: number) => formatVol(v) },
        splitLine: { show: false },
        splitNumber: 2
      }
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: zoomStart, end: 100 },
      {
        type: 'slider',
        xAxisIndex: [0, 1],
        bottom: 2,
        height: 18,
        start: zoomStart,
        end: 100,
        borderColor: '#ddd',
        fillerColor: 'rgba(64,158,255,0.12)'
      }
    ],
    series
  } as EChartsOption
}

function render() {
  nextTick(() => updateChart(buildOption()))
}

watch(() => [props.rows, props.mode], render, { deep: true })
onMounted(render)
</script>

<style scoped>
.stock-chart {
  min-height: 320px;
}
</style>
