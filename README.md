# 临时环境申请与生命周期管理服务

自助式临时环境服务：用户可自助申请、查询、销毁临时环境。
服务保证多用户并发申请、额度受限、分配失败重试时结果可预期、可解释。

## 运行

```bash
uv sync
uv run python main.py        # 启动 HTTP 服务
uv run pytest                # 运行全部单测（日志含业务覆盖信息）
```

## 状态机

环境按以下阶段单向流转，非法流转一律被拒绝并返回 `illegal_transition`：

```
requested --申请受理--> provisioning --分配成功--> ready --销毁--> destroying --> destroyed
                           |
                           +--分配失败--> requested（回滚，释放已占资源，可重试）
```

| 当前状态 | 允许的下一状态 |
|---|---|
| requested | provisioning |
| provisioning | ready（成功）/ requested（失败回滚） |
| ready | destroying |
| destroying | destroyed |
| destroyed | （终态） |

- 同一环境的并发销毁：只有一个请求触发真实流转，其余幂等返回同一终态 `destroyed`，销毁流转只记录一次。
- 所有状态变更在单把锁内完成，不会出现并发重复分配或重复销毁。

## 额度规则

- 两级额度：每用户上限（`per_user_quota`）与整体上限（`global_quota`），构造时校验均为正整数且个人额度不大于整体额度。
- 计入额度的状态：`provisioning` / `ready` / `destroying`（即真实占用资源的环境）；`requested`（未分配或失败已回滚）与 `destroyed` 不计入。
- 申请与额度检查、计数在同一临界区内完成，并发申请不会超发，计数不多算不少算（测试断言 `global_active == 实际成功数`）。
- 拒绝原因可区分：
  - `quota_exceeded_user`：超出个人额度
  - `quota_exceeded_global`：超出整体额度
  - `invalid_argument`：非法参数（空 owner/request_id、非法额度配置等）
- 被拒绝的申请不占用任何额度；销毁后额度立即释放，可再次申请。

## 幂等与失败语义

- **幂等申请**：以 `(owner, request_id)` 为幂等键。相同请求重复提交返回同一环境（同一 `env_id`），不重复占用额度。
- **失败回滚**：分配中途失败时，环境记录与已占额度一并释放，状态不残留；同一 `request_id` 可直接重试（`apply` 或 `POST /envs/retry`），重试成功后再重复提交仍返回同一环境。
- **失败原因可区分**：参数错误（`invalid_argument`）、额度不足（`quota_exceeded_user/global`）、内部异常（`internal`）分别返回不同原因码与 HTTP 状态码。
- **内部异常屏蔽**：内部异常细节只写入服务端日志，调用方仅收到 `internal` + 通用提示，不泄露堆栈或后端细节。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/envs` | 申请环境，body: `{owner, request_id}`，幂等 |
| POST | `/envs/retry` | 分配失败后重试，body 同上 |
| GET | `/envs/{env_id}` | 查询环境（含流转历史） |
| POST | `/envs/{env_id}/destroy` | 销毁环境，body: `{operator}`，仅属主 |
| GET | `/quota/{owner}` | 查询额度使用情况 |

错误响应统一为 `{"reason": ..., "message": ...}`，reason → HTTP 状态码：
`invalid_argument`→400，`forbidden`→403，`not_found`→404，
`quota_exceeded_*`/`illegal_transition`→409，`internal`→500。

## 验证方法

```bash
uv run pytest -v
```

测试按业务分组，运行日志会打印每个用例所属业务与结果，并在结束时汇总覆盖的业务：

| 测试文件 | 覆盖业务 |
|---|---|
| `tests/test_lifecycle.py` | 生命周期状态机流转：正常流转、非法流转拒绝、幂等销毁、并发销毁只生效一次 |
| `tests/test_quota_concurrency.py` | 并发额度控制：个人/整体上限、并发不超发、计数精确、拒绝不占额度、销毁释放额度 |
| `tests/test_retry_idempotency.py` | 失败重试与幂等：失败释放资源、可重试、重试幂等、原因可区分、内部异常不泄露 |
| `tests/test_api.py` | HTTP 层：端到端流程、拒绝原因与状态码映射、内部异常屏蔽 |

并发用例通过多线程 + barrier 同时发起申请/销毁，断言成功数不超过额度、
失败原因均为额度类、额度计数与实际一致。
