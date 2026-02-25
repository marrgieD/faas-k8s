import logging
import os
import traceback
from typing import Any, Dict, Optional

import kopf
from kubernetes import client, config
from kubernetes.client import ApiException
from kubernetes.config.config_exception import ConfigException

# CRD 基本信息
CRD_GROUP = "faas.example.com"
CRD_VERSION = "v1alpha1"
CRD_PLURAL = "functions"

# 默认镜像（假设你有一个能从 ConfigMap 挂载并执行 Python 代码的 FaaS Runtime 镜像）
DEFAULT_RUNTIME_IMAGE = os.getenv("FAAS_RUNTIME_IMAGE", "faas-python-runner:latest")
DEFAULT_PORT = int(os.getenv("FAAS_SERVICE_PORT", "8080"))

# 全局 K8s 客户端，在 startup 时初始化
apps_v1_api: Optional[client.AppsV1Api] = None
autoscaling_v2_api: Optional[client.AutoscalingV2Api] = None
core_v1_api: Optional[client.CoreV1Api] = None

# 配置日志
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("faas-operator")


# -------------------------
# 工具函数
# -------------------------


def load_kube_config() -> None:
    """
    优先加载 InCluster 配置，失败后回退到本地 kubeconfig。
    """
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes configuration.")
    except ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig configuration.")


def get_resource_names(fn_name: str) -> Dict[str, str]:
    """
    根据 Function 名称生成 Deployment / Service / ConfigMap 的名字。
    """
    safe_name = fn_name.lower()
    return {
        "deployment": f"fn-{safe_name}-deploy",
        "service": f"fn-{safe_name}-svc",
        "configmap": f"fn-{safe_name}-code",
    }


def validate_spec(spec: Dict[str, Any]) -> None:
    """
    对 Function 的 spec 做基础校验。
    校验失败时抛出 kopf.PermanentError，避免无限重试。
    """
    code = spec.get("code")
    runtime = spec.get("runtime")
    min_replicas = spec.get("minReplicas", 0)
    max_replicas = spec.get("maxReplicas")

    if not isinstance(code, str) or not code.strip():
        raise kopf.PermanentError("spec.code 必须是非空字符串。")

    if not isinstance(runtime, str) or not runtime.strip():
        raise kopf.PermanentError("spec.runtime 必须是非空字符串，例如 'python3.9'。")

    if max_replicas is None:
        raise kopf.PermanentError("spec.maxReplicas 为必填字段，且必须 >= 1。")

    if not isinstance(min_replicas, int) or min_replicas < 0:
        raise kopf.PermanentError("spec.minReplicas 必须是 >= 0 的整数。")

    if not isinstance(max_replicas, int) or max_replicas < 1:
        raise kopf.PermanentError("spec.maxReplicas 必须是 >= 1 的整数。")

    if min_replicas > max_replicas:
        raise kopf.PermanentError("spec.minReplicas 不能大于 spec.maxReplicas。")


def build_configmap_body(
    name: str,
    namespace: str,
    code: str,
    owner: Dict[str, Any],
) -> client.V1ConfigMap:
    """
    存储函数源码的 ConfigMap。
    """
    metadata = client.V1ObjectMeta(
        name=name,
        namespace=namespace,
        labels={
            "app.kubernetes.io/name": "faas-function",
            "app.kubernetes.io/managed-by": "faas-operator",
        },
        owner_references=[
            client.V1OwnerReference(
                api_version=f"{CRD_GROUP}/{CRD_VERSION}",
                kind="Function",
                name=owner["metadata"]["name"],
                uid=owner["metadata"]["uid"],
                controller=True,
                block_owner_deletion=True,
            )
        ],
    )

    data = {
        # 单 key 存储源码
        "handler.py": code,
    }

    return client.V1ConfigMap(
        api_version="v1",
        kind="ConfigMap",
        metadata=metadata,
        data=data,
    )


