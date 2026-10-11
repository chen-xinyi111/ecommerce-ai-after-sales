<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { api, post, money, executionLabels, type Application, type ExecutionDetail, type ExecutionJob, type OrderDetail } from '../api/client'
import { useIdentity } from '../stores/identity'
import { businessCopy, serviceError } from '../presentation'
const props = defineProps<{ application: Application }>()
const emit = defineEmits<{ updated: [] }>()
const identity = useIdentity()
const staff = computed(() => identity.actor?.role === 'staff')
const detail = ref<ExecutionDetail | null>(null)
const order = ref<OrderDetail | null>(null)
const busy = ref(false)
const loading = ref(true)
const error = ref('')
const note = ref('')
const reconciliation = ref('')
async function reconcile() {
  busy.value = true; error.value = ''
  try { reconciliation.value = (await post<{ message: string }>(`/applications/${props.application.id}/reconcile`, { note: note.value.trim() })).message; note.value = ''; await load(); emit('updated') }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
let generation = 0
let pending: { fingerprint: string; request_id: string } | null = null
async function load() {
  const current = ++generation
  const id = props.application.id
  loading.value = true; error.value = ''
  try {
    const result = await api<ExecutionDetail>(`/applications/${id}/execution`)
    if (current !== generation || id !== props.application.id) return
    detail.value = result
    if (result.job?.status === 'completed') {
      const updated = await api<OrderDetail>(`/orders/${props.application.order_id}`)
      if (current === generation && id === props.application.id) order.value = updated
    }
  } catch (e) { if (current === generation) error.value = (e as Error).message }
  finally { if (current === generation) loading.value = false }
}
async function execute(retry = false) {
  const app = props.application
  const body = { preview_id: app.preview_id, preview_version: app.preview_version, content_sha256: app.content_sha256, ...(retry ? { note: note.value.trim() } : {}) }
  const kind = retry ? 'retry-execution' : 'execute'
  const fingerprint = JSON.stringify({ id: app.id, kind, ...body })
  if (!pending || fingerprint !== pending.fingerprint) pending = { fingerprint, request_id: crypto.randomUUID() }
  busy.value = true; error.value = ''
  try {
    await post<ExecutionJob>(`/applications/${app.id}/${kind}`, { ...body, request_id: pending.request_id })
    pending = null; note.value = ''; await load(); emit('updated')
  } catch (e) { error.value = (e as Error).message; emit('updated') }
  finally { busy.value = false }
}
watch(() => [props.application.id, props.application.next_event_id], load, { immediate: true })
onUnmounted(() => generation++)
</script>
<template>
  <section class="execution-panel" aria-label="售后办理结果">
    <div class="panel-heading"><h3>办理结果</h3><el-tag effect="plain">{{ loading ? '加载中' : executionLabels[detail?.job?.status || 'not_started'] }}</el-tag></div>
    <p v-if="loading">正在加载办理记录…</p>
    <p v-if="error" class="error-text" role="alert">{{ serviceError(error) }}</p>
    <template v-if="!loading && !error && !detail?.job">
      <template v-if="application.status === 'approved' && application.workflow_status === 'complete'"><p>方案已审核通过，办理前将核对订单及售后条件。</p><el-button v-if="staff" type="primary" :loading="busy" @click="execute()">创建工单并执行</el-button><p v-else>等待客服发起执行，可在此查看结果。</p></template>
      <p v-else>尚无执行任务，需要先完成用户确认与客服审批。</p>
    </template>
    <template v-else-if="!loading && !error && detail?.job">
      <p v-if="['queued', 'running'].includes(detail.job.status)">正在办理，可稍后刷新查看进度。</p>
      <p v-if="detail.job.result"><strong>{{ businessCopy(detail.job.result.label) }}</strong></p>
      <p v-if="detail.work_order">工单 {{ detail.work_order.id }} · {{ detail.work_order.status === 'completed' ? '执行已完成' : detail.work_order.status === 'cancelled' ? '已停止' : '等待动作提交' }}</p>
      <p v-if="detail.reservation">库存 {{ detail.reservation.sku_id }} · {{ detail.reservation.quantity }} 件 · {{ ({ held: '已预留', consumed: '已用于发货', released: '已释放' } as Record<string, string>)[detail.reservation.status] }}</p>
      <p v-if="detail.refund">退款金额：{{ money(detail.refund.amount_cents) }} · {{ detail.refund.payment_id }}</p>
      <p v-if="detail.shipment">运单：{{ detail.shipment.tracking_number }} · 已发货 {{ detail.shipment.quantity }} 件</p>
      <p v-if="detail.repair">维修受理：等待检查。受理完成不代表商品已修复。</p>
      <p v-if="order">订单累计退款 {{ money(order.refunded_cents) }} / 实付 {{ money(order.paid_cents) }}；本次记录已反映到订单售后历史。</p>
      <template v-if="detail.job.status === 'failed'"><p>办理已中断，尚未发货的库存已释放。请核对订单后重试。</p><template v-if="staff && application.status === 'approved'"><el-input v-model="note" type="textarea" maxlength="500" placeholder="填写核对结果与重试依据" aria-label="执行重试意见" /><el-button type="primary" :loading="busy" :disabled="!note.trim()" @click="execute(true)">核对后重试执行</el-button></template></template>
      <p v-if="detail.job.status === 'stopped'">当前申请已停止，请重新生成方案、确认与审批。</p>
      <p v-if="detail.job.status === 'manual_review'" role="alert">办理结果需要人工核对。请联系人工客服核对退款或发货记录，避免重复办理。</p>
      <template v-if="staff && detail.job.status === 'manual_review'"><el-input v-model="note" type="textarea" maxlength="500" placeholder="填写退款、发货记录的核对结果" aria-label="人工核对意见" /><el-button :loading="busy" :disabled="!note.trim()" @click="reconcile">保存并重新核对结果</el-button><p v-if="reconciliation">{{ reconciliation }}</p></template>
    </template>
  </section>
</template>
