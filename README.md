# Ephemeral Env Provisioner

自助式临时环境申请与生命周期管理服务。用户可自助申请、查询、重试、续租和销毁临时环境；
申请时可指定租约时长，到期系统自动回收并释放额度。在多用户并发、额度受限、分配失败重试、
租约到期回收与续租撞车的场景下，结果可预期、可解释。

## 运行

```bash
uv sync
uv run python main.py        # 启动 HTTP 服务
uv run pytest                # 运行全部单测（日志实时展示业务覆盖）
```

## 租约规则

- 申请时可选传 `ttl_seconds`（正整数秒）：
  - **指定时长**：环境就绪后带租约，到期时间 = 申请受理时刻 + ttl；到达到期时间，
    系统自动执行 `READY -> EXPIRED` 回收并释放额度，无需任何人手动销毁。
  - **不指定 / 传 null（默认）**：环境永不过期，行为与旧版本完全一致，老用法不受影响。
- **续租**：租约未到期、环境处于 READY 时可调续租，从“续租当前时刻”起重新计时，
  可反复续租；续租只顺延到期时间，**不重复占用额度**。
- 下列续租请求被明确拒绝，原因互不相同：
  - `LEASE_EXPIRED`（409）：租约已到期并回收；
  - `ENV_DESTROYED`（409）：环境已被手动销毁；
  - `LEASE_NOT_ACTIVE`（409）：环境未就绪（如分配失败停留在 REQUESTED），或本就没有租约；
  - `INVALID_PARAM`（400）：续租时长不是正整数。
- `EXPIRED` 与 `DESTROYED` 同为**终态**：已回收/已销毁的环境不能被续租、重试或销毁拉回。
- 到期回收的触发方式（同一套回收逻辑，均为幂等）：
  - 后台定时任务周期性调用服务的 `reap_expired()`；
  - 管理接口 `POST /envs/reap-expired` 确定性触发；
  - 查询（`get` / `list`）时惰性回收恰好到期的环境，保证看到真实状态。
- **回收只发生一次**：状态迁移与额度释放都在服务同一把锁内、以 `env.quota_held` 为唯一
  配对依据完成。重复触发回收、回收与销毁/续租并发，额度都只会释放一次，不会多放或错乱。

## 状态机

```
REQUESTED --申请受理
   |--开始分配--> ALLOCATING --成功--> READY --发起销毁--> DESTROYING --完成--> DESTROYED
   |                |                    |
   |                |                    --租约到期(自动回收, 释放额度)--> EXPIRED（终态）
   |                --失败(释放额度, 可重试)--> REQUESTED
   --取消--> DESTROYING
```

合法流转表（其余一律拒绝，返回 `ILLEGAL_TRANSITION` 并说明源/目标状态）：

| 源状态 | 允许的目标状态 |
|---|---|
| REQUESTED | ALLOCATING, DESTROYING |
| ALLOCATING | READY, REQUESTED（失败回滚） |
| READY | DESTROYING, **EXPIRED（租约到期自动回收）** |
| DESTROYING | DESTROYED |
| DESTROYED | （终态） |
| EXPIRED | （终态） |

- 状态迁移在锁内完成：同一环境不会被并发重复分配（`_allocate` 只在 REQUESTED 下进入 ALLOCATING），
  也不会被并发重复销毁（重复销毁收到 `ILLEGAL_TRANSITION`）。
- 续租与到期回收的“判到期 + 改时间 / 迁移终态 + 释放额度”都在同一把锁内原子完成：
  二者不可能同时发生——要么续租成功环境继续保留，要么被回收，结果由谁先拿到锁唯一决定。
- 每次流转与每次续租都记录在 `env.history`（含时间戳，续租记录为 `RENEWED@...(until=...)`），
  可解释、可审计。

## 额度规则

- 双维度上限：单用户额度 + 全局额度（要求 单用户上限 ≤ 全局上限，否则配置即被拒绝）。
- `acquire` 全有或全无：任一维度超限即拒绝，不产生任何计数变化；拒绝原因可区分：
  - `QUOTA_EXCEEDED_USER`（单用户超限）/ `QUOTA_EXCEEDED_GLOBAL`（全局超限）。
- 计数口径：REQUESTED / ALLOCATING / READY 占用额度；分配失败回滚、手动销毁、租约到期回收
  都会 `release`，与 `acquire` 严格配对。释放以环境上的 `quota_held` 标志收口，
  并发下不超发、不少算、不多放（有并发单测验证，含续租撞回收、销毁撞回收）。
- 查询：`GET /quota?owner=xxx` 返回用户与全局的已用/上限。

## 幂等语义

