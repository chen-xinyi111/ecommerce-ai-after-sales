<script setup lang="ts">
import { ref, watch } from 'vue'
import { api, ApiError, money, labels, type OrderDetail, type Logistics, type Inventory } from '../api/client'
import { productName, businessCopy } from '../presentation'
import PreviewForm from './PreviewForm.vue'
const props = defineProps<{ orderId: string | null; compact?: boolean }>()
const emit = defineEmits<{ consult: [id: string] }>()
const detail = ref<OrderDetail | null>(null)
const logistics = ref<Logistics | null>(null)
const inventory = ref<Inventory[]>([])
const error = ref('')
const logisticsNote = ref('')
const loading = ref(false)
const tab = ref('items')
let generation = 0
watch(() => props.orderId, async (id) => {
  const current = ++generation
  detail.value = null; logistics.value = null; inventory.value = []; error.value = ''; logisticsNote.value = ''
  if (!id) return
  loading.value = true
  try {
    const order = await api<OrderDetail>(`/orders/${id}`)
    if (current !== generation) return
    detail.value = order
    const results = await Promise.allSettled([
      api<Logistics>(`/orders/${id}/logistics`),
      Promise.all(order.items.map(item => api<Inventory>(`/inventory/${item.sku_id}`))),
    ])
    if (current !== generation) return
    if (results[0].status === 'fulfilled') logistics.value = results[0].value
    else logisticsNote.value = results[0].reason instanceof ApiError && results[0].reason.status === 404 ? '该订单暂无物流记录' : '物流暂时无法加载，请重新选择订单重试'
    if (results[1].status === 'fulfilled') inventory.value = results[1].value
  } catch (e) { if (current === generation) error.value = e instanceof Error ? e.message : '订单加载失败' }
  finally { if (current === generation) loading.value = false }
}, { immediate: true })
</script>
<template>
  <section class="panel detail-panel" v-loading="loading">
    <div v-if="error" class="empty-state"><p class="error-text" role="alert">{{ error }}</p></div>
    <template v-else-if="detail">
      <div class="panel-heading"><div><div class="eyebrow">订单详情</div><h3>{{ detail.id }}</h3></div><el-tag effect="plain" :type="detail.refunded_cents ? 'warning' : 'success'">{{ labels[detail.status] || detail.status }}</el-tag></div>
      <div class="order-meta"><span>购买日期 {{ detail.purchased_on }}</span><span>{{ detail.user_id }}</span></div>
      <div class="amount-row"><div><span>实付金额</span><strong>{{ money(detail.paid_cents) }}</strong></div><div><span>累计退款</span><strong>{{ money(detail.refunded_cents) }}</strong></div></div>
      <div v-if="!compact" class="order-actions"><el-button type="primary" @click="tab = 'preview'">申请售后</el-button><el-button text @click="emit('consult', detail.id)">咨询此订单</el-button></div>
      <el-tabs v-model="tab">
        <el-tab-pane label="方案预览" name="preview"><PreviewForm :order="detail" /></el-tab-pane>
        <el-tab-pane label="商品与库存" name="items"><div v-for="item in detail.items" :key="item.id" class="product-row"><div class="product-avatar">品</div><div class="product-info"><strong>{{ productName(item.product_name, item.sku_id) }}</strong><span>{{ item.sku_id }} · 数量 {{ item.quantity }}</span><span>单价 {{ money(item.unit_price_cents) }} · 历史售后数量 {{ item.after_sales_quantity }}</span><span v-if="inventory.find(row => row.sku_id === item.sku_id)">可用库存 {{ inventory.find(row => row.sku_id === item.sku_id)?.available }}</span></div></div><p v-if="!inventory.length" class="muted small">库存暂时无法加载，稍后重选订单重试。</p></el-tab-pane>
        <el-tab-pane label="物流轨迹" name="logistics"><template v-if="logistics"><div class="logistics-info"><strong>{{ labels[logistics.status] || logistics.status }}</strong><span>{{ logistics.tracking_number }}</span></div><el-timeline><el-timeline-item v-for="event in logistics.events" :key="event.occurred_at + event.status" :timestamp="new Date(event.occurred_at).toLocaleString('zh-CN')">{{ businessCopy(event.description) }}</el-timeline-item></el-timeline></template><el-empty v-else :description="logisticsNote" :image-size="70" /></el-tab-pane>
        <el-tab-pane label="支付与售后" name="history"><div v-for="payment in detail.payments" :key="payment.id" class="record-row"><strong>支付记录</strong><span>{{ payment.id }}</span><span>实付 {{ money(payment.paid_cents) }} · 退款 {{ money(payment.refunded_cents) }}</span></div><div v-for="record in detail.after_sales_history" :key="record.id" class="record-row"><strong>{{ labels[record.kind] || record.kind }} · {{ labels[record.status] || record.status }}</strong><span>数量 {{ record.quantity }} · {{ money(record.refund_cents) }}</span></div><p v-if="!detail.after_sales_history.length" class="muted">暂无历史售后记录</p></el-tab-pane>
      </el-tabs>
    </template>
    <el-empty v-else-if="!loading && !error" description="选择一笔订单，查看详情" :image-size="90" />
  </section>
</template>
