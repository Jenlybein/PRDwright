"""配置相关路由。

按要求先建立空路由文件，接口后续再补充。
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/config", tags=["config"])

# TODO: 后续在此添加配置相关接口