def build_deployment_body(
    name: str,
    namespace: str,
    spec: Dict[str, Any],
    code_cm_name: str,
    owner: Dict[str, Any],
) -> client.V1Deployment:
    """
    创建/更新用的 Deployment 描述。
    """
    min_replicas = spec.get("minReplicas", 0)
    runtime = spec.get("runtime")
    labels = {
        "app.kubernetes.io/name": "faas-function",
        "app.kubernetes.io/component": "runtime",
        "faas.example.com/function": owner["metadata"]["name"],
    }

    metadata = client.V1ObjectMeta(
        name=name,
        namespace=namespace,
        labels=labels,
        owner_references=[
            client.V1OwnerReference(
                api_version=f"{CRD_GROUP}/{CRD_VERSION}",
                kind="Function",
                name=owner["metadata"]["name"],
                uid=owner["metadata"]["uid"],
                controller=True,
                block_owner_deletion=True,
            )
        ],
    )

    # 容器定义：挂载 ConfigMap，并通过环境变量传递运行时信息
    container = client.V1Container(
        name="runtime",
        image=DEFAULT_RUNTIME_IMAGE,
        image_pull_policy="IfNotPresent",
        ports=[client.V1ContainerPort(container_port=DEFAULT_PORT)],
        env=[
            client.V1EnvVar(name="FUNCTION_RUNTIME", value=runtime),
            client.V1EnvVar(name="FUNCTION_CODE_PATH", value="/var/faas/function/handler.py"),
        ],
        volume_mounts=[
            client.V1VolumeMount(
                name="function-code",
                mount_path="/var/faas/function",
                read_only=True,
            )
        ],
        # 简单资源限制示例，可按需调整
        resources=client.V1ResourceRequirements(
            requests={"cpu": "50m", "memory": "64Mi"},
            limits={"cpu": "500m", "memory": "256Mi"},
        ),
    )

    pod_spec = client.V1PodSpec(
        containers=[container],
        volumes=[
            client.V1Volume(
                name="function-code",
                config_map=client.V1ConfigMapVolumeSource(name=code_cm_name),
            )
        ],
    )

    pod_template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels=labels),
        spec=pod_spec,
    )

    deployment_spec = client.V1DeploymentSpec(
        replicas=min_replicas,
        selector=client.V1LabelSelector(match_labels=labels),
        template=pod_template,
    )

    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=metadata,
        spec=deployment_spec,
    )


def build_service_body(
    name: str,
    namespace: str,
    owner: Dict[str, Any],
) -> client.V1Service:
    """
    为函数暴露 HTTP 入口的 Service。
    """
    labels = {
        "app.kubernetes.io/name": "faas-function",
        "app.kubernetes.io/component": "runtime",
        "faas.example.com/function": owner["metadata"]["name"],
    }

    metadata = client.V1ObjectMeta(
        name=name,
        namespace=namespace,
        labels=labels,
        owner_references=[
            client.V1OwnerReference(
                api_version=f"{CRD_GROUP}/{CRD_VERSION}",
                kind="Function",
                name=owner["metadata"]["name"],
                uid=owner["metadata"]["uid"],
                controller=True,
                block_owner_deletion=True,
            )
        ],
    )

    service_spec = client.V1ServiceSpec(
        type="ClusterIP",
        selector=labels,
        ports=[
            client.V1ServicePort(
                name="http",
                port=DEFAULT_PORT,
                target_port=DEFAULT_PORT,
                protocol="TCP",
            )
        ],
    )

    return client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=metadata,
        spec=service_spec,
    )


def upsert_configmap(
    namespace: str,
    body: client.V1ConfigMap,
    logger_: logging.Logger,
) -> str:
    """
    创建或更新 ConfigMap（幂等）。
    """
    assert core_v1_api is not None
    name = body.metadata.name

    try:
        # 先尝试读取，若存在则 patch
        core_v1_api.read_namespaced_config_map(name=name, namespace=namespace)
        logger_.info("ConfigMap %s 已存在，执行 patch。", name)
        core_v1_api.patch_namespaced_config_map(name=name, namespace=namespace, body=body)
    except ApiException as e:
        if e.status == 404:
            logger_.info("ConfigMap %s 不存在，执行 create。", name)
            try:
                core_v1_api.create_namespaced_config_map(namespace=namespace, body=body)
            except ApiException as inner_e:
                logger_.error(
                    "创建 ConfigMap %s 失败: %s\n%s",
                    name,
                    inner_e,
                    traceback.format_exc(),
                )
                raise kopf.TemporaryError(f"创建 ConfigMap 失败: {inner_e}", delay=30)
        else:
            logger_.error(
                "读取 ConfigMap %s 失败(status=%s): %s\n%s",
                name,
                e.status,
                e,
                traceback.format_exc(),
            )
            raise kopf.TemporaryError(f"读取/更新 ConfigMap 失败: {e}", delay=30)

    return name


