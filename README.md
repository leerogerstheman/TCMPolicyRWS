# 中医药政策与真实世界研究工具 (TCMPolicyRWS)

面向**中医药政策文本挖掘**与**中医药真实世界研究(RWS)**的量化分析工具。

本项目为药事管理方向的实证研究提供可复现的分析流水线:从中国政府网政策文件库
采集中医药相关政策文件,建立可全文检索的语料库并分析政策变迁;同时提供中药
不良反应特征、用药依从性(PDC/MPR)、中成药联合用药模式、DRG/医保付费下
中医药成本结构等分析模块。

---

## 零、实测结果(真实政策数据)

以下数字全部来自对**中国政府网政策文件库真实数据**的一次完整采集
(`python tcm_rws.py crawl`),可复现:

| 指标 | 数值 |
|---|---|
| 收录政策文件 | **188 份** |
| 成功抽取全文 | **183 份(97.3%)** |
| 全文平均长度 | 约 2,854 字(最长 15,055 字) |
| 年份跨度 | **2008 – 2026**(有文件的年度共 18 个) |
| 发文机关(归一化后) | **22 个** |

### 年度分布(政策变迁的直接体现)

```
2008 ███ 3          2018 ████████████ 12
2009 █ 1            2019 ███████████████ 15
2010 ████ 4         2020 ████████████████ 16
2011 █ 1            2021 ██████████████████████ 22
2012 ██ 2           2022 ██████████████████████ 22
2013 █ 1            2023 ██████████████████████████ 26
2015 ███ 3          2024 █████████████████████████ 25
2016 █████ 5        2025 ██████████████████ 18
2017 ██ 2           2026 ██████████ 10
```

**可观察到的政策拐点**:2018 年(12 份)与 2019 年(15 份)出现明显跃升,
此前的 2008–2017 十年间每年仅 1–5 份。2019 年中共中央、国务院《关于促进
中医药传承创新发展的意见》发布后,年度发文量稳定在 16–26 份的高位 —— 这是
中医药政策从"部门专项"上升为"国家战略"在文本层面的直接体现。

### 发文机关分布(归一化后)

| 机关 | 文件数 |
|---|---|
| 国家中医药管理局 | 59 |
| 国务院 | 36 |
| 国家卫生健康委员会 | 28 |
| 国家药品监督管理局 | 21 |
| 国家医疗保障局 | 19 |
| 其他 17 个机关 | 25 |

> **机关名称归一化是一个必要步骤,不是锦上添花。** 原始数据中同一机构有多种
> 写法(`中医药局` / `国家中医药局` / `国家中医药管理局` / `中医药管理局办公室`
> / `国家中医药局综合司` …),不处理时按机关聚合会产生 **80+ 个"机关"**,其中
> 多数实为同一机构,统计结果不可用。归一化后收敛到 22 个,同时保留
> `puborg_raw` 原始值以保证可审计。

### 关于 5 份未能抽取全文的文件

183/188 成功,其余 5 份**并非解析缺陷**,而是数据源的客观限制:这些页面
(主要为较早年份的国务院文件)正文以**影印图片**(`<img>`)形式发布,页面中
不存在可抽取的文本层,另有 1 份为前端脚本注入的分页文档。

工具会明确区分三类原因并记入 `body_status` / `body_error` 字段:

| 状态 | 含义 |
|---|---|
| `image_only` | 正文容器内仅含图片(扫描件),**该文件本身没有可抽取文本** |
| `paginated` | 页面分页,正文由前端脚本注入,服务端 HTML 不含正文 |
| `node_missing` | 容器不存在,可能是页面结构变更 —— **这一类才提示解析器需修复** |

区分它们很重要:把前两类误记为"节点缺失",会把数据源限制错误地报成代码缺陷。

---

## 一、重要声明(请先阅读)

### 1.1 数据边界

| 数据类型 | 来源 | 性质 |
|---|---|---|
| 中医药政策文本 | 中国政府网政策文件库(`www.gov.cn`)公开检索接口 | **真实公开数据** |
| RWS 演示数据 | `examples/` 下 4 个 CSV | **人工构造的合成数据,非真实患者数据** |

* `examples/` 中所有文件的头部均带有显著的合成数据声明。其中的任何"结果"
  仅用于验证代码逻辑,**不具备任何临床、流行病学或实证含义**。
