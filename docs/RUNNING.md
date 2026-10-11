# 运行青禾售后服务台

## Docker 本机演示

安装 Docker Desktop 后，在仓库根目录打开 PowerShell，执行：

```powershell
./scripts/local/Docker.ps1 -Action Start
```

首次运行会生成被 Git 忽略的 `docker/.env.commerce`，随机生成数据库密码与内部服务密钥，构建前后端并启动 PostgreSQL、Redis、Kafka 和应用。
打开 http://127.0.0.1:18080/app/ 。演示用户 `user001`，演示客服 `staff001`，公共模拟密码 `Demo-1101!`。这些是合成演示身份，不能用于真实部署。
默认使用确定性离线模型和容器内模拟业务账本，不需要 API Key。维修结果表示已受理，不表示维修完成。

```powershell
./scripts/local/Docker.ps1 -Action Status
./scripts/local/Docker.ps1 -Action Stop
```

停止不会删除数据库卷。不要在保存业务数据的环境执行 `docker compose down -v`。

## Windows 开发与独立模拟接口

在仓库根目录依次执行：

```powershell
./scripts/local/Setup.ps1
./scripts/local/Database.ps1 -Action Init
./scripts/local/Database.ps1 -Action Migrate
./.venv/Scripts/python.exe -m commerce.seed
./scripts/local/Frontend.ps1 -Action Setup
./scripts/local/Workstation.ps1 -Offline -Transport postgres
```

本机入口 http://127.0.0.1:8080/app/ ，独立 HTTP 模拟业务服务在 127.0.0.1:8081。使用 PostgreSQL 队列即可体验业务，无需本机 Kafka。
Setup 会下载锁定的 uv、Python 和 PostgreSQL，并为新的开发目录生成 `.env`；Frontend Setup 安装锁定的 Node.js、pnpm 与依赖。

需要真实模型时，在本机环境变量或 `.env` 设置自己的 `DASHSCOPE_APIKEY`，再运行不带 `-Offline` 的启动命令。真实模型会产生供应商调用费用；本项目不附带任何个人密钥。
新注册账号初始没有订单，订单须由模拟系统关联；注册和管理员重置密码不依赖真实邮箱。

## 开发检查

```powershell
cd frontend
pnpm test
pnpm run build
```

API 类型：`pnpm run types`。新增后端接口后先从根目录运行 `python scripts/export_business_api.py`。
数据库相关 pytest 使用独立测试库；运行前须准备 PostgreSQL 配置，不要把自己的使用库当成测试库。

## 业务范围

订单、退款、库存、物流均使用模拟系统。替换企业接口需要实现适配层并验证业务事实同步、结果查询和回调。
Docker 和本机脚本只绑定本机端口。此运行说明用于本机演示，公开 GitHub 源码不等于部署在线网站。
