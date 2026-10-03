"""中国政府网(www.gov.cn)政策文件库采集器。

数据源与访问边界
----------------
本模块**只**访问中国政府网政策文件库的公开 JSON 检索接口与公开文章页:

* 检索接口 ``https://sousuo.www.gov.cn/search-gov/data``
  - ``t=zhengcelibrary_gw`` 国务院文件(政府文件)
  - ``t=zhengcelibrary_bm`` 部门文件
* 文章页正文位于 ``<div id="UCAP-CONTENT">``,为服务端渲染,可直接解析。

**明确不做的事**:不使用代理池、不伪造 UA 以绕过风控、不请求
nmpa.gov.cn / cde.org.cn / nhc.gov.cn 等部署了 JS 反爬(WAF, 返回 412/202)
的站点 —— 绕过反爬既违反站点意愿也不稳定,本工具不提供该能力。
对 gov.cn 亦保持礼貌访问:固定间隔 sleep,失败指数退避,不并发轰炸。

解析注意事项(踩坑记录)
----------------------
gov.cn 的文章页 HTML 存在不规范嵌套,实验表明 **lxml 解析器会静默丢失
``UCAP-CONTENT`` 节点**(返回 None 而不报错),而标准库 ``html.parser``
可正确解析。因此正文抽取固定使用 ``html.parser``,并在 __init__ 中回退。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from . import db

# ---------------------------------------------------------------------------
# 检索关键词集
# ---------------------------------------------------------------------------
# 设计思路:仅用"中医药"一词会大幅漏检。政策文本中同一议题常以
# 单字词根("中药""中医")、具体对象("中药饮片""经典名方""中成药")、
# 或制度概念("中西医结合""民族药""治未病")出现。
# 关键词按用途分层,便于报告里说明检索策略的可复现性。
DEFAULT_KEYWORDS: tuple[str, ...] = (
    # 一级词根 —— 覆盖面最广
    "中医药",
    "中药",
    "中医",
    # 具体监管对象与制度概念
    "经典名方",
    "中药饮片",
    "中西医结合",
    "中成药",
    "民族药",
    "中医医院",
    "治未病",
    "中医药服务",
    "中药配方颗粒",
)

# 检索接口类型 -> 中文标签
SOURCES: dict[str, str] = {
    "zhengcelibrary_gw": "国务院文件",
    "zhengcelibrary_bm": "部门文件",
}

SEARCH_ENDPOINT = "https://sousuo.www.gov.cn/search-gov/data"

_UA = "TCMPolicyRWS/1.0 (research tool; polite crawler; contact: local user)"
_TAG_RE = re.compile(r"<[^>]+>")
# 空白归一化:gov.cn 的 puborg 字段混用普通空格、\u2002(EN SPACE)、
# \u00a0(NBSP)、\u3000(全角空格)作为多机关分隔符。这些不可见字符若不归一,
# 同一个机构会因分隔符不同而被算作不同实体,按机关聚合随即失效。
_WS_RE = re.compile(r"[ \t\u00a0\u2002\u2003\u2009\u3000]+")
_MULTI_WS_RE = re.compile(r"\s{2,}")


# ---------------------------------------------------------------------------
# 发文机关归一化
# ---------------------------------------------------------------------------
# 为什么必须做归一化:gov.cn 的 puborg 字段是自由文本,同一机构存在大量变体
# (「中医药局」「国家中医药局」「国家中医药管理局」「国家中医药管理局医政司」…),
# 且多部门联合发文被塞进同一个字符串,用空格 / \u2002 等混合分隔。
# 不归一化时按机关聚合会产生 80+ 个"机关",其中多数实为同一机构,
# 统计结果不可用。
#
# 归一化策略(两段式,可复核):
#   1) 按分隔符切成多个机关名,每个分别做别名映射到规范名
#   2) 规范名去重后:第一个作为"主要发文机关",其余作为"参与机关"
# 保留原始字符串在 puborg_raw 字段,保证任何统计数字都能回溯核验。

# 别名表:键为规范名,值为该机构的各种写法(子串匹配,故按长度降序尝试)
ORG_ALIASES: dict[str, tuple[str, ...]] = {
    "国家中医药管理局": (
        "国家中医药管理局", "国家中医药局", "中医药管理局", "中医药局",
        "国家中医药管理", "中医局",
    ),
    "国家药品监督管理局": (
        "国家药品监督管理局", "国家药监局", "药品监督管理局", "药监局",
        "国家食品药品监督管理总局", "食品药品监管总局",
    ),
    "国家卫生健康委员会": (
        "国家卫生健康委员会", "国家卫生健康委", "国家卫生计生委",
        "卫生和计划生育委员会", "卫生健康委", "卫生部", "卫生厅", "卫健委",
    ),
    "国家医疗保障局": (
        "国家医疗保障局", "国家医保局", "医疗保障局", "医保局",
    ),
    "国家发展和改革委员会": (
        "国家发展和改革委员会", "国家发展改革委", "发展和改革委员会",
        "发展改革委", "发改委",
    ),
    "国家市场监督管理总局": (
        "国家市场监督管理总局", "国家市场监管总局", "市场监督管理总局",
        "市场监管总局",
    ),
    "国家疾病预防控制局": (
        "国家疾病预防控制局", "国家疾控局", "疾病预防控制局", "疾控局",
    ),
    "国务院": ("国务院办公厅", "国务院"),
    "全国人民代表大会常务委员会": (
        "全国人民代表大会常务委员会", "全国人大常委会", "全国人大",
    ),
    "中共中央": ("中共中央办公厅", "中共中央", "中央委员会"),
    "教育部": ("教育部",),
    "科学技术部": ("科学技术部", "科技部"),
    "工业和信息化部": ("工业和信息化部", "工信部"),
    "财政部": ("财政部",),
    "人力资源和社会保障部": ("人力资源和社会保障部", "人力资源社会保障部", "人社部"),
    "农业农村部": ("农业农村部", "农业部"),
    "商务部": ("商务部",),
    "民政部": ("民政部",),
    "文化和旅游部": ("文化和旅游部", "文化部", "国家旅游局"),
    "国家林业和草原局": ("国家林业和草原局", "国家林草局", "林业局"),
    "国家知识产权局": ("国家知识产权局", "知识产权局"),
    "中国气象局": ("中国气象局", "气象局"),
    "国家标准化管理委员会": ("国家标准化管理委员会", "标准委"),
    "国家宗教事务局": ("国家宗教事务局", "宗教局"),
    "国务院国有资产监督管理委员会": ("国务院国有资产监督管理委员会", "国资委"),
    "生态环境部": ("生态环境部", "环境保护部", "环保部"),
    "自然资源部": ("自然资源部", "国土资源部"),
    "住房和城乡建设部": ("住房和城乡建设部", "住建部"),
    "交通运输部": ("交通运输部",),
    "水利部": ("水利部",),
    "应急管理部": ("应急管理部",),
    "海关总署": ("海关总署",),
    "国家税务总局": ("国家税务总局", "税务总局"),
    "国家体育总局": ("国家体育总局", "体育总局"),
    "国家统计局": ("国家统计局", "统计局"),
    "中国科学院": ("中国科学院",),
    "中国工程院": ("中国工程院",),
    "中国残疾人联合会": ("中国残疾人联合会", "残联"),
    "中华全国总工会": ("中华全国总工会", "总工会"),
    "共青团中央": ("共青团中央",),
}

# 预处理:把别名按长度降序排好,避免短别名(如"卫生部")抢先匹配长别名
_ALIAS_INDEX: list[tuple[str, str]] = sorted(
    ((alias, canon) for canon, aliases in ORG_ALIASES.items() for alias in aliases),
    key=lambda t: -len(t[0]),
)

# 多机关分隔符:归一化空白后,用这些字符切分
_ORG_SPLIT_RE = re.compile(r"[、,，;；/\n]+|\s{1,}|和(?=[\u4e00-\u9fff]*局)")


def normalize_org_name(name: str) -> str:
    """把单个机关名映射到规范名。无法匹配时返回去除内部司局后缀的原文。

    对未收录的机构,剥掉"办公厅/办公室/综合司/医政司"等**内设机构后缀** ——
    这些后缀不改变发文主体的身份,保留会让同一机构分裂成多个。
    """
    n = _WS_RE.sub(" ", (name or "")).strip()
    if not n:
        return ""
    for alias, canon in _ALIAS_INDEX:
        if alias in n:
            return canon
    # 未收录:剥离内设机构后缀,保留主体
    for suffix in ("办公厅", "办公室", "综合司", "医政司", "人事司", "规划司",
                   "科技司", "计财司", "政策法规司", "监督司", "医政医管局",
                   "秘书局", "秘书处", "党组"):
        if n.endswith(suffix) and len(n) > len(suffix):
            n = n[: -len(suffix)]
            break
    return n


def split_orgs(puborg: str) -> list[str]:
    """把联合发文机关串切成规范化的机关列表(去重、保序)。

    处理流程:归一化不可见空白 -> 按分隔符切分 -> 逐个映射规范名 -> 去重。
    返回的第一个元素即"主要发文机关"。
    """
    if not puborg:
        return []
    s = _WS_RE.sub(" ", puborg).strip()
    parts = [p.strip() for p in _ORG_SPLIT_RE.split(s) if p and p.strip()]
    out: list[str] = []
    for p in parts:
        canon = normalize_org_name(p)
        if canon and canon not in out:
            out.append(canon)
    return out


@dataclass
class CrawlStats:
    """一次采集运行的统计,用于如实汇报产出与失败情况。"""

    queries_run: int = 0
    items_seen: int = 0
    items_new: int = 0
    bodies_fetched: int = 0
    bodies_failed: int = 0
    pages_fetched: int = 0
    errors: list[str] = field(default_factory=list)
    per_query_total: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "queries_run": self.queries_run,
            "items_seen": self.items_seen,
            "items_new": self.items_new,
            "bodies_fetched": self.bodies_fetched,
            "bodies_failed": self.bodies_failed,
            "pages_fetched": self.pages_fetched,
            "n_errors": len(self.errors),
            "per_query_total": dict(self.per_query_total),
        }


# ---------------------------------------------------------------------------
# HTTP 基础
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 30, retries: int = 3) -> bytes | None:
    """带指数退避的 GET。失败返回 None(由调用方决定是否记为错误)。

    不抛异常而是返回 None:单条文章页失败不应中断整个采集任务。
    """
    delay = 1.0
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
    if last is not None:
        return None
    return None


def _clean_title(title: str) -> str:
    """检索结果中的标题含 ``<em>`` 高亮标签,需剥离后入库。"""
    return _WS_RE.sub(" ", _TAG_RE.sub("", title or "")).strip()


def parse_pubdate(raw: str) -> str:
    """把接口返回的 ``2025.03.20`` 归一为 ISO ``2025-03-20``。

    解析失败返回空串而非抛异常 —— 日期缺失不应导致整条记录被丢弃。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = re.match(r"(\d{4})[.\-/年](\d{1,2})[.\-/月](\d{1,2})", raw)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    m = re.match(r"(\d{4})[.\-/年](\d{1,2})", raw)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        try:
            return datetime(y, mo, 1).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return ""


