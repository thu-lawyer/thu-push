"""site_source — 清华官网静态栏目源（信息源清单第二类「清华官方网站」接入层）。

2026-10-11 起接入《清华信息源清单》（134 个微信公众号 + 50 个官网栏目）：
公众号侧走 we-mp-rss 引擎（gzh_source.py），官网栏目分散在
www.tsinghua.edu.cn 及各院系/部门子站，页面结构各异。本模块维护一张
**已核实**站点注册表 ``SITES``：每个源的列表页 URL 与解析函数都必须先在
真实页面上取证、配 fixture 测试后才允许注册 —— 未核实的源进不来，
防止「静默空转」。

已注册：
- xykd   清华大学校园看点（https://www.tsinghua.edu.cn/xykd.htm，静态页，
         每页 12 张活动卡片）。清单中的「新清华学堂演出活动」由该页演出类
         卡片覆盖，暂不单列。

条目契约（与 campus 主链路同构，走 store.upsert_items 主通道）：
- id           = f"{site_id}-{文章数字id}"，跨次运行稳定，按 id 幂等去重；
- published_at = 首次入库时刻（ISO+08:00）。卡片上的活动时间是**事件**语义
  而非发布时间，未来时间戳会落出已发送窗口，故入库时间才进 published_at，
  活动时间走 detail 预填（ai_event_time），由既有 timeparse/渲染链路消费；
- detail       = 预填 {ai_event_time, ai_event_location, ai_summary,
  detail_status="complete"}，使 enrich（pending_enrich 按
  detail_status != 'complete' 过滤）永不为本模块条目请求 pkuknow 详情端点。

故障隔离：本模块抛出的所有异常由 cmd_fetch 捕获记入 anomalies（error 级，
走既有 failure mail 通道），不影响同轮 campus/gzh 抓取结果。
"""
from __future__ import annotations

import html as _html
import re
import urllib.request
from datetime import datetime

XYKD_URL = "https://www.tsinghua.edu.cn/xykd.htm"
XYKD_ORIGIN = "https://www.tsinghua.edu.cn"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class SiteSourceError(Exception):
    """站点源抓取/解析失败（网络错误、结构漂移、空页面）。"""


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", "", fragment)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_html(url: str, timeout: int = 20, retries: int = 3) -> str:
    """抓取列表页 HTML。全部尝试失败抛 SiteSourceError。仅用标准库。"""
    last_exc: Exception | None = None
    for _attempt in range(max(1, int(retries))):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except Exception as exc:  # URLError / HTTPError / timeout / socket
            last_exc = exc
    raise SiteSourceError(
        f"{url} 抓取失败（重试 {max(1, int(retries))} 次）: "
        f"{type(last_exc).__name__}: {last_exc}"
    )


#: xykd 卡片：li 块内含 info/1002/<aid>.htm 链接。页脚导航 li 不含该路径，
#: 天然排除（首轮实抓验证：裸 <li> 会捞进整页导航）。
_LI_RE = re.compile(
    r"<li[^>]*>(?:(?!</li>).)*?info/1002/(?P<aid>\d+)\.htm(?:(?!</li>).)*?</li>",
    re.S,
)
_LINK_TITLE_RE = re.compile(
    r'<a[^>]*href="(?P<href>[^"]*info/1002/\d+\.htm)"[^>]*title="(?P<title>[^"]*)"'
)
_LINK_ANY_RE = re.compile(
    r'<a[^>]*href="(?P<href>[^"]*info/1002/\d+\.htm)"[^>]*>(.*?)</a>', re.S
)
_SPAN_RE = re.compile(r"<span[^>]*>(.*?)</span>", re.S)
_TEXT1_RE = re.compile(r'<div class="text1">(.*?)</div>', re.S)
_TEXT2_RE = re.compile(r'<div class="text2">(.*?)</div>', re.S)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)


