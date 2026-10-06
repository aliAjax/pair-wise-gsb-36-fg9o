# 跨区域水资源使用权分配、转让与质押担保

一个仅使用 Python 标准库实现的水权账户、计量、转让审批、质押担保登记和干旱情景服务。SQLite 保存账户额度、取水记录、转让、质押、处置单、季节/影响规则和完整审计日志。

## 运行

```bash
python app.py --init
python app.py --port 8007
```

打开 <http://127.0.0.1:8007>。`--init` 会创建北区水库和河口灌区两个示例账户，并添加一条 7 月季节上限和一条最小留存规则。数据库默认是 `water_rights.db`，可用 `--db` 或 `WATER_DB` 修改。旧版本数据库会自动迁移：已有账户和转让在没有质押记录时一律按**未质押**兼容。

## 分层结构

数据、判定、存储和页面分开：

- `waterright/domain.py`：纯判定层（无 SQL）——额度占用口径、质押生命周期、到期处置规则、干旱分配算法。
- `waterright/store.py`：存储层——SQLite schema、迁移和行级读写。
- `waterright/service.py`：用例编排——`BEGIN IMMEDIATE` 事务把判定与写入原子化。
- `app.py`：HTTP 路由；`static/index.html`：页面，只展示接口数据。

## 额度占用口径

同一单位额度只能处于「已取水 / 待审转让预占 / 质押中 / 处置冻结」之一：

```
可用 = 许可额度 - 已取水 - 待审转让预占 - 质押中 - 处置冻结
```

质押提交后额度进入「质押中」；受益人申请处置后该笔变为「处置冻结」（不再计入质押中，但仍不可用）；处置审批后按结果生成转让（额度转给受益人账户）或释放（回到持有人可用）。

所有占用类写操作（发起转让、批准转让、登记取水、提交质押、申请处置）都在同一事务中先 `BEGIN IMMEDIATE` 再判定并写入，因此两个窗口同时提交、或质押与转让撞上时**只有一方成功**。

## 质押与处置流程

1. `POST /api/pledges`：账户持有人提交质押，填写 `account_id`、`amount`、`maturity_date`（到期日）、`beneficiary`（受益人）。额度不足（已被取水/待审转让/质押占用）时拒绝。
2. 到期未履约时，受益人 `POST /api/disposals` 申请处置（带 `pledge_id`）。处置单先把质押额**冻结**，质押状态 `pledged → frozen`；未到期、重复申请、非受益人都会被拒绝。
3. 审核人 `POST /api/disposals/{id}/approve`（body 可带 `transfer_to_account_id` 转让给受益人账户）或 `/reject`（释放）：
   - 批准转让：在同一事务生成一条 `approved` 转让单（带唯一的 `disposal_id`），扣减持有人额度并增加受益人账户额度，质押置为 `transferred`。
   - 释放：质押置为 `released`，额度回到可用。
   - 转让生成与扣减在同一事务，`transfers.disposal_id` 有唯一索引；写入失败后按**同一处置单**重试是幂等的，返回既有单据、不重复扣减。对同一处置单给出相反决定会被拒绝。
   - 处置申请人不能审批自己的处置单。

## API

请求头 `X-User` 和 `X-Role` 模拟身份，角色包括 `editor`、`reviewer`、`meter`、`viewer`（受益人可凭 `X-User` 匹配质押受益人身份申请处置）。

- `POST /api/accounts`：建立账户（额度、优先级、有效期）。
- `POST /api/rules/season` / `/api/rules/impact`：季节用水比例上限 / 上下游最小留存比例。
- `POST /api/transfers`、`POST /api/transfers/{id}/approve|reject`：转让（待审批金额立即预占）。
- `POST /api/usage`：登记实际取水，同一账户同一事件编号只入账一次。
- `POST /api/pledges`：提交质押。
- `POST /api/disposals`：受益人申请处置（冻结）。
- `POST /api/disposals/{id}/approve|reject`：处置审批（转让或释放，支持幂等重试）。
- `GET /api/accounts`：账户列表，含可用、质押、冻结、待审转让各栏。
- `GET /api/accounts/{id}`：账户明细，含每笔质押及其处置单。
- `GET /api/accounts/{id}/available`：额度占用明细（同时保留旧字段 `reserved_outgoing`）。
- `GET /api/pledges` / `GET /api/disposals`：质押与处置单列表。
- `GET /api/drought/simulate?supply=1000&reduction=0.3`：高优先级先分配，同级按剩余额度比例分配。
- `GET /api/audit`：完整操作审计（含 `pledge.created`、`disposal.filed`、`disposal.transferred/released`）。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：转让审批与计量、重复计量事件、季节/最小留存、预占冲突、自审冲突；质押与待审转让/取水互斥、质押-质押与质押-转让的并发单胜、到期冻结-转让/释放、非受益人与未到期拒绝、写入失败后按同一处置单重试不重复扣减、旧库迁移与未质押兼容。