def upsert_deployment(
    namespace: str,
    body: client.V1Deployment,
    logger_: logging.Logger,
) -> str:
    """
    创建或更新 Deployment（幂等）。
    """
    assert apps_v1_api is not None
    name = body.metadata.name

    try:
        apps_v1_api.read_namespaced_deployment(name=name, namespace=namespace)
        logger_.info("Deployment %s 已存在，执行 patch。", name)
        apps_v1_api.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
    except ApiException as e:
        if e.status == 404:
            logger_.info("Deployment %s 不存在，执行 create。", name)
            try:
                apps_v1_api.create_namespaced_deployment(namespace=namespace, body=body)
            except ApiException as inner_e:
                logger_.error(
                    "创建 Deployment %s 失败: %s\n%s",
                    name,
                    inner_e,
                    traceback.format_exc(),
                )
                raise kopf.TemporaryError(f"创建 Deployment 失败: {inner_e}", delay=30)
        else:
            logger_.error(
                "读取 Deployment %s 失败(status=%s): %s\n%s",
                name,
                e.status,
                e,
                traceback.format_exc(),
            )
            raise kopf.TemporaryError(f"读取/更新 Deployment 失败: {e}", delay=30)

    return name


def upsert_service(
    namespace: str,
    body: client.V1Service,
    logger_: logging.Logger,
) -> str:
    """
    创建或更新 Service（幂等）。
    注意 Service 的某些字段（如 clusterIP）不可变，这里使用 patch 并保持其不变。
    """
    assert core_v1_api is not None
    name = body.metadata.name

    try:
        existing = core_v1_api.read_namespaced_service(name=name, namespace=namespace)
        # 保持 clusterIP 不变
        if existing.spec and existing.spec.cluster_ip:
            body.spec.cluster_ip = existing.spec.cluster_ip

        logger_.info("Service %s 已存在，执行 patch。", name)
        core_v1_api.patch_namespaced_service(name=name, namespace=namespace, body=body)
    except ApiException as e:
        if e.status == 404:
            logger_.info("Service %s 不存在，执行 create。", name)
            try:
                core_v1_api.create_namespaced_service(namespace=namespace, body=body)
            except ApiException as inner_e:
                logger_.error(
                    "创建 Service %s 失败: %s\n%s",
                    name,
                    inner_e,
                    traceback.format_exc(),
                )
                raise kopf.TemporaryError(f"创建 Service 失败: {inner_e}", delay=30)
        else:
            logger_.error(
                "读取 Service %s 失败(status=%s): %s\n%s",
                name,
                e.status,
                e,
                traceback.format_exc(),
            )
            raise kopf.TemporaryError(f"读取/更新 Service 失败: {e}", delay=30)

    return name

def build_hpa_body(
    name: str,
    namespace: str,
    spec: Dict[str, Any],
    deploy_name: str,
    owner: Dict[str, Any],
) -> client.V2HorizontalPodAutoscaler:
    """生成 HPA 配置：当 CPU 超过 50% 时自动扩容"""
    min_replicas = max(1, spec.get("minReplicas", 1)) # HPA必须至少为1
    max_replicas = spec.get("maxReplicas", 3)

    metadata = client.V1ObjectMeta(
        name=name,
        namespace=namespace,
        labels={"app.kubernetes.io/managed-by": "faas-operator"},
        owner_references=[
            client.V1OwnerReference(
                api_version=f"{CRD_GROUP}/{CRD_VERSION}",
                kind="Function",
                name=owner["metadata"]["name"],
                uid=owner["metadata"]["uid"],
                controller=True,
                block_owner_deletion=True,
            )
        ],
    )

    hpa_spec = client.V2HorizontalPodAutoscalerSpec(
        scale_target_ref=client.V2CrossVersionObjectReference(
            api_version="apps/v1",
            kind="Deployment",
            name=deploy_name,
        ),
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        metrics=[
            client.V2MetricSpec(
                type="Resource",
                resource=client.V2ResourceMetricSource(
                    name="cpu",
                    target=client.V2MetricTarget(
                        type="Utilization",
                        average_utilization=50  # 阈值：50% CPU
                    ),
                ),
            )
        ],
    )

    return client.V2HorizontalPodAutoscaler(
        api_version="autoscaling/v2",
        kind="HorizontalPodAutoscaler",
        metadata=metadata,
        spec=hpa_spec,
    )

