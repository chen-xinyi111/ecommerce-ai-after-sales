import { createRouter, createWebHashHistory } from 'vue-router'
import Login from './views/Login.vue'
import Workspace from './views/Workspace.vue'
import { workspaceQuery } from './navigation'
import { useIdentity } from './stores/identity'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', component: Login },
    { path: '/workspace', component: Workspace },
    { path: '/:pathMatch(.*)*', redirect: '/workspace' },
  ],
})
router.beforeEach(async (to) => {
  const identity = useIdentity()
  await identity.restore()
  if (!identity.actor && to.path !== '/login') return '/login'
  if (identity.actor && to.path === '/login' && !['verify', 'reset'].includes(String(to.query.mode || ''))) return '/workspace'
  if (identity.actor && to.path === '/workspace') {
    const query = workspaceQuery(to.query, identity.actor.role === 'staff')
    if (JSON.stringify(query) !== JSON.stringify(to.query)) return { path: to.path, query, replace: true }
  }
})
