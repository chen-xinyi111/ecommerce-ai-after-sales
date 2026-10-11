# 青禾 · AI 电商售后服务台

面向用户与客服的电商售后应用，支持订单查询、AI 咨询、退款、换货、补发、维修申请，以及人工协作和办理进度跟踪。

**主要流程：** 选择订单 → 选择售后类型 → 填写原因和材料 → 核对并确认方案 → 客服审批 → 查看办理结果。

订单、支付、退款、库存和物流使用模拟业务系统，未接入真实企业订单或支付。截图来自本机应用及已有模拟订单，部分页面展示空状态。

![用户订单页面](docs/screenshots/user-orders.jpg)

## 功能与页面

| 使用角色 | 功能入口 |
| --- | --- |
| 用户 | 我的订单、我的售后、帮助与咨询、站内通知、账号设置 |
| 客服 | 全部订单、售后审批、咨询记录、人工咨询、库存查询、任务与异常、售后政策 |
| 管理员 | 客服账号创建、账号密码重置 |

以下每项均提供实际页面截图，点击展开查看。

<details>
<summary><strong>账号登录与注册</strong></summary>

账号和密码注册，注册成功即可登录；不依赖真实邮箱。忘记密码由服务台管理员核实并重置。

![登录页面](docs/screenshots/login.jpg)

![注册页面](docs/screenshots/register.jpg)

![找回密码说明](docs/screenshots/password-help.jpg)

</details>

<details>
<summary><strong>我的订单</strong></summary>

按账号展示订单并支持订单号查询，查看商品、实付金额、累计退款和库存；从订单发起售后或咨询。

![用户订单与商品详情](docs/screenshots/user-orders.jpg)

</details>

<details>
<summary><strong>物流与支付记录</strong></summary>

查看模拟物流轨迹、运单、支付记录及历史售后结果。

![订单物流轨迹](docs/screenshots/order-logistics.jpg)

![支付与售后历史](docs/screenshots/order-history.jpg)

</details>

<details>
<summary><strong>分步售后申请</strong></summary>

支持退款、换货、补发、维修。依次选择类型、填写商品与数量、说明原因及收货情况，再核对材料和方案。

![选择售后类型](docs/screenshots/user-application-type.jpg)

![填写申请信息](docs/screenshots/user-application-info.jpg)

</details>

<details>
<summary><strong>材料补充与方案确认</strong></summary>

支持文字说明和 PNG / JPEG 图片材料；展示资格、数量依据、退款估算和政策依据。用户核对确认后提交客服审批，确认本身不执行退款或发货。

![材料上传与方案确认](docs/screenshots/user-materials-preview.jpg)

</details>

<details>
<summary><strong>我的售后与办理结果</strong></summary>

查询申请状态、客服意见及退款、发货或维修受理结果。业务办理前需要用户确认和客服审批，执行时再次检查条件。

![售后进度与已完成退款](docs/screenshots/user-applications.jpg)

</details>

<details>
<summary><strong>帮助与 AI 咨询</strong></summary>

咨询关联订单并保留历史，支持多轮沟通与方案预览；用户可转人工，接管期间助手暂停自动回复。

![帮助与使用流程](docs/screenshots/user-help.jpg)

![用户售后咨询](docs/screenshots/user-chat.jpg)

</details>

<details>
<summary><strong>站内通知</strong></summary>

展示审批、材料补充、办理结果和异常通知，支持标记已读和进入关联申请；下图展示当前空通知状态。

![站内通知](docs/screenshots/user-notices.jpg)

</details>

<details>
<summary><strong>用户账号设置</strong></summary>

登录后修改密码；修改或管理员重置密码后撤销该账号已有会话。

![用户账号设置](docs/screenshots/user-account.jpg)

</details>

<details>
<summary><strong>客服订单与集中待办</strong></summary>

客服查询全部模拟订单，集中查看待审批、人工咨询和处理异常数量，并进入对应模块。

![客服订单与服务待办](docs/screenshots/staff-orders.jpg)

</details>

<details>
<summary><strong>售后审批</strong></summary>

查看用户确认的方案和材料，填写审批意见，批准、拒绝或要求补充材料。审批绑定方案版本，影响方案的业务信息变化后需重新确认。

![客服方案审批](docs/screenshots/staff-approvals.jpg)

</details>

<details>
<summary><strong>客服咨询记录</strong></summary>

按订单查看已有咨询和助手生成的方案，保留沟通背景。

![客服咨询记录](docs/screenshots/staff-chat.jpg)

</details>

<details>
<summary><strong>人工咨询</strong></summary>

客服查看待接单和处理中咨询，接单、回复并结束服务；人工沟通记录保留，结束后可继续 AI 咨询。

![人工咨询工作台](docs/screenshots/staff-support.jpg)

</details>

<details>
<summary><strong>库存查询</strong></summary>

按 SKU 搜索现有、预留和可用库存，提供只读库存查询。

![库存查询](docs/screenshots/staff-inventory.jpg)

</details>

<details>
<summary><strong>任务与异常</strong></summary>

查看异步办理、咨询和审批状态；支持受控重试与人工核对，结果不确定时先核对账本，避免重复退款或发货。

![任务与异常处理](docs/screenshots/staff-operations.jpg)

</details>

<details>
<summary><strong>售后政策</strong></summary>

按类型查看期限、适用条件、生效范围和历史版本；发布产生新版本，保留旧政策与申请快照。

![售后政策管理](docs/screenshots/staff-policies.jpg)

</details>

<details>
<summary><strong>管理员账号设置</strong></summary>

管理员创建客服账号，核实用户请求后重置账号密码；普通用户和客服没有管理员权限。

![管理员账号设置](docs/screenshots/staff-account.jpg)

</details>

## 业务能力

- 订单、退款、库存、物流提供可替换适配接口，支持数据库模拟实现与独立 HTTP 模拟服务。
- 方案由确定性业务规则校验；用户确认、客服审批和执行复核共同约束业务写入。
- 异步处理保存任务状态，结合幂等、库存预留与结果核对处理失败和响应丢失。
- 用户数据按账号隔离；客服和管理员操作受角色权限控制，材料访问需要会话鉴权。
- Vue 3 与 TypeScript 提供工作台界面，FastAPI 提供业务接口，LangGraph 组织售后助手流程，PostgreSQL 保存业务数据；异步任务支持 PostgreSQL 队列或 Kafka，Redis 提供协作基础设施。

[本机运行说明](docs/RUNNING.md) · [开源来源与许可](NOTICE.md)
