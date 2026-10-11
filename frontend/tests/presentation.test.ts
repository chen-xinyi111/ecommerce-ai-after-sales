import assert from 'node:assert/strict'
import test from 'node:test'
import { actorName, productName, businessCopy, assistantCopy, serviceError } from '../src/presentation.ts'
import { workspaceQuery } from '../src/navigation.ts'

test('known fixture names are neutral; unknown names remain verbatim', () => {
  assert.equal(actorName('模拟用户001'), '用户001')
  assert.equal(actorName('模拟客服001'), '客服001')
  assert.equal(actorName('模拟实验室'), '模拟实验室')
  assert.equal(actorName('模拟用户001', 'USR-new-account'), '模拟用户001')
  assert.equal(actorName('模拟用户999'), '模拟用户999')
  assert.equal(serviceError('百炼拒绝了本次模型请求，请检查平台额度后重新发送。'), '咨询服务暂时繁忙，请稍后重试或转人工客服。')
  assert.equal(productName('3.3 模拟验收商品', 'SKU-001'), '商品 SKU-001')
  assert.equal(productName('模拟商品032', 'SKU-032'), '商品032')
  assert.equal(productName('模拟飞行器套装', 'SKU-010'), '模拟飞行器套装')
  assert.equal(productName('模拟商品999', 'SKU-999'), '模拟商品999')
  assert.equal(productName('模拟商品001', 'SUPPLIER-SKU-001'), '模拟商品001')
  assert.equal(businessCopy('模拟包裹已签收'), '包裹已签收')
  assert.equal(businessCopy('模拟refund政策：期限7天；具体资格待1.3规则校验'), '退款政策：售后期限 7 天。')
})

test('presentation does not mutate confirmation snapshots or remove business constraints', () => {
  const snapshot = Object.freeze({ id: 'preview-id', version: 3, content_sha256: 'original-hash', summary: '符合模拟政策，可以生成待确认方案。', warnings: Object.freeze(['所有订单与政策均为模拟用途；业务日期固定为 2026-10-09。', '维修预览仅判断基础资格，不代表已完成故障诊断。']) })
  const original = JSON.stringify(snapshot)
  assert.equal(businessCopy(snapshot.summary), '符合售后条件，请核对方案后提交申请。')
  assert.equal(businessCopy(snapshot.warnings[0]!), '')
  assert.equal(businessCopy(snapshot.warnings[1]!), snapshot.warnings[1])
  assert.equal(JSON.stringify(snapshot), original)
  assert.equal(businessCopy('当前申请不符合模拟政策：超过期限'), '当前申请不符合售后条件：超过期限')
  assert.equal(businessCopy('模拟退款已入账：100.00 元；数据库事务已提交'), '退款已入账：100.00 元')
  assert.equal(businessCopy('模拟维修已受理，等待检查；未声称修复完成'), '维修已受理，等待检查')
  assert.equal(businessCopy('LangGraph Interrupt 已持久化，等待客服审批'), '等待客服审批')
})

test('historical server templates are cleaned while arbitrary message text is preserved', () => {
  const message = '模拟订单 ORD-200001，状态 delivered，实付 100 元。\nORD-200001-I1 · 模拟商品032 · 1 件\n预览完成后等待后续用户确认与客服审批功能开放。'
  assert.equal(assistantCopy(message), '订单 ORD-200001，状态 delivered，实付 100 元。\nORD-200001-I1 · 商品032 · 1 件\n请核对方案并提交申请，客服审核后办理。')
  assert.equal(assistantCopy('客户反馈：模拟商品032有问题'), '客户反馈：模拟商品032有问题')
  assert.equal(assistantCopy('客户反馈\n\n第二段'), '客户反馈\n\n第二段')
})

test('removed pages and staff-only pages return to orders with stale parameters cleared', () => {
  for (const tab of ['cases', 'guide', 'roadmap', 'evaluation', 'invalid']) {
    assert.deepEqual(workspaceQuery({ tab, thread: 'old', application: 'old' }, true), { tab: 'orders' })
  }
  for (const tab of ['inventory', 'operations', 'support', 'policies']) {
    assert.deepEqual(workspaceQuery({ tab }, false), { tab: 'orders' })
    assert.deepEqual(workspaceQuery({ tab }, true), { tab })
  }
  assert.deepEqual(workspaceQuery({ tab: 'chat', thread: 'active', application: 'old' }, false), { tab: 'chat', thread: 'active' })
  assert.deepEqual(workspaceQuery({ tab: 'applications', application: 'active' }, false), { tab: 'applications', application: 'active' })
  assert.deepEqual(workspaceQuery({ tab: 'orders', order: 'ORD-200001', application: 'old' }, false), { tab: 'orders', order: 'ORD-200001' })
  assert.deepEqual(workspaceQuery({ tab: 'orders', order: '../wrong' }, false), { tab: 'orders' })
})
