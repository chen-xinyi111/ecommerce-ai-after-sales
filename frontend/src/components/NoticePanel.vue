<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, post } from '../api/client'
const router = useRouter()
const opened = ref(false), unread = ref(0), error = ref('')
const rows = ref<{ id: number; title: string; application_id: string; read_at: string | null }[]>([])
let timer: ReturnType<typeof setInterval> | undefined
async function load() { try { const data = await api<{ items: typeof rows.value; unread: number }>('/notifications'); rows.value = data.items; unread.value = data.unread; error.value = '' } catch (e) { error.value = (e as Error).message } }
async function choose(row: typeof rows.value[number]) { try { await post(`/notifications/${row.id}/read`, {}); opened.value = false; await router.replace({ query: { tab: 'applications', application: row.application_id } }); await load() } catch (e) { error.value = (e as Error).message } }
onMounted(() => { void load(); timer = setInterval(() => { if (!document.hidden) void load() }, 15000) })
onUnmounted(() => clearInterval(timer))
</script>
<template><el-button text @click="opened = true; load()">通知{{ unread ? `（${unread}）` : '' }}</el-button><el-dialog v-model="opened" title="服务通知" width="min(520px, 94vw)"><p v-if="error" class="error-text">{{ error }}</p><button v-for="row in rows" :key="row.id" class="thread-card" @click="choose(row)"><strong>{{ row.title }}</strong><span>{{ row.read_at ? '已读' : '未读' }}</span></button><el-empty v-if="!rows.length && !error" description="暂无通知" /></el-dialog></template>
