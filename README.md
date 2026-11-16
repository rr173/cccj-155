# 队列加工器（cohort_builder）

从纵向就诊流水构建分析队列：给每个人输出**索引日**或**待补资料**状态，并附上可核对的原始证据。

只使用 Python 标准库 + DuckDB。不做跨人身份解析、不做科研统计、不计算疗效。

---

## 1. 业务口径

### 输入

一条方案 `plan.json` + 一组脱敏文件（事件表、每类来源各一份覆盖表）：

```json
{
  "plan_id": "main-example",
  "lookback_days": 3,
  "confirm_days": 2,
  "events_file": "events.csv",
  "sources": [
    {"source_id": "A", "kind": "exclusion",  "coverage_file": "excl_A.csv"},
    {"source_id": "B", "kind": "qualifying", "coverage_file": "coverage_B.csv"}
  ]
}
```

* `events_file` 列：`person,event_id,revision,day,kind`（可选第 6 列 `retracted`）。
* 每份 `coverage_file` 列：`person,start_day,end_day`，**闭区间**。
* 路径可写绝对路径，或相对 `plan.json` 所在目录。
* 文件支持 `.csv` / `.tsv` / `.parquet` / `.json` / `.jsonl`。所有文件都由 DuckDB 直接读取，任何内容都不会被当作 Python 代码执行。

### 事实解析

* 每条事件按 `(person, event_id)` 取**最高 `revision`** 作为唯一事实。
* 接收顺序 / 文件顺序**不参与**业务排序。`src_line` 只用于报错与追溯。
* 撤销：最新 revision 的 `day` 与 `kind` 同时为空（或可选的 `retracted` 列为 `true` 且无载荷）即视为停用；停用后该事件不再参与任何判定。
* 同一 `(person, event_id, revision)` 对应两个不同事实、或半空字段、或非法字段 → **整次输入拒绝**（`event_identity_conflict` / `event_row_invalid`）。

### 候选与窗口

* 候选 = 每个人**有效 encounter 日**（去重）。
* 回看窗口 `[d-W, d)`：起点包含，终点**不含** `d`。
* 确认窗口 `[d, d+F]`：两端**都包含**。

### 三值判定

对候选 `d`：

| 条件 | 结果 |
|---|---|
| `[d-W,d)` 内实际出现 exclusion | 回看 = **false** |
| 回看无 exclusion，且所有 exclusion 来源都完整覆盖该窗口 | 回看 = **true** |
| 回看无 exclusion，但覆盖不完整 | 回看 = **unknown** |
| `[d,d+F]` 内实际出现 qualifying | 确认 = **true** |
| 无 qualifying，且所有 qualifying 来源都完整覆盖窗口 | 确认 = **false** |
| 无 qualifying，但覆盖不完整 | 确认 = **unknown** |

合取：**任一 false 即 false**；两项 true 才 true；其余 unknown。

> **正向事件即使落在声明的覆盖之外，仍然是真实证据**，绝不会因为“资料不全”被删除。
> 覆盖缺失只影响“**没看到** 能不能推出“**不存在**”。

### 每人结论

按日排序候选：

* 首个**非 false** 候选为 `true` → `indexed`，`index_day = d`。
* 首个非 false 候选为 `unknown` → `pending`，`earliest_pending_day = d`；
  **即使后面有 true 也不能跳过**，同时导出后面已知 true 的日期（`later_true_days`）。
* 全部候选为 false → `no_index`。
* 没有任何 encounter → `no_candidate`。

### 来源覆盖语义

* 同一类（exclusion / qualifying）有多个来源时，**每个来源都必须单独完整覆盖整个窗口**，才能从“缺席”推出“不存在”。A 来源的覆盖**不能**替 B 背书。
* 覆盖区间可拼接**相邻**段（`[7,8]`+`[9,9]` → `[7,9]`）；**单日缺口**（`[7,7]`+`[9,9]`，缺 8）不会被合并掉。
* 一类来源必须至少声明一个，否则无法推断“不存在”，整次拒绝（`plan_no_sources_for_kind`）。
* 覆盖表坏行、区间倒置、缺列、重复列、超范围 → **整次拒绝**，不做部分接受。
* “资料不足”不是运行失败，而是 `unknown` / `pending`。

---

## 2. 输出

`build` 在 `--out` 目录产出：