* 本工具**不采集、不推断、不发布**任何真实患者数据。运行完整演示流程
  **无需任何真实患者数据**。
* 使用真实数据时,数据合规性(伦理审查、知情同意、去标识化)由使用者负责。

### 1.2 能力边界

* 本工具**不提供临床建议**,**不作为医保结算依据**,**不构成法律意见**。
* 本工具**不对政策条文作法律解释**;政策原文以发布机关正式文本为准。
* 本工具**不尝试绕过任何网站的反爬虫机制**(见 3.2 节)。

---

## 二、快速开始

### 2.1 环境要求

* Python 3.10+(开发与验证环境:Python 3.12)
* 必需:`pandas`、`numpy`(表格导出)
* 可选但推荐:`matplotlib`(图表)、`beautifulsoup4`(正文解析)、
  `scipy`(更精确的 p 值)、`openpyxl` + `XlsxWriter`(XLSX 导出)、`pytest`
* 缺少可选依赖时程序会自动降级运行,不会崩溃(详见 5.3 节)

### 2.2 安装与运行

```powershell
# Windows PowerShell
$env:PYTHONIOENCODING='utf-8'

# 1) 采集真实政策数据(约 5-15 分钟,含正文抓取)
python tcm_rws.py crawl --max-pages 4 --sleep 1.0

# 2) 查看采集结果与环境
python tcm_rws.py info

# 3) 政策变迁分析(出图 + 导出 + 报告)
python tcm_rws.py analyze

# 4) 全文检索(注意 2 字词走 LIKE 回退)
python tcm_rws.py search 中医药 --limit 10

# 5) RWS 分析(使用合成样例数据)
python tcm_rws.py rws `
  --adr examples/sample_adr_synthetic.csv `
  --rx examples/sample_prescriptions_synthetic.csv `
  --coprescribing examples/sample_coprescribing_synthetic.csv `
  --drg examples/sample_drg_synthetic.csv --rate 11000

# 6) 一键跑通全流程
python tcm_rws.py demo
```

### 2.3 运行测试

```powershell
# 方式一:独立运行(不需要 pytest)
python tests/test_core.py

# 方式二:pytest
python -m pytest tests/ -q
```

两种方式均可,**39 个测试全部通过**。测试中的期望值均为**手算推导**
(推导过程写在每个测试的 docstring 中),而非"程序输出即断言"。

---

## 三、数据来源与方法

### 3.1 政策数据采集

**来源**:中国政府网政策文件库公开 JSON 检索接口

```
https://sousuo.www.gov.cn/search-gov/data
  ?t=zhengcelibrary_bm        # 部门文件(亦支持 zhengcelibrary_gw 国务院文件)
  &q=<关键词>
  &timetype=timeqb&sort=pubtime&sortType=1
  &searchfield=title&p=1&n=20
```

**检索策略**:按**标题**匹配中医药相关关键词。当前内置关键词集(12 个):

```
中医药、中药、中医、经典名方、中药饮片、中西医结合、
中成药、民族药、中医医院、治未病、中医药服务、中药配方颗粒
```

> **为什么限定标题匹配**:全文匹配的噪音极高 —— 附件表格中偶然出现"中药"
> 二字就会把大量非中医药政策域的文件(如综合性医药产业方案)纳入语料,
> 从而严重扭曲政策变迁的时间线。标题匹配牺牲部分召回率换取更高的精确率,
> 这对统计口径的可靠性是必要的取舍。

**正文抽取**:文章页正文位于 `<div id="UCAP-CONTENT">`,服务端渲染。

> **踩坑记录**:gov.cn 的文章页 HTML 存在不规范嵌套。实验表明
> **lxml 解析器会静默丢失 `UCAP-CONTENT` 节点**(返回 `None` 而不报错),
> 而标准库 `html.parser` 可正确解析。因此正文抽取固定使用 `html.parser`,
> 并配有基于 div 嵌套深度计数的正则回退路径。
>
> **抽取结果必须校验并留痕**:抓取后校验正文非空且长度达标,
> 成功写 `body_status='ok'`,失败写 `body_status='failed'` 与 `body_error` 原因。
> **失败绝不静默写为空正文** —— 否则下游无法区分"抓取失败"与
> "该文件确实没有正文",会把数据缺口误当作事实。

