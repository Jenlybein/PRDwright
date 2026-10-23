"""SQLite 并发写：两个写者撞在一起时必须**排队**，而不是抛 `database is locked`。

为什么值得单独钉
----------------
`core/db.py::connect()` 每建一条连接都开着 `PRAGMA busy_timeout`。这一行属于
「删掉之后本地全绿」的那一类：单线程用例永远不会有两个写者同时想拿写锁，所以
它没有任何用例能顺带覆盖到。

它消失后的症状极难复现、也极容易被误判：

- 只在并发下出现（生成任务的定时 flush + 用户请求的保存撞在一起）；
- 报的是 `sqlite3.OperationalError: database is locked`，看起来像「SQLite 不行」，
  于是结论往往是「换 Postgres」而不是「少了一行 pragma」；
- 失败是**偶发**的：CI 上跑十次绿九次，第十次红在一条与改动无关的用例上。

因此这里直接构造「A 持写锁、B 要写」的场景，断言 B 的结局是**成功**（排队等到了），
而不是异常。
"""

from __future__ import annotations

import threading
import time

from backend.core.db import connect, utc_now
from backend.repositories.auth_repo import LoginFailureRepository


def _insert_failure(conn, username: str) -> None:
    """写一条登录失败记录（最小的、会拿写锁的业务写入）。"""
    conn.execute(
        "INSERT INTO login_failures (username, ip, created_at) VALUES (?, ?, ?)",
        (username, "", utc_now().isoformat()),
    )


def test_second_writer_waits_instead_of_failing(isolated_db):  # noqa: ARG001
    """A 持着写锁时，B 必须等到 A 提交后再写成功，不允许抛裸 `database is locked`。

    做法：主线程 `BEGIN IMMEDIATE` 拿到 RESERVED 锁并写一行（不提交），另起一个
    线程走**同样的 `connect()`** 去写第二行；确认线程已启动后主线程再提交。
    若 `busy_timeout` 没生效，线程那一刻就会抛 `OperationalError` 而不是等待——
    而线程里的异常默认只打到 stderr，用例会「绿着」漏掉它，所以这里必须自己收回来。
    """
    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            with connect() as conn:
                started.set()
                _insert_failure(conn, "bob")
                conn.commit()
        except BaseException as exc:  # noqa: BLE001 - 线程里什么都可能抛，全都要收回来
            errors.append(exc)
        finally:
            finished.set()

    with connect() as holder:
        holder.execute("BEGIN IMMEDIATE")
        _insert_failure(holder, "alice")  # 未提交 → 此刻持有写锁

        thread = threading.Thread(target=writer, name="second-writer")
        thread.start()
        assert started.wait(5), "写线程没有启动，用例本身有问题"

        # 给第二个写者足够的时间真正撞上锁、进入 busy 等待
        time.sleep(0.3)
        holder.commit()

    assert finished.wait(10), "写线程在超时内没有完成：busy_timeout 没生效，它一直在等"
    thread.join(5)

    assert errors == [], f"第二个写者抛了异常（应当是排队等待）：{errors!r}"
    # 两条都真的落库了：队列排到了，而不是「等待后静默丢弃」
    assert LoginFailureRepository.count_since("alice", "1970-01-01T00:00:00") == 1
    assert LoginFailureRepository.count_since("bob", "1970-01-01T00:00:00") == 1
