"""site_source 测试：xykd 解析、防漂移双闸、入库幂等与 enrich 免疫。"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from notice_digest import site_source, store as store_module
from notice_digest.config import load_config

NOW = datetime(2026, 10, 11, 23, 0, tzinfo=timezone(timedelta(hours=8)))

FIXTURE = """
<html><body>
<ul class="news">
<li>
 <div class="text1"><span>读书活动</span><a href="/info/1002/128619.htm" title="清华读书文化月开幕">清华读书文化月开幕</a><p>2026年秋季读书文化月启动，欢迎师生参加。</p></div>
 <div class="text2"><p class="de">2026年10月13日 15:00</p><a href="#">地点</a><p>图书馆老馆</p></div>
</li>
<li>
 <div class="text1"><span>展览</span><a href="/info/1002/128549.htm" title="艺博特展">艺博特展开幕</a><p>长期展览。</p></div>
 <div class="text2"><p class="de">2026年7月10日–2026年12月6日</p><a href="#">地点</a><p>艺术博物馆</p></div>
</li>
<li>
 <div class="text1"><span>演出</span><a href="https://www.tsinghua.edu.cn/info/1002/128500.htm" title="新清华学堂演出">音乐会</a><p>简介。</p></div>
 <div class="text2"><p class="de">2026年10月20日 19:00</p></div>
</li>
<li><a href="/xxgk/list.htm">学校概况</a></li>
</ul>
</body></html>
"""


class TestParseXykd(unittest.TestCase):
    def test_cards(self):
        items = site_source.parse_xykd(FIXTURE, now=NOW)
        self.assertEqual(len(items), 3)
        first = items[0]
        self.assertEqual(first["id"], "xykd-128619")
        self.assertEqual(first["source_name"], "清华大学校园看点")
        self.assertEqual(first["title"], "清华读书文化月开幕")
        self.assertEqual(first["category"], "读书活动")
        self.assertEqual(first["url"], "https://www.tsinghua.edu.cn/info/1002/128619.htm")
        self.assertEqual(first["published_at"], NOW.isoformat())
        self.assertEqual(first["intent_group"], "activity")
        self.assertEqual(first["detail"]["ai_event_time"], "2026年10月13日 15:00")
        self.assertEqual(first["detail"]["ai_event_location"], "图书馆老馆")
        self.assertIn("读书文化月", first["detail"]["ai_summary"])
        # 区间时间原样保留（timeparse 上层消费）
        self.assertEqual(
            items[1]["detail"]["ai_event_time"], "2026年7月10日–2026年12月6日"
        )
        # 绝对链接不重复拼 origin；缺地点段留空
        self.assertEqual(
            items[2]["url"], "https://www.tsinghua.edu.cn/info/1002/128500.htm"
        )
        self.assertEqual(items[2]["detail"]["ai_event_location"], "")

    def test_dedupe_same_aid(self):
        items = site_source.parse_xykd(FIXTURE + FIXTURE, now=NOW)
        self.assertEqual(len(items), 3)

    def test_no_title_attr_fallback(self):
        html = (
            '<ul><li><div class="text1"><span>讲座</span>'
            '<a href="/info/1002/129001.htm">无title属性的标题</a></div>'
            '<div class="text2"><p class="de">2026年10月15日 14:00</p></div></li></ul>'
        )
        items = site_source.parse_xykd(html, now=NOW)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "无title属性的标题")

    def test_drift_raises(self):
        # 有 info/1002 链接但解析不出卡片（缺 text1 结构）→ 硬失败
        html = "<ul><li><a href='/info/1002/999.htm'>x</a></li></ul>"
        with self.assertRaises(site_source.SiteSourceError):
            site_source.parse_xykd(html, now=NOW)

    def test_empty_page_raises(self):
        with self.assertRaises(site_source.SiteSourceError):
            site_source.parse_xykd("<html><body>维护中</body></html>", now=NOW)


class TestIngestRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_module.Store(Path(self.tmp.name) / "t.db")
        self.store.init_schema()
        self._orig_fetch = site_source.fetch_html

    def tearDown(self):
        site_source.fetch_html = self._orig_fetch
        self.tmp.cleanup()

    def test_ingest_twice_and_enrich_immunity(self):
        site_source.fetch_html = lambda *a, **k: FIXTURE
        stats = site_source.ingest_site(self.store, "xykd", now=NOW)
        self.assertEqual(stats["new"], 3)
        self.assertEqual(stats["known"], 0)
        stats2 = site_source.ingest_site(self.store, "xykd", now=NOW)
        self.assertEqual(stats2["new"], 0)
        self.assertEqual(stats2["known"], 3)
        row = self.store.conn.execute(
            "SELECT detail_status, intent_group, source_name FROM items "
            "WHERE id='xykd-128619'"
        ).fetchone()
        self.assertEqual(row["detail_status"], "complete")
        self.assertEqual(row["intent_group"], "activity")
        self.assertEqual(row["source_name"], "清华大学校园看点")
        # 关键免疫断言：站点条目永不进 enrich 池
        self.assertEqual(self.store.pending_enrich(50), [])

    def test_unknown_site_raises(self):
        with self.assertRaises(site_source.SiteSourceError):
            site_source.ingest_site(self.store, "nosuch", now=NOW)


class TestSiteConfig(unittest.TestCase):
    def test_defaults(self):
        # 站点源 opt-in：未配置必须关闭——任何环境不得「克隆即连真站」
        cfg = load_config(Path("/nonexistent.yaml"), Path("/nonexistent.env"))
        self.assertFalse(cfg.site_enabled)
        self.assertEqual(cfg.site_sources, ["xykd"])

    def test_env_enables(self):
        os.environ["ND_SITE_ENABLED"] = "1"
        try:
            cfg = load_config(Path("/nonexistent.yaml"), Path("/nonexistent.env"))
            self.assertTrue(cfg.site_enabled)
        finally:
            os.environ.pop("ND_SITE_ENABLED", None)

    def test_env_overrides(self):
        os.environ["ND_SITE_ENABLED"] = "0"
        os.environ["ND_SITE_SOURCES"] = "xykd, foo"
        try:
            cfg = load_config(Path("/nonexistent.yaml"), Path("/nonexistent.env"))
            self.assertFalse(cfg.site_enabled)
            self.assertEqual(cfg.site_sources, ["xykd", "foo"])
        finally:
            os.environ.pop("ND_SITE_ENABLED", None)
            os.environ.pop("ND_SITE_SOURCES", None)


if __name__ == "__main__":
    unittest.main()