### 3.2 发文机关归一化

gov.cn 的 `puborg` 字段是**自由文本**,存在两个必须处理的问题:

1. **同一机构多种写法**:`中医药局` / `国家中医药局` / `国家中医药管理局` /
   `中医药管理局办公室` / `国家中医药局综合司` / `国家中医药管理局医政司` …
2. **联合发文塞进同一字段**,且用**不可见字符混合分隔** ——
   普通空格、`\u2002`(EN SPACE)、`\u00a0`(NBSP)、`\u3000`(全角空格)。

不处理时按机关聚合会产生 80+ 个"机关",其中多数实为同一机构,统计结果不可用。

**归一化策略(两段式,可复核)**:

1. 先把各类不可见空白统一为普通空格,再按分隔符切分;
2. 逐个映射到规范名(别名表 `ORG_ALIASES`,按别名长度降序匹配以避免
   短别名如"卫生部"抢先命中长别名);未收录的机构剥离内设机构后缀
   (办公厅/办公室/综合司/医政司…)保留主体。

数据保留三个字段以满足不同口径:

| 字段 | 含义 |
|---|---|
| `puborg` | **主要发文机关**(联合发文的第一个规范名) |
| `puborg_all` | 全部参与机关(规范名,`、` 分隔) |
| `puborg_raw` | 原始字符串(可回溯核验) |
| `n_orgs` | 参与发文机关数(联合发文 > 1) |

由此提供**两个不可混用的统计口径**:

* **按牵头机关**(`timeline_by_agency`):各机关计数之和 = 文件总数,便于核对;
* **按参与发文**(`participation_by_agency`):拆分联合发文,**分母是机关提及次数**。
  该口径可揭示在联合发文中排名靠后的机关的实际参与度 —— 例如国家中医药管理局
  常出现在多部委联合发文的中间位置,仅按牵头机关统计会显著低估其参与度。

### 3.3 未访问的站点(数据覆盖的真实边界)

`nmpa.gov.cn`(国家药监局)、`cde.org.cn`(药审中心)、`nhc.gov.cn`(国家卫健委)
部署了 JavaScript 反爬机制(返回 HTTP 412/202)。**本工具不尝试绕过此类访问
控制** —— 绕过反爬既违背站点意愿,也导致采集结果不可稳定复现。

由此产生的**影响必须被如实说明**:上述机构的原始文件(如药典标准、技术指导
原则、药品注册批件)**未纳入本语料**。因此本语料反映的是"经中国政府网政策
文件库收录并公开的中医药政策文件",而非中国中医药政策的全集。

### 3.4 存储与检索

* **SQLite**,`policy` 表存元数据与正文,`policy_keyword` 表存主题词频。
* **FTS5 全文索引**,采用 `trigram` 分词器(此即中文检索方案)。

> **关键的检索设计约束**:FTS5 内置的 `unicode61` 分词器按空白与标点切词,
> 对中文等于把整句当作单个 token,检索基本失效。`trigram` 分词器按 3 字符
> 滑窗建索引,是官方支持的可行方案。但 **trigram 无法为长度 < 3 的查询串
> 生成三元组**,因此 `MATCH '中医'` 恒为空集 —— 这是分词器的数学限制,
> 不是 bug。
>
> 由于"中医""中药"恰恰是最常用的中文检索词,本工具对 < 3 字符的查询
> **自动回退到 `LIKE` 检索**,并在结果中返回 `fallback_used` 标记,
> 避免使用者误以为"没有匹配结果"。

### 3.5 政策分析口径

| 分析项 | 方法 | 关键说明 |
|---|---|---|
| 年度分布 | 按 `pubyear` 分组计数 | 同比变化率在前一年基数 < 3 时返回 `None`(基数过小,增长率无解释力) |
| 发文机关 | 按 `puborg` 原始字符串分组 | **不做联合发文拆分**,保证数字可直接回溯核验 |
| 政策主题 | 规则分类器(`THEME_RULES`),标题优先 | 确定性规则,可复核;规则边界直接决定主题占比 |
| 主题演变 | 关键词**文档覆盖率** | 非原始词频 —— 原始词频受文件长度影响,长文件天然词多 |
| 阶段划分 | `POLICY_ERAS` 人工设定 | 依据关键制度节点,**不是数据驱动的自动分段** |
| 断点检测 | 稳健标准化(中位数 / MAD),\|z\| ≥ 1.5 | 用 MAD 而非标准差,因标准差会被离群年份自身抬高而掩盖断点 |

