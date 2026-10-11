<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useIdentity } from './stores/identity'
const router = useRouter()
const identity = useIdentity()
function expired() { identity.clear(); void router.replace('/login') }
function retry() { window.location.reload() }
onMounted(() => window.addEventListener('session-expired', expired))
onUnmounted(() => window.removeEventListener('session-expired', expired))
</script>
<template><main v-if="identity.connectionError" class="connection-page"><section class="panel"><h2>服务暂时无法连接</h2><p class="muted">{{ identity.connectionError }}</p><el-button type="primary" @click="retry">重新连接</el-button></section></main><router-view v-else /></template>
