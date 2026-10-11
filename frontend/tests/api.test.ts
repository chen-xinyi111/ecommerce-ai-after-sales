import assert from 'node:assert/strict'
import test from 'node:test'
import { api, ApiError } from '../src/api/client.ts'

test('connection failures offer a customer-facing retry message', async () => {
  const saved = globalThis.fetch
  globalThis.fetch = async () => { throw new TypeError('network unavailable') }
  try {
    await assert.rejects(api('/orders'), (e: unknown) => e instanceof ApiError && e.status === 0 && e.message === '暂时无法连接服务。请稍后重试。')
  } finally { globalThis.fetch = saved }
})

test('expired sessions trigger login recovery and permissions remain distinct', async () => {
  const savedFetch = globalThis.fetch
  const savedWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const events = new EventTarget()
  let expired = false
  events.addEventListener('session-expired', () => { expired = true })
  Object.defineProperty(globalThis, 'window', {value: events, configurable: true})
  try {
    globalThis.fetch = async () => new Response('{}', {status: 401})
    await assert.rejects(api('/orders'), (e: unknown) => e instanceof ApiError && e.status === 401 && e.message === '登录已失效，请重新登录。')
    assert.equal(expired, true)
    globalThis.fetch = async () => new Response('{}', {status: 403})
    await assert.rejects(api('/operations'), (e: unknown) => e instanceof ApiError && e.message === '当前账号无权执行此操作。')
  } finally {
    globalThis.fetch = savedFetch
    if (savedWindow) Object.defineProperty(globalThis, 'window', savedWindow)
    else Reflect.deleteProperty(globalThis, 'window')
  }
})