### 3.6 统计方法

* **卡方检验**:Pearson 独立性检验。p 值优先用 `scipy`;无 `scipy` 时用
  **正则化不完全伽马函数的级数/连分式展开**(Numerical Recipes 的 `gammq`),
  精度足以支撑显著性判定(测试中以卡方分布 95% 分位点校验:χ²=3.841459, df=1 → p≈0.05)。
* **Cramér's V**:与 p 值**同时报告**。原因是 p 值随样本量增大而系统性变小,
  大样本下极弱关联也会"显著";V 提供 0–1 的关联强度度量。
* **期望频数警告**:当期望频数 < 5 的格子占比 > 20% 时,输出显式警告
  (卡方近似此时不可靠),而不是默默给出一个不可信的 p 值。
* **PDC / MPR**:定义与差异见 4.2 节。

---

## 四、RWS 模块说明

### 4.1 中药不良反应(ADR)特征分析

输入 CSV 列(全部可选,缺失列对应分析自动跳过):

```
report_id, sex, age, age_group, route, drug_name, drug_category,
reaction, outcome, causality, severity, report_year, dose_days, report_source
```

**分母口径(关键)**:性别 / 年龄 / 给药途径 / 转归 / 关联性评价的构成比
分母是**报表数**;而涉及品种分析时,同一报表可涉及多品种,
故品种构成比的分母是**品种提及次数**。两个分母不可混用 —— 本工具分别
计算,并在输出字段名与报告中显式标注所用分母。

### 4.2 用药依从性(PDC / MPR / 持续性)

输入 CSV 列:`patient_id, fill_date, days_supply, drug_name`

设观察窗为 `[window_start, window_end]`,共 `D` 天:

```
MPR = 观察窗内供药总天数 / D          (截尾至 1,因重叠取药可使原始值 > 1)
PDC = 观察窗内被覆盖的不重复天数 / D   (≤ 1)
持续性 = 首次供药到允许间隙(grace period)内最后一次供药的间隔天数
```

> **PDC 与 MPR 的唯一差别来源是重叠处方的处理**:PDC 对同一天的
> 多张处方**只计一次**,因此不会被提前续方抬高,国际上通常以 PDC 为首选
> 指标,MPR 用于敏感性分析。测试中已分别验证"无重叠时 PDC == MPR"
> 与"有重叠时 PDC < MPR"两个不变量。
>
> **偏倚提示**:若观察窗由数据推导(取首末取药日),PDC/MPR 会**系统性
> 高估**依从性,因为停药后的空白期被排除在窗口之外。本工具在结果中返回
> `window_derived` 标记,并在报告中显式提示此偏倚方向。
>
> **数据质量控制**:`days_supply` 缺失或非正数的记录被**排除并计数**,
> 而不是默认按 1 天计入(后者会系统性低估依从性)。

### 4.3 中成药联合用药模式

输入 CSV 列:`patient_id, visit_date, drug_names, diagnosis`
(`drug_names` 支持 `;` `,` `、` `/` `|` `+` 多种分隔符)

输出:多药联用率、高频联用对、品种共现度(度中心性)、组内品种数分布。

> **关键限定**:共现(association)**不等于**相互作用(interaction)。
> 本模块只描述**处方层面的药品共存模式**,不推断配伍合理性、不评价安全性、
> 不构成用药建议。共现频次高也可能仅反映适应症聚集或临床路径习惯。

### 4.4 DRG/医保付费下中医药成本结构

支持**宽表**(每病组一行)与**长表**(项目明细,自动按病组汇总并按名称归类)两种输入。

输出:每病组支付标准(`权重 × 费率`)、实际成本、盈亏、中医类项目成本占比、
盈亏平衡费率,以及"中医成本占比 vs 盈亏率"的相关性(描述性)。

> **边界**:支付标准以当地医保部门正式文件为准。本模块仅基于使用者提供的
> 权重与费率做**算术推算**,不可用于实际结算。报告中的相关系数**不构成因果
> 推断** —— 病种、病情复杂度、收治结构等混杂因素未受控。

