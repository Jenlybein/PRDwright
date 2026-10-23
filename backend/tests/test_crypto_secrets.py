"""主密钥与密钥加密入库（ADR-053）的契约测试。

守三件事，每一件对应一种「上线才发现」的失败：

1. **加解密互逆**：加密入库、调用前解密，任何一环错位都会让密钥「配了却调不通」。
2. **库里的必须是密文**：如果仓库层或迁移把明文写进去，「加密入库」就名不副实。
3. **主密钥缺失要明确报错**：静默生成一把新的会让旧密文全部报废，而现象只是「报错」。
"""

from __future__ import annotations

import pytest

from backend.core import crypto
from dbsupport import connect
from conftest import model_providers as ModelProviderRepository
def test_encrypt_decrypt_roundtrip():
    token = crypto.encrypt_value("sk-test-123")
    assert token and token != "sk-test-123"
    assert crypto.decrypt_value(token) == "sk-test-123"


def test_empty_value_stays_empty():
    """空串表示「未配置」，加密前后都应是空串，且不触碰主密钥。"""
    assert crypto.encrypt_value("") == ""
    assert crypto.decrypt_value("") == ""
    assert crypto.is_configured("") is False


def test_is_configured_rejects_corrupt_or_rotated_token():
    """不可解的密文不能算「已配置」——否则体检会给假绿灯。"""
    assert crypto.is_configured("not-a-valid-fernet-token") is False


def test_missing_master_key_fails_with_actionable_message(tmp_path, monkeypatch):
    """主密钥缺失时必须报错并指向生成脚本，不能静默造一把新的。"""
    missing = tmp_path / "no-such-dir" / "master.key"
    monkeypatch.setenv("PRDWRIGHT_MASTER_KEY_PATH", str(missing))
    crypto.reset_cache()

    with pytest.raises(RuntimeError) as excinfo:
        crypto.encrypt_value("sk-x")
    assert "generate_keys.py" in str(excinfo.value)


def test_model_providers_schema_uses_value_token():
    """迁移后表里只有 value_token，旧的环境变量名列已删除。"""
    with connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(model_providers)")}
    assert "value_token" in columns
    assert "api_key_env_var" not in columns


def test_repository_stores_ciphertext_not_plaintext():
    """仓库层拿到什么就存什么；调用方传进来的必须是密文。"""
    plain = "sk-never-in-db"
    ModelProviderRepository.create(
        provider_id="cfg_crypto001",
        name="加密用例",
        protocol="ark",
        value_token=crypto.encrypt_value(plain),
    )

    row = ModelProviderRepository.get("cfg_crypto001")
    assert row is not None
    assert row["value_token"] and row["value_token"] != plain
    assert crypto.decrypt_value(row["value_token"]) == plain
