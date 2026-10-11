import type { components } from './schema'

export type Actor = components['schemas']['Principal']
export type Order = components['schemas']['OrderSummary']
export type OrderDetail = components['schemas']['OrderDetail']
export type OrderPage = components['schemas']['OrderPage']
export type Logistics = components['schemas']['Logistics']
export type Inventory = components['schemas']['Inventory']
export type Thread = components['schemas']['ChatThread']
export type History = components['schemas']['ThreadHistory']
export type Run = components['schemas']['ChatRun']
export type Message = History['messages'][number]
export type ChatEvent = components['schemas']['ChatEvent']
export type Preview = components['schemas']['StoredPreview']
export type PreviewDraft = components['schemas']['PreviewDraft']
export type Application = components['schemas']['Application']
export type ApplicationDetail = components['schemas']['ApplicationDetail']
export type ApplicationPage = components['schemas']['ApplicationPage']
export type ExecutionJob = components['schemas']['ExecutionJob']
export type ExecutionDetail = components['schemas']['ExecutionDetail']
export type Operations = components['schemas']['Operations']

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try { response = await fetch(`/api/v1${path}`, {
    ...init, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...init.headers },
  }) } catch { throw new ApiError(0, '暂时无法连接服务。请稍后重试。') }
  if (!response.ok) {
    const data = await response.json().catch(() => ({ detail: '连接异常，请稍后再试' }))
    if (response.status === 401) window.dispatchEvent(new Event('session-expired'))
    const message = response.status === 401 && path !== '/auth/login' ? '登录已失效，请重新登录。' : response.status === 403 ? '当前账号无权执行此操作。' : typeof data.detail === 'string' ? data.detail : '请求参数无效，请检查必填项。'
    throw new ApiError(response.status, message)
  }
  if (response.status === 204) return undefined as T
  return response.json()
}
export const post = <T>(path: string, body: unknown) => api<T>(path, { method: 'POST', body: JSON.stringify(body) })
export const money = (cents: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(cents / 100)
export const executionLabels: Record<string, string> = { not_started: '等待办理', queued: '待办理', running: '办理中', completed: '办理完成', failed: '办理中断', stopped: '已停止', manual_review: '人工核对中' }
export const labels: Record<string, string> = {
  paid: '待发货', shipped: '运输中', delivered: '已签收', cancelled: '已取消',
  partially_refunded: '部分退款', lost: '物流丢失', queued: '等待处理', running: '回复中',
  completed: '已完成', failed: '已中断', user: '用户', staff: '客服',
  refund: '退款', exchange: '换货', reship: '补发', repair: '维修',
  pending_approval: '等待客服审批', decision_recorded: '审批意见已保存', approved: '审批已通过', rejected: '客服已拒绝', superseded: '方案已修改，申请失效', conditions_changed: '条件变化，需重新确认', waiting: '等待客服处理', complete: '流程已结束',
}
