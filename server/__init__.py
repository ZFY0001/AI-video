"""一言片场 · Web 服务层。

只做三件事：把 `app.engine.Engine` 的回调桥成事件流、把运行产物暴露成 HTTP、
托管前端构建产物。业务逻辑一行都不在这里——全部在 `pipeline/` 与 `app/engine.py`。
"""
