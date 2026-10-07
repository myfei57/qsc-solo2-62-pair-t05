# 氨检探头标定与量程管理

针对"换过一批新探头、量程不同、目前只有一个系数、旧数据按哪个口径算说不清"的问题，
本模块把"一个系数"升级为**多点标定 + 版本化 + 迁移状态机**，并把所有有歧义的
口径在代码与配置中固定下来。

代码入口：`coldstore/calibration/`
管理端点：`/calibration/*`（见下），审计写入统一的 append-only 审计流。

## 1. 标定版本（多点标定）

* 每个版本是一条分段线性曲线：`points = [{raw, reference}, ...]`，raw 严格递增，
  中间线性插值。
* 版本状态：`draft → active → superseded`。旧版本**只退役、不删除**，历史读数
  永远可以按当时的版本重算（`/calibration/recompute`）。
* 单点标定自动兼容老的"一个系数"口径：`factor = reference/raw`，过原点直线，
  对整个探头量程有效（不标 out_of_span）。
* 每个版本登记自己的适用量程 `range_min..range_max`（探头物理量程）、单位与
  最小样本数 `min_samples`。启动时自动建立基线版本 `legacy-v1`（系数 1.0，
  0–1000 ppm），老系统零配置可用。
* 版本作用域可列出：`GET /calibration/scope`，每行形如
  `status=active raw[0..100] probe range[0..500] ppm -> concentration[0..90] ppm (trusted)`。

## 2. 读数按版本折算

每条读数同时登记：原始值、浓度、**绑定版本 version、实际折算版本
converted_version（=basis）、迁移模式 mode、可信标志 trusted、标志位 flags**。

超界口径（默认值可在运行时配置，`POST /calibration/policy`）：

| 情形 | 默认口径 | 结果标志 |
|---|---|---|
| 样本不足（标定点 < min_samples） | **回退到上一版本** `insufficient=fallback` | `insufficient_fallback` |
| 同上（配置为最近一段） | 用最近一段线性外推，读数**不可信** | `insufficient`，trusted=false |
| 超出标定跨度但在探头量程内 | **夹到端点参考值** `span=clamp` | `out_of_span` |
| 同上（配置外推） | 最近一段线性延伸 | `out_of_span` |
| 超出探头物理量程 | 不出浓度 | `out_of_range`，trusted=false |

明确决策：**样本不足时默认回退上一版本**，绝不拿新探头的外推值冒充正式口径；
需要外推必须显式配置，且读数带 `insufficient`、trusted=false。

## 3. 新旧版本并存与迁移（浓度口径一致性）

迁移状态机：`legacy → dual → migrated`（dual 可 abort 回 legacy）。

* `POST /calibration/migration/dual {"version": "v2"}`：新版本激活，与旧版本
  **并存**。双跑期间：
  * 上报浓度仍以**旧口径为准**（`concentration` = 旧口径，`legacy_concentration`
    并存），联锁/阈值看到的浓度不变，保证迁移期间口径一致；
  * 每条原始读数同时按两个版本折算，自动进入双跑比对样本。
* `GET /calibration/cross`：比对结果（样本数、需达样本数、最大偏差、容差内
  样本数、是否通过）。
* `POST /calibration/migration/promote`：**闸门**——比对样本数达标（默认 ≥3）
  且最大偏差 ≤ 容差（默认 2 ppm，可配）才允许统一到新版本；不达标返回 400。
  通过后旧版本 superseded、模式转 migrated，上报浓度切换为新口径。
* `POST /calibration/migration/abort`：终止双跑，新版本退回 draft，继续旧口径。
* 迁移完成后仍绑定旧版本的探头读数会自动切到当前基准并标 `rebased`；绑定版本
  与实际折算版本分别留痕。

旧数据口径：历史原始读数不被改写；`POST /calibration/recompute {"version":...}`
可把任意历史读数在任意保留版本下重算，输出存储值/原口径与新口径对照。

## 4. 审计

浓度、标定版本、迁移状态全部进统一审计流（kind 前缀 `calibration.`）：
`version_created / version_activated / version_superseded / probe_registered /
probe_version / probe_lost / probe_repaired / probe_switch / policy / reading /
migration_begin / migration_promoted / migration_aborted`。

`reading` 审计明细含 probe、raw、concentration、basis、bound_version、
converted_version、mode、trusted、flags。查询：`GET /calibration/audit`。

## 5. 失校策略：切备用探头 vs 整体降级

明确选边，**默认切备用探头**（`failover=standby`，与系统内风机/冷凝风机的
主备切换一致）：

* 主探头失校（`POST /calibration/probe/lost`）→ 自动切健康备用探头，读数标
  `failover`，`source` 标明实际供样探头；
* 没有健康备用探头 → **整体降级**，不出浓度，标 `degraded`；
* 配置为 `failover=degraded` → 失校即整体降级，不切备用；
* `repair` 后主探头回备用位（需显式 `probe/switch` 恢复主用）。

健康检查 `/health/checks` 的 `calibration` 项会反映主探头失校、是否有备用、
双跑比对失败等状态。

## 6. 端点一览

```
GET  /calibration                     总状态（版本/探头/策略/迁移/比对）
GET  /calibration/versions            所有标定版本
POST /calibration/version             新建多点标定（draft）
POST /calibration/activate            激活 draft
GET  /calibration/scope               每次标定的适用范围
GET  /calibration/probes              探头清单与各自基准
POST /calibration/probe               注册探头（带量程、主备角色）
POST /calibration/probe/version       探头换到某标定版本
POST /calibration/probe/lost          标记失校
POST /calibration/probe/repair        解除失校
POST /calibration/probe/switch        主用切换
POST /calibration/policy              insufficient/span/failover/tolerance/min_cross_samples
POST /calibration/read                原始读数 → 浓度（含全部口径留痕）
GET  /calibration/readings            折算后读数
POST /calibration/recompute           旧读数按指定版本重算
GET  /calibration/migration           迁移状态
POST /calibration/migration/dual      进入双跑
POST /calibration/migration/promote   比对通过后统一到新版本
POST /calibration/migration/abort     中止双跑
GET  /calibration/cross               双跑比对结果
GET  /calibration/audit               标定/迁移审计条目
```
