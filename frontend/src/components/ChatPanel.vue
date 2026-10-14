<script setup lang="ts">
import { ref, watch, onUnmounted, onMounted, nextTick, computed } from 'vue'
import { useIdentity } from '../stores/identity'
import { ElMessage } from 'element-plus'
import { Promotion, Refresh } from '@element-plus/icons-vue'
import { api, post, type History, type Message, type Run, type ChatEvent } from '../api/client'
import PreviewCard from './PreviewCard.vue'
import { assistantCopy, serviceError } from '../presentation'
import type { Preview } from '../api/client'
const props = defineProps<{ threadId: string }>()
const emit = defineEmits<{ loaded: [orderId: string]; updated: [] }>()
const messages = ref<Message[]>([])
const draft = ref('')
const busy = ref(false)
const loading = ref(false)
const error = ref('')
const phase = ref('可以开始咨询')
const connection = ref('')
const scrollbox = ref<HTMLElement | null>(null)
const identity = useIdentity()
type Support = { session: { state: string; staff_id: string | null } | null; messages: { id: number; role: string; content: string; created_at: string }[] }
const support = ref<Support>({ session: null, messages: [] })
const human = computed(() => !!support.value.session && support.value.session.state !== 'closed')
const staff = computed(() => identity.actor?.role === 'staff')
const canHumanReply = computed(() => !staff.value || (support.value.session?.state === 'active' && support.value.session.staff_id === identity.actor?.id))
const timeline = computed(() => [
  ...messages.value.map(row => ({ ...row, key: 'ai-' + row.id })),
  ...support.value.messages.map(row => ({ ...row, key: 'human-' + row.id, status: 'complete', preview: null })),
].sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime()))
let supportTimer: ReturnType<typeof setInterval> | undefined
async function loadSupport() {
  const current = generation
  const data = await api<Support>(`/threads/${props.threadId}/support`)
  if (current === generation) support.value = data
}
async function supportAction(action: string) {
  error.value = ''
  try { await post(`/threads/${props.threadId}/support/${action}`, {}); await loadSupport(); if (human.value) { closeStream(); busy.value = false }; emit('updated') }
  catch (e) { error.value = (e as Error).message }
}
onMounted(() => { supportTimer = setInterval(() => { if (!document.hidden) void loadSupport().catch(e => error.value = e.message) }, 3000) })
let source: EventSource | null = null
let generation = 0
let pending: { message: string; request_id: string } | null = null
let lastSeq = 0
function closeStream() { source?.close(); source = null }
async function scroll() { await nextTick(); if (scrollbox.value) scrollbox.value.scrollTop = scrollbox.value.scrollHeight }
function apply(event: ChatEvent, run: Run) {
  if (event.seq && event.seq <= lastSeq) return
  if (event.seq) lastSeq = event.seq
  const payload = event.payload as Record<string, unknown>
  if (event.kind === 'token') {
    const assistant = messages.value.find(message => message.run_id === run.id && message.role === 'assistant')
    if (assistant) assistant.content += String(payload.text ?? '')
    void scroll()
  } else if (event.kind === 'preview') {
    const assistant = messages.value.find(message => message.run_id === run.id && message.role === 'assistant')
    if (assistant) assistant.preview = payload as unknown as Preview
    void scroll()
  } else if (event.kind === 'status') phase.value = String(payload.label ?? '正在处理')
  else if (event.kind === 'done' || event.kind === 'error') {
    if (payload.reconnect) { connection.value = '连接恢复中'; return }
    error.value = event.kind === 'error' ? String(payload.message ?? '本次回复失败') : ''
    phase.value = event.kind === 'error' ? '回复已中断' : '回复已完成'
    busy.value = false; connection.value = ''; closeStream(); pending = null
    void refresh(false).catch(e => error.value = e.message); emit('updated')
  }
}
function listen(run: Run) {
  closeStream(); lastSeq = 0; busy.value = true
  const assistant = messages.value.find(message => message.run_id === run.id && message.role === 'assistant')
  if (assistant) assistant.content = ''
  const current = generation
  source = new EventSource(`/api/v1/runs/${run.id}/events`)
  source.onopen = () => { connection.value = '' }
  source.onmessage = (message) => { if (current === generation) apply(JSON.parse(message.data), run) }
  source.onerror = () => {
    if (current !== generation) return
    connection.value = '连接暂时中断，正在恢复…'
    void api<{ run: Run; events: ChatEvent[] }>(`/runs/${run.id}?after=${lastSeq}`).then(data => {
      if (current !== generation) return
      for (const event of data.events) apply(event, run)
    }).catch(e => { error.value = e instanceof Error ? e.message : '连接异常' })
  }
}
async function refresh(reconnect = true) {
  const current = generation
  const data = await api<History>(`/threads/${props.threadId}`)
  if (current !== generation) return
  messages.value = data.messages; emit('loaded', data.thread.order_id)
  await loadSupport()
  const latest = data.runs.at(-1)
  if (latest) {
    if (reconnect && !human.value && ['queued', 'running'].includes(latest.status)) listen(latest)
    else if (reconnect) {
      const result = await api<{ run: Run; events: ChatEvent[] }>(`/runs/${latest.id}`)
      if (current !== generation) return
      const failure = result.events.find(event => event.kind === 'error')
      if (failure) { error.value = String(failure.payload.message); phase.value = '回复已中断' }
      else phase.value = '回复已完成'
    }
  }
  void scroll()
}
watch(() => props.threadId, async () => {
  support.value = { session: null, messages: [] }
  generation++; closeStream(); busy.value = false; loading.value = true; error.value = ''; draft.value = ''; pending = null; messages.value = []; phase.value = '可以开始咨询'; connection.value = ''
  try { await refresh() } catch (e) { error.value = e instanceof Error ? e.message : '会话加载失败' }
  finally { loading.value = false }
}, { immediate: true })
async function send() {
  const text = draft.value.trim()
  if (!text || busy.value) return
  if (text.length > 2000) { ElMessage.warning('消息最多 2,000 字'); return }
  const current = generation
  if (!pending || pending.message !== text) pending = { message: text, request_id: crypto.randomUUID() }
  busy.value = true; error.value = ''
  try {
    if (human.value) {
      await post(`/threads/${props.threadId}/support/messages`, { content: text, request_id: pending.request_id })
      if (current !== generation) return
      draft.value = ''; pending = null; busy.value = false; await loadSupport(); await scroll(); emit('updated'); return
    }
    const run = await post<Run>(`/threads/${props.threadId}/messages`, pending)
    if (current !== generation) return
    draft.value = ''; await refresh(false); listen(run); emit('updated')
  } catch (e) { if (current === generation) { busy.value = false; error.value = e instanceof Error ? e.message : '发送失败，点击发送可重试' } }
}
function shortcut(event: KeyboardEvent) { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); void send() } }
onUnmounted(() => { generation++; closeStream(); clearInterval(supportTimer) })
</script>
<template>
  <section class="panel chat-panel" v-loading="loading">
    <div class="chat-heading"><h3>{{ human ? (support.session?.state === 'queued' ? '等待人工客服接单' : '人工客服处理中') : '智能售后助手' }}</h3><el-button v-if="!staff && !human" :disabled="busy" @click="supportAction('request')">转人工</el-button><el-button v-if="staff && support.session?.state === 'queued'" @click="supportAction('claim')">接单</el-button><el-button v-if="staff && human && canHumanReply" @click="supportAction('close')">结束服务</el-button></div>
    <div class="chat-boundary">{{ human ? '已转人工，智能售后助手暂停回复。可以继续补充问题，客服上线后回复。' : support.session?.state === 'closed' ? '人工服务已结束，可以继续咨询；人工沟通记录将作为咨询背景。' : '描述问题获取帮助。办理售后请从订单提交申请，由客服审核。' }}</div>
    <div ref="scrollbox" class="message-list" aria-live="polite">
      <div v-if="!messages.length" class="chat-welcome"><span class="assistant-avatar">助</span><h3>你好，有什么需要帮忙？</h3><p>这次咨询已关联选定订单。<br>可以询问订单情况，也可以描述退款、换货、补发或维修申请。</p><button class="suggestion" @click="draft = '请查询这个订单，说明订单状态、商品明细和历史退款。'">查询这笔订单的情况 →</button></div>
      <article v-for="message in timeline" :key="message.key" class="message" :class="message.role === 'user' ? 'user' : 'assistant'"><span class="message-avatar">{{ message.role === 'user' ? '我' : message.role === 'staff' ? '服' : '助' }}</span><div class="message-body"><div class="message-name">{{ message.role === 'user' ? '用户' : message.role === 'staff' ? '人工客服' : '智能售后助手' }}<span>{{ new Date(message.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}</span></div><p class="message-content">{{ (message.role === 'assistant' ? assistantCopy(message.content) : message.content) || (message.status === 'partial' ? '正在准备回复…' : '本次未获得回复') }}</p><PreviewCard v-if="message.preview" :preview="message.preview" /><span v-if="message.status === 'failed'" class="interrupted">回复中断 · 已保留收到的内容</span></div></article>
    </div>
    <div class="chat-status"><span><i class="status-dot" :class="{ busy }"></i>{{ human ? (support.session?.state === 'queued' ? '等待接单' : '人工服务中') : phase }}</span><span v-if="connection" role="status">{{ connection }}</span><el-button v-if="!busy" text size="small" :icon="Refresh" @click="refresh().catch(e => error = e.message)">刷新记录</el-button></div>
    <p v-if="error" class="chat-error" role="alert">{{ serviceError(error) }}</p>
    <form class="composer" @submit.prevent="send"><el-input v-model="draft" type="textarea" :rows="3" resize="none" maxlength="2000" placeholder="描述遇到的问题，或直接询问订单情况…" :disabled="busy" aria-label="咨询消息" @keydown="shortcut" /><div class="composer-footer"><span>Ctrl + Enter 发送 · 内容自动保存</span><el-button type="primary" native-type="submit" :icon="Promotion" :loading="busy" :disabled="!draft.trim() || loading || (staff && !canHumanReply)">{{ busy ? (human ? '发送中' : '回复中') : '发送消息' }}</el-button></div></form>
  </section>
</template>
