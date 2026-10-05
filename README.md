# Ephemeral Env Provisioner

自助式临时环境申请与生命周期管理服务。用户可自助申请、查询、重试和销毁临时环境；
在多用户并发、额度受限、分配失败重试的场景下，结果可预期、可解释。

## 运行

```bash
uv sync
uv run python main.py        # 启动 HTTP 服务
uv run pytest                # 运行全部单测（日志实时展示业务覆盖）
```

## 状态机

```
REQUESTED --申请受理
   |--开始分配--> ALLOCATING --成功--> READY --发起销毁--> DESTROYING --完成--> DESTROYED
   |                |
   |                --失败(释放额度, 可重试)--> REQUESTED
   --取消--> DESTROYING
```

合法流转表（其余一律拒绝，返回 `ILLEGAL_TRANSITION` 并说明源/目标状态）：

| 源状态 | 允许的目标状态 |
|---|---|
| REQUESTED | ALLOCATING, DESTROYING |
| ALLOCATING | READY, REQUESTED（失败回滚） |
| READY | DESTROYING |
| DESTROYING | DESTROYED |
| DESTROYED | （终态） |

- 状态迁移在锁内完成：同一环境不会被并发重复分配（`_allocate` 只在 REQUESTED 下进入 ALLOCATING），也不会被并发重复销毁（重复销毁收到 `ILLEGAL_TRANSITION`）。
- 每次流转记录在 `env.history`，含时间戳与失败原因，可解释、可审计。

## 额度规则

- 双维度上限：单用户额度 + 全局额度（要求 单用户上限 ≤ 全局上限，否则配置即被拒绝）。
- `acquire` 全有或全无：任一维度超限即拒绝，不产生任何计数变化；拒绝原因可区分：
  - `QUOTA_EXCEEDED_USER`（单用户超限）/ `QUOTA_EXCEEDED_GLOBAL`（全局超限）。
- 计数口径：REQUESTED / ALLOCATING / READY 占用额度；分配失败回滚与销毁完成都会 `release`，
  与 `acquire` 严格配对，并发下不超发、不少算、不多算（有并发单测验证）。
- 查询：`GET /quota?owner=xxx` 返回用户与全局的已用/上限。

## 幂等语义

- 申请以 `(owner, idempotency_key)` 为幂等键：重复提交返回**同一环境**，不重复占用额度。
- 分配失败：状态回滚到 REQUESTED、释放已占额度、`failure_reason=ALLOCATE_FAILED`；
  可调用重试接口再次分配，重试时重新占额度（仍受额度约束）。
- 失败原因可区分：
  - `INVALID_PARAM`（400）：参数非法；
  - `QUOTA_EXCEEDED_USER/GLOBAL`（409）：额度不足；
  - `ALLOCATE_FAILED`：分配器内部失败，环境回滚可重试；
  - `INTERNAL`（500）：未预期异常。
- 内部异常只写服务端日志，对外仅暴露错误码与通用文案，不泄露堆栈/内部细节。
- 跨用户访问他人环境统一返回 `ENV_NOT_FOUND`（404），不泄露环境存在性。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/envs` | 申请环境，body: `{owner, idempotency_key}`，幂等 |
| GET | `/envs?owner=` | 列出我的环境 |
| GET | `/envs/{id}?owner=` | 查询单个环境（含 history） |
| POST | `/envs/{id}/retry?owner=` | 分配失败后重试 |
| DELETE | `/envs/{id}?owner=` | 销毁环境并回收额度 |
| GET | `/quota?owner=` | 查询额度用量 |

错误响应统一为 `{"error": <错误码>, "message": <可解释文案>}`。

## 验证方法

```bash
uv run pytest            # 18 个用例，日志实时输出（pyproject 中已开 log_cli）
```

测试按业务域组织，运行日志可看到每个业务域的用例与断言内容：

- `tests/test_lifecycle.py` — 生命周期流转：全链路 happy path、非法流转拒绝（可区分原因）、
  终态保护、8 线程并发销毁仅 1 个成功、跨用户访问隔离。
- `tests/test_quota_concurrency.py` — 额度与并发：单用户/全局上限、非法参数、
  幂等重复提交不重复占额度、销毁回收额度、20 线程并发申请不超发不少算、
  同用户 10 线程并发不超单用户上限。
- `tests/test_retry_and_failure.py` — 失败与重试：失败回滚+释放额度+重试成功、
  持续失败无额度泄漏、非失败状态禁止重试、重试仍受额度约束、
  内部异常不泄露给调用方、并发分配同一环境分配器只执行一次。

## 代码结构

```
app/errors.py   可区分错误码与 Rejection
app/models.py   状态机（合法流转表）与 Environment 模型
app/quota.py    线程安全的双维度额度管理
app/service.py  生命周期服务：幂等申请 / 分配 / 重试 / 销毁 / 查询
main.py         FastAPI HTTP 层：错误码 -> HTTP 状态映射
tests/          按业务域划分的单测
```