- 申请以 `(owner, idempotency_key)` 为幂等键：重复提交返回**同一环境**，不重复占用额度
  （重复提交若恰好撞上该环境到期，会先完成惰性回收，再返回其终态）。
- 分配失败：状态回滚到 REQUESTED、释放已占额度、`failure_reason=ALLOCATE_FAILED`；
  可调用重试接口再次分配，重试时重新占额度（仍受额度约束）。
- 失败原因可区分：
  - `INVALID_PARAM`（400）：参数非法（含 ttl / 续租时长非法，请求体校验失败也统一为 400）；
  - `QUOTA_EXCEEDED_USER/GLOBAL`（409）：额度不足；
  - `LEASE_NOT_ACTIVE / LEASE_EXPIRED / ENV_DESTROYED`（409）：续租时机不合法，原因可区分；
  - `ALLOCATE_FAILED`：分配器内部失败，环境回滚可重试；
  - `INTERNAL`（500）：未预期异常。
- 内部异常只写服务端日志，对外仅暴露错误码与通用文案，不泄露堆栈/内部细节。
- 跨用户访问他人环境统一返回 `ENV_NOT_FOUND`（404），不泄露环境存在性。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/envs` | 申请环境，body: `{owner, idempotency_key, ttl_seconds?}`，幂等 |
| GET | `/envs?owner=` | 列出我的环境 |
| GET | `/envs/{id}?owner=` | 查询单个环境（含 ttl_seconds/expires_at/history） |
| POST | `/envs/{id}/retry?owner=` | 分配失败后重试 |
| POST | `/envs/{id}/renew?owner=` | 续租，body: `{ttl_seconds}`，返回新到期时间 |
| POST | `/envs/reap-expired` | 确定性触发到期回收，返回 `{reaped: [...], count}` |
| DELETE | `/envs/{id}?owner=` | 销毁环境并回收额度 |
| GET | `/quota?owner=` | 查询额度用量 |

错误响应统一为 `{"error": <错误码>, "message": <可解释文案>}`。

## 验证方法

```bash
uv run pytest            # 61 个用例，日志实时输出（pyproject 中已开 log_cli）
```

到期相关验证**不依赖真实等待**：`EnvService` 支持注入时钟（`clock=` 参数），
测试用可手动推进的假时钟把时间拨到到期点，再调用 `reap_expired()` / 查询 / 续租，
确定性地覆盖“刚好到期 / 未到期续租成功 / 已过期回收胜出”等临界情况。

测试按业务域组织，运行日志可看到每个业务域的用例与断言内容：

- `tests/test_lifecycle.py` — 生命周期流转：全链路 happy path、非法流转拒绝（可区分原因）、
  终态保护、8 线程并发销毁仅 1 个成功、跨用户访问隔离。
- `tests/test_quota_concurrency.py` — 额度与并发：单用户/全局上限、非法参数、
  幂等重复提交不重复占额度、销毁回收额度、20 线程并发申请不超发不少算、
  同用户 10 线程并发不超单用户上限。
- `tests/test_retry_and_failure.py` — 失败与重试：失败回滚+释放额度+重试成功、
  持续失败无额度泄漏、非失败状态禁止重试、重试仍受额度约束、
  内部异常不泄露给调用方、并发分配同一环境分配器只执行一次。
- `tests/test_lease.py` — 租约：带/不带 ttl 的申请与默认兼容、批量与惰性回收、
  回收幂等只放一次额度、续租顺延不多占额度、三类续租拒绝原因可区分、
  到期临界点续租撞回收（8+8 线程）结果唯一且额度一致、未到期续租胜 / 已过期回收胜、
  并发重复回收只生效一次、销毁撞回收单终态、终态拉不回。
- `tests/test_api_lease.py` — 租约接口端到端（FastAPI TestClient）：ttl 视图字段、
  续租/回收接口形态、非法参数统一 400 `INVALID_PARAM`（不出现 422）、
  对不该续租的环境续租三种原因可区分、内部异常只返回 500 `INTERNAL` 且不泄露细节。

## 代码结构

```
app/errors.py   可区分错误码与 Rejection（含 LEASE_* / ENV_DESTROYED）
app/models.py   状态机（合法流转表，含 READY->EXPIRED）与 Environment 租约字段
app/quota.py    线程安全的双维度额度管理
app/service.py  生命周期服务：幂等申请 / 分配 / 重试 / 续租 / 到期回收 / 销毁 / 查询，
                支持注入时钟；状态迁移与额度变更在同一把锁内配对
main.py         FastAPI HTTP 层：错误码 -> HTTP 状态映射、参数校验统一为 INVALID_PARAM
tests/          按业务域划分的单测
```
