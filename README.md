### 1. 环境准备

```bash
cd /home/duanmj4/cloudnative

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
# requirements.txt 至少包含：
# kopf
# kubernetes
# fastapi
# uvicorn[standard]
# httpx
```

安装 kind（已装可跳过）：

```bash
kind create cluster --name faas
kubectl cluster-info
```

### 2. 构建 Runner 镜像并导入 kind

```bash
cd /home/duanmj4/cloudnative/runner

docker build -t faas-python-runner:latest .

# 把本地镜像灌进 kind 集群
kind load docker-image faas-python-runner:latest --name faas
```

确认镜像名要和 operator 里 `DEFAULT_RUNTIME_IMAGE` 一致（默认是 `faas-python-runner:latest`）。

### 3. 安装 CRD + 启动 Operator

```bash
cd ./cloudnative
kubectl apply -f crd.yaml
kubectl get crd functions.faas.example.com
```

新开一个终端（记得激活 venv，且 **unset 代理**）：

```bash
cd ./cloudnative
source venv/bin/activate

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

kopf run operator/main.py --namespace default
```

保持这个终端不关。

### 4. 创建一个 Function（代码走 ConfigMap 注入）

`function-hello.yaml` 目前是：

```yaml
apiVersion: faas.example.com/v1alpha1
kind: Function
metadata:
  name: hello
  namespace: default
spec:
  code: |
    def main():
        return "hello from runner"
  runtime: python3.9
  minReplicas: 1
  maxReplicas: 3
```

注意 `code` 下面两行要用 **2–4 个空格缩进**，不要混 TAB。

应用：

```bash
kubectl apply -f function-hello.yaml
kubectl get functions
kubectl get deploy,svc,pod -l faas.example.com/function=hello -n default
```

因为 `minReplicas: 1`，等一会儿应该看到：

- `deployment.apps/fn-hello-deploy   1/1`
- 有一个 `fn-hello-deploy-xxxxx` Pod 处于 `Running`.

### 5. 直连 Runner 验证代码是否真的执行

```bash
kubectl port-forward svc/fn-hello-svc 18080:8080 -n default
# 另开一终端：
curl http://localhost:18080/
```

**预期返回：**

```text
hello from runner
```

### 6. 启动 Gateway + 体验 /invoke

网关有两种模式：

- **集群内模式（IN_CLUSTER=true）**：部署到 K8s 里；
- **本地调试模式（IN_CLUSTER=false + port-forward）**。

你现在已经在用 `port-forward` 了，所以走本地调试模式比较简单：

1. 保持前面的 `kubectl port-forward svc/fn-hello-svc 18080:8080` 在一个终端跑着。
2. 启动 gateway：

```bash
cd /home/duanmj4/cloudnative
source venv/bin/activate

export FAAS_IN_CLUSTER=false
export FAAS_UPSTREAM_HOST=localhost
export FAAS_UPSTREAM_PORT=18080

uvicorn gateway.main:app --host 0.0.0.0 --port 8000
```

3. 再在另一个终端发请求：

```bash
curl http://localhost:8000/invoke/hello
```

**预期：**

- 第一次：如果你把 `minReplicas` 改回 `0`，网关会触发 scale 0→1，然后等 Pod Ready，再转发；
- 返回内容应该也是 `hello from runner`。



很好，现在本地模式已经通了，切到 **IN_CLUSTER=true** 就是把 gateway 也塞进 kind 里跑一遍整链路。下面给你一套最小步骤。

### 1. 为 gateway 做一个镜像并丢进 kind

在项目根目录新建一个简单 Dockerfile（例如 `gateway/Dockerfile`，如果你已经有就按现有来）：

```dockerfile
FROM python:3.9-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn httpx kubernetes

COPY gateway /app/gateway

ENV FAAS_IN_CLUSTER=true \
    FAAS_NAMESPACE=default \
    FAAS_SERVICE_PORT=8080

EXPOSE 8000

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

构建并加载到 kind（假设镜像叫 `faas-gateway:latest`，名字你可以自定，只要和下面 Deployment 对上）：

```bash
cd /home/duanmj4/cloudnative
docker build -t faas-gateway:latest -f gateway/Dockerfile .
kind load docker-image faas-gateway:latest --name faas
```

### 2. 在集群里部署 gateway（IN_CLUSTER=true）

准备一个最小的 Deployment + Service（保存成 `gateway-deploy.yaml`）：

应用：

```bash
kubectl apply -f gateway-deploy.yaml
kubectl get pod -n faas-system
```

pod `faas-gateway-xxx` Ready 后，gateway 已经在集群内、`IN_CLUSTER=true` 模式下跑着了。

### 3. 体验 /invoke（集群内调用）

现在：

- `Function hello` 的 CRD 还在；
- Operator 还在跑；
- Runner 镜像已导入，`fn-hello-deploy` 副本数可以先设成 0（体验 scale‑to‑zero）。

你可以从宿主机通过 port‑forward 打进 gateway：

```bash
kubectl port-forward svc/faas-gateway 8000:8000 -n faas-system
```

另开一个终端：

```bash
curl http://localhost:8000/invoke/hello
```

观察：

```bash
kubectl get deploy,pod -l faas.example.com/function=hello -n default
```

- 一开始 `fn-hello-deploy` 是 `0/0`；
- 发请求后，gateway 在 **集群内部** 用 Service DNS 调用、触发 `_ensure_scaled()` → `replicas` 变成 1；
- Pod Ready 后，请求被转发到 `fn-hello-svc`，返回你在 `handler.py` 里的 `"hello from runner"`。

这样你就完整跑了一遍 **“Operator + Gateway + Runner 全在 K8s 集群里，IN_CLUSTER=true 的 FaaS 流程”**。
