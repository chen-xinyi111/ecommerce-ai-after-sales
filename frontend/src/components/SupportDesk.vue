<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, post } from '../api/client'
const router = useRouter(), error = ref(''), busy = ref(false)
const rows = ref<{ thread_id: string; order_id: string; state: string; staff_id: string | null }[]>([])
let timer: ReturnType<typeof setInterval> | undefined
async function load() { try { rows.value = (await api<{ items: typeof rows.value }>('/support')).items; error.value = '' } catch (e) { error.value = (e as Error).message } }
async function choose(row: typeof rows.value[number]) { busy.value = true; try { if (row.state === 'queued') await post(`/threads/${row.thread_id}/support/claim`, {}); await router.replace({ query: { tab: 'chat', thread: row.thread_id } }) } catch (e) { error.value = (e as Error).message; await load() } finally { busy.value = false } }
onMounted(() => { void load(); timer = setInterval(() => { if (!document.hidden) void load() }, 5000) }); onUnmounted(() => clearInterval(timer))
</script>
<template><section class="panel support-desk"><div class="panel-heading"><h3>人工咨询</h3><el-button text @click="load">刷新</el-button></div><p v-if="error" class="error-text">{{ error }}</p><div v-for="row in rows" :key="row.thread_id" class="support-row"><div><strong>{{ row.order_id }}</strong><p>{{ row.state === 'queued' ? '等待接单' : '客服处理中' }}</p></div><el-button :loading="busy" @click="choose(row)">{{ row.state === 'queued' ? '接单并查看' : '查看咨询' }}</el-button></div><el-empty v-if="!rows.length && !error" description="暂无待处理的人工咨询" /></section></template>
