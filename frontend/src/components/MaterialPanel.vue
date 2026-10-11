<script setup lang="ts">
import { ref, watch } from 'vue'
import { api, post } from '../api/client'
import { useIdentity } from '../stores/identity'
const props = defineProps<{ target: string; application?: boolean; editable?: boolean; refreshKey?: number }>()
const emit = defineEmits<{ updated: [] }>()
const identity = useIdentity()
const rows = ref<{ id: string; note: string; name: string; mime: string | null }[]>([])
const requests = ref<{ id: string; note: string; state: string }[]>([])
const note = ref(''), error = ref(''), image = ref<string | null>(null), name = ref(''), busy = ref(false)
let generation = 0
function path() { return `/${props.application ? 'applications' : 'previews'}/${props.target}/materials` }
async function load() {
  const current = generation
  try { const data = await api<{ items: typeof rows.value; requests: typeof requests.value }>(path()); if (current === generation) { rows.value = data.items; requests.value = data.requests } }
  catch (e) { if (current === generation) error.value = (e as Error).message }
}
async function file(event: Event) {
  const selected = (event.target as HTMLInputElement).files?.[0]
  image.value = null; name.value = ''; error.value = ''
  if (!selected) return
  if (!['image/png', 'image/jpeg'].includes(selected.type) || selected.size > 2 * 1024 * 1024) { error.value = '请选择不超过 2 MB 的 PNG、JPEG 图片'; (event.target as HTMLInputElement).value = ''; return }
  const current = generation
  const reader = new FileReader()
  reader.onload = () => { if (current === generation) { image.value = String(reader.result).split(',')[1] ?? null; name.value = selected.name } }
  reader.readAsDataURL(selected)
}
async function save() {
  busy.value = true; error.value = ''
  try { await post(path(), { note: note.value, image: image.value, name: name.value || '图片' }); note.value = ''; image.value = null; name.value = ''; await load(); emit('updated') }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
watch(() => props.target, () => { generation++; rows.value = []; requests.value = []; note.value = ''; image.value = null; name.value = ''; error.value = ''; void load() }, { immediate: true })
watch(() => props.refreshKey, () => { void load() })
</script>
<template>
  <section class="material-panel"><h3>申请材料</h3>
    <p v-for="request in requests" :key="request.id" :class="request.state === 'pending' ? 'error-text' : 'muted'">{{ request.state === 'pending' ? '请补充' : '已补充' }}：{{ request.note }}</p>
    <div v-for="row in rows" :key="row.id" class="material-row"><p>{{ row.note }}</p><a v-if="row.mime" :href="`/api/v1/materials/${row.id}/image`" target="_blank" rel="noopener">查看图片：{{ row.name }}</a></div>
    <p v-if="!rows.length" class="muted small">{{ editable ? '可填写问题说明或提供商品图片。' : '暂无材料记录。' }}</p>
    <form v-if="editable && identity.actor?.role === 'user'" @submit.prevent="save"><el-input v-model="note" type="textarea" :rows="2" maxlength="2000" placeholder="描述问题或补充说明" aria-label="材料说明" /><label class="file-label">添加图片（PNG / JPEG，最大 2 MB）<input :key="target + rows.length" type="file" accept="image/png,image/jpeg" aria-label="选择材料图片" @change="file" /></label><span v-if="name" class="small">{{ name }}</span><el-button :disabled="!note.trim() && !image" :loading="busy" @click="save">保存材料</el-button></form>
    <p v-if="error" role="alert" class="error-text">{{ error }} <el-button text @click="load">刷新材料</el-button></p>
  </section>
</template>
