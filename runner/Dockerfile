FROM python:3.9-slim

WORKDIR /app

# 安装 runner 需要的极简依赖
RUN pip install --no-cache-dir fastapi uvicorn

# 把我们写的服务入口放进去
COPY server.py /app/server.py

# 暴露 Operator 期望的 8080 端口
EXPOSE 8080

# 容器启动时，立刻启动 Uvicorn Web 服务器，等待请求
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]