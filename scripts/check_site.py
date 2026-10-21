"""Validate the static Pages site without installing dependencies."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, unquote
import json
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[1] / 'site'

class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags, self.ids, self.links, self.jsonld = [], set(), [], []
        self.capture = False
        self.feed(text)
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a))
        if 'id' in a:
            assert a['id'] not in self.ids, f'Duplicate id: {a["id"]}'
            self.ids.add(a['id'])
        for name in ('href', 'src'):
            if name in a:
                self.links.append(a[name])
        if tag == 'script' and a.get('type') == 'application/ld+json':
            self.capture = True
    def handle_endtag(self, tag):
        if tag == 'script':
            self.capture = False
    def handle_data(self, data):
        if self.capture:
            self.jsonld.append(json.loads(data))
    def attrs(self, tag, **criteria):
        return [a for t, a in self.tags if t == tag and all(a.get(k) == v for k, v in criteria.items())]

pages = {p: Page(p.read_text()) for p in root.rglob('*.html') if p.name == 'index.html'}
assert len(pages) == 2, 'Expected English and Chinese homepages'
slug = root.parent.name
base = f'https://lucky-goals.github.io/{slug}/'
urls = set()
for path, page in pages.items():
    expected = base + ('zh/' if path.parent.name == 'zh' else '')
    assert page.attrs('html', lang='zh-CN' if path.parent.name == 'zh' else 'en')
    assert len(page.attrs('h1')) == 1
    assert page.attrs('title')
    assert page.attrs('meta', name='description')[0].get('content')
    assert page.attrs('meta', name='google-site-verification')[0].get('content')
    assert not page.attrs('meta', name='robots', content='noindex')
    assert page.attrs('link', rel='canonical')[0]['href'] == expected
    urls.add(expected)
    alternates = {a['hreflang']: a['href'] for a in page.attrs('link', rel='alternate')}
    assert alternates == {'en': base, 'zh-CN': base + 'zh/', 'x-default': base}
    assert page.jsonld[0]['codeRepository'] == f'https://github.com/lucky-goals/{slug}'
    for link in page.links:
        url = urlsplit(link)
        if url.scheme or url.netloc:
            assert url.scheme == 'https', f'Unexpected scheme: {link}'
            continue
        target = (path.parent / unquote(url.path)).resolve() if url.path else path
        assert target.is_relative_to(root.resolve()), f'Link escapes site: {link}'
        if target.is_dir():
            target /= 'index.html'
        assert target.exists(), f'Broken link in {path}: {link}'
        if url.fragment and target in pages:
            assert unquote(url.fragment) in pages[target].ids, f'Broken anchor: {link}'
ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9', 'x': 'http://www.w3.org/1999/xhtml'}
sitemap = ET.parse(root / 'sitemap.xml')
assert {n.text for n in sitemap.findall('s:url/s:loc', ns)} == urls
for node in sitemap.findall('s:url', ns):
    assert {a.attrib['hreflang']: a.attrib['href'] for a in node.findall('x:link', ns)} == {'en': base, 'zh-CN': base+'zh/', 'x-default': base}
print(f'PASS: {slug}: two languages, metadata, JSON-LD, sitemap, local files and anchors')