# ---------------------------------------------------------------------------
# 检索接口
# ---------------------------------------------------------------------------

def search_policies(
    keyword: str,
    *,
    page: int = 1,
    page_size: int = 20,
    source: str = "zhengcelibrary_bm",
    field: str = "title",
    timeout: int = 30,
) -> tuple[list[dict[str, Any]], int]:
    """调用政策文件库检索接口,返回 ``(条目列表, 命中总数)``。

    ``searchfield=title`` 限定标题匹配。标题命中比全文命中噪音小得多
    (全文匹配常因附件表格里偶然出现"中药"二字而引入大量非政策域文件),
    这对政策变迁统计的准确性至关重要。
    """
    if source not in SOURCES:
        raise ValueError(f"未知来源: {source};可选 {list(SOURCES)}")
    qs = urllib.parse.urlencode(
        {
            "t": source,
            "q": keyword,
            "timetype": "timeqb",   # 全部时间范围
            "sort": "pubtime",
            "sortType": "1",
            "searchfield": field,
            "p": page,
            "n": page_size,
        }
    )
    raw = _http_get(f"{SEARCH_ENDPOINT}?{qs}", timeout=timeout)
    if raw is None:
        return [], 0
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return [], 0
    # 命中为空时接口仍返回 JSON,但 searchVO 为 null —— 必须显式判空,
    # 否则 None['totalCount'] 会抛 TypeError。
    sv = payload.get("searchVO")
    if not sv:
        return [], 0
    items = sv.get("listVO") or []
    return items, int(sv.get("totalCount") or 0)


