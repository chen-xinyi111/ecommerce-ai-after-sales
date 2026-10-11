<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { serviceError } from '../presentation'
import { useIdentity } from '../stores/identity'
import { post, labels, type Application, type Preview } from '../api/client'
const props = defineProps<{ preview: Preview }>()
const identity = useIdentity()
const router = useRouter()
const accepted = ref(false)
const busy = ref(false)
const error = ref('')
const application = ref<Application | null>(null)
let requestId = crypto.randomUUID()
watch(() => props.preview.id, () => { accepted.value = false; application.value = null; error.value = ''; requestId = crypto.randomUUID() })
async function confirm() {
  busy.value = true; error.value = ''
  try {
    application.value = await post<Application>(`/previews/${props.preview.id}/confirm`, { preview_id: props.preview.id, preview_version: props.preview.version, content_sha256: props.preview.content_sha256, request_id: requestId })
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
function progress() { void router.replace({ query: { tab: 'applications', application: application.value?.id } }) }
</script>
<template>
  <div v-if="preview.status === 'eligible'" class="confirmation-box">
    <template v-if="identity.actor?.role === 'user'">
      <template v-if="!application"><el-checkbox v-model="accepted">我已核对商品、数量及方案内容，确认申请此方案</el-checkbox><p class="muted small">确认后由客服审批；此步骤不会退款、发货或扣减库存。修改方案后需重新确认。</p><el-button type="primary" :disabled="!accepted" :loading="busy" @click="confirm">确认方案并提交审批</el-button></template>
      <template v-else><p>申请状态：{{ labels[application.status] }}</p><el-button type="primary" plain @click="progress">查看申请进度</el-button></template>
      <p v-if="error" class="error-text" role="alert">{{ serviceError(error) }}</p>
    </template>
    <p v-else class="muted small">方案须由订单所属用户登录后本人确认。客服预览不能代替用户确认。</p>
  </div>
</template>
