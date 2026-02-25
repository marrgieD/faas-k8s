# 基于 K8s 的轻量级 Serverless 函数计算平台 (Python 版)

本项目是一个针对边缘计算场景设计的轻量级 FaaS（Function-as-a-Service）平台。利用 Kubernetes 原生能力，实现了高弹性的函数调度与冷启动极速优化。

## ✨ 核心特性

- **微秒级冷启动 (Scale-to-Zero)**：废弃传统的“源码即镜像”冗长流水线。首创**“预热运行时池 + ConfigMap 动态代码注入”**架构，将冷启动时间从秒级/分钟级优化至微秒级。
- **并发与防惊群**：Gateway 网关内置流量拦截与内存锁机制，确保在缩容到 0 的状态下，突发高并发请求仅触发一次 Pod 扩容，其余请求优雅挂起等待。
- **K8s 原生控制器**：基于 Python `kopf` 框架开发自定义 Operator，实现对 `Function` CRD 的生命周期自动化管理（自动对齐 Deployment 与 Service）。

## 🚀 快速开始 (Quick Start)

以下指南将带你在本地 `kind` 集群中完整跑通整个 FaaS 流程。

### 1. 环境准备

确保你已安装 `Docker`、`kubectl` 和 `kind`。
创建一个测试用的本地集群：

```bash
kind create cluster --name faas
kubectl cluster-info
```

准备 Python 虚拟环境并安装核心依赖：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 构建核心镜像并装载至集群

本系统包含两个核心镜像：负责执行 Python 源码的 **Runner** 镜像，以及负责流量拦截与转发的 **Gateway** 镜像。

```bash
# 1. 构建并装载 Runner 预热镜像
cd runner
docker build -t faas-python-runner:latest .
kind load docker-image faas-python-runner:latest --name faas
cd ..

# 2. 构建并装载 Gateway 网关镜像
docker build -t faas-gateway:latest -f gateway/Dockerfile .
kind load docker-image faas-gateway:latest --name faas
```

### 3. 部署集群基础组件 (CRD & Gateway)

将我们定义的 Function 资源规范，以及网关服务部署到 K8s 中：



```bash
# 部署 CRD
kubectl apply -f crd.yaml

# 部署 Gateway (包含 Namespace, RBAC, Deployment 和 Service)
kubectl apply -f gateway-deploy.yaml

# 检查 Gateway 是否就绪
kubectl get pods -n faas-system
```

### 4. 启动 FaaS Operator (控制面)

在本地终端中直接启动 Operator（它会自动读取 `~/.kube/config` 连接到 kind 集群）。 *⚠️ 注意：请保持此终端开启，不要关闭。*

```bash
# 确保在 venv 环境下，并取消可能存在的系统代理以免影响 K8s API 通信
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

kopf run operator/main.py --namespace default
```

## 🎯 体验 Serverless 冷启动

现在，你的 FaaS 平台已经就绪！我们将部署一个名为 `hello` 的函数，并体验它从 0 副本自动扩容的神奇过程。

### 1. 部署函数

新开一个终端，查看 `function-hello.yaml`，它的 `minReplicas` 配置为 `0`：

```bash
kubectl apply -f function-hello.yaml

# 此时查看 Pod，你会发现没有任何 hello 函数的实例在运行（Scale to Zero）
kubectl get deploy,pod -l [faas.example.com/function=hello](https://faas.example.com/function=hello) -n default
```

### 2. 触发调用

我们需要将集群内 Gateway 的端口映射到宿主机来模拟外部用户请求：

```bash
kubectl port-forward svc/faas-gateway 8000:8000 -n faas-system
```

再新开一个终端，发起请求：

```bash
curl http://localhost:8000/invoke/hello
```

### 3. 见证奇迹的时刻

当上述 `curl` 命令发出时，你会观察到：

1. 请求会被 Gateway 拦截并挂起。
2. Operator 终端中会打印出扩容日志。
3. K8s 中瞬间拉起一个 `fn-hello-deploy-xxx` 的 Pod。
4. Pod 就绪（毫秒级注入代码）后，终端成功返回：`hello from runner`！

之后再次执行 `curl`，请求将瞬间返回，因为预热容器已经在线。

### 这个版本为什么更好？

1. **彻底分离了镜像构建和部署**：把 Runner 和 Gateway 两个镜像的构建放在了同一站，逻辑更紧凑。
2. **隐藏了本地联调的复杂环境变量**：简历项目展示的是**“你的成果”**，面试官只想知道在 K8s 里怎么跑通，之前那些 `FAAS_IN_CLUSTER=false` 和繁杂的 `export` 会让人觉得系统不够云原生。
3. **凸显了亮点**：把你的“毫秒级冷启动”和“Scale-to-Zero”体验过程变成了剧本式的操作，给克隆你代码的人一种极强的成就感。

### 场景二：原生 HPA 自动扩缩容 (1 -> N)

我们在后台发起并发死循环请求（模拟 CPU 飙升）：

修改函数配置为：minReplicas: 1, maxReplicas: 5

可以利用压测工具（如 hey 或 apache bench）或并发脚本对网关施压

查看 K8s 原生 HPA 状态，见证 Pod 从 1 自动扩容到 N 的过程：

```bash
kubectl get hpa
kubectl get pods -w
```

1. **终端 A（观察网关转发）**：确保你的 port-forward 还在运行着。

   Bash

   ```bash
   kubectl port-forward svc/faas-gateway 8000:8000 -n faas-system
   ```

2. **终端 B（实时观察 K8s 指标）**：开启这个监控命令，它每 2 秒刷新一次，你可以亲眼看到 CPU 使用率破表，以及 Pod 数量的增加。

   Bash

   ```bash
   watch -n 2 "kubectl get hpa; echo ''; kubectl get pods -l faas.example.com/function=cpu-test"
   ```

   *(最开始你应该只看到 1 个 Pod，且 HPA 的 TARGETS 栏显示大概 `0%/50%`)*

3. **终端 C（发起总攻）**：在这个窗口运行你的压测脚本！

   Bash

   ```bash
   source venv/bin/activate
   python stress_test.py
   ```

### 你将会观察到什么现象？

1. 脚本跑起来大概 15 秒后（K8s 采集指标需要一点时间），你会看到终端 B 里的 HPA TARGETS 飙升到了 `100%/50%` 甚至更高。
2. 紧接着，K8s 会果断出手，下面的 Pod 列表会瞬间从 1 个变成 2 个，再变成 4 个、5 个（达到你设定的 `maxReplicas` 上限）。
3. 60 秒后压测脚本结束，流量停止。
4. 再等大约 5 分钟（K8s 原生的缩容冷却期，防止网络抖动导致频繁起停），多余的 Pod 会自动被销毁，再次缩回 1 个副本。
