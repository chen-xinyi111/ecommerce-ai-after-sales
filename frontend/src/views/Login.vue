<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { post } from '../api/client'
import { useIdentity } from '../stores/identity'
import { Lock, User } from '@element-plus/icons-vue'
const username = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')
const router = useRouter()
const identity = useIdentity()
const route = useRoute()
const mode = ref(String(route.query.mode || 'login'))
const success = ref('')
const token = ref(typeof route.query.token === 'string' ? route.query.token : '')
const title = computed(() => ({ login: '登录服务台', register: '注册用户账号', forgot: '找回密码', reset: '设置新密码', verify: '验证邮箱' })[mode.value] || '登录服务台')
function switchMode(next: string) { mode.value = next; error.value = ''; success.value = ''; password.value = '' }
onMounted(async () => {
  if (token.value) {
    if (identity.actor) { try { await identity.logout() } catch { identity.clear() } }
    await router.replace({ path: '/login', query: { mode: mode.value } })
  }
  if (mode.value === 'verify') {
    busy.value = true
    try { success.value = (await post<{ message: string }>('/auth/verify-email', { token: token.value })).message; mode.value = 'login' }
    catch (e) { error.value = (e as Error).message; mode.value = 'forgot' }
    finally { busy.value = false; token.value = '' }
  }
})
async function login() {
  if (mode.value === 'forgot') return
  if (mode.value !== 'login') {
    if (mode.value === 'register' && !/^[A-Za-z][A-Za-z0-9_-]{2,31}$/.test(username.value)) { error.value = '账号需为 3–32 位，以字母开头，可包含数字、下划线或连字符'; return }
    if (['register', 'reset'].includes(mode.value) && password.value.length < 10) { error.value = '密码至少需要 10 位'; return }
    busy.value = true; error.value = ''; success.value = ''
    try {
      const path = mode.value === 'register' ? '/auth/register' : '/auth/reset-password'
      const body = mode.value === 'register' ? { username: username.value.trim(), password: password.value } : { token: token.value, password: password.value }
      success.value = (await post<{ message: string }>(path, body)).message
      mode.value = 'login'; token.value = ''; password.value = ''
    } catch (e) { error.value = (e as Error).message }
    finally { busy.value = false }
    return
  }
  if (!username.value.trim() || !password.value) { error.value = '请输入账号和密码'; return }
  busy.value = true; error.value = ''
  try { await identity.login(username.value.trim(), password.value); await router.push('/workspace') }
  catch (e) { error.value = e instanceof Error ? e.message : '登录失败，请重试' }
  finally { busy.value = false }
}
</script>
<template>
  <main class="login-page">
    <section class="login-story">
      <div class="brand"><span class="brand-mark">售</span><span>智能售后<span class="brand-sub">电商服务台</span></span></div>
      <div class="story-body"><div class="eyebrow light">每一份信任，都有回应</div><h1>让售后咨询<br>简单、清晰、安心。</h1><p>从一笔订单开始，了解商品、支付与物流。<br>让每一次沟通，都有可追溯的记录。</p><div class="story-lines"><span>01　核验订单</span><span>02　沟通问题</span><span>03　保留记录</span></div></div>
      <p class="story-foot">电商智能售后服务台</p>
    </section>
    <section class="login-form-area"><form class="login-card" @submit.prevent="login">
      <h2>{{ title }}</h2><p class="muted">{{ mode === 'register' ? '设置账号和密码，注册后即可登录' : mode === 'forgot' ? '请联系本机服务台管理员重置密码。登录后也可在账号设置中修改密码。' : '查看订单，办理售后' }}</p>
      <template v-if="['login', 'register'].includes(mode)"><label for="username">账号</label><el-input id="username" v-model="username" :prefix-icon="User" size="large" placeholder="请输入账号" autocomplete="username" required /></template>
      <template v-if="['login', 'register', 'reset'].includes(mode)"><label for="password">{{ mode === 'reset' ? '新密码' : '密码' }}</label><el-input id="password" v-model="password" :prefix-icon="Lock" size="large" type="password" show-password :placeholder="mode === 'login' ? '请输入密码' : '至少 10 位'" :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" required /></template>
      <p v-if="error" class="error-text" role="alert">{{ error }}</p>
      <p v-if="success" class="account-success" role="status">{{ success }}</p>
      <el-button v-if="mode !== 'forgot'" class="login-submit" type="primary" size="large" native-type="submit" :loading="busy">{{ mode === 'login' ? '登录' : mode === 'register' ? '注册账号' : '重置密码' }}</el-button>
      <div class="login-links"><template v-if="mode === 'login'"><el-button text @click="switchMode('register')">注册账号</el-button><el-button text @click="switchMode('forgot')">忘记密码</el-button></template><el-button v-else text @click="switchMode('login')">返回登录</el-button></div>
    </form></section>
  </main>
</template>
