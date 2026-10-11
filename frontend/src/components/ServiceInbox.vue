<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
const router = useRouter(), error = ref('')
const data = ref<{ approval: number; exceptions: number; support: number; materials: { id: string; order_id: string }[] }>({ approval: 0, exceptions: 0, support: 0, materials: [] })
async function load() { try { data.value = await api('/service-inbox'); error.value = '' } catch (e) { error.value = (e as Error).message } }
onMounted(load)
</script>
<template><section class="panel service-inbox"><div class="panel-heading"><h3>服务待办</h3><el-button text @click="load">刷新</el-button></div><div class="inbox-actions"><button @click="router.replace({ query: { tab: 'applications' } })">待审批 <strong>{{ data.approval }}</strong></button><button @click="router.replace({ query: { tab: 'support' } })">人工咨询 <strong>{{ data.support }}</strong></button><button @click="router.replace({ query: { tab: 'operations' } })">处理异常 <strong>{{ data.exceptions }}</strong></button></div><details v-if="data.materials.length"><summary>待补充材料（{{ data.materials.length }}）</summary><button v-for="row in data.materials" :key="row.id" class="thread-card" @click="router.replace({ query: { tab: 'applications', application: row.id } })">{{ row.order_id }} · 查看补充请求</button></details><p v-if="error" class="error-text">{{ error }}</p></section></template>
