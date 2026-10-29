import importlib
import os
import time
import unittest
from unittest import mock

import cloudinary_scan
from cloudinary_scan import parse, transformation
from collect import merge


class ParseTest(unittest.TestCase):
    def test_none_without_cloudinary(self):
        self.assertIsNone(parse('<img src="https://cdn.example.com/a.jpg">'))

    def test_res_url_with_transforms(self):
        html = ('<img src="https://res.cloudinary.com/acme/image/upload/w_400,c_fill,f_auto,q_auto/v1/a.jpg">'
                '<img src="https://res.cloudinary.com/acme/image/upload/v1/b.png">')
        found = parse(html)
        self.assertEqual(found["clouds"], {"acme": 2})
        self.assertEqual(found["unique_urls"], 2)
        self.assertTrue(found["f_auto"] and found["q_auto"])
        self.assertIn("w_400,c_fill,f_auto,q_auto", found["top_transforms"])

    def test_escaped_json_and_private_cdn(self):
        html = r'{"img":"https:\/\/acme-res.cloudinary.com\/image\/upload\/w_200\/x.jpg"}'
        found = parse(html)
        self.assertEqual(found["private_cdn"], ["acme"])
        self.assertEqual(found["clouds"], {"acme": 1})

    def test_custom_domain_needs_transforms(self):
        found = parse('<img src="https://img.shop.com/image/upload/w_300,h_300,c_fill/v1/p.jpg">')
        self.assertEqual(found["custom_hosts"], ["img.shop.com"])
        self.assertIsNone(parse('<img src="https://img.shop.com/image/upload/p.jpg">'))

    def test_measure_without_images(self):
        from cloudinary_scan import measure
        self.assertEqual(measure({"_urls": ["res.cloudinary.com/a/video/upload/v.mp4"]}, 5, 1), {"measured": 0})

    def test_transformation(self):
        self.assertEqual(transformation("w_400,c_fill/v1/a.jpg"), "w_400,c_fill")
        self.assertEqual(transformation("v1/a.jpg"), "")


class SlowPageTest(unittest.TestCase):
    """`re` holds the GIL, so a page that makes a pattern backtrack freezes every worker thread."""

    def finishes_fast(self, html):
        t = time.time()
        parse(html)
        self.assertLess(time.time() - t, 2)

    def test_long_runs_without_cloudinary(self):
        self.finishes_fast("x" * 3_000_000)
        self.finishes_fast("https://" + "a" * 2_000_000 + "/")
        self.finishes_fast("https://a.com/image/upload/" + "w_" + "a_" * 500_000)

    def test_long_runs_around_cloudinary_text(self):
        self.finishes_fast("x" * 1_000_000 + "-res.cloudinary.com/image/upload/a.jpg")
        self.finishes_fast("res.cloudinary.com/-res.cloudinary.com/" + "x" * 2_000_000)

    def test_private_cdn_name_is_the_whole_run_before_the_host(self):
        found = parse('<img src="https://my-shop-2-res.cloudinary.com/image/upload/w_9/a.jpg">')
        self.assertEqual(found["private_cdn"], ["my-shop-2"])
        self.assertIsNone(parse("<img src='https:// -res.cloudinary.com/image/upload/a.jpg'>"))


class UserAgentTest(unittest.TestCase):
    def reload_with(self, value):
        env = {k: v for k, v in os.environ.items() if k != "CLOUDINARY_SCAN_UA"}
        if value is not None:
            env["CLOUDINARY_SCAN_UA"] = value
        with mock.patch.dict(os.environ, env, clear=True):
            return importlib.reload(cloudinary_scan).UA

    def tearDown(self):
        importlib.reload(cloudinary_scan)

    def test_default_points_to_this_repo(self):
        self.assertIn("github.com/taktekhq/cloudinary-scan", self.reload_with(None))

    def test_env_overrides(self):
        self.assertEqual(self.reload_with("MyScan/2 (+https://example.com)"), "MyScan/2 (+https://example.com)")

    def test_empty_env_falls_back(self):
        self.assertEqual(self.reload_with(""), cloudinary_scan.DEFAULT_UA)


class MergeTest(unittest.TestCase):
    def test_sorted_by_rank_and_one_row_per_domain(self):
        a = '{"rank": 30, "domain": "c.com"}\n{"rank": 10, "domain": "a.com"}\n'
        b = '\n{"rank": 20, "domain": "b.com"}\n{"rank": 10, "domain": "a.com"}\n'
        self.assertEqual([r["domain"] for r in merge([a, b])], ["a.com", "b.com", "c.com"])

    def test_empty(self):
        self.assertEqual(merge(["", "\n"]), [])


if __name__ == "__main__":
    unittest.main()
