/** 离开页面前先问一次：脏状态在页面里，导航在壳层，中间隔着路由。
 *
 * 页面挂一个"返回提示文案"的函数（空串 = 可以放心走），App 的导航按钮与浏览器
 * 刷新/关闭都读它。不用层层传 prop，也不用把编辑状态提升到壳层。
 */
type EditGuard = () => string

let guard: EditGuard | null = null

export function setEditGuard(next: EditGuard | null) {
  guard = next
}

export function editGuardMessage(): string {
  return guard ? guard() : ''
}