def normalize_item(item: dict[str, Any], keyword: str, source: str) -> dict[str, Any]:
    """把接口条目转成入库字典(含发文机关归一化)。"""
    title = _clean_title(item.get("title", ""))
    url = (item.get("url") or "").strip()
    pubdate = parse_pubdate(item.get("pubtimeStr") or item.get("pubtime") or "")
    puborg_raw = _WS_RE.sub(" ", (item.get("puborg") or item.get("fwdw") or "")).strip()
    orgs = split_orgs(puborg_raw)
    return {
        # doc_key 优先用 URL(唯一且稳定);无 URL 时用 标题+日期 兜底
        "doc_key": url or f"{title}|{pubdate}",
        "title": title,
        "pcode": (item.get("pcode") or item.get("fwzh") or "").strip(),
        # puborg 存归一化后的主要发文机关,便于直接聚合;
        # 原始串与完整机关列表另存,保证可回溯核验。
        "puborg": orgs[0] if orgs else puborg_raw,
        "puborg_raw": puborg_raw,
        "puborg_all": "、".join(orgs),
        "n_orgs": len(orgs),
        "pubdate": pubdate,
        "doc_type": SOURCES[source],
        "category": (item.get("childtype") or "").strip(),
        "source": "中国政府网政策文件库",
        "url": url,
        "summary": _WS_RE.sub(" ", (item.get("summary") or "")).strip(),
        "matched_keyword": keyword,
    }