def upsert_hpa(namespace: str, body: client.V2HorizontalPodAutoscaler, logger_: logging.Logger) -> str:
    """创建或更新 HPA（幂等）"""
    assert autoscaling_v2_api is not None
    name = body.metadata.name

    try:
        autoscaling_v2_api.read_namespaced_horizontal_pod_autoscaler(name=name, namespace=namespace)
        logger_.info("HPA %s 已存在，执行 patch。", name)
        autoscaling_v2_api.patch_namespaced_horizontal_pod_autoscaler(name=name, namespace=namespace, body=body)
    except ApiException as e:
        if e.status == 404:
            logger_.info("HPA %s 不存在，执行 create。", name)
            autoscaling_v2_api.create_namespaced_horizontal_pod_autoscaler(namespace=namespace, body=body)
        else:
            raise kopf.TemporaryError(f"读取/更新 HPA 失败: {e}", delay=30)
    return name

def update_status(
    name: str,
    namespace: str,
    patch: kopf.Patch,
    phase: str,
    reason: Optional[str],
    deployment_name: Optional[str],
    service_name: Optional[str],
    generation: Optional[int],
) -> None:
    """
    更新自定义资源的 status 字段，供 kubectl get/describe 使用。
    """
    status: Dict[str, Any] = patch.setdefault("status", {})
    status["phase"] = phase
    status["reason"] = reason
    status["deploymentName"] = deployment_name
    status["serviceName"] = service_name
    if generation is not None:
        status["observedGeneration"] = generation


def reconcile_function(
    name: str,
    namespace: str,
    spec: Dict[str, Any],
    body: Dict[str, Any],
    patch: kopf.Patch,
    logger_: logging.Logger,
) -> Dict[str, Any]:
    """
    核心对齐逻辑：根据 Function 资源的期望状态创建/更新
    ConfigMap + Deployment + Service。
    """
    validate_spec(spec)

    names = get_resource_names(name)
    code = spec.get("code")

    # 1. ConfigMap（保存源码）
    cm_body = build_configmap_body(
        name=names["configmap"],
        namespace=namespace,
        code=code,
        owner=body,
    )
    cm_name = upsert_configmap(namespace=namespace, body=cm_body, logger_=logger_)

    # 2. Deployment（runtime）
    deploy_body = build_deployment_body(
        name=names["deployment"],
        namespace=namespace,
        spec=spec,
        code_cm_name=cm_name,
        owner=body,
    )
    deploy_name = upsert_deployment(namespace=namespace, body=deploy_body, logger_=logger_)

    # 3. Service（HTTP 入口）
    svc_body = build_service_body(
        name=names["service"],
        namespace=namespace,
        owner=body,
    )
    svc_name = upsert_service(namespace=namespace, body=svc_body, logger_=logger_)

    # 4. 新增：注入 HPA (只有当 maxReplicas > 1 且 minReplicas >= 1 时，才启用 HPA)
    if spec.get("maxReplicas", 1) > 1 and spec.get("minReplicas", 0) >= 1:
        hpa_body = build_hpa_body(
            name=f"{names['deployment']}-hpa",
            namespace=namespace,
            spec=spec,
            deploy_name=deploy_name,
            owner=body,
        )
        upsert_hpa(namespace=namespace, body=hpa_body, logger_=logger_)

    update_status(
        name=name,
        namespace=namespace,
        patch=patch,
        phase="Ready",
        reason=None,
        deployment_name=deploy_name,
        service_name=svc_name,
        generation=body.get("metadata", {}).get("generation"),
    )

    logger_.info(
        "Function %s/%s 对齐完成，对应 Deployment=%s, Service=%s",
        namespace,
        name,
        deploy_name,
        svc_name,
    )

    # 返回值会写入 status（由 kopf 处理）
    return {
        "deploymentName": deploy_name,
        "serviceName": svc_name,
        "phase": "Ready",
    }


