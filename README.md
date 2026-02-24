# 基于 K8s 的轻量级 Serverless 函数计算平台 (Python 版)

本项目是一个针对边缘计算场景设计的轻量级 FaaS（Function-as-a-Service）平台。利用 Kubernetes 原生能力，实现了高弹性的函数调度与冷启动极速优化。

## ✨ 核心特性

- **微秒级冷启动 (Scale-to-Zero)**：废弃传统的“源码即镜像”冗长流水线。首创**“预热运行时池 + ConfigMap 动态代码注入”**架构，将冷启动时间从秒级/分钟级优化至微秒级。
- **并发与防惊群**：Gateway 网关内置流量拦截与内存锁机制，确保在缩容到 0 的状态下，突发高并发请求仅触发一次 Pod 扩容，其余请求优雅挂起等待。
- **K8s 原生控制器**：基于 Python `kopf` 框架开发自定义 Operator，实现对 `Function` CRD 的生命周期自动化管理（自动对齐 Deployment 与 Service）。

---

## 🚀 快速开始 (Quick Start)

以下指南将带你在本地 `kind` 集群中完整跑通整个 FaaS 流程。

### 1. 环境准备
确保你已安装 `Docker`、`kubectl` 和 `kind`。
创建一个测试用的本地集群：
```bash
kind create cluster --name faas
kubectl cluster-info
