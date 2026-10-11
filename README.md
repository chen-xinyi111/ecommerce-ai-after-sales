# 青禾 · AI 电商售后服务台

面向用户与客服的电商售后应用，支持退款、换货、补发、维修申请，以及 AI 咨询和人工协作。

**核心流程：** 查询订单 → 提交售后申请 → 核对并确认方案 → 客服审批 → 查看办理结果。

## 核心功能

- **用户售后：** 查询订单，填写原因与材料，确认方案并跟踪申请进度。
- **AI 与人工协作：** 关联订单开展多轮咨询、生成方案，并支持转人工接管。
- **客服办理：** 审核方案和材料，批准、拒绝或要求补充材料；异步办理并核对结果。

## 页面展示

点击展开查看核心页面。

<details>
<summary><strong>订单与售后申请</strong></summary>

查询订单与商品，从订单发起售后申请。

![我的订单](docs/screenshots/user-orders.jpg)

![填写售后申请](docs/screenshots/user-application-info.jpg)

</details>

<details>
<summary><strong>AI 咨询与人工协作</strong></summary>

关联订单咨询售后方案，需要时转交客服处理。

![AI 售后咨询](docs/screenshots/user-chat.jpg)

![人工咨询工作台](docs/screenshots/staff-support.jpg)

</details>

<details>
<summary><strong>客服审批与办理结果</strong></summary>

客服审核已确认的方案，用户查看申请进度和办理结果。

![客服售后审批](docs/screenshots/staff-approvals.jpg)

![我的售后与办理结果](docs/screenshots/user-applications.jpg)

</details>

Vue 3 / TypeScript · FastAPI · LangGraph · PostgreSQL

订单、支付、退款、库存和物流使用模拟业务系统，未接入真实企业订单或支付。截图展示已有模拟数据。

[本机运行说明](docs/RUNNING.md) · [开源来源与许可](NOTICE.md)
