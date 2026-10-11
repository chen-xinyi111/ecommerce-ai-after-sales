import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, post, ApiError, type Actor } from '../api/client'

export const useIdentity = defineStore('identity', () => {
  const actor = ref<Actor | null>(null)
  const initialized = ref(false)
  const connectionError = ref('')
  async function restore() {
    if (initialized.value) return
    try { actor.value = await api<Actor>('/auth/me') }
    catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        connectionError.value = error instanceof Error ? error.message : '连接暂时不可用'
        return
      }
    }
    connectionError.value = ''
    initialized.value = true
  }
  async function login(username: string, password: string) {
    actor.value = await post<Actor>('/auth/login', { username, password })
    initialized.value = true
  }
  async function logout() {
    await post<void>('/auth/logout', {})
    actor.value = null
  }
  function clear() { actor.value = null; initialized.value = true }
  return { actor, connectionError, restore, login, logout, clear }
})
