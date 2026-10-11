<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { businessCopy, serviceError } from '../presentation'
import { useIdentity } from '../stores/identity'
import { api, post, labels, executionLabels, type Application, type ApplicationDetail, type ApplicationPage } from '../api/client'
import PreviewCard from './PreviewCard.vue'
import ExecutionPanel from './ExecutionPanel.vue'
import MaterialPanel from './MaterialPanel.vue'
const materialNote = ref('')
async function requestMaterials() {
  if (!detail.value || !materialNote.value.trim()) return
  busy.value = true; error.value = ''
  try { await post(`/applications/${detail.value.application.id}/request-materials`, { note: materialNote.value }); materialNote.value = ''; await refresh() }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function consultApplication() {
  if (!detail.value) return
  try { const thread = await post<{ id: string }>('/threads', { order_id: detail.value.application.order_id }); await router.replace({ query: { tab: 'chat', thread: thread.id } }) }
  catch (e) { error.value = (e as Error).message }
}
const nextStep = computed(() => {
  const app = detail.value?.application
  if (!app) return ''
  if (app.execution_state === 'completed') return '办理结果已更新，请查看退款、运单或维修受理记录。'
  if (['queued', 'running'].includes(app.execution_state)) return '正在办理，请稍后刷新查看结果。'
  if (['failed', 'manual_review', 'stopped'].includes(app.execution_state)) return '客服正在核对办理结果。需要帮助时可咨询此申请。'
  if (['conditions_changed', 'superseded'].includes(app.status)) return '请返回订单重新填写申请并确认新方案。'
  if (app.status === 'rejected') return '请查看客服意见，补充信息后可重新申请。'
  if (app.status === 'cancelled') return '申请已取消。如仍需办理，请重新申请。'
  if (app.status === 'approved') return '审批已通过，等待客服办理。'
  if (app.status === 'decision_recorded') return '客服审批意见已保存，等待继续办理。'
  return '申请已提交，请等待客服审核；有补充请求时在下方提交材料。'
})
const identity = useIdentity()
const staff = computed(() => identity.actor?.role === 'staff')
const route = useRoute()
const router = useRouter()
const rows = ref<Application[]>([])
const total = ref(0)
const page = ref(1)
const filter = ref(staff.value ? 'pending_approval' : '')
const detail = ref<ApplicationDetail | null>(null)
const error = ref('')
const busy = ref(false)
const note = ref('')
const connection = ref('')
const selected = computed(() => typeof route.query.application === 'string' ? route.query.application : null)
const canCancel = computed(() => detail.value && ['pending_approval', 'decision_recorded', 'approved'].includes(detail.value.application.status) && !['completed', 'manual_review'].includes(detail.value.application.execution_state))
function ended(app: Application) { return ['complete', 'failed'].includes(app.workflow_status) && !['queued', 'running'].includes(app.execution_state) }
const canResume = computed(() => detail.value && ['waiting', 'failed'].includes(detail.value.application.workflow_status) && (detail.value.application.decision || detail.value.application.workflow_status === 'failed') && !['superseded', 'cancelled', 'conditions_changed'].includes(detail.value.application.status))
let source: EventSource | null = null
let seq = 0
let generation = 0
let listGeneration = 0
let detailGeneration = 0
let pending: { fingerprint: string; request_id: string } | null = null
function close() { source?.close(); source = null }
async function list() {
  const request = ++listGeneration
  const data = await api<ApplicationPage>(`/applications?offset=${(page.value - 1) * 20}&limit=20${filter.value ? '&status=' + filter.value : ''}`)
  if (request !== listGeneration) return
  rows.value = data.items; total.value = data.total
}
async function loadDetail(id: string) {
  if (id !== selected.value) return
  const request = ++detailGeneration
  const current = generation
  const result = await api<ApplicationDetail>(`/applications/${id}`)
  if (id !== selected.value || current !== generation || request !== detailGeneration) return
  detail.value = result
  seq = result.application.next_event_id
  if (ended(result.application)) { connection.value = result.application.workflow_status === 'complete' ? '流程已结束' : '流程需重试'; close() }
}
function stream(id: string) {
  if (id !== selected.value || !detail.value || ended(detail.value.application)) return
  source = new EventSource(`/api/v1/applications/${id}/events?after=${seq}`)
  const active = source
  source.onmessage = event => {
    if (source !== active || id !== selected.value) return
    const data = JSON.parse(event.data)
    if (data.kind === 'error') { connection.value = data.payload.label; close(); return }
    if (data.seq <= seq) return
    seq = data.seq
    void loadDetail(id).then(() => list()).catch(e => error.value = e.message)
  }
  source.onopen = () => { if (source === active && id === selected.value) connection.value = '进度已连接' }
  source.onerror = () => { if (source !== active || id !== selected.value) return; connection.value = '正在重新连接'; void loadDetail(id).then(() => list()).catch(e => error.value = e.message) }
}
async function refresh() {
  busy.value = true; error.value = ''
  try { await list(); if (selected.value) { close(); await loadDetail(selected.value); stream(selected.value) } }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
function choose(id: string) { void router.replace({ query: { tab: 'applications', application: id } }) }
async function action(kind: 'cancel' | 'decision' | 'resume', decision?: 'approved' | 'rejected') {
  if (!detail.value) return
  const app = detail.value.application
  const body = { preview_id: app.preview_id, preview_version: app.preview_version, content_sha256: app.content_sha256, ...(decision ? { decision, note: note.value.trim() } : {}) }
  const fingerprint = JSON.stringify({ id: app.id, kind, ...body })
  if (!pending || pending.fingerprint !== fingerprint) pending = { fingerprint, request_id: crypto.randomUUID() }
  busy.value = true; error.value = ''
  try {
    await post<Application>(`/applications/${app.id}/${kind}`, { ...body, request_id: pending.request_id })
    pending = null; note.value = ''; await refresh()
  } catch (e) { error.value = (e as Error).message; await loadDetail(app.id).catch(() => {}) }
  finally { busy.value = false }
}
watch(selected, async id => {
  generation++; close(); detail.value = null; seq = 0; note.value = ''; pending = null; error.value = ''
  if (id) { try { await loadDetail(id); stream(id) } catch (e) { error.value = (e as Error).message } }
}, { immediate: true })
watch(filter, () => { page.value = 1; void refresh() })
void list().catch(e => error.value = e.message)
onUnmounted(close)
</script>
<template>
  <div class="application-workbench">
    <section class="panel application-list"><div class="panel-heading"><h3>{{ staff ? '售后审批' : '我的售后' }}</h3><el-button text :loading="busy" @click="refresh">刷新</el-button></div><el-select v-model="filter" placeholder="全部申请" aria-label="申请状态筛选"><el-option label="全部申请" value="" /><el-option v-for="status in ['pending_approval', 'decision_recorded', 'approved', 'rejected', 'cancelled', 'superseded', 'conditions_changed']" :key="status" :label="labels[status]" :value="status" /></el-select><button v-for="row in rows" :key="row.id" class="thread-card" :class="{ selected: selected === row.id }" @click="choose(row.id)"><strong>{{ row.order_id }}</strong><p>{{ row.execution_state === 'not_started' ? labels[row.status] : executionLabels[row.execution_state] }}</p><span>{{ new Date(row.confirmed_at).toLocaleString('zh-CN') }}<template v-if="staff"> · {{ row.user_id }}</template></span></button><el-empty v-if="!rows.length" description="暂无申请；用户确认合格方案后才会进入审批" :image-size="65" /><el-pagination v-if="total > 20" v-model:current-page="page" :page-size="20" :total="total" small layout="prev, next" @current-change="refresh" /></section>
    <section class="panel application-detail"><p v-if="error" class="error-text" role="alert">{{ serviceError(error) }}</p><template v-if="detail"><div class="panel-heading"><div><h3>{{ labels[detail.application.status] }}</h3><p class="muted small">{{ detail.application.order_id }} · {{ labels[detail.application.workflow_status] }}<template v-if="connection && connection !== labels[detail.application.workflow_status]"> · {{ businessCopy(connection) }}</template></p></div><el-tag effect="plain">{{ executionLabels[detail.application.execution_state] }}</el-tag></div><p class="muted small">申请编号 {{ detail.application.id }}<br>用户确认于 {{ new Date(detail.application.confirmed_at).toLocaleString('zh-CN') }}</p><div class="application-next"><strong>下一步</strong><p>{{ nextStep }}</p><el-button v-if="!staff" text @click="consultApplication">咨询此申请</el-button><el-button v-if="!staff && ['rejected','cancelled','superseded','conditions_changed'].includes(detail.application.status)" type="primary" @click="router.replace({query:{tab:'orders',order:detail.application.order_id}})">重新申请</el-button></div><MaterialPanel :target="detail.application.id" :refresh-key="detail.application.next_event_id" application :editable="detail.application.status === 'pending_approval'" @updated="refresh" /><ExecutionPanel :application="detail.application" @updated="refresh" /><details class="preview-evidence"><summary>用户确认的方案</summary><PreviewCard :preview="detail.preview" hide-confirmation /></details>
      <div class="approval-controls"><template v-if="staff && detail.application.status === 'pending_approval' && detail.application.workflow_status === 'waiting'"><el-input v-model="note" type="textarea" :rows="3" maxlength="500" show-word-limit placeholder="填写审批依据或拒绝原因" aria-label="审批意见" /><el-input v-model="materialNote" placeholder="需要用户补充的材料" aria-label="补充材料要求" /><el-button :disabled="!materialNote.trim()" :loading="busy" @click="requestMaterials">请求补充材料</el-button><div class="approval-buttons"><el-button type="primary" :loading="busy" :disabled="!note.trim()" @click="action('decision', 'approved')">批准此版本方案</el-button><el-button type="danger" plain :loading="busy" :disabled="!note.trim()" @click="action('decision', 'rejected')">拒绝申请</el-button></div></template><template v-if="staff && canResume"><p>审批意见已保存，请继续办理。系统将再次核对售后条件。</p><el-button type="primary" :loading="busy" @click="action('resume')">{{ detail.application.workflow_status === 'failed' ? '重试审批流程' : '继续办理' }}</el-button></template><el-button v-if="!staff && canCancel" type="danger" plain :loading="busy" @click="action('cancel')">取消申请</el-button><p v-if="detail.application.decision_note">客服意见：{{ detail.application.decision_note }}</p><p v-if="detail.application.status === 'approved'">用户确认与客服审批已完成。办理进度和结果见下方记录。</p><p v-if="['superseded', 'conditions_changed'].includes(detail.application.status)">此申请已失效，请生成新方案并重新确认。</p></div>
      <details><summary>审批与执行记录</summary><el-timeline class="approval-timeline"><el-timeline-item v-for="event in detail.events" :key="event.seq" :timestamp="new Date(event.created_at).toLocaleString('zh-CN')"><strong>{{ businessCopy(String(event.payload.label || '处理记录')) }}</strong><p class="muted small">{{ event.actor_id ? (event.actor_id.startsWith('STAFF-') ? '客服处理' : event.actor_id.startsWith('USR-') ? '用户操作' : '系统处理') : '系统处理' }}</p><p v-if="event.payload.note">{{ event.payload.note }}</p></el-timeline-item></el-timeline></details></template><el-empty v-else-if="!error" description="选择申请，查看确认方案与审批记录" :image-size="90" /></section>
  </div>
</template>
