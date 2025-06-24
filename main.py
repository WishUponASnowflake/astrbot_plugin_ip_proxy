# -*- coding: utf-8 -*-
import asyncio
import aiohttp
import time
import re  # <-- [新增] 导入正则表达式模块
from asyncio import StreamReader, StreamWriter, Task, Server

from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult

# 使用 @register 装饰器注册插件
@register(
    "astrbot_plugin_ip_proxy",
    "timetetng",
    "一个将HTTP代理API转换为本地代理的AstrBot插件 ",
    "1.3", # 版本号+0.1
    "https://github.com/timetetng/astrbot_plugin_ip_proxy"
)
class IPProxyPlugin(Star):
    """
    IP代理插件主类。
    采用 AstrBot 配置系统，通过API获取HTTP代理IP，并在本地启动一个代理服务。
    用户可以通过独立的指令来控制和配置代理服务。
    所有指令均需要管理员权限。
    """
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config: AstrBotConfig = config
        self.server: Server | None = None
        self.server_task: Task | None = None
        self.current_ip: str | None = None
        self.current_port: int | None = None
        self.last_validation_time: float | None = None
        self.ip_usage_count: int = 0
        self.ip_lock = asyncio.Lock()
        self.http_session = aiohttp.ClientSession()
        
        logger.info("IP代理插件: 插件已加载，配置已注入。")

        if self.config.get("start_on_load", True):
            logger.info("IP代理插件: 根据配置，正在自动启动代理服务...")
            self.server_task = asyncio.create_task(self.start_local_proxy_server())
    
    # --- [新增] ---
    # 用于从HTTP请求头中解析目标主机名
    def _extract_hostname(self, request_data: bytes) -> str | None:
        """从客户端初始请求数据中解析目标主机名"""
        try:
            request_str = request_data.decode('utf-8', errors='ignore')
            
            # 1. 匹配HTTPS的CONNECT请求, e.g., CONNECT example.com:443 HTTP/1.1
            connect_match = re.search(r'CONNECT\s+([a-zA-Z0-9.-]+):\d+', request_str, re.IGNORECASE)
            if connect_match:
                return connect_match.group(1).lower()

            # 2. 匹配HTTP请求的Host头, e.g., Host: example.com
            host_match = re.search(r'Host:\s+([a-zA-Z0-9.-]+)', request_str, re.IGNORECASE)
            if host_match:
                return host_match.group(1).lower()
                
        except Exception:
            pass # 解码或正则匹配失败
        return None

    # --- [核心修改] ---
    # 重写 handle_connection，加入白名单验证逻辑
    async def handle_connection(self, reader: StreamReader, writer: StreamWriter):
        addr = writer.get_extra_info('peername')
        remote_writer: StreamWriter | None = None
        tasks = []

        try:
            # --- 阶段1: 读取并验证请求 ---
            initial_data = await asyncio.wait_for(reader.read(4096), timeout=10.0)
            if not initial_data: return

            allowed_domains = set(self.config.get("allowed_domains", []))
            if not allowed_domains:
                logger.error("域名白名单 'allowed_domains' 未配置或为空，所有请求都将被拒绝。")
                writer.write(b'HTTP/1.1 500 Internal Server Error\r\nConnection: close\r\n\r\n')
                await writer.drain()
                return

            hostname = self._extract_hostname(initial_data)
            if not hostname or hostname not in allowed_domains:
                logger.info(f"拒绝来自 {addr} 的请求，目标主机 '{hostname or '未知'}' 不在白名单中。")
                writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
                await writer.drain()
                return
            
            # --- 阶段2: 连接上游代理 (验证通过后才执行) ---
            logger.debug(f"接受来自 {addr} 的请求，转发到白名单主机: {hostname}")
            remote_ip, remote_port = await self.get_valid_ip()
            if not remote_ip or not remote_port:
                logger.error(f"无法为来自 {addr} 的白名单请求获取有效代理IP。")
                writer.write(b'HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n')
                await writer.drain()
                return

            connect_timeout = self.config.get("connect_timeout", 10)
            conn_future = asyncio.open_connection(remote_ip, remote_port)
            remote_reader, remote_writer = await asyncio.wait_for(conn_future, timeout=connect_timeout)

            # --- 阶段3: 转发数据 ---
            # 首先，将我们已读的初始数据发送给远程代理
            remote_writer.write(initial_data)
            await remote_writer.drain()

            # 然后，创建任务继续转发后续数据
            async def forward(src: StreamReader, dst: StreamWriter):
                try:
                    while not src.at_eof():
                        data = await src.read(4096)
                        if not data: break
                        dst.write(data); await dst.drain()
                except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError): pass
                finally:
                    if not dst.is_closing(): dst.close()
            
            task1 = asyncio.create_task(forward(reader, remote_writer))
            task2 = asyncio.create_task(forward(remote_reader, writer))
            tasks = [task1, task2]

            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()

        except asyncio.TimeoutError:
            logger.debug(f"客户端 {addr} 在10秒内未发送有效请求头，连接关闭。")
        except Exception as e:
            logger.error(f"处理连接 {addr} 时发生错误: {e!r}")
            for task in tasks:
                if not task.done(): task.cancel()
        finally:
            # 统一关闭所有流
            if remote_writer and not remote_writer.is_closing():
                remote_writer.close()
            if not writer.is_closing():
                writer.close()
            logger.debug(f"与 {addr} 的连接已关闭。")
            
    # --- 以下方法保持不变 ---

    async def get_new_ip(self) -> tuple[str | None, int | None]:
        api_url = self.config.get("api_url")
        if not api_url or "YOUR_TOKEN" in api_url:
            logger.warning("IP代理插件: API URL 未配置，无法获取新IP。")
            return None, None
        try:
            if self.http_session.closed: self.http_session = aiohttp.ClientSession()
            async with self.http_session.get(api_url) as response:
                response.raise_for_status()
                ip_port = (await response.text()).strip()
                if ":" in ip_port:
                    ip, port_str = ip_port.split(":")
                    port = int(port_str)
                    self.ip_usage_count += 1
                    logger.info(f"IP代理插件: 获取到新IP: {ip}:{port}。累计已使用: {self.ip_usage_count}个")
                    return ip, port
                else:
                    logger.warning(f"IP代理插件: API返回格式错误: {ip_port}")
                    return None, None
        except Exception as e:
            logger.error(f"IP代理插件: 获取IP失败: {e}")
            return None, None

    async def is_ip_valid(self, ip: str, port: int) -> bool:
        validation_url = self.config.get("validation_url", "http://www.baidu.com")
        timeout_config = aiohttp.ClientTimeout(total=self.config.get("validation_timeout", 5))
        proxy_url = f"http://{ip}:{port}"
        try:
            if self.http_session.closed: self.http_session = aiohttp.ClientSession()
            async with self.http_session.get(validation_url, proxy=proxy_url, timeout=timeout_config) as response:
                if response.status == 200:
                    logger.info(f"IP {ip}:{port} 验证成功。")
                    return True
        except Exception as e:
            logger.warning(f"IP {ip}:{port} 访问 {validation_url} 验证失败: {e}")
        return False

    async def get_valid_ip(self) -> tuple[str | None, int | None]:
        async with self.ip_lock:
            ip_expiration_time = self.config.get("ip_expiration_time", 300)
            validation_interval = self.config.get("validation_interval", 60)
            if self.current_ip and self.current_port and self.last_validation_time:
                ip_age = time.time() - self.last_validation_time
                if ip_expiration_time > 0 and ip_age > ip_expiration_time:
                    logger.info(f"IP {self.current_ip}:{self.current_port} 已使用超过 {ip_expiration_time} 秒，强制获取新IP。")
                    self.current_ip = None
                elif ip_age < validation_interval:
                    logger.debug(f"使用缓存中的IP: {self.current_ip}:{self.current_port} (验证间隔内)")
                    return self.current_ip, self.current_port
                else:
                    logger.debug(f"IP {self.current_ip}:{self.current_port} 需重新验证...")
                    if await self.is_ip_valid(self.current_ip, self.current_port):
                        self.last_validation_time = time.time()
                        logger.debug(f"IP {self.current_ip}:{self.current_port} 验证成功，继续使用。")
                        return self.current_ip, self.current_port
                    else:
                        logger.info(f"IP {self.current_ip}:{self.current_port} 重新验证失败，获取新IP。")
                        self.current_ip = None
            if not self.current_ip:
                for _ in range(3):
                    new_ip, new_port = await self.get_new_ip()
                    if new_ip and new_port:
                        if await self.is_ip_valid(new_ip, new_port):
                            self.current_ip, self.current_port = new_ip, new_port
                            self.last_validation_time = time.time()
                            return self.current_ip, self.current_port
                    logger.warning("获取的新IP无效或验证失败，1秒后重试...")
                    await asyncio.sleep(1)
            if not self.current_ip:
                logger.error("多次尝试后，仍无法获取到有效的IP地址。")
                return None, None
            return self.current_ip, self.current_port

    async def start_local_proxy_server(self):
        listen_host = self.config.get("listen_host", "127.0.0.1")
        local_port = self.config.get("local_port", 8888)
        try:
            self.server = await asyncio.start_server(self.handle_connection, listen_host, local_port)
            logger.info(f"本地代理服务器已启动，监听地址: {listen_host}:{local_port}")
            await self.server.serve_forever()
        except asyncio.CancelledError:
            logger.info("本地代理服务器任务被取消。")
        except Exception as e:
            logger.error(f"启动本地代理服务器失败: {e}，请检查端口是否被占用或配置是否正确。")
        finally:
            if self.server and self.server.is_serving():
                self.server.close(); await self.server.wait_closed()
            logger.info("本地代理服务器已关闭。")
            self.server = None; self.server_task = None

    async def terminate(self):
        logger.info("IP代理插件正在终止...")
        if self.server_task and not self.server_task.done():
            self.server_task.cancel()
            try: await self.server_task
            except asyncio.CancelledError: pass
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        logger.info("IP代理插件已终止。")

    # ---- 指令处理部分 (无需修改) ----
    @filter.command("开启代理", alias={"启动代理", "代理开启"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def start_proxy(self, event: AstrMessageEvent) -> MessageEventResult:
        if self.server_task and not self.server_task.done():
            return event.plain_result("代理服务已经在运行中。")
        self.server_task = asyncio.create_task(self.start_local_proxy_server())
        listen_host = self.config.get("listen_host", "127.0.0.1")
        local_port = self.config.get("local_port", 8888)
        return event.plain_result(f"代理服务已启动，监听于 {listen_host}:{local_port}")

    @filter.command("关闭代理", alias={"代理关闭", "取消代理"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def stop_proxy(self, event: AstrMessageEvent) -> MessageEventResult:
        if not self.server_task or self.server_task.done():
            return event.plain_result("代理服务未在运行。")
        self.server_task.cancel()
        try: await self.server_task
        except asyncio.CancelledError: pass
        self.server_task = None; self.server = None
        return event.plain_result("代理服务已停止。")

    @filter.command("代理状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def status_proxy(self, event: AstrMessageEvent) -> MessageEventResult:
        status_text = "✅运行中" if self.server_task and not self.server_task.done() else "❌已停止"
        ip_text = f"{self.current_ip}:{self.current_port}" if self.current_ip else "无"
        listen_host = self.config.get("listen_host", "127.0.0.1")
        local_port = self.config.get("local_port", 8888)
        status_message = (
            f"--- IP代理插件状态 ---\n"
            f"运行状态: {status_text}\n"
            f"监听地址: {listen_host}:{local_port}\n"
            f"当前代理IP: {ip_text}\n"
            f"累计获取IP数: {self.ip_usage_count}\n\n"
            f"白名单域名: {', '.join(self.config.get('allowed_domains', ['未配置']))}"
        )
        return event.plain_result(status_message)

    @filter.command("修改代理API")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_api_url(self, event: AstrMessageEvent, api_url: str) -> MessageEventResult:
        self.config["api_url"] = api_url
        self.config.save_config()
        return event.plain_result(f"✅ 代理API地址已更新为: {api_url}")

    @filter.command("修改监听地址")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_listen_host(self, event: AstrMessageEvent, host: str) -> MessageEventResult:
        self.config["listen_host"] = host
        self.config.save_config()
        return event.plain_result(f"✅ 监听地址已更新为: {host}\n重启代理后生效。")

    @filter.command("修改监听端口")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_listen_port(self, event: AstrMessageEvent, port: int) -> MessageEventResult:
        self.config["local_port"] = port
        self.config.save_config()
        return event.plain_result(f"✅ 监听端口已更新为: {port}\n重启代理后生效。")

    @filter.command("修改测试url")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_validation_url(self, event: AstrMessageEvent, url: str) -> MessageEventResult:
        self.config["validation_url"] = url
        self.config.save_config()
        return event.plain_result(f"✅ 验证URL已更新为: {url}")
        
    @filter.command("修改IP失效时间")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_ip_expiration_time(self, event: AstrMessageEvent, seconds: int) -> MessageEventResult:
        self.config["ip_expiration_time"] = seconds
        self.config.save_config()
        if seconds > 0:
            return event.plain_result(f"✅ IP绝对失效时间已更新为: {seconds} 秒。")
        else:
            return event.plain_result(f"✅ IP绝对失效时间已设置为永不强制失效。")