# -------------------------
# Kopf 入口
# -------------------------


@kopf.on.startup()
def startup(logger: logging.Logger, **_: Any) -> None:
    """
    Operator 启动时调用：初始化 K8s 客户端与 Kopf 设置。
    """
    global apps_v1_api, core_v1_api, autoscaling_v2_api 

    logger.info("FaaS Operator 正在启动，初始化 Kubernetes 客户端...")
    load_kube_config()
    apps_v1_api = client.AppsV1Api()
    core_v1_api = client.CoreV1Api()
    autoscaling_v2_api = client.AutoscalingV2Api()  
    logger.info("Kubernetes 客户端初始化完成。")


@kopf.on.create(CRD_GROUP, CRD_VERSION, CRD_PLURAL)
def on_create(
    spec: Dict[str, Any],
    name: str,
    namespace: str,
    body: Dict[str, Any],
    patch: kopf.Patch,
    logger: logging.Logger,
    **_: Any,
):
    """
    当创建新的 Function 资源时触发。
    """
    logger.info("检测到 Function 创建事件: %s/%s", namespace, name)
    try:
        return reconcile_function(
            name=name,
            namespace=namespace,
            spec=spec,
            body=body,
            patch=patch,
            logger_=logger,
        )
    except kopf.PermanentError:
        # 已经在 validate_spec 中记录了原因，这里直接抛出即可
        logger.exception("Function %s/%s 规范校验失败。", namespace, name)
        raise
    except Exception as e:  # 捕获所有异常避免 Operator 崩溃
        logger.error(
            "处理 Function 创建事件失败: %s/%s, error=%s\n%s",
            namespace,
            name,
            e,
            traceback.format_exc(),
        )
        # 使用 TemporaryError 触发重试
        raise kopf.TemporaryError(f"创建 Function 失败: {e}", delay=30)


@kopf.on.update(CRD_GROUP, CRD_VERSION, CRD_PLURAL)
def on_update(
    spec: Dict[str, Any],
    name: str,
    namespace: str,
    body: Dict[str, Any],
    patch: kopf.Patch,
    logger: logging.Logger,
    **_: Any,
):
    """
    当 Function 资源被更新（code / runtime / minReplicas / maxReplicas 等）时触发。
    """
    logger.info("检测到 Function 更新事件: %s/%s", namespace, name)
    try:
        return reconcile_function(
            name=name,
            namespace=namespace,
            spec=spec,
            body=body,
            patch=patch,
            logger_=logger,
        )
    except kopf.PermanentError:
        logger.exception("Function %s/%s 规范校验失败。", namespace, name)
        raise
    except Exception as e:
        logger.error(
            "处理 Function 更新事件失败: %s/%s, error=%s\n%s",
            namespace,
            name,
            e,
            traceback.format_exc(),
        )
        raise kopf.TemporaryError(f"更新 Function 失败: {e}", delay=30)


@kopf.on.resume(CRD_GROUP, CRD_VERSION, CRD_PLURAL)
def on_resume(
    spec: Dict[str, Any],
    name: str,
    namespace: str,
    body: Dict[str, Any],
    patch: kopf.Patch,
    logger: logging.Logger,
    **_: Any,
):
    """
    Operator 重启/恢复时，对现有的 Function 做一次对齐，保证幂等。
    """
    logger.info("Operator 恢复，对 Function 进行重新对齐: %s/%s", namespace, name)
    try:
        return reconcile_function(
            name=name,
            namespace=namespace,
            spec=spec,
            body=body,
            patch=patch,
            logger_=logger,
        )
    except kopf.PermanentError:
        logger.exception("Function %s/%s 规范校验失败（on_resume）。", namespace, name)
        raise
    except Exception as e:
        logger.error(
            "恢复 Function 状态失败: %s/%s, error=%s\n%s",
            namespace,
            name,
            e,
            traceback.format_exc(),
        )
        raise kopf.TemporaryError(f"恢复 Function 失败: {e}", delay=60)