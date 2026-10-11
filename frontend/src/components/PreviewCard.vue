<script setup lang="ts">
import { money, labels, type Preview } from '../api/client'
import { businessCopy } from '../presentation'
import ConfirmPreview from './ConfirmPreview.vue'
defineProps<{ preview: Preview; hideConfirmation?: boolean }>()
const statusLabels = { eligible: '符合基础资格', needs_info: '需要补充信息', ineligible: '当前不符合条件' }
const reasons: Record<string, string> = { quality: '质量问题', wrong_item: '错发商品', not_received: '未收到货', changed_mind: '不想要了' }
</script>
<template>
  <section class="preview-card" aria-label="售后方案预览">
    <div class="preview-top"><div><span class="eyebrow">售后方案</span><h3>{{ preview.request.kind ? labels[preview.request.kind] : '售后' }}方案</h3></div><el-tag :type="preview.status === 'eligible' ? 'success' : preview.status === 'needs_info' ? 'warning' : 'danger'" effect="plain">{{ statusLabels[preview.status] }}</el-tag></div>
    <p class="preview-summary">{{ businessCopy(preview.summary) }}</p>
    <div class="preview-amount" v-if="preview.refund_cents != null"><span>预览退款金额</span><strong>{{ money(preview.refund_cents) }}</strong></div>
    <dl class="preview-facts"><div><dt>订单 / 商品</dt><dd>{{ preview.order_id }}<br>{{ preview.request.order_item_id || '待选择' }}</dd></div><div><dt>申请信息</dt><dd>{{ preview.request.quantity ?? '待补充' }} 件 · {{ preview.request.reason ? reasons[preview.request.reason] : '原因待补充' }} · {{ preview.request.received == null ? '收货状态待补充' : preview.request.received ? '已收到' : '未收到' }}</dd></div><div><dt>数量依据</dt><dd>购买 {{ preview.facts.purchased_quantity ?? '—' }} / 历史售后 {{ preview.facts.historical_after_sales_quantity ?? '—' }} / 剩余 {{ preview.facts.remaining_quantity ?? '—' }}</dd></div><div><dt>可退金额与库存</dt><dd>剩余可退 {{ money(preview.facts.remaining_refund_cents) }} · 可用库存 {{ preview.facts.available_stock ?? '—' }} 件</dd></div></dl>
    <div v-if="preview.questions.length" class="preview-questions"><strong>请补充以下信息</strong><p v-for="question in preview.questions" :key="question">{{ question }}</p></div>
    <details class="preview-evidence"><summary>查看政策与校验依据</summary><template v-if="preview.policy"><p>有效期 {{ preview.policy.valid_from }} 至 {{ preview.policy.valid_until }}</p><p>购买后 {{ preview.facts.age_days }} 天 / 允许 {{ preview.policy.window_days }} 天（含边界）；以购买日期计算。</p><p>{{ businessCopy(preview.policy.explanation) }}</p></template><p v-else>政策将在选择售后类型后加载。</p><p v-for="check in preview.checks" :key="check.code" :class="{ 'error-text': !check.passed }">{{ check.passed ? '✓' : '×' }} {{ businessCopy(check.explanation) }}</p></details>
    <div v-if="preview.warnings.some(warning => businessCopy(warning))" class="preview-warning"><template v-for="warning in preview.warnings" :key="warning"><p v-if="businessCopy(warning)">{{ businessCopy(warning) }}</p></template></div><div class="preview-id"><span>预览 v{{ preview.version }} · {{ new Date(preview.created_at).toLocaleString('zh-CN') }}</span></div>
    <ConfirmPreview v-if="!hideConfirmation" :preview="preview" />
  </section>
</template>
