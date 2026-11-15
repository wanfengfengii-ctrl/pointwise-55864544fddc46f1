# 两列行组扫描的公开约定

## 业务入口与合法输入
生产代码为 semantics.py、pruner.py、scanner.py；fixture.py 只构造数据及统计，samples.py 是正常样例，checks.py 保留已有业务断言。本轮只新增质量验证，不改上述文件。
使用 Python 标准库。pruner.can_skip(stats, predicate) 返回布尔值；scanner.scan(groups, predicate, pruning=True) 返回 {"rows":[行...],"skipped":[行组编号...]}。pruning=False 完全绕过裁剪；编号从0开始，命中行按行组及组内源顺序返回，保留重复。
输入均合法且调用期间不变；不要求测试畸形输入的报错。所有入口不得修改输入。本题不增加查询语法或文件格式。

行是 {"x":整数或null,"y":整数或null}，Python中null为None，整数不含bool。
行组是 {"stats":统计,"data":[行...]}，data长度等于rows。统计格式：
{"rows":3,"x":{"nulls":1,"lo":2,"hi":8},"y":{"nulls":0,"lo":0,"hi":9}}
rows为非负整数；每列nulls在0到rows之间且等于data实际NULL数。
非空值存在时lo、hi是整数、lo<=hi且包住该列全部非空值；它们是安全闭界，允许宽于实际极值，不要求端点出现。全空列或零行组的lo、hi均为null。
fixture.group(rows)返回统计紧致的合法行组；测试可把非空列的闭界放宽。

谓词是JSON可往返的列表树：
比较为 [op,a,b]，op属于 =、!=、<、<=、>、>=，a/b各为列名"x"/"y"或整数常量；允许同列及常量间比较。
逻辑为 ["and",p,q]、["or",p,q]、["not",p]。
单棵树最多16个比较叶子，根深度为1，总深度最多16；常量与元数据整数绝对值不超过10的30次方。
比较任一操作数为NULL时结果为UNKNOWN。NOT UNKNOWN仍为UNKNOWN；AND遇FALSE为FALSE，否则双方TRUE才为TRUE；OR遇TRUE为TRUE，否则双方FALSE才为FALSE；其余为UNKNOWN。只有TRUE命中，FALSE及UNKNOWN均不输出。

## 裁剪业务不变量
can_skip只能读取统计与谓词，不能读取data。
非零行组的候选单行集合按两列独立取笛卡尔积：nulls>0时该列允许NULL，nulls<rows时允许闭界内任一整数。
这里判定的是元数据允许的行，不是实际data中是否已有命中；未记录的列间相关性不能用于裁剪。
can_skip必须且仅当候选集合中不存在使完整谓词为TRUE的行时返回True；零行组总返回True。
因此不能以永不裁剪满足无漏行；不裁剪时也须验证确有候选行见证。
scan开启裁剪时skipped必须精确等于逐组can_skip为True的编号；跳过的组不作逐行过滤。
开关裁剪的rows须相等，并独立符合逐行三值语义；仅用同一个生产过滤器自比不足以证明正确。

## 样例与现有运行
python3 samples.py打印可直接json.loads读取的输入对象，包含groups及predicate。
样例谓词为 ["and",[">=","x",3],["<","x","y"]]。
python3 checks.py成功输出SCAN_NORMAL_OK；python3 checks.py demo还打印真实扫描结果：
{"rows":[{"x":3,"y":5},{"x":3,"y":5}],"skipped":[0,2]}
这些小样例不是完整质检套件。

## 本轮质量交付的公开入口
新增oracle.py，提供 judge(stats,predicate)，返回 {"skip":布尔值,"witness":候选行或null}；skip为False时witness必须使谓词为TRUE且满足统计允许的单行范围，skip为True时witness为null。
独立判定及其逐行语义不能导入或复制生产语义、分支展开、差分约束闭包步骤，也不能调用外部求解器；测试驱动可调用生产入口和fixture。
判定整个谓词，不能把各比较的独立可能真值直接合并；说明为什么有限判定覆盖全部允许整数，不能逐整数展开大跨度区间。
用手工可核对案例和小整数域完整枚举交叉核验oracle，再对元数据决策、开关裁剪扫描和独立逐行结果贯通核对。
覆盖NULL与NOT、同列矛盾、跨列次序、等号和严格边界、全空/零行、宽统计及重复行；固定种子差分须保存重跑参数。
在隔离副本或可撤销补丁内实际执行两类变异：将NOT UNKNOWN当成TRUE；将共享列的比较当成彼此独立。每类须成功返回但产生错误的裁剪决策或扫描行，并留下具体期望与实际；异常退出不算检出，不改保留的生产文件。
有限压力聚焦大整数跨度和上限内长谓词，记录规模、完整调用耗时与Python分配峰值，不设机器相关速度门槛。

新增qa.py，支持python3 qa.py verify、python3 qa.py demo。verify重跑原回归和本轮核验，重新生成evidence.json并核对REPORT.md；demo展示真实输入、裁剪及独立期望。
evidence.json结构自行设计，保存案例标识、输入或可重建参数、随机种子、期望与实际、反例和变异见证、压力实测。
REPORT.md引用案例解释覆盖依据、有限判定依据、差异、资源数据、剩余风险及上线意见；核验须检查引用和结论与重算证据一致。
发现生产偏差须保存可重跑反例并明确阻断，不要求修复生产功能；如实记录的生产偏差可作为质量结论。验证器自身错误、证据不一致或漏报偏差必须使verify非零。
.pointwise-runtime.json供调度读取，version保持1、kind保持cli、setup为argv数组列表；verify/demo分别为上述真实核验和演示的argv数组。初始清单指向原回归，交付时接入qa入口。
不涉及真实列式格式读取、分布式扫描、并发恢复或生产算法优化。
