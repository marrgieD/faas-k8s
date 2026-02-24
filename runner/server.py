import os
import sys
import importlib.util
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

app = FastAPI()

# Operator 挂载代码的路径
CODE_PATH = os.getenv("FUNCTION_CODE_PATH", "/var/faas/function/handler.py")

def execute_user_code():
    """动态加载并执行用户的 handler.py"""
    if not os.path.exists(CODE_PATH):
        return "Error: Code not found"
    
    try:
        # 动态导入挂载的 python 文件
        spec = importlib.util.spec_from_file_location("user_handler", CODE_PATH)
        user_module = importlib.util.module_from_spec(spec)
        sys.modules["user_handler"] = user_module
        spec.loader.exec_module(user_module)
        
        # 假设用户的代码里有一个名为 main() 的函数，如果没有，我们可以退一步捕获标准输出
        if hasattr(user_module, "main"):
            return str(user_module.main())
        else:
            return "Function executed, but no main() function found to return data."
    except Exception as e:
        return f"Function execution error: {str(e)}"

# 捕获所有 HTTP 请求并交给用户代码处理
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def catch_all(request: Request, path: str):
    # 在真实的生产环境，这里会把 request 参数传给 user_module.main(request)
    result = execute_user_code()
    return PlainTextResponse(result)