# CO-IoA Clean

通信机会约束下的任务组建与动态调整。保留 IoA 共用的 Agent/Server 服务、连接管理与 WebSocket 收发，业务流程由独立 P—C—A—E、任务状态和消息状态管理。

本目录可以独立复制、安装、运行，无需原 `IoA_202610_repo`。包含机制源码、13 个场景、前端、实验 API、真实模型 API 适配、Docker 配置、批实验、测试和文档。能力执行仍采用仿真实现，不声称真实设备控制。

## 1. 安装环境

Python 3.11+。Windows 本机验证使用独立 CPython 3.11.15；原 Anaconda 派生环境曾出现 OR-Tools 原生求解异常。

```powershell
uv python install 3.11
uv venv --python 3.11 --managed-python .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.lock.txt
uv pip install --python .venv\Scripts\python.exe --no-deps -e .
```

Linux/macOS 的解释器路径改为 `.venv/bin/python`。也可使用标准 venv 和 pip 安装 `-e ".[test]"`。源文件采用 UTF-8。

本工作区已验证的环境另位于目录上一级 `.venv-co-ioa-clean`，可直接使用 `..\.venv-co-ioa-clean\Scripts\python.exe` 替代下面的 `.venv\Scripts\python.exe`。

## 2. 基础实验与完整场景批运行

```powershell
.venv\Scripts\python.exe -m co_ioa.run --scenario delayed_contact --backend ioa --planner rule --output runs/demo
.venv\Scripts\python.exe -m co_ioa.experiments --backend ioa --planner rule --repetitions 3 --seed 0 --output runs/full_rule
.venv\Scripts\python.exe -m pytest -q
```

`ioa` 启动本机真实 HTTP/WebSocket 服务，`offline` 保留相同业务机制的直接传输替身。规则场景是确定性的，改变 seed 不会凭空产生随机场景差异；重复主要用于一致性检查。LLM 输出可能随机，seed 不保证远端模型可复现。

支持 `--scenario configs/examples/custom_scenario.json` 读取自定义完整场景。配置格式见 `co_ioa/scenarios/*.json` 和 `docs/REQUIREMENTS.md`。批实验脚本在 `scripts/`。

## 3. 真实模型 API

```powershell
Copy-Item .env.example .env
# 编辑 .env，填写实际服务、模型名和密钥。
.venv\Scripts\python.exe -m co_ioa.run --env-file .env --scenario delayed_contact --backend ioa --planner llm --output runs/llm_demo
.venv\Scripts\python.exe -m co_ioa.experiments --env-file .env --planner llm --repetitions 3 --output runs/full_llm
```

适配的是提供 `POST {CO_IOA_API_BASE}/chat/completions` 的兼容服务，要求支持 `messages`、JSON 输出格式和 `choices[0].message.content`。模型名由环境配置，不硬编码提供商。密钥只放环境或本地 `.env`，不会复制进镜像或结果。无认证本地模型必须显式设置 `CO_IOA_ALLOW_UNAUTHENTICATED=true`，示例见 `configs/examples/local_model.env.example`。

LLM 只生成需求。能力模板时长、硬约束、调整权限和依赖校验始终执行；成员选择由 CP-SAT 完成。无配置会明确失败，不能回退到规则规划并冒称真实模型调用。`result.json.model_calls` 保存模型名、服务地址、调用状态、用量与现实耗时。

13 个固定场景的 `requirements` 是首次规划的任务规格：LLM 必须保留任务 ID、必需性、时间、依赖和参数。能力别名按相同含义比较，模型擅自增加“分析结果”步骤或漏掉任务会收到纠正反馈；系统不会用预设答案替换模型输出。结果上报由执行器完成，不需要另造分析任务。收到目标更新时，模型必须完整表达更新事件的任务规格；匹配失败时只能修订允许调整的字段，已接收、执行中、已完成及已发出的任务受到保护。

模型请求遇到超时、429 或可重试的 5xx 默认最多再试 2 次。`CO_IOA_MODEL_TIMEOUT` 是初始读取超时（默认 120 秒），超时后逐次加倍至 `CO_IOA_MODEL_MAX_TIMEOUT`（默认初始值的 2 倍）；`CO_IOA_MODEL_RETRY_BACKOFF` 控制退避，最长 30 秒。外层本机 HTTP 等待预算覆盖这些重试和需求纠正。等待外部模型使用现实时间，仿真时钟保持在当前事件时刻。

