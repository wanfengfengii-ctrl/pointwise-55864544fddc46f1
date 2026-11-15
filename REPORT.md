# 两列筛选统计裁剪 · 独立质检报告

结论先行：**现有生产实现（`pruner.py` / `scanner.py` / `semantics.py`）在本轮核验范围内未发现统计裁剪漏行或遗漏可裁剪组的偏差，生产偏差计数为 0，建议可上线。** 本报告的全部数字均由 `python3 qa.py verify` 重算，并与 `evidence.json` 交叉核对；报告内 `qa-claims` 块被 `qa.py` 解析，若与重算证据不一致则 verify 非零。

- 重算命令：`python3 qa.py verify`（同时重写 `evidence.json`）
- 演示命令：`python3 qa.py demo`（打印真实输入、裁剪决策、独立期望与见证）
- 原始回归：`python3 checks.py`（保留未改，仍输出 `SCAN_NORMAL_OK`）
- 生产文件摘要见 `evidence.json` 的 `production_sha256`，并与初始提交基线逐文件比对（`production_baseline`）。

## 1. 独立判定 oracle.py 的设计

`oracle.judge(stats, predicate)` 返回 `{"skip": bool, "witness": row|null}`，跳过时 `witness` 为 null，不跳过时给出满足统计单行范围且使完整谓词为 TRUE 的具体见证。它不导入、不调用、不复制 `pruner` 的布尔择一展开、`semantics` 的逐行实现，也不使用差分约束闭包、外部求解器或逐整数枚举；`oracle_independence_static` 用 AST 静态检查导入数=0、禁用符号=空、未调用 `range`。

- NULL 按三值逻辑单独处理：逐组枚举元数据允许的「列可为 NULL / 必须非空」组合（`nulls>0` 才允许 NULL，`nulls<rows` 才允许非空），任一操作数为 NULL 时比较为 UNKNOWN，NOT UNKNOWN 仍为 UNKNOWN。
- 非空列按谓词常量把闭界切成有限单元（等于某个常量、相邻常量之间的整数段），并对两列联合枚举 `x-y` 的三种符号（`<`/`=`/`>`）。每类单元内任一原子比较的真值恒定，单元间的严格/非严格关系按区间不等式可行性精确判定。

## 2. 有限判定依据（为何不需要逐整数展开大区间）

**有限判定**的核心是「真值只依赖单元与联合符号」：所有原子只把某一列与整数常量、或两列彼此、或两常量比较。给定每个非空列落在哪个常量单元、以及 `x-y` 的符号，每个原子真值即被固定。设谓词含 `k` 个不同整数常量（至多 32 个，受 16 比较叶子上限约束），单列至多 `2k+1` 个单元，故候选代表行数为 `(null 模式组合) × (x 单元 × y 单元 × 3 种符号)`，与闭界跨度无关。`finite_decision_basis` 记录了 `manual_wide_bounds_*` 等案例的单元数与代表行上限 vs 笛卡尔积对数（例如跨度为 ±10^30 时笛卡尔积约 4×10^60 对，但判定代表行 ≤ 27）；`stress_wide_16leaf` 进一步在 16 叶子、跨度 2×10^30 上实测，判定与扫描结果仍精确匹配。

## 3. 覆盖与交叉核验（小域穷举 + 固定种子差分）

- **小域穷举**：`small_domain_pruning` 在 219 个统计组合 × 216 个原子（含 `x`/`y`/常量各作左右操作数、全部 6 个运算符）共 **47,304** 次，把 `oracle.judge` 与全整数枚举逐一比对，并同时比对生产 `can_skip`，零不一致；每个非跳过见证都回验「三值 TRUE + 落在允许范围」。`small_domain_pairs` 叠加两叶子 AND/OR 及其外层 NOT，共 **191,808** 次；`small_domain_rows` 在完整枚举域上对 **6,776** 次逐行三值结果比对 oracle 与生产 `semantics.evaluate`，零不一致。
- **固定种子差分**：`differential_pruning_seeded` 用种子 `7 / 2024 / 20261115`、每种子 350 例、1–16 叶子、含小界/±10^30 宽界/单点/巨界与 5 个常量，共 **1,050** 次元数据决策比对；`differential_rows_seeded` 共 **4,200** 次逐行比对。参数全部写入 `evidence.json`（`params` 与 `seeds`），可直接重跑。
- **端到端扫描**：`scan_pipeline_samples`/`scan_pipeline_seeded` 断言开关裁剪的 rows 相等且都等于独立期望、`skipped` 恰为逐组 `can_skip` 的组号、命中保序保重复（含显式重复行与 NULL 行）；`scan_immutability` 与 `production_files_unchanged` 证明输入未被修改。

