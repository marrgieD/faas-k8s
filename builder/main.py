import io
import os
import shutil
import tempfile
from typing import Optional

import docker
from docker.errors import APIError, BuildError, DockerException
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="FaaS Builder", version="1.0.0")

DOCKER_BASE_IMAGE = os.getenv("FAAS_BUILDER_BASE_IMAGE", "python:3.9-slim")
FAAS_IMAGE_PREFIX = os.getenv("FAAS_IMAGE_PREFIX", "faas-fn")
DEFAULT_CMD = os.getenv("FAAS_DEFAULT_CMD", "python handler.py")


class BuildRequest(BaseModel):
    function_name: str
    code: str
    base_image: Optional[str] = None
    image_tag: Optional[str] = None


class BuildResponse(BaseModel):
    image: str
    logs: str


def _generate_dockerfile(base_image: str) -> str:
    """
    Dockerfile 说明（Layer Caching 优化）：
    1. 尽量将变化频率低的层（基础镜像、系统依赖、Python 依赖等）放在前面；
    2. 将业务代码 COPY 放在 Dockerfile 的靠后位置，这样仅代码变更时，
       前面的层会命中缓存，构建时间极大缩短；
    3. 若你的依赖通过 requirements.txt 指定，可先 COPY requirements.txt
       再 RUN pip install -r requirements.txt，最后 COPY 代码。
    """
    return f"""
FROM {base_image}

# 最小化本地化和缓存影响
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

# 安装常见运行时依赖（可按需扩展）
RUN apt-get update \\
    && apt-get install -y --no-install-recommends \\
       build-essential curl \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 在真实生产中，你可以在这里:
#   COPY requirements.txt .
#   RUN pip install --no-cache-dir -r requirements.txt
# 将依赖安装单独成层，以充分利用 Docker layer cache

# 仅当代码变更时，才会重新执行 COPY 这层，从而提升冷启动构建性能
COPY handler.py /app/handler.py

# 示例：默认命令。实际可以改为 gunicorn/uvicorn 等 WSGI/ASGI 服务器
CMD {DEFAULT_CMD}
""".lstrip()


def _build_image(req: BuildRequest) -> BuildResponse:
    client = docker.from_env()
    base_image = req.base_image or DOCKER_BASE_IMAGE
    image_tag = req.image_tag or f"{FAAS_IMAGE_PREFIX}-{req.function_name}:latest"

    build_dir = tempfile.mkdtemp(prefix="faas-build-")
    logs_buffer = io.StringIO()

    try:
        # 写入 Python 源码
        handler_path = os.path.join(build_dir, "handler.py")
        with open(handler_path, "w", encoding="utf-8") as f:
            f.write(req.code)

        # 写入 Dockerfile
        dockerfile_content = _generate_dockerfile(base_image)
        dockerfile_path = os.path.join(build_dir, "Dockerfile")
        with open(dockerfile_path, "w", encoding="utf-8") as f:
            f.write(dockerfile_content)

        # 执行 docker build
        try:
            image, build_logs = client.images.build(
                path=build_dir,
                tag=image_tag,
                rm=True,
                pull=False,
                decode=True,
            )
        except (APIError, BuildError) as e:
            if isinstance(e, BuildError):
                for line in e.build_log:
                    if "stream" in line:
                        logs_buffer.write(line["stream"])
            raise

        # 收集日志
        for chunk in build_logs:
            if "stream" in chunk:
                logs_buffer.write(chunk["stream"])
            elif "errorDetail" in chunk:
                logs_buffer.write(str(chunk["errorDetail"]))

        return BuildResponse(image=image_tag, logs=logs_buffer.getvalue())

    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


@app.post("/build", response_model=BuildResponse)
def build_function_image(req: BuildRequest):
    """
    接收 Python 源码并构建镜像。
    """
    try:
        return _build_image(req)
    except (APIError, BuildError) as e:
        raise HTTPException(status_code=500, detail=f"Docker build 失败: {e}")
    except DockerException as e:
        raise HTTPException(status_code=500, detail=f"Docker 客户端异常: {e}")
    except Exception as e:  # 兜底
        raise HTTPException(status_code=500, detail=f"未知错误: {e}")