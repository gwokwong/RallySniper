/**
 * 离线图标加载器
 *
 * 用于在内网环境下支持 Iconify 图标的离线加载。
 * 通过预加载图标集数据，避免运行时从 CDN 获取图标。
 *
 * 使用方式：
 * 1. 安装所需图标集：pnpm add -D @iconify-json/[icon-set-name]
 * 2. 在此文件中导入并注册图标集
 * 3. 在组件中使用：<ArtSvgIcon icon="ri:home-line" />
 *
 * @module utils/ui/iconify-loader
 * @author Art Design Pro Team
 */

import { addIcon } from '@iconify/vue'

/**
 * 主升浪捕手自定义菜单图标：递增阳线 + 上升箭头。
 * 使用 currentColor，自动跟随主题菜单颜色；本地注册，不依赖 CDN。
 */
addIcon('mainwave:menu', {
  width: 24,
  height: 24,
  body: `
    <path d="M6.5 13.5v4.5M12 10.5v7.5M17.5 6.5v11.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
    <rect x="4.9" y="14.5" width="3.2" height="3.2" rx="0.5" fill="currentColor"/>
    <rect x="10.4" y="11.5" width="3.2" height="5.2" rx="0.5" fill="currentColor"/>
    <rect x="15.9" y="7" width="3.2" height="8.2" rx="0.5" fill="currentColor"/>
    <path d="M3 11.8 8 8.7l3.6 1.6 9.2-6.1" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M20.8 4.2h-5.3l4.7 4.5Z" fill="currentColor"/>
  `
})

// 离线图标集（内网环境可取消以下注释并安装 @iconify-json 对应图标包）
// import { addCollection } from '@iconify/vue'
// // 系统必要图标库
// import riIcons from '@iconify-json/ri/icons.json'

// // 演示图标库（可选，生产环境可移除）
// import svgSpinners from '@iconify-json/svg-spinners/icons.json'
// import lineMd from '@iconify-json/line-md/icons.json'

// // 注册离线图标集

// addCollection(riIcons)
// addCollection(svgSpinners)
// addCollection(lineMd)