手工可核对案例（均同时给出 oracle 与生产结论）：

| 案例 | 场景 | 期望 skip | 说明 |
|---|---|---|---|
| `manual_null_not` | `NOT(x=1)`，`x` 可为 NULL | False | `x=2` 使 NOT(FALSE)=TRUE，见证 `{"x":2,...}` |
| `manual_null_negated_conjunction` | 两列全 NULL 的 `NOT(AND)` | True | 全 UNKNOWN，NOT UNKNOWN 仍 UNKNOWN |
| `manual_same_column_contradiction` | `x>8 AND x<5` | True | 共享列不可独立满足 |
| `manual_cross_column_order` | `x<y AND x>y` | True | 同一对列不能同时满足 |
| `manual_cross_column_equality_gap` | `x=y`，`x∈[1,2]`、`y∈[5,6]` | True | 闭界不相交 |
| `manual_equality_strict_bounds` | `x<=y AND x>y` | True | 严格边界矛盾 |
| `manual_all_null` | 全 NULL 的 OR/NOT 组合 | True | 所有原子 UNKNOWN |
| `manual_zero_rows` | 零行组 | True | 契约规定恒跳过 |
| `manual_wide_bounds_true` | ±10^30 跨度的可满足合取 | False | 宽界内仍有见证，不逐整数展开 |
| `manual_wide_bounds_impossible` | ±10^30 跨度的同列矛盾 | True | 跨度巨大但仍精确裁剪 |
| `manual_not_tautology` | `NOT(x!=x)`，`x` 非空候选 | False | `x!=x` 为 FALSE，取反得 TRUE |

## 4. 业务检出证据：两类实际变异

两类变异都在**隔离临时副本**中施加，生产文件本身未被改动；每类变异均正常退出（`exit_code=0`）却产生错误决策/错误行，按「成功返回但结果错误」判定为检出：

- `mutation_null_not_negation`：把 `semantics.py` 的 NOT UNKNOWN 改成 TRUE。输入 `{"x":2,"y":0}`、`{"x":None,"y":7}`，谓词 `NOT(x=1)`；期望仅含 `{"x":2,...}`，变异体多输出 NULL 行，`detected=true`。
- `mutation_shared_column_independence`：把 `pruner.py` 的择一可行性改成「每条边各自可行即可」。P1 `x>8 AND x<5`（同样 P2 `x=y AND x!=y`）期望 `can_skip=true`、scan 跳过 `[0]`；变异体判 `can_skip=false`、scan 返回空且不跳过 `[]`，`detected=true`。

异常退出（非零）不计为检出，两类变异均为零退出且结果差异被 `expected_correct` vs `actual_mutated` 明确记录在 `evidence.json`。

## 5. 有限压力与资源实测

`stress_wide_16leaf`（16 叶子、深度 16、跨度 2×10^30）与 `stress_deep_16leaf`（16 叶子、深度 16、共享列矛盾不可满足），各 12 组共 4,800 行，每组同时跑生成谓词（可满足或不可满足）与一个可满足见证谓词。实测（无机器相关速度门槛，仅记录规模与耗时/内存，数值随运行波动）：每案例判定 + 双开关扫描总耗时约 0.8–1.7s，Python `tracemalloc` 峰值约 1.7–2.3MB，`skip` 与逐行结果全部匹配独立期望；精确数值见 `evidence.json` 的 `stress[*].elapsed_seconds` 与 `peak_python_bytes`（这两项是测量值，不参与证据可复现性比对）。详见 `evidence.json` 的 `stress` 字段。

## 6. 差异与剩余风险

