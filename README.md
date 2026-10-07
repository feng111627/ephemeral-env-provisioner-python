# Ephemeral Env Provisioner

自助式临时环境申请与生命周期管理服务。用户可自助申请、查询、重试和销毁临时环境；
申请时还可以指定**租约时长**，到期后系统自动回收环境、释放额度，无需人工记得销毁。
在多用户并发、额度受限、分配失败重试、续租与到期回收相撞的场景下，结果可预期、可解释。

## 运行

```bash
uv sync
uv run python main.py        # 启动 HTTP 服务（自带后台到期回收线程，1s 轮询）
uv run pytest                # 运行全部单测（日志实时展示业务覆盖）
```

## 租约规则

- 申请时传 `ttl_seconds`（正数，单位秒，支持毫秒级小数）即为环境设置租约；
  到期后回收器把环境收进终态 `EXPIRED` 并**释放其占用的额度**，别人即可重新申请。
- **不指定 `ttl_seconds` 时环境永久保留，与旧用法完全一致**：没有到期时间、不会被回收；
  旧的请求体、接口与状态机路径保持兼容。
- **续租**：租约未到期且环境 `READY` 时可调用续租接口，把到期时间向后顺延；
  - 可多次续租；顺延从"当前到期时间"起算（不会因为临近到期而吃亏）；
  - 续租只改到期时间，**不会重复占用额度**；
  - 续租时长必须为正数，否则 `INVALID_PARAM`（400）。
- 续租的拒绝原因彼此可区分：

| 场景 | 错误码 | HTTP |
|---|---|---|
| 环境已到期回收（EXPIRED，含临界时刻到期优先） | `LEASE_EXPIRED` | 409 |
| 环境已被手动销毁（DESTROYED） | `ENV_DESTROYED` | 409 |
| 环境未就绪（REQUESTED/ALLOCATING，如分配失败） | `ENV_NOT_READY` | 409 |
| 环境未设置租约（永久保留，本就无需续租） | `LEASE_NOT_RENEWABLE` | 409 |
| 环境不存在 / 非本人 | `ENV_NOT_FOUND` | 404 |
| 续租时长非正数 / ttl 非正数 | `INVALID_PARAM` | 400 |

- 回收的两种触发方式：
  - **后台回收线程**：服务启动时自动运行（`EnvService(reap_interval=...)` / HTTP 应用生命周期，
    默认 1s 间隔）；
  - **确定性触发**：`POST /reap` 立即扫描回收一次，返回 `{"reaped": n}`；
    或直接调用 `service.reap_expired()`。测试通过注入假时钟（`clock=`）与手动推进时间，
    到期验证**不需要真实等待**。
- 回收可被重复触发：已回收/已销毁的环境不会被二次处理，**额度只放开一次**。

## 状态机

```
REQUESTED --申请受理
   |--开始分配--> ALLOCATING --成功--> READY --发起销毁--> DESTROYING --完成--> DESTROYED
   |                |                                              ^
   |                --失败(释放额度, 可重试)--> REQUESTED           |
   |--取消--> DESTROYING ------------------------------------------+
   |
   |--租约到期--> EXPIRED                 READY --租约到期--> EXPIRED
```

合法流转表（其余一律拒绝，返回 `ILLEGAL_TRANSITION` 并说明源/目标状态）：

| 源状态 | 允许的目标状态 |
|---|---|
| REQUESTED | ALLOCATING, DESTROYING, **EXPIRED（租约到期）** |
| ALLOCATING | READY, REQUESTED（失败回滚） |
| READY | DESTROYING, **EXPIRED（租约到期）** |
| DESTROYING | DESTROYED |
| DESTROYED | （终态） |
| EXPIRED | （终态） |

- `EXPIRED` 与 `DESTROYED` 同为终态：已回收/已销毁的环境不能再被续租、重试或销毁拉回。
- 状态迁移在锁内完成：同一环境不会被并发重复分配（`_allocate` 只在 REQUESTED 下进入 ALLOCATING），
  也不会被并发重复销毁/重复回收（重复者收到 `ILLEGAL_TRANSITION` / 续租收到 `LEASE_EXPIRED`）。
- **续租与到期回收并发时结果确定**：临界时刻（`now >= 到期时间`）到期优先，续租被拒绝为
  `LEASE_EXPIRED` 且环境进入 EXPIRED；未到期则续租成功。"续租成功继续留"与"被回收"不会同时发生。
- 每次流转与续租记录在 `env.history`，含时间戳与原因（如 `EXPIRED@...(LEASE_EXPIRED)`、
  `RENEWED@...(until=...)`），可解释、可审计。

## 额度规则

- 双维度上限：单用户额度 + 全局额度（要求 单用户上限 ≤ 全局上限，否则配置即被拒绝）。
- `acquire` 全有或全无：任一维度超限即拒绝，不产生任何计数变化；拒绝原因可区分：
  - `QUOTA_EXCEEDED_USER`（单用户超限）/ `QUOTA_EXCEEDED_GLOBAL`（全局超限）。