| 文件 | 内容 |
|---|---|
| `queue.csv` | 每人一行：`status`、`index_day`、`earliest_pending_day`、候选计数、后置 true 日 |
| `pending.csv` | **按具体来源/日期缺口**展开：人、最早待判日、窗口、来源、`gap_start`、`gap_end`、缺失天数、后置 true 日 |
| `candidates.csv` | 每个候选一行：三值结论、原因、窗口边界、实际 exclusion/qualifying 数、覆盖完整性 |
| `explain_candidates.csv` | 每个候选的完整 JSON：实际支持/排除事件、各来源已覆盖片段与缺口、窗口是否为空 |
| `explain_persons.csv` | 每人 JSON：状态、索引日/待判日、后置 true 日、候选索引 |
| `checks_plan_sources.csv` | 方案与来源检查：每来源行数、覆盖人数、合并段数、覆盖日跨度 |
| `run_report.json` | 运行摘要、输入指纹（SHA-256）、计数、耗时、截断上限说明 |

解释不是“完整性布尔值”：每个窗口都列出

* `exclusion_count` / `qualifying_count`（精确总数）
* `exclusions` / `qualifiers`（实际事件，含 `day/event_id/revision/input_line`）
* 每个来源的 `covered`（已覆盖片段）、`gaps`（缺口）、`covered_days/window_days/gap_days/gap_count`

超长列表有显式上限（见下），并在字段里带 `*_truncated` 标记；**总数始终精确**。

### 旧结果固定

`queue.csv` 等一旦写出即视为固定结果。再次向同一目录 `build` 会被**拒绝**（退出码 3）。
需要重建时用 `--force`：旧目录会被整体移到 `<out>.prev`，不会被删除。
方案变更或新输入版本请写新目录。

---

## 3. 命令

```bash
# 只校验方案和来源声明（不读数据文件）
python -m cohort_builder check --plan plan.json

# 校验方案 + 完整解析数据文件的列/字段/冲突（不做推导）
python -m cohort_builder check --plan plan.json --emit-staged

# 构建
python -m cohort_builder build --plan plan.json --out out/

# 大数据：落盘数据库 + 溢出目录 + 内存上限
python -m cohort_builder build --plan plan.json --out out/ \
    --db out/work.duckdb --temp-dir /tmp/duckdb-spill --memory-limit 4GB --threads 8
```

退出码：`0` 成功；`2` 输入被拒绝（`run_report.json` 里给出结构化 issue 列表）；`3` 拒绝覆盖已有结果。

---

## 4. 算法：不逐日展开

窗口可以长达 10⁹ 天，处理成本与窗口长度**无关**：

* 事件按 `(person, kind)` 稠密排名；窗口计数 = 两个**边界排名之差**（ASOF 有序归并，单次扫描）。
* 覆盖按 `(source, person)` 做区间并集（相邻合并、缺口保留），再对每个 `(候选窗口 × 声明来源)` 只做常数次 ASOF 边界查找 + 前缀长度相减。
* 覆盖片段与缺口由**排名区间**展开后哈希连接得到，只对不完整窗口展开，且每个窗口有显式上限（200 片段 / 200 缺口）。
* 窗口内事件同理按排名区间取回，单窗口上限 200 条，超出即标记 `truncated`，总数仍精确。
* 所有明细表都落在 DuckDB 中；`--db` 可让整个中间过程落盘，超过内存时自动溢出到 `--temp-dir`。

规模参考（4 线程，本机容器）：

| 输入 | 事件数 | 候选数 | 耗时 |
|---|---|---|---|
| 5 000 人 × 40 就诊 | 27 万 | 20 万 | ~8 s |
| 12 000 人 × 50 就诊 | 80 万 | 60 万 | ~16 s |
| 窗口 W=10⁹、F=10⁹ | — | 300 | <1 s |

---

## 5. 测试

```bash
python -m pytest tests -q
```

* `tests/test_semantics.py`：主例逐项复现（缺第 8 日 → pending@10 不取 20；补齐 → index 10；补 exclusion@8 → index 20；撤掉 20 确认覆盖与 qualifying → unknown），端点包含性、A/B 来源不互相背书、相邻拼接、单日缺口、同日多事件、修订乱序、长跨度。
* `tests/test_validation.py`：空/null/坏字段、重复列、缺失列、倒置区间、身份冲突、空来源类、负数窗口、超范围数值整体拒绝。
* `tests/test_cli.py`：输出文件、退出码、拒绝覆盖、`--force` 归档。
* `tests/reference.py`：独立的天级枚举参考实现（不使用引擎代码）。随机差分测试在上千个随机用例上与引擎逐人、逐候选比对三值结论与后续 true 日。
