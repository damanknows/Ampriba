"""Rebuild planner-facing labels from visible DOM nodes, never raw extraction text."""

import re
import unicodedata
from urllib.parse import urlsplit

from playwright.async_api import Page

from shared.config import ALLOWED_ORIGINS
from shared.schemas import DOMElement


def is_allowed_url(url: str) -> bool:
    if not url or re.search(r"[\s\\\x00-\x1f\x7f]", url):
        return False
    try:
        parts = urlsplit(url)
        return (
            parts.scheme in {"http", "https"}
            and f"{parts.scheme}://{parts.netloc.lower()}" in ALLOWED_ORIGINS
        )
    except ValueError:
        return False


_INJECTION = re.compile(
    r"\b(?:ignore|disregard|override|forget)\s+(?:(?:all|the|any|your)\s+)*"
    r"(?:(?:previous|prior|above|system|safety|earlier)\s+)*(?:instructions?|rules?|prompts?)\b"
)
_URL = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|//|(?:javascript|data|file):)[^\s<>\"']*", re.I
)
_HIDDEN_STYLE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|"
    r"opacity\s*:\s*0(?:\.0+)?(?![0-9.])|"
    r"font-size\s*:\s*0(?:\.0+)?(?:px|em|rem|%)?(?![0-9.])|"
    r"aria-hidden\s*=\s*['\"]?true)"
)


def _classify_text(text: str) -> list[str]:
    """Return rule markers triggered by normalized text or selector metadata."""
    normalized = unicodedata.normalize('NFKC', text).casefold()
    normalized = ''.join(c for c in normalized if unicodedata.category(c) != 'Cf')
    rules: list[str] = []
    if _INJECTION.search(normalized):
        rules.append('instruction_override')
    if any(not is_allowed_url(m.group()) for m in _URL.finditer(normalized)):
        rules.append('unauthorized_url')
    if _HIDDEN_STYLE.search(normalized):
        rules.append('hidden_style')
    return rules


def suspicious_content(text: str) -> str | None:
    rules = _classify_text(text)
    return rules[0] if rules else None


_SNAPSHOT = """selectors => {
    function visible(el) {
        for (; el; el = el.parentElement) {
            const s = getComputedStyle(el);
            if (el.hidden || el.getAttribute('aria-hidden') === 'true' ||
                s.display === 'none' || ['hidden','collapse'].includes(s.visibility) ||
                Number(s.opacity) === 0 || parseFloat(s.fontSize) === 0) return false;
        }
        return true;
    }
    function label(el) {
        const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
        let text = '';
        while (walker.nextNode()) {
            const node = walker.currentNode;
            if (visible(node.parentElement)) text += ' ' + node.data;
        }
        return text.replace(/\\s+/g, ' ').trim();
    }
    return selectors.map(selector => {
        const nodes = document.querySelectorAll(selector);
        const el = nodes[0];
        if (nodes.length !== 1 || !visible(el)) return null;
        const labels = [label(el), ...Array.from(el.labels || []).filter(visible).map(label),
            el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.title];
        if (el.matches('input[type=submit],input[type=button],input[type=reset]')) labels.push(el.value);
        return {tag: el.localName, text: labels.filter(Boolean).join(' ').trim(),
                href: el.hasAttribute('href') ? el.href : ''};
    });
}"""


async def sanitize_dom(
    page: Page, elements: list[DOMElement]
) -> tuple[list[DOMElement], list[str]]:
    """Drop hidden/flagged targets. Flags contain rule and element ID only."""
    rows = await page.evaluate(_SNAPSHOT, [el.selector for el in elements])
    clean, flags = [], []
    for el, row in zip(elements, rows, strict=True):
        if row is None:
            continue
        rules = _classify_text(' '.join((row['text'], el.selector, row['tag'])))
        if row['href'] and not is_allowed_url(row['href']) and 'unauthorized_url' not in rules:
            rules.append('unauthorized_url')
        if rules:
            flags.extend(f'{el.id}:{rule}' for rule in rules)
        else:
            clean.append(DOMElement(id=el.id, selector=el.selector, tag=row['tag'], text=row['text']))
    return clean, flags


async def sanitize_fn(elements: list[DOMElement]) -> tuple[list[DOMElement], list[str]]:
    """Signature used by Member 1's runner."""
    clean: list[DOMElement] = []
    flags: list[str] = []
    for el in elements:
        rules = _classify_text(' '.join((el.text, el.selector, el.tag)))
        if rules:
            flags.extend(f'{el.id}:{rule}' for rule in rules)
        else:
            clean.append(el)
    return clean, flags


async def sanitize_page_text(page: Page) -> tuple[str, list[str]]:
    """Return visible text, quarantining the whole page on any suspicious content."""
    snapshot = await page.evaluate("""() => {
        function visible(el) {
            for (; el; el = el.parentElement) {
                const s = getComputedStyle(el);
                if (el.hidden || el.getAttribute('aria-hidden') === 'true' ||
                    s.display === 'none' || ['hidden','collapse'].includes(s.visibility) ||
                    Number(s.opacity) === 0 || parseFloat(s.fontSize) === 0) return false;
            }
            return true;
        }
        const walker = document.createTreeWalker(document.body || document.documentElement,
            NodeFilter.SHOW_TEXT);
        const chunks = [];
        while (walker.nextNode()) {
            if (visible(walker.currentNode.parentElement)) {
                const text = walker.currentNode.data.trim();
                if (text) chunks.push(text);
            }
        }
        const links = Array.from(document.querySelectorAll('a[href]'))
            .filter(visible).map(link => link.href);
        return {chunks, links};
    }""")
    content = ' '.join(snapshot['chunks'])
    rule = suspicious_content(content)
    if rule:
        return '', [rule]
    if any(not is_allowed_url(url) for url in snapshot['links']):
        return '', ['unauthorized_url']
    return content, []
