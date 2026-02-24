import asyncio
import os
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from kubernetes import client, config
from kubernetes.client import ApiException
from kubernetes.config.config_exception import ConfigException

app = FastAPI(title="FaaS Gateway", version="1.0.0")

# K8s 基本配置
CRD_GROUP = "faas.example.com"
CRD_VERSION = "v1alpha1"
CRD_PLURAL = "functions"

NAMESPACE = os.getenv("FAAS_NAMESPACE", "default")
SERVICE_PORT = int(os.getenv("FAAS_SERVICE_PORT", "8080"))
POLL_INTERVAL_SECONDS = float(os.getenv("FAAS_POLL_INTERVAL", "0.5"))
POLL_TIMEOUT_SECONDS = float(os.getenv("FAAS_POLL_TIMEOUT", "60"))

# 这里使用“内存锁”，保证在单实例网关内不会重复触发扩容。
# 若需多实例分布式锁，可替换为 Redis 实现（如 aioredis+SETNX）。
_function_locks: Dict[str, asyncio.Lock] = {}

apps_v1_api: Optional[client.AppsV1Api] = None


def init_kube_client() -> None:
    global apps_v1_api
    try:
        config.load_incluster_config()
        print("Loaded in-cluster kube config.")
    except ConfigException:
        config.load_kube_config()
        print("Loaded local kubeconfig.")
    apps_v1_api = client.AppsV1Api()


@app.on_event("startup")
async def on_startup():
    init_kube_client()


def _deployment_name_from_function(fn_name: str) -> str:
    # 与控制面命名保持一致：fn-{name}-deploy
    return f"fn-{fn_name.lower()}-deploy"


def _service_name_from_function(fn_name: str) -> str:
    # 与控制面命名保持一致：fn-{name}-svc
    return f"fn-{fn_name.lower()}-svc"


async def _get_or_create_lock(fn_name: str) -> asyncio.Lock:
    lock = _function_locks.get(fn_name)
    if lock is None:
        # 简单保护：创建新锁时加全局锁也可以，这里依赖 GIL+字典写少量冲突即可。
        lock = asyncio.Lock()
        _function_locks[fn_name] = lock
    return lock


async def _get_deployment_ready_replicas(fn_name: str) -> int:
    assert apps_v1_api is not None
    deploy_name = _deployment_name_from_function(fn_name)
    try:
        dep = await asyncio.to_thread(
            apps_v1_api.read_namespaced_deployment,
            name=deploy_name,
            namespace=NAMESPACE,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Function {fn_name} 不存在 Deployment")
        raise HTTPException(status_code=502, detail=f"查询 Deployment 失败: {e}")

    status = dep.status
    return int(status.ready_replicas or 0)


async def _scale_deployment_to_one(fn_name: str) -> None:
    """
    将 Deployment 的 replicas 从 0 调整到 1。
    """
    assert apps_v1_api is not None
    deploy_name = _deployment_name_from_function(fn_name)

    # 使用 scale 子资源，避免完整 patch 出错
    scale_body = {"spec": {"replicas": 1}}

    try:
        await asyncio.to_thread(
            apps_v1_api.patch_namespaced_deployment_scale,
            name=deploy_name,
            namespace=NAMESPACE,
            body=scale_body,
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Function {fn_name} 不存在 Deployment")
        raise HTTPException(status_code=502, detail=f"扩容 Deployment 失败: {e}")


async def _wait_for_pod_ready(fn_name: str) -> None:
    """
    轮询 Deployment 状态直到 ready_replicas >= 1 或超时。
    """
    total = 0.0
    while total < POLL_TIMEOUT_SECONDS:
        ready = await _get_deployment_ready_replicas(fn_name)
        if ready >= 1:
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        total += POLL_INTERVAL_SECONDS
    raise HTTPException(
        status_code=504,
        detail=f"等待 Function {fn_name} Pod 就绪超时 ({POLL_TIMEOUT_SECONDS}s)",
    )


async def _ensure_scaled(fn_name: str) -> None:
    """
    Scale-to-Zero 核心逻辑，配合内存锁避免并发重复扩容。
    """
    lock = await _get_or_create_lock(fn_name)
    async with lock:
        # 双重检查，避免已经有其他请求扩容成功
        ready = await _get_deployment_ready_replicas(fn_name)
        if ready > 0:
            return

        # 副本为 0 -> 先扩容到 1
        await _scale_deployment_to_one(fn_name)
        # 再等待就绪
        await _wait_for_pod_ready(fn_name)


async def _proxy_to_function_service(
    fn_name: str,
    request: Request,
) -> Response:
    """
    使用 httpx 反向代理到对应 Service。
    """
    svc_name = _service_name_from_function(fn_name)
    # 在集群内部通过 Service DNS 访问
    target_url = f"http://{svc_name}.{NAMESPACE}.svc.cluster.local:{SERVICE_PORT}{request.url.path}"

    # 保留查询参数
    if request.url.query:
        target_url += f"?{request.url.query}"

    # 复制请求头（去掉 host，交给 httpx/内核重写）
    headers = dict(request.headers)
    headers.pop("host", None)

    # 读取 body（FastAPI 的 Request 是 async 的）
    body = await request.body()

    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.request(
                method=request.method,
                url=target_url,
                content=body,
                headers=headers,
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"调用后端 Service 失败: {e}")

    # 将下游响应返回给调用方
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
        media_type=resp.headers.get("content-type"),
    )


@app.api_route("/invoke/{function_name}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def invoke_function(function_name: str, request: Request):
    """
    FaaS 网关入口：
    1. 检查 Deployment 的 ready_replicas；
    2. 若为 0，触发 Scale-to-One 并等待 Pod 就绪；
    3. 将请求代理到对应 Service。
    """
    # 1. 如有必要，触发冷启动唤醒
    await _ensure_scaled(function_name)

    # 2. 反向代理到 Service
    return await _proxy_to_function_service(function_name, request)