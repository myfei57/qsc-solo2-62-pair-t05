# 氨检探头标定与量程管理

新一批探头量程与旧探头不同，旧系统只有一个 `ammonia:calibration` 系数，
历史读数按哪个口径折算无法追溯。本子系统把“系数”升级为**带量程的多点标定
版本**，并规定了版本共存、迁移、样本不足、失校处置四条口径。代码位于
`coldstore/calib/`，HTTP 端点前缀 `/calibration`。

## 1. 标定模型

* 一个标定版本（`CalVersion`）= 探头仪器量程 `[raw_min, raw_max]` + 若干标定点
  `(raw 原始信号, reference 标准浓度 ppm)`。
* 折算曲线为按 raw 排序的**分段线性插值**（`calib/curve.py`）。旧的单一系数
  等价于过原点的两点直线，因此旧口径可以无损表达。
* 落在已标定跨度内才插值；跨度外只给外推值并置 `extrapolated/within_span=false`，
  **绝不为了出数而把外推值当作可信浓度**。
* 版本生命周期 `draft → active → retired`，只增不改；每条读数永久登记
  `version_id`，历史读数任何时候都能按当时的版本重算。

### 样本不足口径（已选边）

新建版本先落 `draft`，满足下列条件才能 `activate`（`qualify()`，阈值见
`MIN_POINTS=3`、`MIN_COVERAGE=0.80`）：

* 至少 3 个互不相同、单调不降的标定点；
* 标定点覆盖仪器量程至少 80%；
* raw 非负、reference 非负。

样本不足时**不采用“最近一段标定”**：草稿保持 `draft`，绝不用于实时折算；
读数**回退到上一 active 版本**，读数质量置 `fallback`。没有任何 active
版本时（首次部署且草稿不合格）直接降级（见第 3 节），而不是猜一个系数。

## 2. 迁移与口径一致性

迁移以监测点（point）为单位，状态机：

```
single ──migrate──▶ dual_run ──cutover(过闸)──▶ new
                        │
                        └──rollback──▶ single（保留旧版本）
```

* `dual_run`（双轨并存）：每条原始信号同时按新、旧两个版本折算。
  **旧口径始终是唯一权威值**（`authoritative=true`），新口径仅作 `shadow`
  影子值登记，用于比对。因此迁移期间对外只有一个浓度口径，不会出现两套数。
  新老探头原始量程不同的，影子值取备用探头自己的原始信号（`shadow_raw`）。
* 偏差闸门 `gate`：要求至少 `min_pairs`（默认 5）对同点比对值，且每对满足
  `|新-旧| ≤ tolerance_ppm + tolerance_ratio × 旧`。不达标禁止 cutover
  （可用 `force` 强制并留痕）。
* `cutover`：激活新版本、退役旧版本、把新口径健康探头切为 primary，此后统一
  到新版本；旧版本永久保留以复算历史数据。
* `rollback`：放弃新版本，回到旧版本单口径。

### 历史数据口径

旧的单系数在首次启动时被**冻结**为不可变版本 `legacy-factor-v1`（basis
`legacy`）并绑定默认主探头（`calib/legacy.py`，幂等）。旧数据从此有了明确
的版本归属，与新版本并存直到迁移完成。

## 3. 失校处置口径（已选边）

探头被判失校的情形：标记 `out_of_calibration/invalid`，或原始信号超出自身
仪器量程。处置优先级：

1. **切备用探头优先**：存在健康、状态 `standby`、口径与当前权威口径一致、
   且其 active 版本覆盖该信号的备用探头 → 自动切为 primary，读数质量
   `switched`，并审计 `probe-failover`。
2. **否则整体降级**：该点浓度置空、`authoritative=false`、质量 `degraded`，
   审计 `probe-degrade`/`reading-degraded`；`trip_on_degrade`（默认开）联动
   安全联锁跳闸。不存在“漂移探头继续出可信浓度”的中间态——失校时宁可不测。

读数质量取值：`nominal / fallback / switched / degraded`。

## 4. 审计登记

每条读数写入审计（kind `ammonia-reading`），detail 含：
`point / ppm / basis / version / migration / quality`；失校切换与降级、
标定草稿/激活、迁移开始/切换/回滚、探头登记/绑定/标记均有独立 kind。
近窗读数（每点最近 512 条）通过 `/calibration/readings` 可查。

## 5. 管理端点

| 方法 路径 | 作用 |
| --- | --- |
| GET `/calibration/versions` | 列出每个标定版本及其**适用范围**（仪器量程、已标定浓度跨度、生效起止、样本） |
| POST `/calibration/draft` | 多点标定建草稿（返回是否合格及不合格原因） |
| POST `/calibration/activate` | 激活合格草稿、退役当前版本 |
| GET `/calibration/gate` | 双轨偏差闸门状态 |
| POST `/calibration/migrate` `/cutover` `/rollback` | 迁移流转 |
| POST `/calibration/reading` | 原始信号录入，返回折算浓度与全部溯源字段 |
| GET `/calibration/readings` | 该点读数（含版本、迁移状态、影子值） |
| POST `/calibration/probe`、GET `/calibration/probes`、`/probe/bind`、`/probe/mark` | 探头登记/列表/绑定版本/标记失校 |
| GET `/calibration`、GET `/calibration/points`、POST `/calibration/point` | 监测点状态 |

标定状态同时进入 `/snapshot`、`/describe`、`/report` 与 `/health/checks`
（含总体行 `calibration` 与每点行 `calibration:<point>`，降级点报 fail）。

## 6. 测试

* `tests/test_calibration.py`：曲线数学、资格口径、回退、双轨权威唯一、
  闸门拦截、切换/回滚、失校切换与整体降级、审计字段、适用范围列表。
* `tests/test_console.py::CalibrationConsoleCase`：完整迁移生命周期与
  失校跳闸的端到端 HTTP 验证。