# ---------------------------------------------------------------------------
# 正文抽取
# ---------------------------------------------------------------------------

def extract_body(html: str) -> str:
    """从 gov.cn 文章页抽取正文纯文本(``div#UCAP-CONTENT``)。

    固定使用 ``html.parser``:见模块 docstring 关于 lxml 静默失败的说明。
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        return _extract_body_regex(html)

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return _extract_body_regex(html)
    node = soup.find(id="UCAP-CONTENT")
    if node is None:
        return _extract_body_regex(html)
    text = node.get_text("\n", strip=True)
    return _normalize_body(text)


def _extract_body_regex(html: str) -> str:
    """无 bs4 时的回退:按 div 嵌套深度配对,截取 UCAP-CONTENT 内容。

    gov.cn 正文内部还会嵌套 div,故不能简单取到第一个 ``</div>``,
    必须做深度计数。
    """
    m = re.search(r'<div[^>]*id="UCAP-CONTENT"[^>]*>', html)
    if not m:
        return ""
    start = m.end()
    depth, i = 1, start
    for tag in re.finditer(r"<(/?)div\b[^>]*>", html[start:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            i = start + tag.start()
            break
    else:
        i = min(len(html), start + 400_000)
    inner = html[start:i]
    inner = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", inner)
    inner = re.sub(r"(?i)<br\s*/?>|</p>", "\n", inner)
    return _normalize_body(_TAG_RE.sub(" ", inner))


def _normalize_body(text: str) -> str:
    lines = [_WS_RE.sub(" ", ln).strip() for ln in (text or "").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def fetch_body(url: str, timeout: int = 30) -> str:
    """下载并抽取单篇文章正文。失败返回空串。"""
    raw = _http_get(url, timeout=timeout)
    if raw is None:
        return ""
    # gov.cn 页面为 UTF-8;charset 声明缺失时按 UTF-8 处理,错误字符替换,
    # 避免个别坏字节让整篇正文丢失。
    return extract_body(raw.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# 主采集流程
# ---------------------------------------------------------------------------

def crawl(
    db_path: str,
    keywords: Iterable[str] = DEFAULT_KEYWORDS,
    *,
    sources: Iterable[str] = ("zhengcelibrary_bm", "zhengcelibrary_gw"),
    max_pages: int = 3,
    page_size: int = 20,
    with_body: bool = True,
    sleep: float = 1.0,
    max_docs: int | None = None,
    verbose: bool = True,
) -> CrawlStats:
    """采集中医药相关政策文件并写入 SQLite。

    参数
    ----
    max_pages   : 每个关键词在每个来源翻页上限(每页 page_size 条)。
    with_body   : 是否抓取正文。正文用于主题词频与全文检索,建议开启;
                  关闭可大幅提速但会削弱政策主题演变分析的证据基础。
    max_docs    : 正文抓取条数上限(用于控制运行时长)。
    sleep       : 每次请求之间的间隔秒数,保持礼貌访问。

    Returns
    -------
    CrawlStats
    """
    keywords = list(keywords)
    sources = list(sources)
    stats = CrawlStats()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn = db.connect(db_path)
    try:
        for source in sources:
            for kw in keywords:
                key = f"{source}:{kw}"
                for page in range(1, max_pages + 1):
                    items, total = search_policies(
                        kw, page=page, page_size=page_size, source=source
                    )
                    time.sleep(sleep)
                    if page == 1:
                        stats.per_query_total[key] = total
                        if verbose:
                            print(f"  [{SOURCES[source]}] {kw}: 命中 {total} 条")
                    if not items:
                        break
                    for item in items:
                        stats.items_seen += 1
                        doc = normalize_item(item, kw, source)
                        if not doc["title"]:
                            continue
                        is_new = db.upsert_policy(conn, doc)
                        if is_new:
                            stats.items_new += 1
                    conn.commit()
        stats.queries_run = len(keywords) * len(sources)

        if with_body:
            _fetch_bodies(conn, stats, sleep=sleep, max_docs=max_docs, verbose=verbose)

        # 正文抓取完成后统一做主题标注与关键词抽取
        from .policy_analysis import annotate_themes, extract_keywords_bulk

        n_themed = annotate_themes(conn)
        n_kw = extract_keywords_bulk(conn)
        if verbose:
            print(f"  主题标注 {n_themed} 篇;关键词抽取 {n_kw} 篇")
        conn.commit()
    finally:
        ended = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            db.log_crawl(
                conn,
                started_at=started,
                ended_at=ended,
                source=",".join(sources),
                query=",".join(keywords),
                total_count=sum(stats.per_query_total.values()),
                n_new=stats.items_new,
                n_seen=stats.items_seen,
                note=json.dumps(stats.as_dict(), ensure_ascii=False),
            )
        except Exception:
            pass
        conn.close()
    return stats


def _fetch_bodies(
    conn: Any, stats: CrawlStats, *, sleep: float, max_docs: int | None, verbose: bool
) -> None:
    """为缺正文的记录补齐正文。

    只处理尚未成功抓取(body_status != 'ok')的记录 —— 重复运行时自动跳过
    已成功的页面,使采集可增量续跑而不重复消耗对方站点资源。

    **失败必须显式落库**(body_status='failed' + body_error),而不是静默写空:
    否则"正文为空"就无法区分"尚未抓取"与"抓取失败",
    会让下游误以为该文件确实没有正文。
    """
    rows = conn.execute(
        """SELECT id, url, title FROM policy
           WHERE COALESCE(body_status,'pending') <> 'ok'
             AND COALESCE(url,'') <> ''
           ORDER BY pubdate DESC"""
    ).fetchall()
    if max_docs is not None:
        rows = rows[:max_docs]
    if verbose and rows:
        print(f"  抓取正文:{len(rows)} 篇 ...")
    for i, row in enumerate(rows, 1):
        html = _http_get(row["url"])
        stats.pages_fetched += 1
        if html is None:
            stats.bodies_failed += 1
            msg = "HTTP 下载失败(重试后仍无响应)"
            stats.errors.append(f"{msg}: {row['url']}")
            conn.execute(
                "UPDATE policy SET body_status='failed', body_error=? WHERE id=?",
                (msg, row["id"]),
            )
        else:
            body = extract_body(html.decode("utf-8", errors="replace"))
            # 抓取后必须校验非空 —— 空串意味着正文节点缺失或解析失败,
            # 属于失败而非"该文件没有正文"。
            if body and len(body) >= 20:
                conn.execute(
                    """UPDATE policy SET body=?, body_len=?, body_status='ok',
                                         body_error=NULL WHERE id=?""",
                    (body, len(body), row["id"]),
                )
                stats.bodies_fetched += 1
            else:
                stats.bodies_failed += 1
                msg = (f"正文抽取结果过短或为空(len={len(body)}),"
                       f"疑似 UCAP-CONTENT 节点缺失")
                stats.errors.append(f"{msg}: {row['url']}")
                conn.execute(
                    "UPDATE policy SET body_status='failed', body_error=? WHERE id=?",
                    (msg, row["id"]),
                )
        if i % 20 == 0:
            conn.commit()
            if verbose:
                print(f"    {i}/{len(rows)}")
        time.sleep(sleep)
    conn.commit()
    if verbose:
        print(f"  正文抓取完成:成功 {stats.bodies_fetched},失败 {stats.bodies_failed}")


if __name__ == "__main__":  # 便于单文件快速自检
    import sys

    kw = sys.argv[1] if len(sys.argv) > 1 else "中医药"
    items, total = search_policies(kw, page_size=5)
    print(f"关键词 {kw!r}: 命中 {total} 条,本页 {len(items)} 条")
    for it in items[:5]:
        d = normalize_item(it, kw, "zhengcelibrary_bm")
        print(" -", d["pubdate"], d["puborg"], d["title"][:50])