def parse_xykd(
    page_html: str, *, now: datetime, source_name: str = "清华大学校园看点"
) -> list[dict]:
    """解析校园看点卡片，返回 upsert_items 可直接消费的条目列表。

    防静默漂移双闸（缺一不可，均为硬失败）：
    - 页面完全没有 info/1002 链接 → 改版/不可用；
    - 有 info/1002 链接但一张卡都解析不出 → 结构漂移。
    """
    raw_hits = len(re.findall(r"info/1002/\d+\.htm", page_html))
    items: list[dict] = []
    seen: set[str] = set()
    for li_m in _LI_RE.finditer(page_html):
        li = li_m.group(0)
        aid = li_m.group("aid")
        if aid in seen:  # 同一卡片的第二个链接（图+文双锚点）
            continue
        link = _LINK_TITLE_RE.search(li) or _LINK_ANY_RE.search(li)
        if not link:
            continue
        seen.add(aid)
        titled = _LINK_TITLE_RE.search(li)
        if titled:
            title = _strip_tags(titled.group("title"))
        else:
            title = _strip_tags(link.group(2) or "")
        if not title:
            title = f"xykd-{aid}"
        href = link.group("href")
        if href.startswith(("http://", "https://")):
            url = href
        elif href.startswith("/"):
            url = XYKD_ORIGIN + href
        else:
            url = XYKD_ORIGIN + "/" + href
        text1 = _TEXT1_RE.search(li)
        summary = ""
        if text1:
            ps = _P_RE.findall(text1.group(1))
            if ps:
                summary = _strip_tags(ps[-1])
        text2 = _TEXT2_RE.search(li)
        event_time = ""
        location = ""
        if text2:
            ps = [_strip_tags(p) for p in _P_RE.findall(text2.group(1))]
            if ps:
                event_time = ps[0]
            if len(ps) > 1:
                location = ps[1]
        span_m = _SPAN_RE.search(li)
        category = _strip_tags(span_m.group(1)) if span_m else ""
        items.append(
            {
                "id": f"xykd-{aid}",
                "source_id": "xykd",
                "source_name": source_name,
                "title": title,
                "published_at": now.isoformat(),
                "url": url,
                "category": category or "校园看点",
                "intent_group": "activity",
                "english_title": "",
                "english_source": "",
                "detail": {
                    "ai_event_time": event_time,
                    "ai_event_location": location,
                    "ai_summary": summary,
                    "detail_status": "complete",
                },
            }
        )
    if raw_hits == 0:
        raise SiteSourceError(
            "xykd 页面无任何 info/1002 链接——可能改版或不可用，不能当成静默成功"
        )
    if not items:
        raise SiteSourceError(
            f"xykd 页面含 {raw_hits} 处 info/1002 链接但解析出 0 张卡片——结构漂移"
        )
    return items


#: 已核实站点注册表。新增源的流程：真实页面取证 → 解析函数 + fixture 测试
#: → 注册于此 → ND_SITE_SOURCES 追加 id（或直接依赖此处注册后的默认启用）。
SITES: dict[str, dict] = {
    "xykd": {
        "name": "清华大学校园看点",
        "list_url": XYKD_URL,
        "parser": parse_xykd,
    },
}


def ingest_site(
    store,
    site_id: str,
    *,
    now: datetime,
    timeout: int = 20,
    retries: int = 3,
) -> dict:
    """抓取并入库一个站点源，返回 {id,name,collected,new,known}。

    条目预填 detail（detail_status=complete）并逐条 update_detail 落库，
    使 enrich 池永不包含站点条目。store 为 notice_digest.store.Store。
    """
    try:
        site = SITES[site_id]
    except KeyError:
        raise SiteSourceError(
            f"未知站点源 {site_id}（可用: {', '.join(sorted(SITES))}）"
        ) from None
    page_html = fetch_html(site["list_url"], timeout=timeout, retries=retries)
    items = site["parser"](page_html, now=now)
    new, known = store.upsert_items(items)
    for it in items:
        detail = it.get("detail")
        if detail:
            store.update_detail(str(it["id"]), detail)
    return {
        "id": site_id,
        "name": site["name"],
        "collected": len(items),
        "new": new,
        "known": known,
    }
