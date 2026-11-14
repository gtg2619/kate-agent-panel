"""Offline resources and first-party project conventions."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProjectTests(unittest.TestCase):
    def test_release_versions_match_plugin_and_protocol_handshakes(self):
        cmake = (ROOT / 'CMakeLists.txt').read_text()
        version = re.search(r'project\(kateagentpanel VERSION (\S+)', cmake).group(1)
        metadata = json.loads((ROOT / 'src/plugin.json').read_text())['KPlugin']
        self.assertEqual(metadata['Version'], version)
        from test_bridge import module
        self.assertEqual(module.VERSION, version)

    def test_html_and_woff2_font_dependencies_are_shipped_locally(self):
        web = ROOT / 'web'
        html = (web / 'index.html').read_text()
        for uri in re.findall(r'(?:src|href)="([^"]+)"', html):
            if uri == 'qrc:///qtwebchannel/qwebchannel.js':
                continue
            self.assertNotIn('://', uri)
            self.assertTrue((web / uri).is_file(), uri)
        css = web / 'vendor/katex/katex.min.css'
        # Qt WebEngine uses WOFF2. Upstream CSS also lists legacy WOFF/TTF fallbacks.
        urls = [uri for uri in re.findall(r'url\(([^)]+)\)', css.read_text()) if uri.endswith('.woff2')]
        self.assertGreater(len(urls), 10)
        for uri in urls:
            self.assertTrue((css.parent / uri.strip('"\'')).is_file(), uri)
        for vendor in ('katex', 'markdown-it'):
            self.assertTrue(any((web / 'vendor' / vendor).glob('LICENSE*')))

    def test_first_party_language_is_english(self):
        paths = [ROOT / 'README.md', ROOT / 'src/plugin.json', ROOT / 'web/index.html']
        paths.extend(p for p in (ROOT / 'web').glob('*') if p.suffix in ('.js', '.css'))
        for directory in ('docs', 'src', 'bridge', 'tests', 'scripts'):
            paths.extend(p for p in (ROOT / directory).rglob('*') if p.is_file() and p.suffix in ('.md','.cpp','.h','.py','.sh'))
        for path in paths:
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertIsNone(re.search(r'[\u3400-\u9fff]', path.read_text()))
        self.assertIn('lang="en"', (ROOT / 'web/index.html').read_text())
        metadata = json.loads((ROOT / 'src/plugin.json').read_text())['KPlugin']
        self.assertFalse(any('[' in key for key in metadata))
