<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Box, ChatDotRound, List, SwitchButton, Search, Clock } from '@element-plus/icons-vue'
import { useIdentity } from '../stores/identity'
import { api, post, money, labels, type Order, type OrderPage, type Thread, type Inventory } from '../api/client'
import OrderDetail from '../components/OrderDetail.vue'
import ChatPanel from '../components/ChatPanel.vue'
import ApplicationPanel from '../components/ApplicationPanel.vue'
import OperationsPanel from '../components/OperationsPanel.vue'
import AccountPanel from '../components/AccountPanel.vue'
import NoticePanel from '../components/NoticePanel.vue'
import SupportDesk from '../components/SupportDesk.vue'
import ServiceInbox from '../components/ServiceInbox.vue'
import PolicyPanel from '../components/PolicyPanel.vue'
import { actorName } from '../presentation'
const identity = useIdentity()
const route = useRoute()
const router = useRouter()
const staff = computed(() => identity.actor?.role === 'staff')
const tab = computed(() => String(route.query.tab || 'orders'))
const threadId = computed(() => typeof route.query.thread === 'string' ? route.query.thread : null)
const orders = ref<Order[]>([])
const total = ref(0)
const page = ref(1)
const selected = ref<string | null>(typeof route.query.order === 'string' ? route.query.order : null)
const orderSearch = ref('')
const loading = ref(false)
const error = ref('')
const threads = ref<Thread[]>([])
const threadError = ref('')
const inventoryError = ref('')
const threadTotal = ref(0)
const threadPage = ref(1)
const inventories = ref<Inventory[]>([])
const inventorySearch = ref('')
const inventoryLoading = ref(false)
const filteredInventory = computed(() => inventories.value.filter(item => item.sku_id.toLowerCase().includes(inventorySearch.value.toLowerCase())))
const title = computed(() => ({ orders: staff.value ? '全部订单' : '我的订单', chat: '帮助与咨询', inventory: '库存管理', applications: staff.value ? '售后审批' : '我的售后', operations: '处理异常', support: '人工咨询', policies: '售后政策' })[tab.value] || '售后服务台')
function navigate(next: string) { void router.replace({ query: { tab: next } }) }
async function loadOrders() {
  loading.value = true; error.value = ''
  try {
    const data = await api<OrderPage>(`/orders?offset=${(page.value - 1) * 10}&limit=10`)
    orders.value = data.items; total.value = data.total
    if (!selected.value && data.items[0]) selected.value = data.items[0].id
  } catch (e) { error.value = e instanceof Error ? e.message : '订单加载失败' }
  finally { loading.value = false }
}
async function loadThreads() {
  threadError.value = ''
  try {
  const data = await api<{ items: Thread[]; total: number }>(`/threads?offset=${(threadPage.value - 1) * 30}&limit=30`)
  threads.value = data.items; threadTotal.value = data.total
  } catch (e) { threadError.value = e instanceof Error ? e.message : '咨询记录加载失败' }
}
async function consult(id: string) {
  try {
    const thread = await post<Thread>('/threads', { order_id: id })
    await loadThreads(); await router.replace({ query: { tab: 'chat', thread: thread.id } })
  } catch (e) { ElMessage.error(e instanceof Error ? e.message : '创建咨询失败') }
}
async function searchOrder() {
  const id = orderSearch.value.trim().toUpperCase()
  if (!/^ORD-\d{6}$/.test(id)) { ElMessage.warning('请填写 ORD-200001 这样的订单号'); return }
  selected.value = id
}
async function loadInventory() {
  if (!staff.value) { navigate('orders'); return }
  inventoryLoading.value = true; inventoryError.value = ''
  try { inventories.value = (await api<{ items: Inventory[] }>('/inventory')).items }
  catch (e) { inventoryError.value = e instanceof Error ? e.message : '库存加载失败' }
  finally { inventoryLoading.value = false }
}
async function logout() {
  try { await identity.logout(); await router.replace('/login') }
  catch (e) { ElMessage.error(e instanceof Error ? e.message : '注销失败') }
}
watch(tab, async next => {
  if (next === 'chat') { try { await loadThreads() } catch (e) { ElMessage.error((e as Error).message) } }
  if (next === 'inventory') await loadInventory()
})
watch(() => route.query.order, id => { if (typeof id === 'string') selected.value = id })
onMounted(async () => {
  await loadOrders()
  try { await loadThreads() } catch (e) { error.value = (e as Error).message }
  if (tab.value === 'inventory') await loadInventory()
})
</script>
<template>
  <div class="workspace-shell">
    <aside class="sidebar"><div class="brand"><span class="brand-mark">售</span><span>智能售后<span class="brand-sub">电商服务台</span></span></div><div class="workspace-label">{{ staff ? '客服工作台' : '我的服务中心' }}</div><nav><button :class="{ active: tab === 'orders' }" @click="navigate('orders')"><el-icon><List /></el-icon>{{ staff ? '全部订单' : '我的订单' }}</button><button :class="{ active: tab === 'applications' }" @click="navigate('applications')"><el-icon><Clock /></el-icon>{{ staff ? '售后审批' : '我的售后' }}</button><button :class="{ active: tab === 'chat' }" @click="navigate('chat')"><el-icon><ChatDotRound /></el-icon>{{ staff ? '咨询记录' : '帮助与咨询' }}</button><button v-if="staff" :class="{ active: tab === 'inventory' }" @click="navigate('inventory')"><el-icon><Box /></el-icon>库存查询</button><button v-if="staff" :class="{ active: tab === 'operations' }" @click="navigate('operations')"><el-icon><Clock /></el-icon>任务与异常</button><button v-if="staff" :class="{ active: tab === 'support' }" @click="navigate('support')">人工咨询</button><button v-if="staff" :class="{ active: tab === 'policies' }" @click="navigate('policies')">售后政策</button></nav><button class="logout" @click="logout"><el-icon><SwitchButton /></el-icon>退出登录</button></aside>
    <main class="workspace-main"><header class="topbar"><div><span class="eyebrow">{{ staff ? '客服工作台' : '用户服务中心' }}</span><span class="topbar-divider">/</span><span>{{ { orders: '订单', chat: '咨询', inventory: '库存', applications: staff ? '售后审批' : '我的售后', operations: '任务与异常', support: '人工咨询', policies: '售后政策' }[tab] || '订单' }}</span></div><div class="identity"><NoticePanel v-if="!staff" /><AccountPanel /><el-tag effect="plain" size="small">{{ staff ? '客服' : '用户' }}</el-tag><span>{{ actorName(identity.actor?.display_name || '', identity.actor?.id) }}</span><span class="identity-avatar">{{ staff ? '服' : '我' }}</span></div></header>
      <section class="page-heading"><h1>{{ title }}</h1></section><ServiceInbox v-if="staff" />
      <div v-if="error" class="page-error" role="alert">{{ error }} <el-button text @click="loadOrders">重试</el-button></div>
      <template v-if="tab === 'orders'">

        <div class="order-workbench"><section class="panel order-list" v-loading="loading"><div class="panel-heading"><h3>{{ staff ? '订单列表' : '我的订单' }}</h3><span class="muted small">共 {{ total }} 笔</span></div><form class="search-form" @submit.prevent="searchOrder"><el-input v-model="orderSearch" placeholder="输入订单号查询" :prefix-icon="Search" aria-label="订单号" /><el-button native-type="submit">查询</el-button></form><div class="order-cards"><button v-for="order in orders" :key="order.id" class="order-card" :class="{ selected: selected === order.id }" @click="selected = order.id"><div><strong>{{ order.id }}</strong><span class="order-status">{{ labels[order.status] || order.status }}</span></div><p>{{ order.purchased_on }} <span>{{ money(order.paid_cents) }}</span></p><span v-if="staff" class="muted small">{{ order.user_id }}</span></button><el-empty v-if="!orders.length && !loading" description="暂无订单。请确认购买时使用的账号，或在帮助与咨询查看使用说明。" :image-size="60" /></div><el-pagination v-model:current-page="page" :page-size="10" :total="total" layout="prev, pager, next" :pager-count="5" small @current-change="loadOrders" /></section><OrderDetail :order-id="selected" @consult="consult" /></div>
      </template>
      <div v-else-if="tab === 'chat'" class="chat-workbench"><section class="panel thread-list"><div class="panel-heading"><h3>咨询记录</h3><span class="muted small">{{ threadTotal }} 次</span></div><el-button class="new-consult" plain type="primary" @click="navigate('orders')">＋ 选择订单，新建咨询</el-button><p v-if="threadError" class="error-text" role="alert">{{ threadError }} <el-button text @click="loadThreads">重试加载咨询</el-button></p><button v-for="thread in threads" :key="thread.id" class="thread-card" :class="{ selected: threadId === thread.id }" @click="router.replace({ query: { tab: 'chat', thread: thread.id } })"><strong>{{ thread.order_id }}</strong><p>{{ thread.title }}</p><span>{{ new Date(thread.updated_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) }}</span></button><el-empty v-if="!threads.length && !threadError" description="还没有咨询记录。选择订单后可发起咨询" :image-size="60" /><el-pagination v-if="threadTotal > 30" v-model:current-page="threadPage" :page-size="30" :total="threadTotal" layout="prev, next" small @current-change="loadThreads" /></section><template v-if="threadId"><ChatPanel :thread-id="threadId" @loaded="selected = $event" @updated="loadThreads" /></template><section v-else class="panel choose-chat"><div class="help-steps"><h3>如何办理售后</h3><p>1. 在我的订单中选择订单，点击申请售后。</p><p>2. 选择类型，填写原因、数量及问题材料。</p><p>3. 核对方案后提交，在我的售后查看处理进度。</p><details><summary>没有订单怎么办？</summary><p>请确认登录的是购买时使用的账号。新注册账号尚无订单，需要业务方关联订单后才能办理售后。</p></details></div><el-empty description="需要办理售后：先选择订单，再点击申请售后。需要帮助：选择订单发起咨询，也可以转人工。" :image-size="100" /></section></div>
      <SupportDesk v-else-if="tab === 'support' && staff" /><PolicyPanel v-else-if="tab === 'policies' && staff" /><OperationsPanel v-else-if="tab === 'operations' && staff" /><ApplicationPanel v-else-if="tab === 'applications'" /><section v-else-if="tab === 'inventory' && staff" class="panel inventory-panel" v-loading="inventoryLoading"><div class="panel-heading"><div><h3>完整库存</h3><p class="muted small">共 {{ inventories.length }} 个 SKU · 只读查询</p></div><el-input v-model="inventorySearch" placeholder="搜索 SKU" :prefix-icon="Search" style="width:220px" aria-label="搜索 SKU" /></div><p v-if="inventoryError" class="error-text" role="alert">{{ inventoryError }} <el-button text @click="loadInventory">重试加载库存</el-button></p><el-table v-if="!inventoryError" :data="filteredInventory" stripe><el-table-column prop="sku_id" label="SKU 编号" /><el-table-column prop="on_hand" label="现有库存" /><el-table-column prop="reserved" label="已预留" /><el-table-column label="可用库存"><template #default="{ row }"><el-tag :type="row.available === 0 ? 'danger' : 'success'" effect="plain">{{ row.available }}</el-tag></template></el-table-column></el-table><el-empty v-if="!filteredInventory.length && !inventoryError && !inventoryLoading" description="没有匹配的库存记录" :image-size="60" /></section>
      <footer class="workspace-footer"><span>电商智能售后服务台</span><span>订单查询 · 售后申请 · 进度跟踪</span></footer>
    </main>
  </div>
</template>
