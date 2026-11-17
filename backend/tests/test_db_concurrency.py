"""Postgres 并发写：行级锁 + MVCC（票 #77 从 SQLite 单写者语义改写）。

## 换了什么、为什么换

SQLite 是**单写者**：整个库一把写锁，第二个写者会撞 `database is locked`，
所以那时候专门钉了一条 `PRAGMA busy_timeout`（写者排队而不是失败）。

Postgres 上**没有「库级写锁」这件事**——它用 MVCC + **行级锁**：

- 写**不同行**：互不阻塞（MVCC 允许多个写事务并存）；
- 写**同一行**：后到者排队等前一个事务结束（行锁），不报错、也不丢写。

也就是说，原用例的**被测对象**（`busy_timeout` 这个 pragma）在 PG 上不存在了，
那条性质由 MVCC/行锁保证。但性质本身（「并发写不抛锁错误、写者会排队而不是失败」）
仍然值得钉——所以这里把它换成 PG 的手段来表达：

| 用例 | 判据 |
| --- | --- |
| 不同行的并发写 | 两个写事务同时进行、都不排队、都成功（没有库级锁） |
| 同一行的并发写 | 后到者**排队**（不抛错），前一个提交后它的写落地 |

这两条一起把「排队而不是失败」的原意保留了下来，而把「靠一句 pragma 才成立」
这层实现细节换成了 PG 的真实机制。
"""

from __future__ import annotations

import threading
import time

from backend.core.clock import utc_now
from dbsupport import connect


def _insert_failure(conn, username: str) -> int:
    """写一条登录失败记录（最小的、会拿写锁的业务写入），返回它的 id。"""
    return conn.execute(
        "INSERT INTO login_failures (username, ip, created_at) VALUES (%s, %s, %s) RETURNING id",
        (username, "", utc_now().isoformat()),
    ).fetchone()["id"]


def _seed_two_rows() -> tuple[int, int]:
    with connect() as conn:
        first = _insert_failure(conn, "alice")
        second = _insert_failure(conn, "bob")
        conn.commit()
    return first, second


def test_writers_on_different_rows_do_not_block_each_other(isolated_db):  # noqa: ARG001
    """A 持着行 1 的写锁时，B 写**行 2** 必须立刻完成——Postgres 没有库级写锁。

    这正是原来那条「第二个写者排队」用例的反面：SQLite 的单写者语义下 B 会被挡住，
    Postgres 的行级锁下 B 与 A 毫不相干。若哪天有人把隔离级别或锁粒度调粗
    （例如 `LOCK TABLE`），这条会转红。
    """
    first_id, second_id = _seed_two_rows()

    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            with connect() as conn:
                started.set()
                conn.execute(
                    "UPDATE login_failures SET ip = 'row-2' WHERE id = %s", (second_id,)
                )
                conn.commit()
        except BaseException as exc:  # noqa: BLE001 - 线程里什么都可能抛，全都要收回来
            errors.append(exc)
        finally:
            finished.set()

    with connect() as holder:
        holder.execute("UPDATE login_failures SET ip = 'row-1' WHERE id = %s", (first_id,))
        # 未提交 → 此刻持有行 1 的写锁

        thread = threading.Thread(target=writer, name="different-row-writer")
        thread.start()
        assert started.wait(5), "写线程没有启动，用例本身有问题"

        # 行 2 与行 1 不相干：B 必须在前一个事务结束**之前**就完成
        assert finished.wait(5), "写不同行却排队了：行级锁退化成了表级锁（或库级写锁）"
        holder.commit()

    thread.join(5)
    assert errors == [], f"写不同行的并发写者抛了异常：{errors!r}"

    with connect() as conn:
        rows = {
            row["id"]: row["ip"]
            for row in conn.execute(
                "SELECT id, ip FROM login_failures WHERE id IN (%s, %s)", (first_id, second_id)
            )
        }
    assert rows == {first_id: "row-1", second_id: "row-2"}, "两条写没有都落库"


def test_second_writer_on_the_same_row_waits_instead_of_failing(isolated_db):  # noqa: ARG001
    """A 持着同一行的写锁时，B 必须**等到** A 提交后写成功，不允许抛锁错误。

    这就是原来那条用例的 PG 表达：单写者换成**行锁**之后，「排队而不是失败」
    这条承诺仍然成立。若哪天驱动/配置把行锁改成 `NOWAIT` 或更低的隔离级别，
    这里会看到异常而不是排队。
    """
    row_id, _ = _seed_two_rows()

    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            with connect() as conn:
                started.set()
                conn.execute(
                    "UPDATE login_failures SET ip = 'second' WHERE id = %s", (row_id,)
                )
                conn.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            finished.set()

    with connect() as holder:
        holder.execute("UPDATE login_failures SET ip = 'first' WHERE id = %s", (row_id,))
        # 未提交 → 此刻持有这一行的写锁

        thread = threading.Thread(target=writer, name="same-row-writer")
        thread.start()
        assert started.wait(5), "写线程没有启动，用例本身有问题"

        # 给第二个写者足够时间真正撞上行锁、进入等待
        time.sleep(0.3)
        assert not finished.is_set(), "写同一行却没有排队：行锁没生效"
        holder.commit()

        assert finished.wait(10), "写线程在超时内没有完成：它在等一个永远不会释放的行锁"
    thread.join(5)

    assert errors == [], f"第二个写者抛了异常（应当是排队等待）：{errors!r}"
    # 两条都真的落库了（后写覆盖前写：队列排到了，而不是「等待后静默丢弃」）
    with connect() as conn:
        row = conn.execute(
            "SELECT ip FROM login_failures WHERE id = %s", (row_id,)
        ).fetchone()
    assert row is not None
    assert row["ip"] == "second", "第二个写者的写丢了：排队之后必须真的写进去"
