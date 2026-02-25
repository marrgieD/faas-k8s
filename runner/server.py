import os
import sys
import importlib.util
import traceback
import inspect
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

app = FastAPI()

# Operator 挂载代码的路径
CODE_PATH = os.getenv("FUNCTION_CODE_PATH", "/var/faas/function/handler.py")

def load_user_module():
    """动态加载用户代码，带有异常捕获"""
    if not os.path.exists(CODE_PATH):
        return None, "Error: Code not found at " + CODE_PATH
    try:
        spec = importlib.util.spec_from_file_location("user_handler", CODE_PATH)
        user_module = importlib.util.module_from_spec(spec)
        sys.modules["user_handler"] = user_module
        spec.loader.exec_module(user_module)
        return user_module, None
    except Exception as e:
        return None, f"Module load error: {traceback.format_exc()}"

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def catch_all(request: Request, path: str):
    user_module, err = load_user_module()
    if err:
        return JSONResponse(status_code=500, content={"error": err})

    if not hasattr(user_module, "main"):
        return JSONResponse(status_code=500, content={"error": "No main() function found."})

    # 1. 解析请求参数 (POST解析JSON体，GET解析Query参数)
    if request.method in ["POST", "PUT", "PATCH"]:
        try:
            event = await request.json()
        except Exception:
            event = {"raw_body": (await request.body()).decode("utf-8")}
    else:
        event = dict(request.query_params)

    # 2. 智能执行用户函数
    try:
        # 获取函数签名，看用户是否定义了参数
        sig = inspect.signature(user_module.main)
        
        if len(sig.parameters) > 0:
            result = user_module.main(event)  # 传参调用
        else:
            result = user_module.main()       # 无参调用
            
        # 3. 智能返回 (如果用户返回字典，就转成JSON；否则转成纯文本)
        if isinstance(result, dict):
            return JSONResponse(content=result)
        else:
            return PlainTextResponse(str(result))
            
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Execution error: {traceback.format_exc()}"})