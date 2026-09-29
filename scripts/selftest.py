#!/usr/bin/env python3
"""离线自测：不依赖网络，覆盖解析 / 归类 / 评分 / 导出 / 存储 / 探测解析逻辑。

    python3 scripts/selftest.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from iptvhub import export, rank  # noqa: E402
from iptvhub.classify import Classifier  # noqa: E402
from iptvhub.config import load_groups  # noqa: E402
from iptvhub.parser import NoiseFilter, parse_playlist  # noqa: E402
from iptvhub.netclient import HttpClient, encode_url  # noqa: E402
from iptvhub.probe import ProbeResult, Prober, parse_master, parse_media, sniff_kind  # noqa: E402
from iptvhub.store import Store  # noqa: E402
from iptvhub.videoinfo import (detect_resolution, find_sps_annexb, parse_sps,  # noqa: E402
                               strip_emulation_prevention, ts_payloads)
from iptvhub.util import natural_key, normalize_key, strip_quality_markers  # noqa: E402

GROUPS = load_groups()


class TestUtil(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_key("CCTV-1 綜合 【高清】"), "CCTV1综合高清")
        self.assertEqual(normalize_key("ＣＣＴＶ－５＋"), "CCTV5+")

    def test_quality_strip(self):
        self.assertEqual(strip_quality_markers("湖南卫视 1080P 备用1"), "湖南卫视")
        self.assertEqual(strip_quality_markers("浙江卫视(HD)"), "浙江卫视")

    def test_natural_sort(self):
        names = sorted(["CCTV10", "CCTV2", "CCTV1"], key=natural_key)
        self.assertEqual(names, ["CCTV1", "CCTV2", "CCTV10"])


class TestParser(unittest.TestCase):
    def setUp(self):
        self.noise = NoiseFilter(GROUPS["blocked_keywords"])

    def test_m3u(self):
        content = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-name="CCTV1" tvg-logo="http://l/1.png" group-title="央视",CCTV-1\n'
            'http://a/1.m3u8\n'
            '#EXTGRP:卫视\n'
            '#EXTINF:-1,湖南卫视\n'
            'http://b/2.m3u8\n'
            '#EXTINF:-1,请勿贩卖 2026-01-01\n'
            'http://c/3.m3u8\n'
            '#EXTINF:-1,组播频道\n'
            'rtp://239.1.1.1:5000\n'
        )
        entries = parse_playlist(content, "m3u", source="s")
        self.assertEqual(len(entries), 3)          # rtp 被丢弃
        entries = parse_playlist(content, "m3u", source="s", noise=self.noise)
        self.assertEqual([e.name for e in entries], ["CCTV-1", "湖南卫视"])
        self.assertEqual(entries[0].group, "央视")
        self.assertEqual(entries[1].group, "卫视")  # #EXTGRP 生效

    def test_txt_genre_and_multi_url(self):
        content = "央视频道,#genre#\nCCTV1,http://x/1.m3u8#http://y/1.m3u8\n"
        entries = parse_playlist(content, "txt", source="s")
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e.group == "央视频道" for e in entries))

    def test_format_autodetect(self):
        self.assertTrue(parse_playlist("CCTV1,http://a/1", "auto", url="http://x/list"))
        self.assertTrue(parse_playlist('#EXTM3U\n#EXTINF:-1,A\nhttp://a/1\n', "auto",
                                       url="http://x/list"))


class TestClassifier(unittest.TestCase):
    def setUp(self):
        self.classifier = Classifier(GROUPS)

    def test_canonical_merges_variants(self):
        keys = {self.classifier.canonical(name)[1]
                for name in ["CCTV-1 综合", "cctv1HD", "CCTV 1", "ＣＣＴＶ－１"]}
        self.assertEqual(len(keys), 1)

    def test_cctv5_plus_is_distinct(self):
        self.assertNotEqual(self.classifier.canonical("CCTV5")[1],
                            self.classifier.canonical("CCTV5+")[1])

    def test_cctv_4k_not_eaten_by_quality_strip(self):
        self.assertEqual(self.classifier.canonical("CCTV-4K 高清")[0], "CCTV4K")

    def test_regional_feeds_stay_separate(self):
        self.assertNotEqual(self.classifier.canonical("CCTV4欧洲")[1],
                            self.classifier.canonical("CCTV4美洲")[1])

    def test_groups(self):
        cases = [
            ("CCTV13", "央视频道"), ("湖南卫视", "卫视频道"), ("凤凰卫视资讯", "港澳台频道"),
            ("翡翠台", "港澳台频道"), ("杭州综合", "浙江频道"), ("南京新闻综合", "江苏频道"),
            ("金鹰卡通", "少儿动漫"), ("BBC News", "海外频道"),
        ]
        for name, expected in cases:
            display = self.classifier.canonical(name)[0]
            self.assertEqual(self.classifier.group_of(display), expected, name)

    def test_upstream_group_fallback(self):
        display = self.classifier.canonical("某某台")[0]
        self.assertEqual(self.classifier.group_of(display, "山东频道"), "山东频道")


class TestClassifierOrdering(unittest.TestCase):
    """分组优先级与短关键词误判的回归用例。"""

    def setUp(self):
        self.classifier = Classifier(GROUPS)

    def group(self, name: str) -> str:
        return self.classifier.group_of(self.classifier.canonical(name)[0])

    def test_short_ascii_keywords_need_exact_match(self):
        # "AM" 曾命中 "Asian DrAMa"，"RT" 曾命中 "SpoRTs"
        self.assertNotEqual(self.group("Asian Drama"), "广播频道")
        self.assertNotEqual(self.group("深圳体育 Sports"), "海外频道")

    def test_geo_beats_language_and_theme(self):
        self.assertEqual(self.group("深圳体育 Sports"), "广东频道")
        self.assertEqual(self.group("Anhui TV"), "安徽频道")

    def test_english_channels_go_overseas(self):
        for name in ("BBC News", "RT News", "HBO HD", "Sports TV"):
            self.assertEqual(self.group(name), "海外频道", name)

    def test_platform_game_rooms(self):
        self.assertEqual(self.group("「B站」王者荣耀"), "游戏电竞")
        self.assertEqual(self.group("「斗鱼」和平精英"), "游戏电竞")

    def test_hmt_before_satellite(self):
        self.assertEqual(self.group("凤凰卫视资讯"), "港澳台频道")
        self.assertEqual(self.group("湖南卫视"), "卫视频道")


class TestAdminApi(unittest.TestCase):
    """后台鉴权与配置校验。"""

    def setUp(self):
        import tempfile as _tempfile
        from iptvhub.admin import AdminApi
        from iptvhub.config import load_config
        self.tmpdir = _tempfile.mkdtemp()
        cfg = load_config()
        cfg = dict(cfg, paths=dict(cfg["paths"], data=self.tmpdir))
        cfg["server"] = dict(cfg["server"], admin_token="")
        self.api = AdminApi(cfg, None, None, None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_token_is_generated_and_persisted(self):
        token = self.api.token()
        self.assertEqual(len(token), 32)
        self.assertTrue(os.path.exists(self.api.token_path))
        self.assertEqual(token, self.api.token())

    def test_authorization(self):
        self.assertTrue(self.api.authorized(self.api.token()))
        self.assertFalse(self.api.authorized(""))
        self.assertFalse(self.api.authorized("wrong"))
        self.assertFalse(self.api.authorized(self.api.token() + "x"))

    def test_unknown_route(self):
        status, _ = self.api.handle("GET", "/nope", {}, {})
        self.assertEqual(status, 404)


class TestProbeParsing(unittest.TestCase):
    def test_sniff(self):
        self.assertEqual(sniff_kind(b"#EXTM3U\n#EXT-X-VERSION:3", ""), "hls")
        self.assertEqual(sniff_kind(b"\x47" + b"\x00" * 187 + b"\x47", ""), "ts")
        self.assertEqual(sniff_kind(b"<html><body>404", "text/html"), "text")
        self.assertEqual(sniff_kind(b"", ""), "empty")

    def test_master_playlist(self):
        manifest = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nlow.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080\nhigh.m3u8\n'
        )
        variants = parse_master(manifest, "http://h/live/index.m3u8")
        self.assertEqual(variants[0]["resolution"], "1920x1080")
        self.assertEqual(variants[0]["url"], "http://h/live/high.m3u8")

    def test_media_playlist(self):
        manifest = ("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"k\"\n"
                    "#EXTINF:5,\nseg1.ts\n#EXTINF:5,\nseg2.ts\n")
        segments, encrypted = parse_media(manifest, "http://h/live/index.m3u8")
        self.assertEqual(segments, ["http://h/live/seg1.ts", "http://h/live/seg2.ts"])
        self.assertTrue(encrypted)


class TestUrlEncoding(unittest.TestCase):
    def test_non_ascii(self):
        self.assertEqual(encode_url("http://a.com/直播/1.m3u8"),
                         "http://a.com/%E7%9B%B4%E6%92%AD/1.m3u8")

    def test_no_double_encoding(self):
        self.assertEqual(encode_url("http://a.com/a%20b.ts?q=中文"),
                         "http://a.com/a%20b.ts?q=%E4%B8%AD%E6%96%87")

    def test_idn_host_keeps_port(self):
        self.assertEqual(encode_url("http://普通话.cn:8080/a.ts"),
                         "http://xn--tkv464f82c.cn:8080/a.ts")

    def test_ascii_url_untouched(self):
        url = "http://plain.com/a.m3u8?k=1&x=2"
        self.assertIs(encode_url(url), url)


class TestCircuitBreaker(unittest.TestCase):
    def test_host_circuit_opens_on_connection_errors(self):
        cfg = {"host_failure_limit": 3, "probe_retries": 0}
        prober = Prober(HttpClient({}), cfg)
        for _ in range(3):
            prober._record_host_result("dead.host", ProbeResult(ok=False, error="timeout"))
        self.assertTrue(prober._circuit_open("dead.host"))
        self.assertEqual(prober.probe("http://dead.host/a.m3u8").error, "host_unreachable")

    def test_http_errors_do_not_open_circuit(self):
        prober = Prober(HttpClient({}), {"host_failure_limit": 2})
        for _ in range(5):
            prober._record_host_result("alive.host", ProbeResult(ok=False, error="http_404"))
        self.assertFalse(prober._circuit_open("alive.host"))

    def test_success_resets(self):
        prober = Prober(HttpClient({}), {"host_failure_limit": 2})
        prober._record_host_result("h", ProbeResult(ok=False, error="timeout"))
        prober._record_host_result("h", ProbeResult(ok=True))
        self.assertFalse(prober._circuit_open("h"))


class TestVideoInfo(unittest.TestCase):
    # 取自真实分片 stream_110k_48k_416x234_000.ts 的 H.264 SPS（含 000003 防竞争字节）
    SPS = bytes.fromhex("6764001eacd981a1ff930110000003001000000301e0f162d9a0")

    def test_parse_sps(self):
        self.assertEqual(parse_sps(self.SPS), (416, 234))

    def test_emulation_prevention(self):
        self.assertEqual(strip_emulation_prevention(b"\x00\x00\x03\x01"), b"\x00\x00\x01")
        self.assertEqual(strip_emulation_prevention(b"\x01\x02\x03"), b"\x01\x02\x03")

    def test_find_sps_in_annexb(self):
        stream = b"\x00\x00\x01\x09\x10" + b"\x00\x00\x01" + self.SPS + b"\x00\x00\x01\x68"
        self.assertEqual(parse_sps(find_sps_annexb(stream)), (416, 234))

    def test_detect_from_ts_packets(self):
        payload = b"\x00\x00\x01" + self.SPS
        packet = bytearray(b"\x47\x41\x00\x10")          # sync + PID + payload-only
        packet += payload
        packet += b"\xff" * (188 - len(packet))
        self.assertEqual(detect_resolution(bytes(packet) * 3), "416x234")

    def test_ts_payload_extraction(self):
        packet = bytearray(b"\x47\x41\x00\x30")          # 带 adaptation field
        packet += bytes([3, 0, 0, 0])                      # adaptation_field_length=3
        packet += b"ABCD"
        packet += b"\x00" * (188 - len(packet))
        self.assertTrue(ts_payloads(bytes(packet)).startswith(b"ABCD"))

    def test_garbage_returns_empty(self):
        self.assertEqual(detect_resolution(b"<html>not a stream</html>" * 100), "")
        self.assertEqual(detect_resolution(b""), "")


class TestRank(unittest.TestCase):
    weights = {"stability": 0.45, "speed": 0.25, "quality": 0.20,
               "latency": 0.10, "https_bonus": 0.03, "ipv4_bonus": 0.02}

    @staticmethod
    def row(url, **kwargs):
        base = dict(url=url, channel_key="CCTV1", display_name="CCTV1", group_title="央视频道",
                    alive=1, checks=10, successes=10, ewma=0.99, kbps=3000,
                    resolution="1920x1080", ttfb_ms=200, scheme="http", ip_version=4,
                    host=url.split("/")[2], sources="[]", source_weight=1.0, fail_streak=0,
                    logo="")
        base.update(kwargs)
        return base

    def test_stability_dominates(self):
        stable = self.row("http://h1/a", successes=10, ewma=0.99)
        flaky = self.row("http://h2/b", successes=2, ewma=0.2, fail_streak=3)
        scores = rank.score_all([stable, flaky], self.weights, 0.4)
        self.assertGreater(scores["http://h1/a"], scores["http://h2/b"])

    def test_dead_stream_scores_zero(self):
        dead = self.row("http://h3/c", alive=0)
        self.assertEqual(rank.score_row(dead, self.weights, 0.4), 0.0)

    def test_backup_hosts_are_diverse(self):
        rows = [self.row("http://h1/a", kbps=5000), self.row("http://h1/b", kbps=4000),
                self.row("http://h2/c", kbps=1000)]
        for item in rows:
            item["score"] = rank.score_row(item, self.weights, 0.4)
        channels = rank.pick_best(rows, {"max_backups_per_channel": 1,
                                         "max_per_host_per_channel": 1, "min_score": 0.0})
        hosts = [s["host"] for s in channels[0]["streams"]]
        self.assertEqual(hosts, ["h1", "h2"])

    def test_bias_corrected_ewma(self):
        self.assertAlmostEqual(rank.bias_corrected_ewma(0.4, 1, 0.4), 1.0, places=6)


class TestExport(unittest.TestCase):
    channels = [{
        "key": "CCTV1", "name": "CCTV1", "group": "央视频道", "logo": "l.png",
        "url": "http://a/1", "resolution": "1920x1080", "score": 0.9,
        "streams": [{"url": "http://a/1", "ip_version": 4, "resolution": "1920x1080"},
                    {"url": "http://b/1", "ip_version": 6}],
    }]

    def test_m3u_backups(self):
        self.assertEqual(export.render_m3u(self.channels, False).count("#EXTINF"), 1)
        self.assertEqual(export.render_m3u(self.channels, True).count("#EXTINF"), 2)

    def test_txt_groups(self):
        text = export.render_txt(self.channels, True)
        self.assertIn("央视频道,#genre#", text)
        self.assertIn("CCTV1,http://a/1#http://b/1", text)

    def test_filters(self):
        self.assertEqual(len(export.filter_channels(self.channels, group="央视频道")), 1)
        self.assertEqual(len(export.filter_channels(self.channels, group="卫视频道")), 0)
        self.assertEqual(len(export.filter_channels(self.channels, min_height=2000)), 0)
        ipv6 = export.filter_channels(self.channels, ip_version=6)
        self.assertEqual(ipv6[0]["url"], "http://b/1")


class TestProxy(unittest.TestCase):
    """中转代理：签名、manifest 改写、防开放代理。"""

    def setUp(self):
        import tempfile as _tempfile
        from iptvhub.config import load_config
        from iptvhub.proxy import StreamProxy
        self.tmpdir = _tempfile.mkdtemp()
        cfg = load_config()
        cfg = dict(cfg, paths=dict(cfg["paths"], data=self.tmpdir))

        class FakeStore:
            known = {"http://known/live.m3u8"}

            def stream(self, url):
                return {"url": url} if url in self.known else None

        self.proxy = StreamProxy(cfg, FakeStore())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_signature_roundtrip(self):
        url = "http://h/seg1.ts"
        self.assertTrue(self.proxy.verify(url, self.proxy.sign(url)))
        self.assertFalse(self.proxy.verify(url, "deadbeef"))
        self.assertFalse(self.proxy.verify(url + "x", self.proxy.sign(url)))

    def test_not_an_open_proxy(self):
        self.assertFalse(self.proxy.authorized("http://evil/x.ts", ""))
        self.assertTrue(self.proxy.authorized("http://known/live.m3u8", ""))
        self.assertTrue(self.proxy.authorized("http://evil/x.ts",
                                              self.proxy.sign("http://evil/x.ts")))

    def test_manifest_rewrite(self):
        manifest = (
            "#EXTM3U\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            "#EXTINF:10,\n"
            "seg1.ts\n"
            "#EXTINF:10,\n"
            "http://other/seg2.ts\n"
        )
        out = self.proxy.rewrite_manifest(manifest, "http://host/live/index.m3u8")
        self.assertIn("/proxy?u=http%3A%2F%2Fhost%2Flive%2Fseg1.ts&s=", out)
        self.assertIn("/proxy?u=http%3A%2F%2Fother%2Fseg2.ts&s=", out)
        self.assertIn('URI="/proxy?u=http%3A%2F%2Fhost%2Flive%2Fkey.bin', out)
        self.assertIn("#EXTINF:10,", out)          # 其它标签原样保留
        # 改写后的每个地址都必须能通过签名校验
        import re as _re
        import urllib.parse as _up
        for match in _re.finditer(r"/proxy\?u=([^&\"\s]+)&s=(\w+)", out):
            self.assertTrue(self.proxy.verify(_up.unquote(match.group(1)), match.group(2)))

    def test_manifest_detection(self):
        self.assertTrue(self.proxy.looks_like_manifest(b"#EXTM3U\n", ""))
        self.assertTrue(self.proxy.looks_like_manifest(b"", "application/vnd.apple.mpegurl"))
        self.assertFalse(self.proxy.looks_like_manifest(b"\x47\x00", "video/mp2t"))


class TestFeedback(unittest.TestCase):
    """反馈：校验、限流、去重。"""

    def setUp(self):
        import tempfile as _tempfile
        from iptvhub.feedback import FeedbackService
        handle, self.path = _tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.store = Store(self.path)
        self.store.upsert_candidates([{
            "url": "http://a/1.m3u8", "channel_key": "CCTV1", "display_name": "CCTV1",
            "group_title": "央视频道", "host": "a", "sources": []}])
        self.service = FeedbackService(self.store, lambda: b"test-secret")

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def test_submit_and_list(self):
        status, result = self.service.submit(
            {"url": "http://a/1.m3u8", "kind": "lag", "message": "晚上卡"}, "1.2.3.4")
        self.assertEqual(status, 200)
        self.assertIn("id", result)
        listing = self.service.listing(url="http://a/1.m3u8")
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["kind_label"], "卡顿/缓冲")
        self.assertEqual(listing["items"][0]["nickname"], "匿名")

    def test_rejects_unknown_stream_and_bad_kind(self):
        self.assertEqual(self.service.submit(
            {"url": "http://nope/x", "kind": "ok"}, "1.2.3.4")[0], 404)
        self.assertEqual(self.service.submit(
            {"url": "http://a/1.m3u8", "kind": "bogus"}, "1.2.3.4")[0], 400)
        self.assertEqual(self.service.submit(
            {"url": "http://a/1.m3u8", "kind": "other", "message": ""}, "1.2.3.4")[0], 400)

    def test_duplicate_and_rate_limit(self):
        payload = {"url": "http://a/1.m3u8", "kind": "lag", "message": "一样的话"}
        self.assertEqual(self.service.submit(dict(payload), "5.5.5.5")[0], 200)
        self.assertEqual(self.service.submit(dict(payload), "5.5.5.5")[0], 409)
        for index in range(12):
            self.service.submit({"url": "http://a/1.m3u8", "kind": "lag",
                                 "message": "第%d条" % index}, "6.6.6.6")
        status, _ = self.service.submit(
            {"url": "http://a/1.m3u8", "kind": "lag", "message": "再来一条"}, "6.6.6.6")
        self.assertEqual(status, 429)

    def test_ip_is_not_stored_in_clear(self):
        self.service.submit({"url": "http://a/1.m3u8", "kind": "ok"}, "9.9.9.9")
        rows = self.store.list_feedback(url="http://a/1.m3u8")["items"]
        self.assertNotIn("9.9.9.9", rows[0]["client"])
        self.assertEqual(len(rows[0]["client"]), 16)

    def test_text_is_trimmed(self):
        from iptvhub.feedback import MAX_MESSAGE, clean_text
        self.assertEqual(clean_text("a\x00b", 10), "ab")
        self.assertEqual(len(clean_text("x" * 999, MAX_MESSAGE)), MAX_MESSAGE)


class TestWebAssets(unittest.TestCase):
    """前端资源的静态检查。

    admin.js 曾因一处引号不配对（'...' 与 "..." 混用）整份脚本解析失败，
    页面能打开但所有按钮无响应——这类问题必须在发布前挡住。
    """

    WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
    PAIRS = [("admin.js", "admin.html"), ("app.js", "index.html")]
    DYNAMIC_IDS = {"moreBtn"}  # 由 JS 运行时插入，不在静态 HTML 里

    def test_js_syntax(self):
        try:
            import esprima  # 可选的开发期依赖：pip install esprima
        except ImportError:  # pragma: no cover
            self.skipTest("未安装 esprima，跳过 JS 语法检查")
        for name, _ in self.PAIRS:
            path = os.path.join(self.WEB, name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            try:
                esprima.parseScript(source)
            except Exception as exc:  # noqa: BLE001
                self.fail("%s 语法错误: %s" % (name, exc))

    def test_referenced_ids_exist(self):
        import re
        for js_name, html_name in self.PAIRS:
            with open(os.path.join(self.WEB, js_name), encoding="utf-8") as handle:
                js_source = handle.read()
            with open(os.path.join(self.WEB, html_name), encoding="utf-8") as handle:
                html_source = handle.read()
            used = set(re.findall(r'el\("([^"]+)"\)', js_source))
            used |= set(re.findall(r'getElementById\("([^"]+)"\)', js_source))
            defined = set(re.findall(r'id="([^"]+)"', html_source))
            missing = sorted(used - defined - self.DYNAMIC_IDS)
            self.assertEqual(missing, [], "%s 引用了 %s 中不存在的 id" % (js_name, html_name))

    def test_assets_use_relative_paths(self):
        """静态导出要能在子路径下托管，页面里不能写绝对 /static/。"""
        for _, html_name in self.PAIRS:
            with open(os.path.join(self.WEB, html_name), encoding="utf-8") as handle:
                html_source = handle.read()
            self.assertNotIn('href="/static/', html_source, html_name)
            self.assertNotIn('src="/static/', html_source, html_name)


class TestStore(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.path)
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def test_history_and_ewma(self):
        self.store.upsert_candidates([{
            "url": "http://a/1", "channel_key": "CCTV1", "display_name": "CCTV1",
            "group_title": "央视频道", "host": "a", "sources": ["s1"]}])
        self.store.record_probes([{"url": "http://a/1",
                                   "probe": ProbeResult(ok=True, kbps=1000, ttfb_ms=100)}])
        self.store.record_probes([{"url": "http://a/1",
                                   "probe": ProbeResult(ok=False, error="timeout")}])
        row = self.store.stream("http://a/1")
        self.assertEqual(row["checks"], 2)
        self.assertEqual(row["successes"], 1)
        self.assertEqual(row["fail_streak"], 1)
        self.assertEqual(row["alive"], 0)
        self.assertEqual(row["kbps"], 1000)        # 失败不覆盖上次成功的指标
        self.assertEqual(len(self.store.history("http://a/1")), 2)

    def test_cooldown_and_prune(self):
        self.store.upsert_candidates([{
            "url": "http://a/2", "channel_key": "X", "display_name": "X",
            "group_title": "其他频道", "host": "a", "sources": []}])
        for _ in range(4):
            self.store.record_probes([{"url": "http://a/2",
                                       "probe": ProbeResult(ok=False, error="timeout")}])
        self.assertIn("http://a/2", self.store.cooldown_urls(12))

        # 把 last_seen 回拨 10 天，模拟"上游早就不再收录且长期失败"的源
        conn = self.store._connect()
        with conn:
            conn.execute("UPDATE streams SET last_seen = last_seen - ?", (10 * 86400,))
        removed = self.store.prune(prune_after_days=7, prune_fail_streak=3)
        self.assertEqual(removed["streams_removed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
