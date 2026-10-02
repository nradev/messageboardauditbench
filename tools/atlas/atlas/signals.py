"""Generic content signals and the salience score used to rank small clusters.

Salience = rarity x content richness. Only domain-free signals are used: length,
URLs/hosts, IP addresses, filesystem paths, environment-variable style assignments,
shell commands, code, and mixed-script identifiers. Nothing here knows the dataset.
"""

from __future__ import annotations

import math
import re
import unicodedata

URL = re.compile(r"\b(?:[a-z][a-z0-9+.-]*://|www\.)[^\s\"'<>()\[\]{}|\\^`]+", re.I)
HOST = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,24})\b", re.I)
IPV4 = re.compile(r"\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b")
PATH = re.compile(r"(?<![\w/:.])(?:~|\.{1,2})?/(?:[\w.@-]+/)+[\w.@-]*|\b[A-Z]:\\[\w\\. -]+")
ENVVAR = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
SHELL = re.compile(
    r"(?:^|[\s;|&`$(])(?:sudo|curl|wget|ssh|scp|nc|ncat|socat|chmod|chown|export|echo|cat|grep|sed|awk|"
    r"python3?|pip3?|npm|node|bash|sh|git|docker|kubectl|apt(?:-get)?|crontab|nohup|kill|ps)\s+[-\w./$\"']",
    re.M,
)
CODE = re.compile(
    r"```|^\s*(?:def |class |import |from \S+ import |#include|function\s*\(|function \w+\(|"
    r"(?:const|let|var) \w+ =|public |private |static |return\b|for \(|while \(|if \()",
    re.M,
)
# Common file-like suffixes that the host pattern would otherwise treat as domains.
_NOT_TLD = {
    "py", "js", "ts", "md", "txt", "json", "jsonl", "yaml", "yml", "csv", "html", "htm", "css", "sh", "rb",
    "go", "rs", "java", "c", "h", "cpp", "png", "jpg", "jpeg", "gif", "svg", "pdf", "zip", "gz", "tar", "log",
    "exe", "dll", "so", "toml", "ini", "cfg", "conf", "xml", "lock", "gem", "gemspec", "php",
}


def hosts(text: str) -> set[str]:
    out = set()
    for m in HOST.finditer(text):
        h = m.group(0).lower()
        if h.rsplit(".", 1)[-1] in _NOT_TLD or h.replace(".", "").isdigit():
            continue
        out.add(h)
    return out


def scripts_of(token: str) -> set[str]:
    """Unicode scripts of the letters in a token (LATIN, CYRILLIC, GREEK, ...)."""
    out = set()
    for ch in token:
        if ch.isalpha():
            try:
                out.add(unicodedata.name(ch).split(" ", 1)[0])
            except ValueError:
                pass
    return out


def mixed_script_tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"\w{3,}", text) if len(scripts_of(t)) > 1]


def signal_set(text: str) -> set[str]:
    sample = text[:20000]
    s = set()
    if URL.search(sample) or hosts(sample):
        s.add("url")
    if IPV4.search(sample):
        s.add("ip")
    if PATH.search(sample):
        s.add("path")
    if ENVVAR.search(sample):
        s.add("env")
    if SHELL.search(sample):
        s.add("shell")
    if CODE.search(sample):
        s.add("code")
    if mixed_script_tokens(sample):
        s.add("mixed-script")
    return s


def salience(size: int, text: str, signals: set[str]) -> float:
    rarity = 1.0 / math.sqrt(max(size, 1))
    richness = (1.0 + math.log1p(len(text)) / 4.0) * (1.0 + 0.5 * len(signals))
    return rarity * richness
