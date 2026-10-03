"""中医药政策与真实世界研究工具 (TCM Policy & Real-World Study Toolkit).

本包面向中医药政策文本挖掘与中医药真实世界研究(RWS)的量化分析,
服务于药事管理方向的实证研究需求。

模块划分
--------
db                 : SQLite 存储层,FTS5(trigram) 全文检索 + LIKE 回退
policy_crawler     : www.gov.cn 政策文件库采集器(遵守站点的正常访问礼仪)
policy_analysis    : 政策变迁时间线、发文机关、主题演变与阶段断点检测
charts             : 中文图表渲染(matplotlib + CJK 字体),无字体时回退 SVG
adr                : 中药不良反应/安全性特征分析
compliance         : 用药依从性 PDC/MPR 计算
rws                : 真实世界研究通用分析(联合用药模式等)
drg_tcm            : DRG/医保付费下中医药服务的成本结构分析
report             : Markdown 报告生成
cli                : 命令行入口

重要声明
--------
* 本工具**不采集、不推断、不发布**任何真实患者数据。RWS 模块的演示数据
  位于 ``examples/``,为明确标注的**合成数据**。
* 政策文本来自政府公开信息,工具仅做检索、归集与统计,不改变原文,
  不对政策条文作法律解释。
* 本工具输出**不构成临床建议、医保结算依据或法律意见**。
"""

from __future__ import annotations

__version__ = "1.0.0"
__author__ = "leerogerstheman"

__all__ = ["__version__", "__author__"]