`result.json.planning_attempts` 保存各次模型提案、接受/拒绝原因、Agent 与仿真时刻；`model_calls` 分别保存每次 HTTP 尝试和耗时。模型服务在重试后仍不可用，或提案在纠正后仍违反规格，结果标为 `outcome=error`，并设置 `evaluation_valid=false`，完成率指标为 `null`。这是实验异常，不能计为通信机制导致的失败；终止时执行必要清理。`failed` 继续表示正常运行下的业务失败。批运行逐局写入 `summary.json`，终端同时打印原因。

`--seed` 控制本地实验随机数；当前请求没有给外部模型传 seed。即使 `CO_IOA_TEMPERATURE=0`，也不能承诺远端模型逐字复现。固定任务规格的校验用于保持实验条件一致，不保证所有模型调用成功，也不改变本身不可行的通信场景。

## 4. 前端与实验管理 API

前端保留原 React 项目的列表、消息组件及资产，改为适配新机制。支持选择场景、规划器和传输后端，运行实验、查看任务结果、拖动时间轴，以及回放实际通信窗口和事件。

```powershell
cd im_server/frontend
npm ci --ignore-scripts --no-audit --no-fund
npm test -- --watchAll=false --runInBand
npm run build
cd ../..
.venv\Scripts\python.exe -m co_ioa.api --env-file .env --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000`。如不使用模型，可省略 `--env-file .env`。开发时 `npm start` 的 3000 端口会代理 `/api` 到 8000。

主要接口：

| 接口 | 用途 |
|---|---|
| GET `/api/health` | 健康状态 |
| GET `/api/scenarios` | 内置场景列表 |
| POST `/api/runs` | 提交后台实验任务，立即返回 run_id |
| GET `/api/runs` | 任务列表及运行状态 |
| GET `/api/runs/{id}` | 单局状态 |
| GET `/api/runs/{id}/result` | 结果与传输审计 |
| GET `/api/runs/{id}/events` | JSONL 事件 |
| GET `/api/runs/{id}/scenario` | 输入快照 |
| GET `/api/runs/{id}/manifest` | 源码哈希、依赖及输入指纹 |

POST 示例在 `configs/examples/api_rule.json`、`api_llm.json`。配置了 `CO_IOA_CONTROL_KEY` 时，POST 需 Bearer 控制密钥，前端也有对应输入；模型密钥不输入前端。

API 每次只运行一个实验，其他任务排队，避免单实例仿真的全局时钟和随机状态并发混用。结果持久化到 runs；服务重启会将未完成任务标记 interrupted，不伪造续跑。前端查看运行状态，详细事件在终局后回放。

## 5. Docker

在本目录执行，先准备 `.env`：

```powershell
docker compose --env-file .env -f docker/compose.yaml up --build
```

前端 `http://127.0.0.1:8080`，API `http://127.0.0.1:8000/docs`。结果挂载到本机 `runs/`。不需要 Docker socket、Milvus 或旧 IoA 镜像。

批实验：

```powershell
docker compose --env-file .env -f docker/compose.yaml --profile batch run --rm experiment
docker compose --env-file .env -f docker/compose.yaml --profile batch run --rm experiment python -m co_ioa.experiments --planner llm --repetitions 3 --output /app/runs/full_llm
```

Docker 中访问宿主机模型时，将 API base 中的 `127.0.0.1` 改为 `host.docker.internal`。本机没有 Docker 可执行程序，镜像构建和容器运行尚未实测；见验证报告。

## 6. 目录与产物

```text
co_ioa/                  独立机制、实验驱动、API 与模型适配
im_client/               共用 Agent 服务、WebSocket 客户端和机制适配
im_server/               共用 Server 连接管理、转发及 React 前端
common/ioa_transport.py  不含业务状态的传输信封
configs/examples/        请求、模型和自定义场景示例
docker/                  后端、前端镜像及 Compose
scripts/                 完整实验批运行
tests/                   机制、真实链路、API 和模型传输验证
docs/                    需求、迁移清单、验证与文件说明
```

每局输出 scenario.json、events.jsonl、result.json、manifest.json；批运行另有 summary.json。API 任务另有 job.json。输入、输出、模型调用和源码指纹分开记录。

迁移边界见 [docs/MIGRATION.md](docs/MIGRATION.md)，文件职责见 [docs/FILES.md](docs/FILES.md)，本机验证见 [docs/VALIDATION.md](docs/VALIDATION.md)。

## 7. 实验范围

当前为单进程多 Agent 服务线程、本机真实网络连接、统一仿真时钟。Docker 封装部署不等于各 Agent 已实现独立容器或多机容错。真实模型规划接口已经接通，但未使用任何用户付费凭据做外部模型验收；模拟模型服务测试只验证调用链。无多跳、带宽竞争、主动通信窗口优化、真实设备控制及统筹者故障选举。
