// Presentation only: never change API objects or signed confirmation snapshots.
export function actorName(value: string, id?: string): string {
  const match = value.match(/^模拟(用户|客服)(\d{3})$/)
  if (!match || Number(match[2]) < 1 || Number(match[2]) > (match[1] === '用户' ? 200 : 2)) return value
  if (id && id !== `${match[1] === '用户' ? 'USR' : 'STAFF'}-${match[2]}`) return value
  return `${match[1]}${match[2]}`
}

export function productName(value: string, sku: string): string {
  if (value === '3.3 模拟验收商品') return `商品 ${sku}`
  const match = value.match(/^模拟商品(\d{3})$/)
  if (!match || Number(match[1]) < 1 || Number(match[1]) > 60 || (sku && sku !== `SKU-${match[1]}`)) return value
  return `商品${match[1]}`
}

const copy: Record<string, string> = {
  '百炼拒绝了本次模型请求，请检查平台额度后重新发送。': '咨询服务暂时繁忙，请稍后重试或转人工客服。',
  '模型暂时不可用，已接收内容保留，请稍后重新发送。': '咨询服务暂时不可用，已收到的内容保留，请稍后重试或转人工客服。',
  '本次查询达到处理步数上限，请简化问题后重新发送。': '本次未能完成查询，请补充明确问题或转人工客服。',
  '服务的模型模式已切换，请重新发送这条消息。': '咨询服务已更新，请重新发送这条消息。',
  '模拟包裹已出库': '包裹已出库',
  '模拟物流丢失': '物流丢失',
  '模拟包裹已签收': '包裹已签收',
  '补发需要数据库中的物流丢失记录。': '补发需要物流记录确认包裹丢失。',
  '使用业务日期内有效的最新模拟政策版本。': '依据当前有效的售后政策核验。',
  '符合模拟政策，可以生成待确认方案。': '符合售后条件，请核对方案后提交申请。',
  '所有订单与政策均为模拟用途；业务日期固定为 2026-10-09。': '',
  '这是条件快照，未创建工单、退款、发货或扣减库存；后续执行必须重新核验。': '办理前须由用户确认、客服审批，并再次核对售后条件。',
  '仅为模拟方案预览，未执行任何售后操作。': '方案尚未执行，请核对后提交申请。',
  '预览完成后等待后续用户确认与客服审批功能开放。': '请核对方案并提交申请，客服审核后办理。',
  'LangGraph Interrupt 已持久化，等待客服审批': '等待客服审批',
  '等待客服独立审批与恢复': '等待客服审批',
  '客服决定已保存，等待独立恢复': '客服意见已保存，待继续办理',
  '已通过独立接口请求恢复审批流程': '客服已继续办理',
  '审批流程暂时中断，客服可通过独立恢复接口重试': '审批暂时中断，请客服核对后重试',
  '服务重启，恢复已持久化的审批任务': '正在继续处理审批',
  '客服已提交模拟业务执行': '客服已开始办理',
  '双重确认完成；客服可发起模拟业务执行': '方案已审核通过，等待客服办理',
  '模拟工单已创建，等待执行': '工单已创建，等待办理',
  'MCP 创建工单已事务提交': '工单已创建',
  '执行完成，已核对持久业务结果': '办理已完成，结果已核对',
  '工具响应缺少持久动作结果，需要人工核对': '办理结果待人工核对',
  '三次尝试后中断；已确认无提交结果并释放库存，客服核对后可重试': '办理中断，尚未发货的库存已释放，请客服核对后重试',
  '连接或步骤中断；已查询持久结果，稍后恢复未完成步骤': '处理暂时中断，正在恢复进度',
  '服务重启，先查询已提交步骤再恢复执行': '正在核对结果并继续办理',
  '已存在提交结果但关联记录需对账；不重复执行，不回滚已退款或发货': '已有办理记录，请人工核对退款或发货结果',
  '流程达到步骤或工具调用上限，任务已停止，请联系人工客服。': '暂时无法完成处理，请联系人工客服。',
}

export function businessCopy(value: string): string {
  if (Object.hasOwn(copy, value)) return copy[value]!
  const legacyPolicy = value.match(/^模拟(refund|exchange|reship|repair)政策：期限(\d+)天；具体资格待1\.3规则校验$/)
  if (legacyPolicy) {
    const kinds: Record<string, string> = { refund: '退款', exchange: '换货', reship: '补发', repair: '维修' }
    return `${kinds[legacyPolicy[1]!]}政策：售后期限 ${legacyPolicy[2]} 天。`
  }
  if (/^(模拟退款已入账：\d+\.\d{2} 元|模拟换货已发货|模拟补发已发货|模拟维修已受理，等待检查；未声称修复完成)(；数据库事务已提交)?$/.test(value)) {
    const result = value.replace(/；数据库事务已提交$/, '')
    return result === '模拟维修已受理，等待检查；未声称修复完成' ? '维修已受理，等待检查' : result.replace(/^模拟/, '')
  }
  if (value.startsWith('当前申请不符合模拟政策：')) return value.replace('当前申请不符合模拟政策：', '当前申请不符合售后条件：').replace('补发需要数据库中的物流丢失记录。', copy['补发需要数据库中的物流丢失记录。']!).replace(/库存快照可用 (\d+|None) 件，需要 (\d+) 件；本次不预留库存。/g, '可用库存 $1 件，本次申请 $2 件。')
  if (/^库存快照可用 (\d+|None) 件，需要 (\d+) 件；本次不预留库存。$/.test(value)) return value.replace(/^库存快照可用 (\d+|None) 件，需要 (\d+) 件；本次不预留库存。$/, '可用库存 $1 件，本次申请 $2 件。')
  if (/^模拟(退款|换货|补发|维修)政策(?: v\d+)?：/.test(value)) return value.replace(/^模拟/, '')
  return value
}

// Only recognized, server-authored lines. User messages never pass through here.
export function assistantCopy(value: string): string {
  return value.split('\n').map(line => {
    if (/^模拟订单 ORD-\d{6}，状态 /.test(line)) return line.replace(/^模拟订单 /, '订单 ')
    if (/^离线测试回复：已核验模拟订单 ORD-\d{6}，状态 /.test(line)) {
      return line.replace('离线测试回复：已核验模拟订单 ', '已核验订单 ').replace('支持方案预览；审批、退款和发货尚未开放。', '可查看售后方案，核对后提交客服审核。')
    }
    const item = line.match(/^(ORD-\d{6}-I\d+) · (模拟商品\d{3}|3\.3 模拟验收商品) · (\d+) 件$/)
    if (item) return `${item[1]} · ${item[2] === '3.3 模拟验收商品' ? '商品' : productName(item[2]!, '')} · ${item[3]} 件`
    return businessCopy(line)
  }).join('\n')
}

export function serviceError(value: string): string {
  const converted = businessCopy(value)
  if (converted !== value) return converted
  if (value === '方案编号、版本或摘要不匹配，请重新确认当前方案') return '方案已更新，请重新核对并确认。'
  if (/MCP|traceback|Traceback|SQLSTATE/.test(value)) return '暂时无法完成处理，请重试或联系人工客服。'
  return value
}
