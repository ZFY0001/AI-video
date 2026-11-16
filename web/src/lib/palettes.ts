import type { PaletteId } from './api'

/** 与后端 pipeline/palette.py 的 id 一一对应；只影响配色，不改题材与文案。 */
export const PALETTE_OPTIONS: {
  value: PaletteId
  label: string
  hint: string
  colors: string[]
}[] = [
  { value: 'auto', label: '跟随题材', hint: '使用题材自带配色', colors: [] },
  { value: 'warm', label: '暖阳', hint: '琥珀、麦色与暖白', colors: ['#5A2E10', '#C97430', '#F6D08A'] },
  { value: 'cool', label: '冷夜', hint: '深蓝、青灰与冷白', colors: ['#123049', '#3E93B0', '#8FD0DC'] },
  { value: 'film', label: '胶片', hint: '低饱和土色与奶白', colors: ['#3A3226', '#A08A6A', '#F2E7D0'] },
  { value: 'ink', label: '水墨', hint: '近黑、灰阶与宣纸白', colors: ['#22262A', '#8A9094', '#F4F6F7'] },
  { value: 'vivid', label: '高对比', hint: '深底 + 亮色', colors: ['#141A22', '#4E6A8A', '#FFFFFF'] },
]
