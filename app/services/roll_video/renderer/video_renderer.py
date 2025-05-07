"""视频渲染器模块"""

import os
import sys
import logging
import numpy as np
import subprocess
import threading
import queue
from tqdm import tqdm
import gc
import time
import multiprocessing as mp
from PIL import Image
from typing import Dict, Tuple, List, Optional, Union
import platform
from collections import defaultdict
import psutil
import traceback
import signal
from multiprocessing import shared_memory
import random
import string
import re

from .memory_management import FrameMemoryPool, SharedMemoryFramePool, FrameBuffer
from .performance import PerformanceMonitor
from .frame_processors import (
    _process_frame,
    _process_frame_optimized,
    _process_frame_optimized_shm,
    fast_frame_processor,
    init_shared_memory,
    cleanup_shared_memory,
    init_worker,
    test_worker_shared_memory,
)
from .utils import time_tracker, get_memory_usage, optimize_memory, emergency_cleanup

logger = logging.getLogger(__name__)


class VideoRenderer:
    """视频渲染器，负责创建滚动效果的视频，使用ffmpeg管道和线程读取优化"""

    def __init__(
        self,
        width: int,
        height: int,
        fps: int = 30,
        scroll_speed: int = 5,  # 每帧滚动的像素数（由service层基于行高和每秒滚动行数计算而来）
    ):
        """
        初始化视频渲染器

        Args:
            width: 视频宽度
            height: 视频高度
            fps: 视频帧率
            scroll_speed: 每帧滚动的像素数（由service层基于行高和每秒滚动行数计算而来）
        """
        self.width = width
        self.height = height
        self.fps = fps
        self.scroll_speed = scroll_speed
        self.memory_pool = None
        self.frame_counter = 0
        self.total_frames = 0
        
        # 性能统计数据
        self.performance_stats = {
            "preparation_time": 0,     # 准备阶段时间
            "frame_processing_time": 0, # 帧处理阶段时间
            "encoding_time": 0,         # 视频编码阶段时间
            "total_time": 0,            # 总时间
            "frames_processed": 0,      # 处理的帧数
            "fps": 0,                   # 平均每秒处理的帧数
        }

    def _init_memory_pool(self, channels=3, pool_size=120):
        """
        初始化内存池，预分配帧缓冲区

        Args:
            channels: 通道数，3表示RGB，4表示RGBA
            pool_size: 内存池大小
        """
        logger.info(
            f"初始化内存池: {pool_size}个{self.width}x{self.height}x{channels}帧缓冲区"
        )
        self.memory_pool = []

        try:
            for _ in range(pool_size):
                # 预分配连续内存
                frame = np.zeros(
                    (self.height, self.width, channels), dtype=np.uint8, order="C"
                )
                self.memory_pool.append(frame)
        except Exception as e:
            logger.warning(f"内存池初始化失败: {e}，将使用动态分配")
            # 如果内存不足，减小池大小重试
            if pool_size > 30:
                logger.info(f"尝试减小内存池大小至30")
                self._init_memory_pool(channels, 30)
        return self.memory_pool

    def _get_codec_parameters(self, preferred_codec, transparency_required, channels):
        """
        获取适合当前平台和需求的编码器参数
        
        Args:
            preferred_codec: 首选编码器
            transparency_required: 是否需要透明支持
            channels: 通道数（3=RGB, 4=RGBA）
            
        Returns:
            (codec_params, pix_fmt): 编码器参数列表和像素格式
        """
        # 检查系统平台
        is_macos = platform.system() == "Darwin"
        is_windows = platform.system() == "Windows"
        is_linux = platform.system() == "Linux"
        
        # 透明背景需要特殊处理
        if transparency_required or channels == 4:
            # 透明背景需要特殊处理
            pix_fmt = "rgba"
            # ProRes 4444保留Alpha
            codec_params = [
                "-c:v", "prores_ks", 
                "-profile:v", "4444",
                "-pix_fmt", "yuva444p10le", 
                "-alpha_bits", "16",
                "-vendor", "ap10", 
                "-colorspace", "bt709",
            ]
            logger.info("使用ProRes 4444编码器处理透明视频")
            return codec_params, pix_fmt
        
        # 不透明视频处理
        pix_fmt = "rgb24"
        
        # 检查是否强制使用CPU
        force_cpu = "NO_GPU" in os.environ
        
        # 根据平台和编码器选择参数
        if preferred_codec == "h264_nvenc" and not force_cpu:
            # NVIDIA GPU加速
            if is_windows or is_linux:
                codec_params = [
                    "-c:v", "h264_nvenc",
                    "-preset", "p1",  # 使用最快的预设
                    "-rc", "vbr", 
                    "-cq", "28",  # 更低的质量以提高速度
                    "-b:v", "4M",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                ]
                logger.info("使用NVIDIA GPU加速编码器 (优化性能模式)")
            else:
                # 不支持NVIDIA，回退到CPU
                logger.info("平台不支持NVIDIA编码，切换到libx264")
                codec_params = [
                    "-c:v", "libx264",
                    "-preset", "veryfast",  # 使用更快的预设
                    "-crf", "20",  # 略微降低质量以提高速度
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                ]
        elif preferred_codec == "h264_videotoolbox":
            # 删除VideoToolbox相关分支，使用libx264代替
            logger.info("不支持VideoToolbox，使用libx264")
            codec_params = [
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
            ]
        elif preferred_codec == "prores_ks":
            # ProRes (非透明)
            codec_params = [
                "-c:v", "prores_ks",
                "-profile:v", "3",  # ProRes 422 HQ
                "-pix_fmt", "yuv422p10le",
                "-vendor", "ap10",
                "-colorspace", "bt709",
            ]
            logger.info("使用ProRes编码器 (非透明)")
        else:
            # 默认使用libx264 (高质量CPU编码)
            codec_params = [
                "-c:v", "libx264",
                "-preset", "medium",  # 平衡速度和质量的预设
                "-crf", "20",         # 恒定质量因子 (0-51, 越低质量越高)
                "-pix_fmt", "yuv420p", # 兼容大多数播放器
                "-movflags", "+faststart", # MP4优化
            ]
            logger.info(f"使用CPU编码器: libx264")
        
        return codec_params, pix_fmt

    def _get_ffmpeg_command(
        self,
        output_path: str,
        pix_fmt: str,
        codec_and_output_params: List[str],  # 重命名以更清晰
        audio_path: Optional[str],
    ) -> List[str]:
        """构造基础的ffmpeg命令 - 高性能优化版"""
        command = [
            "ffmpeg",
            "-y",
            # I/O优化参数
            "-probesize",
            "20M",  # 增加探测缓冲区大小
            "-analyzeduration",
            "20M",  # 增加分析时间
            "-thread_queue_size",
            "8192",  # 大幅增加线程队列大小
            # 输入格式参数
            "-f",
            "rawvideo",
            "-vcodec",
            "rawvideo",
            "-s",
            f"{self.width}x{self.height}",
            "-pix_fmt",
            pix_fmt,
            "-r",
            str(self.fps),
            "-vsync",
            "1",  # 添加vsync参数，确保平滑的视频同步
            "-i",
            "-",  # 从 stdin 读取
        ]
        if audio_path and os.path.exists(audio_path):
            command.extend(["-i", audio_path])

        # 添加视频编码器和特定的输出参数 (如 -movflags)
        command.extend(codec_and_output_params)

        if audio_path and os.path.exists(audio_path):
            command.extend(
                ["-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
            )
        else:
            command.extend(["-map", "0:v:0"])

        command.append(output_path)
        return command

    def _reader_thread(self, pipe, output_queue):
        """读取管道输出并放入队列"""
        try:
            with pipe:
                for line in iter(pipe.readline, b""):
                    output_queue.put(line)
        finally:
            output_queue.put(None)  # 发送结束信号

    def create_scrolling_video_optimized(
        self,
        image,
        output_path,
        text_actual_height,
        transparency_required=False,
        preferred_codec="libx264",
        audio_path=None,
        bg_color=(255, 255, 255),
    ):
        """
        使用优化版本创建滚动视频，支持直接传入图像
        
        参数:
            image: 要滚动显示的图像（PIL.Image或NumPy数组）
            output_path: 输出视频文件路径
            text_actual_height: 文本实际高度（像素）
            transparency_required: 是否需要透明度支持
            preferred_codec: 首选视频编码器
            audio_path: 可选音频文件路径
            bg_color: 背景颜色元组(R,G,B)或(R,G,B,A)
        """
        try:
            # 初始化性能统计
            self.performance_stats = {
                "preparation_time": 0,   # 准备阶段时间
                "frame_processing_time": 0,  # 帧处理时间
                "encoding_time": 0,      # 编码时间
                "total_time": 0,         # 总时间
                "frames_processed": 0,   # 处理的帧数
                "fps": 0                 # 平均每秒帧数
            }
            
            # 记录总开始时间
            preparation_start_time = time.time()
            total_start_time = preparation_start_time
            
            # 1. 视频参数准备
            logger.info(f"准备创建滚动视频: {output_path}")
            
            # 将图像转换为numpy数组
            if isinstance(image, np.ndarray):
                img_array = image.copy()
                if len(img_array.shape) == 2:  # 扩展成3通道
                    img_array = np.stack([img_array] * 3, axis=2)
            else:  # PIL.Image
                img_array = np.array(image)
                
            # 确保图像是RGBA或RGB
            if img_array.shape[2] == 4:  # RGBA
                # 有Alpha通道，保留透明度
                if transparency_required:
                    # 不改变，使用RGBA
                    pass
                else:
                    # 将RGBA转换为RGB（用背景色填充）
                    rgb_array = np.zeros((img_array.shape[0], img_array.shape[1], 3), dtype=np.uint8)
                    alpha = img_array[:, :, 3].astype(float) / 255.0
                    
                    # 安全处理背景色，确保是RGB格式
                    if isinstance(bg_color, (list, tuple)):
                        if len(bg_color) >= 3:
                            bg_r, bg_g, bg_b = bg_color[0], bg_color[1], bg_color[2]
                        else:
                            bg_r, bg_g, bg_b = 255, 255, 255  # 默认白色
                    else:
                        bg_r, bg_g, bg_b = 255, 255, 255  # 默认白色
                    
                    # 将RGB通道从RGBA转换出来，使用背景色填充
                    rgb_array[:, :, 0] = (img_array[:, :, 0] * alpha + bg_r * (1 - alpha)).astype(np.uint8)
                    rgb_array[:, :, 1] = (img_array[:, :, 1] * alpha + bg_g * (1 - alpha)).astype(np.uint8)
                    rgb_array[:, :, 2] = (img_array[:, :, 2] * alpha + bg_b * (1 - alpha)).astype(np.uint8)
                    
                    img_array = rgb_array
            
            # 获取图像尺寸和通道数
            img_height, img_width = img_array.shape[:2]
            channels = img_array.shape[2] if len(img_array.shape) > 2 else 1
            
            # 2. 计算滚动参数
            scroll_height = img_height
            fps = 30
            scroll_duration = max(int(scroll_height / 60), 10)  # 至少10秒
            
            # 至少滚动100像素，确保内容可见
            if scroll_height < 100:
                logger.warning(f"图像高度太小 ({scroll_height}px)，已调整为最小高度100px")
                scroll_height = 100
            
            # 每帧滚动的像素数
            pixels_per_second = scroll_height / scroll_duration
            pixels_per_frame = pixels_per_second / fps
            
            # 视频高度 = 实际文本高度（可视区域）
            video_height = min(text_actual_height, 720)  # 限制最大高度
            
            # 计算总帧数
            total_frames = int(fps * scroll_duration) + 1
            
            # 3. 确定编码器参数
            codec_params, pix_fmt = self._get_codec_parameters(
                preferred_codec, transparency_required, channels
            )

            # 4. 滚动参数计算 - 减少中间变量
            scroll_distance = max(text_actual_height, img_height - self.height)
            scroll_frames = (
                int(scroll_distance / self.scroll_speed) if self.scroll_speed > 0 else 0
            )

            # 确保短文本有合理滚动时间
            min_scroll_frames = self.fps * 8
            if scroll_frames < min_scroll_frames and scroll_frames > 0:
                adjusted_speed = scroll_distance / min_scroll_frames
                if adjusted_speed < self.scroll_speed:
                    logger.info(
                        f"文本较短，减慢滚动速度: {self.scroll_speed:.2f} → {adjusted_speed:.2f} 像素/帧"
                    )
                    self.scroll_speed = adjusted_speed
                    scroll_frames = min_scroll_frames

            padding_frames_start = int(self.fps * 2.0)
            padding_frames_end = int(self.fps * 2.0)
            total_frames = padding_frames_start + scroll_frames + padding_frames_end
            self.total_frames = total_frames
            duration = total_frames / self.fps

            logger.info(
                f"文本高:{text_actual_height}, 图像高:{img_height}, 视频高:{self.height}"
            )
            logger.info(
                f"滚动距离:{scroll_distance}, 滚动帧:{scroll_frames}, 总帧:{total_frames}, 时长:{duration:.2f}s"
            )
            logger.info(
                f"输出:{output_path}, 透明:{transparency_required}, 首选编码器:{preferred_codec}"
            )

            # 创建输出目录
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

            # 删除旧的输出文件
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                    logger.info(f"已删除旧的输出文件: {output_path}")
                except Exception as e:
                    logger.warning(f"删除旧输出文件失败: {e}")

            # 5. 创建子进程池
            # 核心数和池大小计算
            cpu_count = mp.cpu_count()
            pool_size = max(2, min(cpu_count - 1, 8))  # 至少2个，最多8个，保留1个核心给主进程

            # 初始化共享内存
            shm = None
            shm_name = f"shm_image_{int(time.time())}_{random.randint(1000, 9999)}"
            try:
                # 创建共享内存
                shm = shared_memory.SharedMemory(name=shm_name, create=True, size=img_array.nbytes)
                # 创建Numpy数组视图并复制数据
                shm_array = np.ndarray(img_array.shape, dtype=img_array.dtype, buffer=shm.buf)
                np.copyto(shm_array, img_array)
                logger.info(f"已将图像数据复制到共享内存 {shm_name}")
                
                # 储存共享内存信息
                shared_dict = {
                    'shm_name': shm_name,
                    'img_shape': img_array.shape,
                    'dtype': img_array.dtype.name,
                }
                
                # 初始化本进程的共享内存
                init_shared_memory(shared_dict)
            except Exception as e:
                logger.error(f"创建共享内存失败: {str(e)}")
                shm_name = None
                shared_dict = None
            
            logger.info(f"创建{pool_size}个进程的进程池（共享内存：{shm_name or '无'}）")
            
            # 记录准备阶段结束，帧处理阶段开始
            preparation_end_time = time.time()
            self.performance_stats["preparation_time"] = preparation_end_time - preparation_start_time
            logger.info(f"准备阶段完成，用时: {self.performance_stats['preparation_time']:.2f}秒")
            
            # 创建管道和队列
            read_stdout, write_stdout = os.pipe()
            read_stderr, write_stderr = os.pipe()
            stdout_queue = queue.Queue()
            stderr_queue = queue.Queue()
            
            # 获取编码器参数和像素格式
            codec_params, pix_fmt = self._get_codec_parameters(preferred_codec, transparency_required, 
                                                              4 if transparency_required else 3)
                
            # 构建完整的ffmpeg命令
            ffmpeg_cmd = self._get_ffmpeg_command(output_path, pix_fmt, codec_params, audio_path)
            
            # 在GPU下记录详细的ffmpeg命令，便于调试
            if preferred_codec.startswith("h264_"):
                logger.info(f"FFmpeg命令: {' '.join(ffmpeg_cmd)}")
            
            # 创建进程池和帧生成器
            try:
                # 如果shared_dict为None，使用最小化的字典
                if shared_dict is None:
                    shared_dict = {'dummy': True}
                
                # 创建进程池（使用spawn确保共享内存兼容性）
                mp_context = mp.get_context("spawn")
                with mp_context.Pool(processes=pool_size, initializer=init_worker, initargs=(shared_dict,)) as pool:
                    
                    # 移除资源限制设置
                    
                    # 启动进程
                    logger.info(f"启动FFmpeg进程...")
                    process = subprocess.Popen(
                        ffmpeg_cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        bufsize=10 * 1024 * 1024,  # 大缓冲区
                    )
                    
                    # 创建stdout和stderr读取线程，防止管道缓冲区满导致FFmpeg阻塞
                    def read_pipe(pipe, name):
                        """读取管道数据，防止缓冲区满"""
                        try:
                            for line in iter(pipe.readline, b''):
                                line_str = line.decode('utf-8', errors='replace').strip()
                                if name == 'stderr' and ('error' in line_str.lower() or 'warning' in line_str.lower()):
                                    logger.warning(f"FFmpeg {name}: {line_str}")
                        except Exception as e:
                            logger.error(f"读取FFmpeg {name}管道时出错: {str(e)}")
                        finally:
                            pipe.close()
                    
                    # 启动读取线程
                    stdout_thread = threading.Thread(target=read_pipe, args=(process.stdout, 'stdout'))
                    stderr_thread = threading.Thread(target=read_pipe, args=(process.stderr, 'stderr'))
                    stdout_thread.daemon = True
                    stderr_thread.daemon = True
                    stdout_thread.start()
                    stderr_thread.start()
                    
                    # 帧处理阶段正式开始（从FFmpeg启动开始计时）
                    frame_processing_start_time = time.time()

                    # 创建帧任务列表
                    frame_tasks = []
                    
                    # 记录开始时间（供进度报告使用）
                    processing_start_time = time.time()
                    
                    # 前面的静止帧
                    for i in range(padding_frames_start):
                        frame_meta = {
                            'width': self.width,
                            'height': self.height,
                            'img_height': img_height,
                            'scroll_speed': 0,  # 静止不滚动
                            'fps': self.fps,
                        }
                        frame_tasks.append((i, frame_meta))
                    
                    # 滚动帧
                    for i in range(scroll_frames):
                        # 计算滚动偏移量
                        frame_idx = padding_frames_start + i
                        frame_meta = {
                            'width': self.width,
                            'height': self.height,
                            'img_height': img_height,
                            'scroll_speed': self.scroll_speed,
                            'fps': self.fps,
                        }
                        frame_tasks.append((frame_idx, frame_meta))
                    
                    # 后面的静止帧
                    for i in range(padding_frames_end):
                        frame_idx = padding_frames_start + scroll_frames + i
                        frame_meta = {
                            'width': self.width,
                            'height': self.height,
                            'img_height': img_height,
                            'scroll_speed': 0,  # 静止不滚动
                            'fps': self.fps,
                        }
                        frame_tasks.append((frame_idx, frame_meta))

                    # 异步处理帧
                    logger.info(f"开始处理{len(frame_tasks)}帧...")
                    
                    # 创建进度条
                    pbar = tqdm(
                        total=len(frame_tasks), 
                        desc=f"渲染视频 ({os.path.basename(output_path)})", 
                        unit="帧",
                        bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]',
                        postfix={"fps": 0.0, "eta": "未知"}
                    )
                    
                    # 进度报告函数
                    last_report_time = time.time()
                    frames_processed = 0
                    
                    # 设置看门狗定时器
                    watchdog_event = threading.Event()
                    
                    def report_progress():
                        nonlocal last_report_time, frames_processed, processing_start_time, pbar
                        current_time = time.time()
                        if current_time - last_report_time >= 0.5:  # 更频繁更新，每0.5秒一次
                            elapsed = current_time - processing_start_time
                            fps = frames_processed / elapsed if elapsed > 0 else 0
                            percent = 100.0 * frames_processed / total_frames if total_frames > 0 else 0
                            
                            # 估计剩余时间
                            if fps > 0:
                                remaining_frames = total_frames - frames_processed
                                eta = remaining_frames / fps
                                eta_str = f"{eta:.1f}秒"
                            else:
                                eta = 0
                                eta_str = "未知"
                            
                            # 更新进度条
                            pbar.set_postfix(
                                fps=f"{fps:.1f}", 
                                eta=eta_str,
                                完成=f"{percent:.1f}%"
                            )
                            pbar.n = frames_processed
                            pbar.refresh()
                                
                            logger.debug(
                                f"进度: {frames_processed}/{total_frames} 帧 "
                                f"({percent:.1f}%, {fps:.1f} fps, 预计剩余: {eta_str})"
                            )
                            last_report_time = current_time
                            
                            # 重置看门狗
                            watchdog_event.set()
                    
                    # 看门狗线程函数
                    def watchdog_handler():
                        """监视进程进度，如果超过30秒没有进展，则终止进程"""
                        watchdog_timeout = 30.0  # 30秒超时
                        last_check_frames = 0
                        last_check_time = time.time()
                        
                        # 添加一个信号标志来控制看门狗是否应该继续运行
                        watchdog_active = True
                        
                        while watchdog_active:
                            # 等待看门狗重置或超时
                            if watchdog_event.wait(watchdog_timeout / 2):
                                # 事件被设置，重置看门狗
                                watchdog_event.clear()
                                last_check_frames = frames_processed
                                last_check_time = time.time()
                                
                                # 检查是否完成所有帧处理，如果完成则退出看门狗
                                if frames_processed >= total_frames:
                                    logger.debug("所有帧已处理完成，看门狗退出")
                                    watchdog_active = False
                                    return
                            else:
                                # 检查是否有进展
                                if frames_processed == last_check_frames:
                                    current_time = time.time()
                                    if current_time - last_check_time > watchdog_timeout:
                                        # 如果已处理了所有帧，不报告卡住
                                        if frames_processed >= total_frames:
                                            logger.debug("所有帧已处理完成，看门狗退出")
                                            return
                                        
                                        logger.error(
                                            f"看门狗检测到处理卡住: {watchdog_timeout}秒内没有进度!"
                                            f"最后处理: {last_check_frames}/{total_frames}帧"
                                        )
                                        # 尝试停止处理
                                        try:
                                            process.terminate()
                                        except:
                                            pass
                                        return  # 停止看门狗
                                else:
                                    # 有进展，重置
                                    last_check_frames = frames_processed
                                    last_check_time = time.time()
                    
                    # 启动看门狗线程
                    watchdog = threading.Thread(target=watchdog_handler)
                    watchdog.daemon = True
                    watchdog.start()
                    
                    # 初始化看门狗
                    watchdog_event.set()

                    # 5. 分批处理并流式输出到FFMPEG
                    chunk_size = 12  # 每批处理帧数
                    total_batches = (len(frame_tasks) + chunk_size - 1) // chunk_size

                    for batch_idx in range(total_batches):
                        # 获取当前批次任务
                        start_idx = batch_idx * chunk_size
                        end_idx = min(start_idx + chunk_size, len(frame_tasks))
                        current_batch = frame_tasks[start_idx:end_idx]
                        
                        try:
                            # 并行处理当前批次
                            results = pool.map(_process_frame_optimized_shm, current_batch)
                            
                            # 按顺序写入FFmpeg
                            for result in results:
                                if result is not None:
                                    frame_idx, frame = result
                                    
                                    # 检查FFmpeg是否仍在运行，如果退出则不再写入
                                    if process.poll() is not None:
                                        logger.warning(f"FFmpeg进程已退出(返回码:{process.returncode})，停止写入帧")
                                        break
                                    
                                    try:
                                        # 优化: 直接写入二进制数据，避免额外复制
                                        frame_bytes = frame.tobytes()
                                        process.stdin.write(frame_bytes)
                                        process.stdin.flush()
                                        
                                        # 更新进度
                                        frames_processed += 1
                                        
                                        # 更新进度条
                                        pbar.update(1)
                                        
                                        # 报告进度
                                        report_progress()
                                    except BrokenPipeError:
                                        logger.warning("FFmpeg管道已关闭，停止写入帧")
                                        break
                                    except Exception as e:
                                        logger.error(f"写入帧数据时出错: {str(e)}")
                                        break
                        except Exception as e:
                            logger.error(f"处理批次 {batch_idx+1}/{total_batches} 时出错: {str(e)}\n{traceback.format_exc()}")
                            # 继续尝试处理其他批次
                        
                        # 检查FFmpeg是否仍在运行
                        if process.poll() is not None:
                            logger.error(f"FFmpeg进程意外退出，返回码: {process.returncode}")
                            # 读取剩余错误输出
                            try:
                                while True:
                                    err_line = stderr_queue.get_nowait()
                                    if err_line is None:
                                        break
                                    logger.error(f"FFmpeg错误: {err_line.decode('utf-8', errors='replace').strip()}")
                            except queue.Empty:
                                pass
                            break

                    # 6. 完成处理，关闭stdin管道
                    # 设置信号通知看门狗线程所有帧处理完成
                    watchdog_event.set()
                    logger.debug(f"所有{frames_processed}帧处理完成，通知看门狗线程退出")
                    
                    # 安全关闭stdin管道
                    try:
                        if process.poll() is None:  # 只在进程仍在运行时关闭stdin
                            process.stdin.close()
                        else:
                            logger.warning(f"FFmpeg进程已退出(返回码:{process.returncode})，跳过关闭stdin")
                    except BrokenPipeError:
                        logger.warning("FFmpeg管道已关闭，无法关闭stdin")
                    except Exception as e:
                        logger.error(f"关闭stdin时出错: {str(e)}")
                    
                    # 记录帧处理阶段结束，编码阶段开始
                    frame_processing_end_time = time.time()
                    self.performance_stats["frame_processing_time"] = frame_processing_end_time - processing_start_time
                    self.performance_stats["frames_processed"] = frames_processed
                    
                    encoding_start_time = time.time()
                    logger.info(f"帧处理阶段完成，用时: {self.performance_stats['frame_processing_time']:.2f}秒，平均: {frames_processed / self.performance_stats['frame_processing_time']:.2f}帧/秒")
                    logger.info(f"等待FFmpeg完成编码...")
                    
                    # 添加超时机制，防止无限等待
                    encoding_timeout = 120  # 编码超时时间，单位：秒，对于GPU加速任务设置更长时间
                    encoding_wait_interval = 0.5  # 检查间隔，单位：秒
                    wait_start_time = time.time()
                    encoding_progress_interval = 5.0  # 日志报告间隔，单位：秒
                    last_progress_time = time.time()
                    
                    # 等待FFmpeg完成，但设置超时
                    return_code = None
                    while process.poll() is None:
                        current_time = time.time()
                        
                        # 检查是否应该报告进度
                        if current_time - last_progress_time >= encoding_progress_interval:
                            encoding_elapsed = current_time - encoding_start_time
                            logger.info(f"FFmpeg编码进行中，已等待 {encoding_elapsed:.1f} 秒...")
                            last_progress_time = current_time
                        
                        # 检查是否超时
                        current_wait_time = current_time - wait_start_time
                        if current_wait_time > encoding_timeout:
                            logger.warning(f"FFmpeg编码阶段已等待{encoding_timeout}秒，超时强制结束")
                            try:
                                process.terminate()
                                # 给进程一点时间来终止
                                time.sleep(1)
                                # 如果仍在运行，强制结束
                                if process.poll() is None:
                                    logger.warning("FFmpeg进程未响应terminate()，尝试强制终止(kill)")
                                    process.kill()
                                    time.sleep(0.5)
                                    
                                # 最后检查
                                if process.poll() is None:
                                    logger.error("无法终止FFmpeg进程，可能需要手动清理")
                                else:
                                    logger.warning(f"FFmpeg进程已终止，返回码: {process.returncode}")
                            except Exception as e:
                                logger.error(f"终止FFmpeg进程时出错: {e}")
                            
                            # 设置错误返回码
                            return_code = -9  # 自定义超时错误码
                            break
                        
                        # 短暂等待后再次检查
                        time.sleep(encoding_wait_interval)
                    
                    # 记录编码结束状态
                    encoding_time = time.time() - encoding_start_time
                    
                    # 如果没有设置返回码（未超时），则获取进程的实际返回码
                    if return_code is None:
                        return_code = process.returncode
                        if return_code == 0:
                            logger.info(f"FFmpeg编码成功完成，用时: {encoding_time:.2f}秒")
                        else:
                            logger.error(f"FFmpeg编码失败，返回码: {return_code}，用时: {encoding_time:.2f}秒")
                    else:
                        logger.warning(f"FFmpeg编码因超时而强制终止，已运行: {encoding_time:.2f}秒")
                    
                    # 记录编码阶段结束
                    encoding_end_time = time.time()
                    self.performance_stats["encoding_time"] = encoding_end_time - encoding_start_time
                    
                    # 记录总时间
                    total_end_time = time.time()
                    self.performance_stats["total_time"] = total_end_time - total_start_time
                    self.performance_stats["fps"] = frames_processed / self.performance_stats["frame_processing_time"] if self.performance_stats["frame_processing_time"] > 0 else 0
                    
                    # 关闭进度条
                    pbar.close()
                    
                    # 确保看门狗线程退出
                    if 'watchdog' in locals():
                        try:
                            # 最后一次设置信号
                            watchdog_event.set()
                            # 等待看门狗线程结束，但最多等待2秒
                            watchdog.join(timeout=2)
                        except Exception as e:
                            logger.debug(f"等待看门狗线程时出错: {e}")
                    
                    # 等待输出线程完成
                    if 'stdout_thread' in locals() and stdout_thread.is_alive():
                        try:
                            stdout_thread.join(timeout=2)
                        except Exception as e:
                            logger.debug(f"等待stdout线程时出错: {e}")
                    
                    if 'stderr_thread' in locals() and stderr_thread.is_alive():
                        try:
                            stderr_thread.join(timeout=2)
                        except Exception as e:
                            logger.debug(f"等待stderr线程时出错: {e}")
                    
                    # 读取剩余输出
                    while True:
                        try:
                            err_line = stderr_queue.get_nowait()
                            if err_line is None:
                                break
                            # 只记录错误和警告
                            err_str = err_line.decode('utf-8', errors='replace').strip()
                            if "error" in err_str.lower() or "warning" in err_str.lower():
                                logger.warning(f"FFmpeg: {err_str}")
                        except queue.Empty:
                            break

                    # 删除对不存在线程的引用
                    # stdout_thread.join(timeout=2)
                    # stderr_thread.join(timeout=2)
                    
                    # 检查退出状态
                    if return_code != 0:
                        logger.error(f"FFmpeg进程异常退出，代码: {return_code}")
                        return None
                    else:
                        # 输出详细的性能统计报告
                        logger.info("=" * 50)
                        logger.info("视频渲染性能报告:")
                        logger.info(f"1. 准备阶段: {self.performance_stats['preparation_time']:.2f}秒 ({self.performance_stats['preparation_time'] / self.performance_stats['total_time'] * 100:.1f}%)")
                        logger.info(f"2. 帧处理阶段: {self.performance_stats['frame_processing_time']:.2f}秒 ({self.performance_stats['frame_processing_time'] / self.performance_stats['total_time'] * 100:.1f}%) - {self.performance_stats['fps']:.2f}帧/秒")
                        logger.info(f"3. 视频编码阶段: {self.performance_stats['encoding_time']:.2f}秒 ({self.performance_stats['encoding_time'] / self.performance_stats['total_time'] * 100:.1f}%)")
                        logger.info(f"总时间: {self.performance_stats['total_time']:.2f}秒，处理 {frames_processed} 帧")
                        logger.info("=" * 50)
                
            except Exception as e:
                logger.error(f"处理视频时出错: {str(e)}\n{traceback.format_exc()}")
                # 如果仍有FFmpeg进程，尝试终止
                try:
                    if 'process' in locals() and process.poll() is None:
                        process.terminate()
                        time.sleep(0.5)
                        if process.poll() is None:
                            process.kill()
                except:
                    pass

                # 检查是否为参数错误，提供具体诊断
                error_str = str(e).lower()
                if 'broken pipe' in error_str:
                    logger.warning("FFmpeg管道错误，可能是编码器参数不兼容导致")
                elif 'invalid argument' in error_str:
                    logger.warning("FFmpeg参数错误，尝试使用更兼容的参数")

                # 垃圾回收
                gc.collect()
                
                # 检查是否已经进行过回退尝试
                if 'retry_attempted' in locals() and retry_attempted:
                    logger.error("已尝试回退方案，仍然失败，放弃处理")
                    return None
                
                # 设置回退标志
                retry_attempted = True

        except Exception as e:
            logger.error(f"视频创建过程失败: {str(e)}\n{traceback.format_exc()}")
            # 回退到标准CPU处理
            logger.info("优化渲染失败，回退到标准CPU渲染")
            os.environ["NO_GPU"] = "1"
            try:
                return self.create_scrolling_video_optimized(
                    image=image,
                    output_path=output_path,
                    text_actual_height=text_actual_height,
                    transparency_required=transparency_required,
                    preferred_codec="libx264",  # 强制使用CPU编码器
                    audio_path=audio_path,
                    bg_color=bg_color,
                )
            except Exception as e2:
                logger.error(f"回退渲染也失败: {str(e2)}")
                return None
            finally:
                # 恢复环境变量
                if "NO_GPU" in os.environ:
                    del os.environ["NO_GPU"]

        return output_path

    def create_scrolling_video(
        self,
        image,
        output_path,
        text_actual_height=None,
        transparency_required=False,
        preferred_codec="h264_nvenc",
        audio_path=None,
        bg_color=(0, 0, 0, 255),
    ):
        """
        创建滚动视频

        Args:
            image: PIL图像对象
            output_path: 输出视频路径
            text_actual_height: 文本实际高度（不含额外填充）
            transparency_required: 是否需要透明背景
            preferred_codec: 首选视频编码器，默认尝试GPU加速
            audio_path: 可选的音频文件路径
            bg_color: 背景颜色，用于非透明视频

        Returns:
            输出视频的路径
        """
        # 确保输出目录存在
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        # 获取图像尺寸
        img_width, img_height = image.size
        logger.info(f"图像尺寸: {img_width}x{img_height}")

        # 如果未提供文本实际高度，则使用图像高度
        if text_actual_height is None:
            text_actual_height = img_height
            logger.info("未提供文本实际高度，使用图像高度")

        # 计算总帧数
        # 1. 开始静止时间（秒）
        start_static_time = 3
        # 2. 结束静止时间（秒）
        end_static_time = 3
        # 3. 滚动所需帧数 = (图像高度 - 视频高度) / 每帧滚动像素数
        scroll_frames = max(0, (img_height - self.height)) / self.scroll_speed
        # 4. 总帧数 = 开始静止帧数 + 滚动帧数 + 结束静止帧数
        total_frames = int(
            (start_static_time * self.fps)
            + scroll_frames
            + (end_static_time * self.fps)
        )

        logger.info(
            f"视频参数: {self.width}x{self.height}, {self.fps}fps, 滚动速度: {self.scroll_speed}像素/帧"
        )
        logger.info(
            f"总帧数: {total_frames} (开始静止: {start_static_time}秒, 滚动: {scroll_frames/self.fps:.2f}秒, 结束静止: {end_static_time}秒)"
        )

        # 确定像素格式和编码器
        if transparency_required:
            # 透明视频使用ProRes 4444
            ffmpeg_pix_fmt = "rgba"
            output_path = os.path.splitext(output_path)[0] + ".mov"
            video_codec_params = [
                "-c:v",
                "prores_ks",
                "-profile:v",
                "4",  # ProRes 4444
                "-pix_fmt",
                "yuva444p10le",
                "-alpha_bits",
                "16",
                "-vendor",
                "ap10",
                "-threads",
                "8",  # 充分利用多核CPU
            ]
            logger.info("使用ProRes 4444编码器处理透明视频")
        else:
            # 不透明视频，尝试使用GPU加速
            ffmpeg_pix_fmt = "rgb24"
            output_path = os.path.splitext(output_path)[0] + ".mp4"

            # 检查操作系统，在macOS上默认使用CPU编码
            is_macos = platform.system() == "Darwin"
            if is_macos:
                # macOS上强制使用CPU编码
                logger.info("检测到macOS系统，将使用CPU编码器")
                os.environ["NO_GPU"] = "1"

            # 检查GPU编码器可用性
            gpu_encoders = ["h264_nvenc", "hevc_nvenc"]
            if preferred_codec in gpu_encoders and not "NO_GPU" in os.environ:
                # 使用更高性能的GPU参数
                video_codec_params = [
                    "-c:v",
                    preferred_codec,
                    "-preset",
                    "p4",  # 提升到p4预设（更高性能）
                    "-b:v",
                    "8M",  # 提升到8M比特率
                    "-pix_fmt",
                    "yuv420p",  # 确保兼容性
                    "-movflags",
                    "+faststart",
                ]
                logger.info(f"使用GPU编码器: {preferred_codec}，预设:p4，比特率:8M")
                use_gpu = True
            else:
                # 回退到CPU编码，但使用更高性能设置
                video_codec_params = [
                    "-c:v",
                    "libx264",
                    "-crf",
                    "20",
                    "-preset",
                    "medium",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    "-threads",
                    "8",
                ]
                logger.info(f"使用CPU编码器: libx264 (GPU编码器不可用或被禁用)")
                use_gpu = False

        # 预分配大型数组，减少内存碎片
        try:
            # 预热内存，减少动态分配开销
            if not transparency_required:
                # 为RGB预分配
                batch_size = 240 if not use_gpu else 120  # 增大批处理大小
                warmup_buffer = np.zeros(
                    (batch_size, self.height, self.width, 3), dtype=np.uint8
                )
                del warmup_buffer
            else:
                # 为RGBA预分配
                batch_size = 120  # 透明视频使用较小的批处理大小
                warmup_buffer = np.zeros(
                    (batch_size, self.height, self.width, 4), dtype=np.uint8
                )
                del warmup_buffer

            # 强制垃圾回收
            gc.collect()
            logger.info("内存预热完成")
        except Exception as e:
            logger.warning(f"内存预热失败: {e}")
            batch_size = 60  # 回退到较小的批处理大小

        # 数据传输模式：直接模式或缓存模式
        # 根据GPU/CPU模式调整批处理大小
        if not "batch_size" in locals():
            batch_size = 60  # 降低批处理大小
        num_batches = (total_frames + batch_size - 1) // batch_size

        # 确定最佳进程数
        try:
            cpu_count = mp.cpu_count()
            # 减少使用的核心数，为系统和ffmpeg留下更多资源
            optimal_processes = min(8, max(2, cpu_count - 1))
            num_processes = optimal_processes
            logger.info(
                f"检测到{cpu_count}个CPU核心，优化使用{num_processes}个进程进行渲染，批处理大小:{batch_size}"
            )
        except:
            num_processes = 6  # 默认使用较少进程
            logger.info(f"使用默认{num_processes}个进程，批处理大小:{batch_size}")

        # 初始化内存池 - 减小池大小以降低内存压力
        channels = 4 if transparency_required else 3
        self._init_memory_pool(channels, pool_size=720)  # 增加内存池大小提高性能

        # 将图像转换为numpy数组
        img_array = np.array(image)

        # 完整的ffmpeg命令
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            # I/O优化参数
            "-probesize",
            "32M",  # 增加探测缓冲区大小
            "-analyzeduration",
            "32M",  # 增加分析时间
            "-thread_queue_size",
            "4096",  # 大幅增加线程队列大小
            # 输入格式参数
            "-f",
            "rawvideo",
            "-vcodec",
            "rawvideo",
            "-s",
            f"{self.width}x{self.height}",
            "-pix_fmt",
            ffmpeg_pix_fmt,
            "-r",
            str(self.fps),
            "-i",
            "-",  # 从stdin读取
        ]

        # 添加音频输入（如果有）
        if audio_path and os.path.exists(audio_path):
            ffmpeg_cmd.extend(["-i", audio_path])

        # 添加视频编码参数
        ffmpeg_cmd.extend(video_codec_params)

        # 添加音频映射（如果有）
        if audio_path and os.path.exists(audio_path):
            ffmpeg_cmd.extend(
                [
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-shortest",
                ]
            )
        else:
            ffmpeg_cmd.extend(["-map", "0:v:0"])

        # 添加输出路径
        ffmpeg_cmd.append(output_path)

        logger.info(f"FFmpeg命令: {' '.join(ffmpeg_cmd)}")

        # 启动FFmpeg进程
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=10**8,  # 增大缓冲区
        )

        # 创建进度条
        pbar = tqdm(total=total_frames, desc="渲染进度")

        # 创建多进程池
        with mp.Pool(processes=num_processes) as pool:
            # 设置全局图像数组
            global _g_img_array
            _g_img_array = img_array

            # 帧计数器
            self.frame_counter = 0
            start_time = time.time()

            try:
                # 处理每个批次
                for batch_idx in range(num_batches):
                    batch_start = batch_idx * batch_size
                    batch_end = min(batch_start + batch_size, total_frames)
                    batch_frames = []

                    # 准备批次帧参数
                    for frame_idx in range(batch_start, batch_end):
                        # 计算当前帧对应的图像Y坐标
                        if frame_idx < start_static_time * self.fps:
                            # 开始静止阶段
                            img_start_y = 0
                        elif frame_idx >= total_frames - end_static_time * self.fps:
                            # 结束静止阶段
                            img_start_y = max(0, img_height - self.height)
                        else:
                            # 滚动阶段
                            scroll_frame_idx = frame_idx - start_static_time * self.fps
                            img_start_y = min(
                                img_height - self.height,
                                int(scroll_frame_idx * self.scroll_speed),
                            )

                        # 添加到批次
                        batch_frames.append(
                            (
                                frame_idx,
                                img_start_y,
                                img_height,
                                img_width,
                                self.height,
                                self.width,
                                transparency_required,
                                bg_color[:3],  # 只传递RGB部分
                            )
                        )

                    # 处理当前批次
                    if len(batch_frames) > 60 and num_processes > 1:
                        # 大批处理：并行处理所有帧
                        pool_results = pool.map(_process_frame_optimized, batch_frames)
                        processed_frames = sorted(pool_results, key=lambda x: x[0])
                    else:
                        # 小批处理：使用fast_frame_processor直接处理
                        try:
                            frames_processed = fast_frame_processor(
                                batch_frames, self.memory_pool, process
                            )
                            self.frame_counter += frames_processed
                            pbar.update(frames_processed)
                            continue  # 跳过后续处理，因为帧已经直接写入
                        except Exception as e:
                            logger.error(f"快速帧处理器失败: {e}，回退到标准处理")
                            # 回退到标准处理
                            processed_frames = []
                            for params in batch_frames:
                                frame_idx, frame = _process_frame_optimized(params)
                                processed_frames.append((frame_idx, frame))

                    # 将处理后的帧写入FFmpeg
                    for _, frame in processed_frames:
                        process.stdin.write(frame.tobytes())
                        self.frame_counter += 1
                        pbar.update(1)

                # 关闭stdin，等待FFmpeg完成
                process.stdin.close()
                process.wait()

                # 检查FFmpeg是否成功
                if process.returncode != 0:
                    stderr = process.stderr.read().decode("utf-8", errors="ignore")
                    logger.error(f"FFmpeg错误: {stderr}")
                    raise Exception(f"FFmpeg处理失败，返回码: {process.returncode}")

                # 计算性能统计
                end_time = time.time()
                total_time = end_time - start_time
                fps = self.frame_counter / total_time if total_time > 0 else 0
                logger.info(
                    f"总渲染性能: 渲染了{self.frame_counter}帧，耗时{total_time:.2f}秒，平均{fps:.2f}帧/秒"
                )

                return output_path

            except Exception as e:
                logger.error(f"视频渲染失败: {str(e)}", exc_info=True)
                # 尝试终止FFmpeg进程
                try:
                    process.terminate()
                except:
                    pass
                raise e

            finally:
                # 关闭进度条
                if 'pbar' in locals():
                    pbar.close()
                    
                # 清理
                _g_img_array = None
                gc.collect()  # 强制垃圾回收

            return output_path

            # 检查是否成功，如果失败则尝试回退到CPU编码
            if not success and not transparency_required:
                # 设置环境变量强制使用CPU编码
                logger.info("优化渲染失败，回退到标准CPU渲染")
                os.environ["NO_GPU"] = "1"
                try:
                    return self.create_scrolling_video_optimized(
                        image=image,
                        output_path=output_path,
                        text_actual_height=text_actual_height,
                        transparency_required=transparency_required,
                        preferred_codec="libx264",  # 强制使用CPU编码器
                        audio_path=audio_path,
                        bg_color=bg_color,
                    )
                finally:
                    # 恢复环境变量
                    if "NO_GPU" in os.environ:
                        del os.environ["NO_GPU"]

        return output_path

    def create_scrolling_video_ffmpeg(
        self,
        image,
        output_path,
        text_actual_height,
        transparency_required=False,
        preferred_codec="libx264",
        audio_path=None,
        bg_color=(255, 255, 255),
    ):
        """
        使用FFmpeg的crop滤镜和时间表达式创建滚动视频
        
        参数:
            image: 要滚动的图像 (PIL.Image或NumPy数组)
            output_path: 输出视频文件路径
            text_actual_height: 文本实际高度
            transparency_required: 是否需要透明通道
            preferred_codec: 首选视频编码器
            audio_path: 可选的音频文件路径
            bg_color: 背景颜色 (R,G,B) 或 (R,G,B,A)
        
        Returns:
            输出视频的路径
        """
        try:
            # 记录开始时间
            total_start_time = time.time()
            
            # 初始化性能统计
            self.performance_stats = {
                "preparation_time": 0,
                "encoding_time": 0,
                "total_time": 0,
                "frames_processed": 0,
                "fps": 0
            }
            
            # 1. 准备图像
            preparation_start_time = time.time()
            
            # 将输入图像转换为PIL.Image对象
            if isinstance(image, np.ndarray):
                # NumPy数组转PIL图像
                if image.shape[2] == 4:  # RGBA
                    pil_image = Image.fromarray(image, 'RGBA')
                else:  # RGB
                    pil_image = Image.fromarray(image, 'RGB')
            elif isinstance(image, Image.Image):
                # 直接使用PIL图像
                pil_image = image
            else:
                raise ValueError("不支持的图像类型，需要PIL.Image或numpy.ndarray")
            
            # 确保输出目录存在
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            
            # 设置临时图像文件路径
            temp_img_path = f"{os.path.splitext(output_path)[0]}_temp.png"
            
            # 临时图像优化选项
            image_optimize_options = {
                "optimize": True,  # 优化图像存储
                "compress_level": 6,  # 中等压缩级别
            }
            
            # 使用PIL直接保存图像，保留原始格式和所有信息
            pil_image.save(temp_img_path, format="PNG", **image_optimize_options)
            
            # 获取图像尺寸
            img_width, img_height = pil_image.size
            
            # 清理内存中的大型对象，确保不会占用过多内存
            del pil_image
            gc.collect()
            
            # 2. 计算滚动参数
            # 滚动距离 = 图像高度 - 视频高度
            scroll_distance = max(0, img_height - self.height)
            
            # 确保至少有8秒的滚动时间
            min_scroll_duration = 8.0  # 秒
            scroll_duration = max(min_scroll_duration, scroll_distance / (self.scroll_speed * self.fps))
            
            # 前后各添加2秒静止时间
            start_static_time = 2.0  # 秒
            end_static_time = 2.0  # 秒
            total_duration = start_static_time + scroll_duration + end_static_time
            
            # 总帧数
            total_frames = int(total_duration * self.fps)
            self.total_frames = total_frames
            
            # 滚动起始和结束时间点
            scroll_start_time = start_static_time
            scroll_end_time = start_static_time + scroll_duration
            
            logger.info(f"视频参数: 宽度={self.width}, 高度={self.height}, 帧率={self.fps}")
            logger.info(f"滚动参数: 距离={scroll_distance}px, 速度={self.scroll_speed}px/帧, 持续={scroll_duration:.2f}秒")
            logger.info(f"时间设置: 总时长={total_duration:.2f}秒, 静止开始={start_static_time}秒, 静止结束={end_static_time}秒")
            
            # 3. 设置编码器参数
            codec_params, pix_fmt = self._get_codec_parameters(
                preferred_codec, transparency_required, 4 if transparency_required else 3
            )
            
            # 准备阶段结束
            preparation_end_time = time.time()
            self.performance_stats["preparation_time"] = preparation_end_time - preparation_start_time
            
            # 4. 创建FFmpeg命令，使用crop滤镜和表达式
            encoding_start_time = time.time()
            
            # 检查系统是否有支持的GPU加速
            gpu_support = {
                "nvidia": False
            }
            
            # 检测NVIDIA GPU
            try:
                # 检测NVIDIA GPU是否存在
                nvidia_result = subprocess.run(['nvidia-smi'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
                gpu_support["nvidia"] = nvidia_result.returncode == 0
                
                if gpu_support["nvidia"]:
                    logger.info("检测到NVIDIA GPU，将尝试使用NVENC编码器")
            except Exception as e:
                logger.info(f"NVIDIA GPU检测出错: {e}，将使用CPU处理")
            
            # 检测是否有任何GPU支持
            has_gpu_support = gpu_support["nvidia"]
            
            # 构建基本FFmpeg命令
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-loop", "1",  # 循环输入图像
                "-i", temp_img_path,  # 输入图像
                "-progress", "pipe:2",  # 输出进度信息到stderr
                "-stats",  # 启用统计信息
                "-stats_period", "1",  # 每1秒输出一次统计信息
                "-max_muxing_queue_size", "1024",  # 限制复用队列大小
            ]
            
            # 添加音频输入（如果有）
            if audio_path and os.path.exists(audio_path):
                ffmpeg_cmd.extend(["-i", audio_path])
                
            # 创建裁剪表达式
            crop_y_expr = f"'if(between(t,{scroll_start_time},{scroll_end_time}),min({img_height-self.height},(t-{scroll_start_time})/{scroll_duration}*{scroll_distance}),if(lt(t,{scroll_start_time}),0,{scroll_distance}))'"
            
            # 始终使用CPU的crop滤镜,GPU没有crop滤镜
            crop_expr = (
                f"crop=w={self.width}:h={self.height}:"
                f"x=0:y={crop_y_expr}"
            )
            
            # 添加滤镜
            ffmpeg_cmd.extend([
                "-filter_complex", crop_expr,
            ])
            
            # 基于GPU支持选择合适的编码器
            if gpu_support["nvidia"] and not transparency_required:
                # 尝试使用NVENC编码器
                if "libx264" in ' '.join(codec_params):
                    logger.info("切换到NVIDIA硬件编码器(h264_nvenc)")
                    for i, param in enumerate(codec_params):
                        if param == "libx264":
                            codec_params[i] = "h264_nvenc"
                            break
            else:
                logger.info("未检测到支持的GPU或使用透明视频，将使用CPU处理")
                
            # 添加公共参数
            ffmpeg_cmd.extend([
                "-t", str(total_duration),  # 设置总时长
                "-vsync", "1",  # 添加vsync参数，确保平滑的视频同步
                "-thread_queue_size", "2048",  # 限制线程队列大小，减少内存使用
            ])
            
            # 添加视频编码参数
            ffmpeg_cmd.extend(codec_params)
            
            # 设置帧率
            ffmpeg_cmd.extend(["-r", str(self.fps)])
            
            # 添加音频映射（如果有）
            if audio_path and os.path.exists(audio_path):
                ffmpeg_cmd.extend([
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-map", "0:v:0",  # 从第1个输入（索引0）获取视频
                    "-map", "1:a:0",  # 从第2个输入（索引1）获取音频
                    "-shortest",
                ])
            
            # 添加输出路径
            ffmpeg_cmd.append(output_path)

            logger.info(f"FFmpeg命令: {' '.join(ffmpeg_cmd)}")

            # 5. 执行FFmpeg命令
            try:
                # 启动进程
                logger.info("启动FFmpeg进程...")
                process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,  # 行缓冲
                    universal_newlines=True  # 使用通用换行符
                )
                
                # 使用新的进度条监控FFmpeg进度
                monitor_thread = PerformanceMonitor.monitor_ffmpeg_progress(
                    process=process,
                    total_duration=total_duration,
                    total_frames=total_frames,
                    encoding_start_time=encoding_start_time
                )
                
                # 获取输出和错误
                stdout, stderr = process.communicate()
                
                # 等待监控线程结束
                if monitor_thread and monitor_thread.is_alive():
                    monitor_thread.join(timeout=2.0)
                
                # 检查进程返回码
                if process.returncode != 0:
                    logger.error(f"FFmpeg处理失败: {stderr}")
                    raise Exception(f"FFmpeg处理失败，返回码: {process.returncode}")
                
                logger.info("FFmpeg处理完成")
                
            except Exception as e:
                logger.error(f"执行FFmpeg命令时出错: {str(e)}")
                raise
            finally:
                # 清理临时文件
                try:
                    if os.path.exists(temp_img_path):
                        os.remove(temp_img_path)
                except Exception as e:
                    logger.warning(f"清理临时文件失败: {str(e)}")
            
            # 编码结束
            encoding_end_time = time.time()
            self.performance_stats["encoding_time"] = encoding_end_time - encoding_start_time
            
            # 计算总时间
            self.performance_stats["total_time"] = encoding_end_time - total_start_time
            
            # 估算处理的帧数和帧率
            self.performance_stats["frames_processed"] = total_frames
            if self.performance_stats["encoding_time"] > 0:
                self.performance_stats["fps"] = total_frames / self.performance_stats["encoding_time"]
            
            # 输出性能统计
            logger.info("\n" + "="*50)
            logger.info("滚动视频生成性能统计 (FFmpeg滤镜方式):")
            logger.info(f"1. 准备阶段: {self.performance_stats['preparation_time']:.2f}秒 ({self.performance_stats['preparation_time']/self.performance_stats['total_time']*100:.1f}%)")
            logger.info(f"2. FFmpeg编码: {self.performance_stats['encoding_time']:.2f}秒 ({self.performance_stats['encoding_time']/self.performance_stats['total_time']*100:.1f}%)")
            logger.info(f"总时间: {self.performance_stats['total_time']:.2f}秒, 估算帧率: {self.performance_stats['fps']:.1f}帧/秒")
            logger.info("="*50 + "\n")
            
            return output_path
            
        except Exception as e:
            logger.error(f"使用FFmpeg滤镜创建滚动视频时出错: {str(e)}")
            logger.error(traceback.format_exc())
            # 记录总时间（即使发生错误）
            self.performance_stats["total_time"] = time.time() - total_start_time
            raise