- 计数口径：REQUESTED / ALLOCATING / READY 中**实际持有额度**的环境占 1 份；
  分配失败回滚、手动销毁完成、租约到期回收都会 `release`，与 `acquire` 严格配对。
  环境上用 `quota_held` 标记与额度计数一一对应，确保即使停在 REQUESTED（分配失败）的环境
  再被销毁/到期回收，也只释放一次额度，不会多放。
- 续租**不触发**任何 acquire/release；并发下不超发、不少算、不多算（有混合并发单测验证
  额度计数恒等于实际持有额度的环境数）。
- 查询：`GET /quota?owner=xxx` 返回用户与全局的已用/上限。

## 幂等语义

- 申请以 `(owner, idempotency_key)` 为幂等键：重复提交返回**同一环境**（含同一租约设置），
  不重复占用额度。
- 分配失败：状态回滚到 REQUESTED、释放已占额度、`failure_reason=ALLOCATE_FAILED`；
  可调用重试接口再次分配，重试时重新占额度（仍受额度约束）；若此时租约已到期则按到期回收、
  返回 `LEASE_EXPIRED`，不允许把已过期环境重试拉回。
- 失败原因可区分：
  - `INVALID_PARAM`（400）：参数非法；
  - `QUOTA_EXCEEDED_USER/GLOBAL`（409）：额度不足；
  - `LEASE_EXPIRED / ENV_DESTROYED / ENV_NOT_READY / LEASE_NOT_RENEWABLE`（409）：续租类拒绝；
  - `ALLOCATE_FAILED`：分配器内部失败，环境回滚可重试；
  - `INTERNAL`（500）：未预期异常。
- 内部异常只写服务端日志，对外仅暴露错误码与通用文案，不泄露堆栈/内部细节
  （端到端用例验证异常中的内网地址/堆栈字样不出现在响应里）。
- 跨用户访问他人环境统一返回 `ENV_NOT_FOUND`（404），不泄露环境存在性。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/envs` | 申请环境，body: `{owner, idempotency_key, ttl_seconds?}`，幂等；不传 ttl 即永久保留 |
| GET | `/envs?owner=` | 列出我的环境 |
| GET | `/envs/{id}?owner=` | 查询单个环境（含 `ttl_seconds`、`lease_expires_at`、history） |
| POST | `/envs/{id}/retry?owner=` | 分配失败后重试 |
| DELETE | `/envs/{id}?owner=` | 销毁环境并回收额度 |
| POST | `/envs/{id}/renew` | 续租，body: `{owner, extend_seconds}`，仅未到期 READY 的租约环境可用 |
| POST | `/reap` | 立即触发一次到期回收（确定性、可重复调用），返回回收数量 |
| GET | `/quota?owner=` | 查询额度用量 |

错误响应统一为 `{"error": <错误码>, "message": <可解释文案>}`。

## 验证方法

```bash
uv run pytest            # 全部用例，日志实时输出（pyproject 中已开 log_cli）
```

测试按业务域组织，运行日志可看到每个业务域的用例与断言内容：

- `tests/test_lifecycle.py` — 生命周期流转：全链路 happy path、非法流转拒绝（可区分原因）、
  终态保护、8 线程并发销毁仅 1 个成功、跨用户访问隔离。
- `tests/test_quota_concurrency.py` — 额度与并发：单用户/全局上限、非法参数、
  幂等重复提交不重复占额度、销毁回收额度、20 线程并发申请不超发不少算、
  同用户 10 线程并发不超单用户上限。
- `tests/test_retry_and_failure.py` — 失败与重试：失败回滚+释放额度+重试成功、
  持续失败无额度泄漏、非失败状态禁止重试、重试仍受额度约束、
  **分配失败后再销毁不会重复释放额度**、内部异常不泄露、并发分配只执行一次。
- `tests/test_lease.py` — 租约：TTL 申请与字段、无 TTL 永久兼容、非法 TTL、
  到期确定性回收+释放额度、回收幂等且额度只放一次、EXPIRED 终态保护、
  续租延后且不多占额度/可多次续租、续租参数校验、
  已回收/已销毁/无租约/未就绪续租原因可区分、临界时刻到期优先、
  8 续租 × 8 回收并发结果唯一、到期前并发续租全部生效、8 销毁 × 8 回收只有一方拿终态、
  申请/续租/回收/销毁混合高并发下额度计数与持有环境数严格相等。
  到期均通过注入的假时钟 + `reap_expired()` 确定性触发，无真实等待。
- `tests/test_api_lease_e2e.py` — HTTP 端到端（TestClient，回收间隔 10ms）：
  带 TTL 申请与查询、续租与后台自动回收释放额度、参数非法返回 400、
  已回收/已销毁/无租约/不存在四种续租拒绝的 error 与状态码可区分、
  内部异常统一 500 INTERNAL 且不泄露细节。

## 代码结构

```
app/errors.py   可区分错误码与 Rejection（含租约续租类错误码）
app/models.py   状态机（含 EXPIRED 终态与到期流转）、租约字段与续租方法
app/quota.py    线程安全的双维度额度管理
app/service.py  生命周期服务：幂等申请(可带 TTL) / 分配 / 重试 / 销毁 / 续租 /
                到期回收(含后台线程) / 查询
main.py         FastAPI HTTP 层：错误码 -> HTTP 状态映射、/renew 与 /reap 接口
tests/          按业务域划分的单测与端到端测试
```