- **生产偏差**：0。所有元数据决策、开关扫描与逐行结果均与独立期望一致，未留存反例。
- **未发现生产偏差不等于逻辑证明**：oracle 的精确性由小域穷举与固定种子差分支撑；对超出已覆盖的深层组合，仍属归纳性证据。
- 变异检出采用隔离副本与可撤销补丁，只验证「业务能检出这两类错误」，不证明其他变异均可被现有断言捕获；本套件新增的 oracle 对两类变异同样会产生差异，但已按要求以实际执行记录为准。
- 统计闭界允许宽于实际极值，判定的是元数据允许行而非真实数据命中；本报告严格按契约不推断列间相关性。
- 不涉及真实列式格式读取、分布式扫描、并发恢复或生产算法优化。

## 6b. 偏差阻断链路的自证

`deviation_blocking` 在另一个隔离副本中向 `pruner.can_skip` 注入「永远返回 False」的偏差，然后以 `python3 qa.py verify` 重跑整套质检：子进程以非零码退出、打印 `QA_VERIFY_BLOCKED`，且 `evidence.json` 中写入了带 `rerun_argv`/`stats`/`predicate` 的可重跑反例（每来源最多保留 40 条，总计数单独记录）。这证明材料偏差会阻断发布而不是静默通过，也证明反例确实被留存。类似地，篡改 `evidence.json` 或用不一致的 `REPORT.md` 声明都会让 verify 非零（已人工验证）。

## 7. 上线意见

生产实现与本轮独立 oracle 在统计裁剪、开关扫描与逐行三值语义上全部一致，`evidence.json` 可重算且与报告一致；建议上线，并保留 `qa.py verify` 作为回归闸门（发现偏差将非零阻断并留存反例）。

```qa-claims
{
  "cases": {
    "deviation_blocking": "pass",
    "differential_pruning_seeded": "pass",
    "differential_rows_seeded": "pass",
    "finite_decision_basis": "pass",
    "manual_all_null": "pass",
    "manual_cross_column_equality_gap": "pass",
    "manual_cross_column_order": "pass",
    "manual_equality_strict_bounds": "pass",
    "manual_not_tautology": "pass",
    "manual_null_negated_conjunction": "pass",
    "manual_null_not": "pass",
    "manual_same_column_contradiction": "pass",
    "manual_wide_bounds_impossible": "pass",
    "manual_wide_bounds_true": "pass",
    "manual_zero_rows": "pass",
    "mutation_null_not_negation": "detected",
    "mutation_shared_column_independence": "detected",
    "oracle_independence_static": "pass",
    "original_regression": "pass",
    "production_baseline": "pass",
    "pruner_stats_only": "pass",
    "scan_immutability": "pass",
    "scan_pipeline_samples": "pass",
    "scan_pipeline_seeded": "pass",
    "small_domain_pairs": "pass",
    "small_domain_pruning": "pass",
    "small_domain_rows": "pass",
    "stress_deep_16leaf": "pass",
    "stress_wide_16leaf": "pass",
    "verifier_selftest": "pass"
  },
  "checks": {
    "differential_pruning_seeded": 1050,
    "differential_rows_seeded": 4200,
    "scan_pipeline_seeded": 16,
    "small_domain_pairs": 191808,
    "small_domain_pruning": 47304,
    "small_domain_rows": 6776
  },
  "mutations": {
    "mutation_null_not_negation": true,
    "mutation_shared_column_independence": true
  },
  "production_deviations": 0,
  "production_sha256": {
    "pruner.py": "567241851128e4c99ad94cab3ffca6b03d28e4e33e9ad0e812f32442ec3d16d0",
    "scanner.py": "22eaebd2463f0289c478fd209097ae07a64a6a6678981083f5a6ba01fa1029ad",
    "semantics.py": "329f37288b4f7210e05531d661f2f185a96d79d27d9950a0e263d66f0d2dc05a"
  }
}
```

> 本报告数字引用自 `evidence.json`。`qa.py verify` 会重新计算并核对上述 `qa-claims`：案例状态、核对次数、变异检出结论、生产偏差计数及生产文件摘要若与重算证据不符，verify 退出非零并阻断。
