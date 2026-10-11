import type { LocationQuery } from 'vue-router'

export function workspaceQuery(query: LocationQuery, staff: boolean): LocationQuery {
  const allowed = staff ? ['orders', 'chat', 'applications', 'inventory', 'operations', 'support', 'policies'] : ['orders', 'chat', 'applications']
  const tab = typeof query.tab === 'string' && allowed.includes(query.tab) ? query.tab : 'orders'
  const result: LocationQuery = { tab }
  if (tab === 'chat' && typeof query.thread === 'string') result.thread = query.thread
  if (tab === 'applications' && typeof query.application === 'string') result.application = query.application
  if (tab === 'orders' && typeof query.order === 'string' && /^ORD-\d{6}$/.test(query.order)) result.order = query.order
  return result
}
