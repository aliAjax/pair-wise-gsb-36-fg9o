# 跨区域水资源使用权分配与转让

一个仅使用 Python 标准库实现的水权账户、计量、转让审批、质押担保登记和干旱情景服务。SQLite 保存账户额度、取水记录、季节规则、上下游影响规则、质押与处置单和完整审计日志。

## 运行

```bash
python app.py --init
python app.py --port 8007
```

打开 <http://127.0.0.1:8007>。`--init` 会创建北区水库和河口灌区两个示例账户，并添加一条 7 月季节上限和一条最小留存规则。数据库默认是 `water_rights.db`，可用 `--db` 或 `WATER_DB` 修改。

## API

请求头 `X-User` 和 `X-Role` 用来模拟身份。角色包括 `editor`、`reviewer`、`meter`、`viewer`。

- `POST /api/accounts`：建立账户（额度、优先级、有效期）。
- `POST /api/rules/season`：设置某地区某月份的用水比例上限。
- `POST /api/rules/impact`：设置上下游转让的最小留存比例。
- `POST /api/transfers`：发起转让；待审批金额立即预占，避免同一额度被重复转卖。
- `POST /api/transfers/{id}/approve|reject`：审核；发起人不能审批自己的记录。
- `POST /api/usage`：按计量事件登记实际取水，同一账户同一事件编号只会入账一次。
- `POST /api/pledges`：账户持有人登记质押（金额、到期日、受益人）；质押额与待审转让预占、取水占用互斥。
- `POST /api/pledges/{id}/release`：履约后解除质押；冻结中的质押必须走处置审批。
- `POST /api/disposals`：质押到期未履约时，受益人申请处置（请求人须为受益人）；处置单先冻结质押额，每个质押仅一张处置单。
- `POST /api/disposals/{id}/decide`：审核人审批处置单；`decision=transfer` 生成等额已批准转让并划转额度，`decision=release` 释放质押。同一处置单重复提交同一结果时返回原审批结果，不重复扣减；写入失败整体回滚，可按原处置单安全重试。
- `GET /api/pledges`、`GET /api/disposals`：质押与待处置明细，可按 `account_id` 过滤。
- `GET /api/accounts/{id}/available`：查看可用额度，以及待审预占、质押中、待处置冻结明细。
- `GET /api/drought/simulate?supply=1000&reduction=0.3`：按高优先级先行分配，同级账户按剩余额度比例分配。
- `GET /api/audit`：完整操作审计。

余额计算、质押登记和审批使用 `BEGIN IMMEDIATE`，把余额判断与写入放在同一事务中；因此两个窗口同时提交（质押与转让相撞）时只有一方成功。可用额度 = 许可额度 − 取水占用 − 待审转让预占 − 质押占用（含待处置冻结）。最小留存比例按转出账户的当前许可额度计算。

## 结构

- `errors.py`：各层共用的领域错误。
- `collateral.py`：质押担保的判定逻辑（纯函数）：额度互斥计算、质押/处置状态机、审批结果解析，不触碰数据库。
- `app.py`：SQLite 存储与 HTTP 接口。
- `static/index.html`：页面，展示可用、质押中和待处置明细。

旧版数据库打开时自动迁移：新增 `pledges`/`disposals` 表，`transfers` 增加 `disposal_id` 列（部分唯一索引保证同一处置单只生成一笔转让）。已有账户和转让的 `disposal_id` 为 NULL，按未质押处理。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖转让审批与实际计量、重复计量事件、季节/最小留存规则、预占导致余额不足、发起人自审冲突，以及质押互斥、质押与转让并发只有一个成功、处置冻结/审批、处置单失败回滚与幂等重试、旧库迁移兼容。

