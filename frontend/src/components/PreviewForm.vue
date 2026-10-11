<script setup lang="ts">
import { productName } from '../presentation'
import { ref, watch } from 'vue'
import { api, post, labels, type OrderDetail, type Preview, type PreviewDraft } from '../api/client'
import PreviewCard from './PreviewCard.vue'
import MaterialPanel from './MaterialPanel.vue'
const props = defineProps<{ order: OrderDetail }>()
const draft = ref<PreviewDraft>({ kind: null, order_item_id: null, quantity: 1, reason: null, received: null })
const preview = ref<Preview | null>(null)
const history = ref<Preview[]>([])
const error = ref('')
const busy = ref(false)
const step = ref(0)
let generation = 0
let pending: { request_id: string; fingerprint: string } | null = null
watch(() => props.order.id, async () => {
  const current = ++generation
  draft.value = { kind: null, order_item_id: null, quantity: 1, reason: null, received: null }
  preview.value = null; history.value = []; error.value = ''; pending = null
  step.value = 0
  try { const data = await api<{ items: Preview[] }>(`/orders/${props.order.id}/previews`); if (current === generation) history.value = data.items }
  catch (e) { if (current === generation) error.value = (e as Error).message }
}, { immediate: true })
async function submit() {
  const current = generation; const orderId = props.order.id
  const fingerprint = JSON.stringify(draft.value)
  if (!pending || pending.fingerprint !== fingerprint) pending = { fingerprint, request_id: crypto.randomUUID() }
  busy.value = true; error.value = ''
  try {
    const result = await post<Preview>(`/orders/${orderId}/previews`, { ...draft.value, request_id: pending.request_id })
    if (current !== generation) return
    preview.value = result
    step.value = 2
    history.value = [result, ...history.value.filter(row => row.id !== result.id)].slice(0, 30)
    pending = null
  } catch (e) { if (current === generation) error.value = (e as Error).message }
  finally { busy.value = false }
}
</script>
<template>
  <div class="preview-form">
    <el-steps :active="step" simple><el-step title="售后类型" /><el-step title="申请信息" /><el-step title="材料与确认" /></el-steps>
    <el-form v-if="step < 2" label-position="top" @submit.prevent="submit">
      <template v-if="step === 0"><el-form-item label="售后类型"><el-select v-model="draft.kind" placeholder="请选择" aria-label="售后类型"><el-option v-for="kind in ['refund', 'exchange', 'reship', 'repair']" :key="kind" :label="labels[kind]" :value="kind" /></el-select></el-form-item><el-button type="primary" :disabled="!draft.kind" @click="step = 1">下一步</el-button></template>
      <template v-else><el-form-item label="申请商品"><el-select v-model="draft.order_item_id" placeholder="选择商品" aria-label="申请商品"><el-option v-for="item in order.items" :key="item.id" :label="productName(item.product_name, item.sku_id)" :value="item.id" /></el-select></el-form-item><div class="preview-form-row"><el-form-item label="申请数量"><el-input-number v-model="draft.quantity" :min="1" :max="100" :precision="0" aria-label="申请数量" /></el-form-item><el-form-item label="收货状态"><el-select v-model="draft.received" placeholder="是否收到" aria-label="收货状态"><el-option label="已收到" :value="true" /><el-option label="未收到" :value="false" /></el-select></el-form-item></div><el-form-item label="申请原因"><el-select v-model="draft.reason" placeholder="选择原因" aria-label="申请原因"><el-option label="质量问题" value="quality" /><el-option label="错发商品" value="wrong_item" /><el-option label="未收到货" value="not_received" /><el-option label="不想要了" value="changed_mind" /></el-select></el-form-item><el-button @click="step = 0">上一步</el-button><el-button type="primary" native-type="submit" :disabled="!draft.order_item_id || !draft.quantity || !draft.reason || draft.received == null" :loading="busy">下一步：核对方案</el-button></template>
    </el-form>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <template v-if="step === 2 && preview"><el-button text @click="step = 1; preview = null">返回修改申请信息</el-button><MaterialPanel :target="preview.id" editable /><PreviewCard :preview="preview" /></template>
    <details v-if="history.length" class="preview-history"><summary>历史方案（{{ history.length }}）</summary><button v-for="row in history" :key="row.id" class="preview-history-row" @click="preview = row; step = 2">{{ labels[row.request.kind || ''] || '待补充' }} · {{ new Date(row.created_at).toLocaleString('zh-CN') }} · {{ row.status === 'eligible' ? '可申请' : row.status === 'needs_info' ? '待补充' : '不符合条件' }}</button></details>
  </div>
</template>