---

## 五、输出与工程说明

### 5.1 输出结构

```
output/
├── report.md              # Markdown 报告(含方法学与局限)
├── policy_analysis.xlsx   # 政策分析多 sheet
├── rws_analysis.xlsx      # RWS 分析多 sheet
├── csv/                   # 每个表单独一份 CSV(UTF-8-BOM,Excel 直接打开不乱码)
└── charts/                # PNG 图表(无 CJK 字体时自动改为 SVG)
```

### 5.2 图表与中文字体

matplotlib 默认字体(DejaVu Sans)**不含 CJK 字形**,中文标签会渲染为"豆腐块"
(□□□)。这属于**可见的图表缺陷**,不是化妆问题。本工具的处理:

1. 按优先级探测系统中文字体(微软雅黑 → 黑体 → 等线 → 宋体 …);
2. **实际渲染一个中文字符串并检查缺字警告**,通过后才采用;
3. 全部不可用时,**自动回退为手写 SVG**(SVG 由浏览器渲染文本,不存在缺字问题)。

本次运行的验证环境使用 `Microsoft YaHei`。渲染正确性通过像素级墨迹分析确认
(正常中文字形的墨迹游程宽度呈自然变化,而豆腐块会产生等高宽度的规则方块)。

### 5.3 降级策略(无硬依赖)

| 依赖 | 缺失时的行为 |
|---|---|
| `matplotlib` | 回退为手写 SVG(PNG/SVG 二选一,文件名扩展名相应变化) |
| `beautifulsoup4` | 回退为正则 + div 嵌套深度计数的正文抽取 |
| `scipy` | 用不完全伽马函数级数展开计算卡方上尾概率,p 值仍可用 |
| `pandas` / `XlsxWriter` | 跳过表格导出并给出警告,不影响分析本身 |

### 5.4 编码处理

全部文件 UTF-8。Windows 控制台默认可能是 GBK,中文输出会抛
`UnicodeEncodeError`,因此:

* 入口处 `sys.stdout.reconfigure(encoding='utf-8', errors='replace')` 包在
  `try/except (AttributeError, ValueError, OSError)` 中;
* 另有 `_safe_print()` 在 `UnicodeEncodeError` 时逐条降级为可编码字符兜底;
* 导出 CSV 使用 `utf-8-sig`,使 Excel 直接双击打开不乱码。

---

## 六、项目结构

```
TCMPolicyRWS/
├── tcm_rws.py                      # 根入口(CLI)
├── src/
│   ├── __init__.py
│   ├── db.py                       # SQLite + FTS5(trigram)+ LIKE 回退
│   ├── policy_crawler.py           # gov.cn 采集器与正文抽取
│   ├── policy_analysis.py          # 时间线 / 机关 / 主题 / 阶段 / 断点
│   ├── rws.py                      # RWS 通用工具 + 联合用药分析
│   ├── adr.py                      # ADR 特征分析 + 卡方检验(无 scipy 亦可)
│   ├── compliance.py               # PDC / MPR / 持续性
│   ├── drg_tcm.py                  # DRG/医保成本结构
│   ├── charts.py                   # 中文图表(matplotlib + SVG 回退)
│   ├── report.py                   # Markdown 报告生成
│   └── cli.py                      # 命令行实现
├── tests/
│   └── test_core.py                # 39 个单元测试(独立运行 + pytest 双模式)
├── examples/                       # 合成样例数据(均带显著声明)
│   ├── sample_adr_synthetic.csv
│   ├── sample_prescriptions_synthetic.csv
│   ├── sample_coprescribing_synthetic.csv
│   └── sample_drg_synthetic.csv
├── data/                           # 采集产生的 SQLite(gitignore)
├── output/                         # 生成物(gitignore)
├── LICENSE                         # MIT
└── README.md
```

---

## 七、已知局限(务必连同结果一并阅读)

### 7.1 政策语料

1. **语料不等于政策全集**。语料来自单一公开文件库的**标题**检索,未覆盖
   地方性文件、未被该库收录的文件,以及标题不含关键词的文件。
2. **收录完整度不均**。政策文件库的电子化回溯覆盖不均,**早期年份计数
   系统性偏低**。本工具在报告的年度分布旁显式列出"文件数 < 3 的年份",
   以提示该局限 —— "某年文件少"不能解释为"该年政策关注度低"。
