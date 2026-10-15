"""内容指纹。

文档的「内容指纹」用在几处：写库时记录、迁移时留痕、排查「正文变了但清单没重算」
时做对照。**它不是并发判据**——本项目不做乐观锁，写入策略是最后写入胜出。

口径固定为 `sha256(utf-8 文本)` 的前 12 位十六进制：短到能打进回执与日志，又足够长
（48 bit）到在本场景下碰撞可忽略。把这个口径收在一处，是为了避免几处各自
`hexdigest()[:n]` 时把 `n` 或编码写岔。
"""

from __future__ import annotations

import hashlib

# 指纹长度（十六进制字符数）
FINGERPRINT_LENGTH = 12


def content_fingerprint(text: str) -> str:
    """返回文本的内容指纹。

    Args:
        text: 任意文本；按 UTF-8 编码后取哈希，因此中文与英文的指纹口径一致。

    Returns:
        12 位十六进制字符串。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]
