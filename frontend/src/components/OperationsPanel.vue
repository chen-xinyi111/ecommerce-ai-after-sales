<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, post, type Operations } from '../api/client'
import { serviceError } from '../presentation'
const data = ref<Operations | null>(null)
const error = ref('')
const busy = ref(false)
const note = ref('')
const selected = ref('')
const states: Record<string, string> = { queued: '待处理', waiting: '待处理', running: '处理中', completed: '已完成', complete: '已完成', failed: '处理异常', stopped: '已停止', manual_review: '待人工核对', not_started: '待办理', cancelled: '已取消' }
async function load() {
  busy.value = true; error.value = ''
  try { data.value = await api<Operations>('/operations') } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function redrive() {
  busy.value = true; error.value = ''
  try { await post(`/operations/dead-letters/${selected.value}/redrive`, { note: note.value }); selected.value = ''; note.value = ''; await load() }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <section class="panel operations-panel">
    <div class="panel-heading"><h3>任务与异常处理</h3><el-button :loading="busy" @click="load">刷新状态</el-button></div>
    <p v-if="error" class="error-text" role="alert">{{ serviceError(error) }}</p>
    <el-skeleton v-if="busy && !data" :rows="4" animated />
    <template v-if="data">
      <p v-for="(counts, kind) in data.tasks" :key="kind"><strong>{{ ({ execution: '售后办理', chat: '咨询处理', approval: '客服审批' } as Record<string, string>)[kind] || '任务处理' }}</strong>：<span v-for="(count, state) in counts" :key="state">{{ states[state] || '待核对' }} {{ count }}　</span></p>
      <h3>异常记录</h3><p class="muted small">售后办理中断时，请到申请详情核对后重试。下方支持重试的记录，可填写核对说明后重新处理。</p>
      <el-empty v-if="!data.dead_letters.length" description="暂无异常记录" :image-size="60" />
      <el-table v-else :data="data.dead_letters"><el-table-column label="发生时间" min-width="180"><template #default="{ row }">{{ new Date(row.created_at).toLocaleString('zh-CN') }}</template></el-table-column><el-table-column label="原因" min-width="160"><template #default="{ row }">{{ ({ publish_exhausted: '暂时无法安排处理', invalid_message: '收到无法识别的处理请求', execution_unavailable: '售后办理多次中断' } as Record<string, string>)[row.error_code] || '处理异常，请核对记录' }}</template></el-table-column><el-table-column label="状态"><template #default="{ row }">{{ row.status === 'resolved' ? '已处理' : '待核对' }}</template></el-table-column><el-table-column label="操作" min-width="160"><template #default="{ row }"><el-button v-if="row.status === 'open' && row.error_code === 'publish_exhausted'" size="small" @click="selected = row.id; note = ''">重试处理</el-button><span v-else class="muted small">{{ row.status === 'resolved' ? '处理记录已保存' : '请联系服务管理员核对' }}</span></template></el-table-column></el-table>
      <div v-if="selected"><el-input v-model="note" type="textarea" placeholder="填写核对结果与重试依据" maxlength="500" aria-label="重试说明" /><el-button type="primary" :loading="busy" :disabled="!note.trim()" @click="redrive">保存说明并重试</el-button><el-button @click="selected = ''; note = ''">取消</el-button></div>
    </template>
  </section>
</template>