3. **年度计数差异不等于政策强度差异**。断点检测是**描述性**提示,
   不构成因果推断:某年文件激增既可能源于政策密集出台,也可能源于文件库补录。
4. **主题归类依赖规则**。`THEME_RULES` 的边界直接决定主题占比;规则是
   公开可复核的,但不同规则会得出不同分布。
5. **里程碑核验不做补造**。若某预设里程碑在语料中未检索到,报告会明确标注
   "未收录",空缺即语料覆盖的真实边界,本工具不会补造文件。
6. **未纳入 NMPA/CDE/NHC 原始文件**(见 3.2 节)。

### 7.2 RWS 分析

1. **自发呈报数据存在漏报**与报告质量不均,**构成比不代表真实发生率**。
2. **缺少暴露人群分母**,无法计算发生率或相对风险。
3. **关联性评价是报告人/评价人的判断**,不等同于因果关系。
4. **处方数据无法观测实际服药行为** —— 持有药物不等于按时服药,
   故 PDC/MPR 是依从性的**代理指标**。
5. **PDC ≥ 80% 的阈值**源自心血管等慢性病领域的研究惯例,
   在中药疗程场景**缺乏专门验证**,仅作分组参考。
6. **联合用药共现不等于药物相互作用**。
7. **DRG 支付标准以当地医保文件为准**,本工具的推算不可用于实际结算。
8. **合成数据不具备实证含义**,不得引用为研究发现。

---

## 八、English Summary

**TCMPolicyRWS** is a reproducible analysis toolkit for **Traditional Chinese
Medicine (TCM) policy text mining** and **TCM real-world study (RWS)** research,
built for pharmacovigilance / pharmaceutical administration research.

**Policy module.** It crawls the Chinese government policy repository
(`www.gov.cn` only — `nmpa.gov.cn`, `cde.org.cn` and `nhc.gov.cn` sit behind a
JavaScript anti-bot WAF returning HTTP 412/202, and this tool deliberately does
**not** attempt to bypass it). Documents are matched by **title** against 12
TCM keywords, stored in SQLite with an **FTS5 `trigram`** full-text index, and
analysed for yearly timelines, issuing agencies, policy themes, phase shifts
and keyword evolution. Because `trigram` cannot index queries shorter than three
characters, two-character Chinese queries (e.g. 中医, 中药) **automatically fall
back to `LIKE` search** and the fallback is reported to the caller.

**RWS module.** It analyses user-supplied CSVs: TCM adverse-drug-reaction
characteristics (with explicit denominators and chi-square testing), medication
adherence via **PDC / MPR / persistence**, TCM co-prescribing patterns, and
TCM cost structure under **DRG-based payment**. Chi-square p-values are computed
without a hard `scipy` dependency, and Cramér's V is always reported alongside
p-values because significance alone is misleading at large sample sizes.

**No real patient data is required.** All files under `examples/` are clearly
labelled **synthetic** data, generated solely to exercise the code paths. They
carry no clinical or epidemiological meaning and must never be cited as findings.

**Honest limitations.** The corpus is title-matched from a single public
repository; it is **not** the complete set of Chinese TCM policies, and early
years are under-represented due to uneven digitisation. Yearly counts therefore
describe *this corpus*, not policy intensity. Co-prescription co-occurrence is
**not** drug interaction. DRG outputs are arithmetic on user-supplied inputs and
must not be used for actual reimbursement. The toolkit provides **no clinical
recommendations** and **no legal interpretation** of policy texts.

**Charts.** matplotlib renders Chinese labels using a detected CJK font
(verified by actually rendering glyphs and checking for missing-glyph warnings);
if no CJK font is available it falls back to hand-written SVG, where the browser
supplies the font, so tofu boxes cannot occur.

```
python tcm_rws.py crawl && python tcm_rws.py analyze   # policy pipeline
python tcm_rws.py rws --adr examples/sample_adr_synthetic.csv   # RWS pipeline
python tcm_rws.py demo                                  # everything
python tests/test_core.py                               # 39 unit tests
```

---

## 九、许可

MIT License — 见 [LICENSE](LICENSE)。作者:**leerogerstheman**。
