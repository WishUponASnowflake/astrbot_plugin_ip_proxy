# -*- coding: utf-8 -*-
import asyncio
import aiohttp
import time
from asyncio import StreamReader, StreamWriter, Task, Server

from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult

# 使用 @register 装饰器注册插件
@register(
    "astrbot_plugin_ip_proxy",
    "timetetng",
    "一个将HTTP代理API转换为本地代理的AstrBot插件 ",
    "1.1",
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
        """
        插件初始化函数。
        - 接收由 AstrBot 传入的配置对象。
        - 初始化插件状态变量。
        - 根据配置决定是否在加载时自动启动服务。
        """
        super().__init__(context)
        self.config: AstrBotConfig = config
        self.server: Server | None = None
        self.server_task: Task | None = None
        self.current_ip: str | None = None
        self.current_port: int | None = None
        self.last_validation_time: float | None = None
        self.ip_usage_count: int = 0
        self.ip_lock = asyncio.Lock()
        
        logger.info("IP代理插件: 插件已加载，配置已注入。")

        # 新增逻辑：根据配置在加载时自动启动服务
        if self.config.get("start_on_load", True):
            logger.info("IP代理插件: 根据配置，正在自动启动代理服务...")
            self.server_task = asyncio.create_task(self.start_local_proxy_server())

    async def get_new_ip(self) -> tuple[str | None, int | None]:
        """
        调用API接口获取新的IP地址和端口。
        """
        api_url = self.config.get("api_url")
        if not api_url or "YOUR_TOKEN" in api_url:
            logger.warning("IP代理插件: API URL 未配置，无法获取新IP。")
            return None, None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url) as response:
                    response.raise_for_status()
                    ip_port = await response.text()
                    ip_port = ip_port.strip()
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
        """
        检测IP是否有效，通过访问指定的验证URL。
        """
        validation_url = self.config.get("validation_url", "http://www.baidu.com")
        timeout = self.config.get("validation_timeout", 5)
        proxy_url = f"http://{ip}:{port}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(validation_url, proxy=proxy_url, timeout=timeout) as response:
                    if response.status == 200:
                        logger.info(f"IP {ip}:{port} 验证成功。")
                        return True
        except Exception as e:
            logger.warning(f"IP {ip}:{port} 访问 {validation_url} 验证失败: {e}")
        return False

    async def get_valid_ip(self) -> tuple[str | None, int | None]:
        """
        获取一个有效的IP地址和端口, 包含绝对失效逻辑。
        """
        async with self.ip_lock:
            ip_expiration_time = self.config.get("ip_expiration_time", 300)
            validation_interval = self.config.get("validation_interval", 60)

            # 检查是否存在当前IP
            if self.current_ip and self.current_port and self.last_validation_time:
                ip_age = time.time() - self.last_validation_time

                # 1. 检查IP是否已超过绝对失效时间 (如果设置为0则不检查)
                if ip_expiration_time > 0 and ip_age > ip_expiration_time:
                    logger.info(f"IP {self.current_ip}:{self.current_port} 已使用超过 {ip_expiration_time} 秒，强制获取新IP。")
                    self.current_ip = None
                
                # 2. 如果未超过绝对失效时间，检查是否在短期验证缓存期内
                elif ip_age < validation_interval:
                    logger.debug(f"使用缓存中的IP: {self.current_ip}:{self.current_port} (验证间隔内)")
                    return self.current_ip, self.current_port
                
                # 3. 如果超过了验证间隔但未超过绝对失效时间，则重新验证
                else:
                    logger.debug(f"IP {self.current_ip}:{self.current_port} 需重新验证...")
                    if await self.is_ip_valid(self.current_ip, self.current_port):
                        self.last_validation_time = time.time()  # 验证成功，更新时间戳
                        logger.debug(f"IP {self.current_ip}:{self.current_port} 验证成功，继续使用。")
                        return self.current_ip, self.current_port
                    else:
                        logger.info(f"IP {self.current_ip}:{self.current_port} 重新验证失败，获取新IP。")
                        self.current_ip = None

            # 如果执行到这里时没有可用的IP (current_ip is None), 则获取新IP
            if not self.current_ip:
                for _ in range(3): # 最多尝试3次
                    new_ip, new_port = await self.get_new_ip()
                    if new_ip and new_port:
                        if await self.is_ip_valid(new_ip, new_port):
                            self.current_ip = new_ip
                            self.current_port = new_port
                            self.last_validation_time = time.time()
                            return self.current_ip, self.current_port
                    logger.warning("获取的新IP无效或验证失败，1秒后重试...")
                    await asyncio.sleep(1)
            
            # 如果循环后仍然没有IP
            if not self.current_ip:
                logger.error("多次尝试后，仍无法获取到有效的IP地址。")
                return None, None
            
            return self.current_ip, self.current_port

    async def handle_connection(self, reader: StreamReader, writer: StreamWriter):
        """
        处理每个客户端连接，将其请求转发到远程代理IP。
        """
        remote_ip, remote_port = await self.get_valid_ip()
        addr = writer.get_extra_info('peername')
        
        if not remote_ip or not remote_port:
            logger.error(f"来自 {addr} 的连接被拒绝，因为无法获取到有效的代理IP。")
            writer.close(); await writer.wait_closed()
            return

        logger.debug(f"接收到来自 {addr} 的连接, 将转发到 {remote_ip}:{remote_port}")
        remote_reader, remote_writer = None, None
        try:
            remote_reader, remote_writer = await asyncio.open_connection(remote_ip, remote_port)
            
            async def forward(src: StreamReader, dst: StreamWriter, direction: str):
                try:
                    while not src.at_eof():
                        data = await src.read(4096)
                        if not data: break
                        dst.write(data); await dst.drain()
                except Exception as e:
                    logger.debug(f"转发数据时发生错误 ({direction}): {e}")
                finally:
                    if not dst.is_closing():
                        dst.close(); await dst.wait_closed()

            task1 = asyncio.create_task(forward(reader, remote_writer, f"{addr} -> remote"))
            task2 = asyncio.create_task(forward(remote_reader, writer, f"remote -> {addr}"))
            await asyncio.wait({task1, task2})

        except Exception as e:
            logger.error(f"处理连接 {addr} 时发生错误: {e}")
        finally:
            if remote_writer and not remote_writer.is_closing():
                remote_writer.close(); await remote_writer.wait_closed()
            if not writer.is_closing():
                writer.close(); await writer.wait_closed()
            logger.debug(f"与 {addr} 的连接已关闭。")
            
    async def start_local_proxy_server(self):
        """
        启动本地代理服务器的主循环。
        """
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
        """
        插件终止时调用的清理函数。
        """
        logger.info("IP代理插件正在终止...")
        if self.server_task and not self.server_task.done():
            self.server_task.cancel()
            try: await self.server_task
            except asyncio.CancelledError: pass
        logger.info("IP代理插件已终止。")

    # ---- 指令处理部分 ----
    
    @filter.command("开启代理", alias={"启动代理", "代理开启"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def start_proxy(self, event: AstrMessageEvent) -> MessageEventResult:
        """启动本地代理服务器"""
        if self.server_task and not self.server_task.done():
            return event.plain_result("代理服务已经在运行中。")
        
        self.server_task = asyncio.create_task(self.start_local_proxy_server())
        listen_host = self.config.get("listen_host", "127.0.0.1")
        local_port = self.config.get("local_port", 8888)
        return event.plain_result(f"代理服务已启动，监听于 {listen_host}:{local_port}")

    @filter.command("关闭代理", alias={"代理关闭", "取消代理"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def stop_proxy(self, event: AstrMessageEvent) -> MessageEventResult:
        """停止本地代理服务器"""
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
        """显示代理服务的当前状态和配置"""
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
        )
        return event.plain_result(status_message)

    @filter.command("修改代理API")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_api_url(self, event: AstrMessageEvent, api_url: str) -> MessageEventResult:
        """通过指令修改获取IP的API地址"""
        self.config["api_url"] = api_url
        self.config.save_config()
        return event.plain_result(f"✅ 代理API地址已更新为: {api_url}")

    @filter.command("修改监听地址")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_listen_host(self, event: AstrMessageEvent, host: str) -> MessageEventResult:
        """通过指令修改本地监听地址"""
        self.config["listen_host"] = host
        self.config.save_config()
        return event.plain_result(f"✅ 监听地址已更新为: {host}\n重启代理后生效。")

    @filter.command("修改监听端口")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_listen_port(self, event: AstrMessageEvent, port: int) -> MessageEventResult:
        """通过指令修改本地监听端口"""
        self.config["local_port"] = port
        self.config.save_config()
        return event.plain_result(f"✅ 监听端口已更新为: {port}\n重启代理后生效。")

    @filter.command("修改测试url")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_validation_url(self, event: AstrMessageEvent, url: str) -> MessageEventResult:
        """通过指令修改用于验证IP有效性的URL"""
        self.config["validation_url"] = url
        self.config.save_config()
        return event.plain_result(f"✅ 验证URL已更新为: {url}")
        
    @filter.command("修改IP失效时间")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_ip_expiration_time(self, event: AstrMessageEvent, seconds: int) -> MessageEventResult:
        """通过指令修改IP的绝对失效时间（秒）"""
        self.config["ip_expiration_time"] = seconds
        self.config.save_config()
        if seconds > 0:
            return event.plain_result(f"✅ IP绝对失效时间已更新为: {seconds} 秒。")
        else:
            return event.plain_result(f"✅ IP绝对失效时间已设置为永不强制失效。")
