import asyncio
import httpx
import time

async def worker(client, worker_id, duration):
    """每个 worker 会在规定时间内拼命发送请求"""
    end_time = time.time() + duration
    count = 0
    while time.time() < end_time:
        try:
            # 请求咱们刚才部署的 cpu-test 函数
            await client.get("http://localhost:8000/invoke/cpu-test", timeout=10.0)
            count += 1
        except:
            pass
    return count

async def main():
    duration = 60  # 持续压测 60 秒
    concurrency = 30  # 模拟 30 个并发用户
    print(f"🚀 开始疯狂压测，持续 {duration} 秒，并发用户数 {concurrency}...")
    
    async with httpx.AsyncClient() as client:
        # 同时启动 30 个任务疯狂发请求
        tasks = [worker(client, i, duration) for i in range(concurrency)]
        results = await asyncio.gather(*tasks)
        
    print(f"✅ 压测结束！总共完成了 {sum(results)} 次函数调用。")

if __name__ == "__main__":
    asyncio.run(main())