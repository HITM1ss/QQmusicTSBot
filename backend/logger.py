"""
统一日志配置模块
"""
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import settings

BEIJING_TIME = timezone(timedelta(hours=8))
_SERVER_LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(\.\d+)?(?=\s|\||$)")


def server_log_beijing_time(line: str) -> str:
    """TeamSpeak 3 logview timestamps are UTC; convert only the record prefix."""
    match = _SERVER_LOG_TIMESTAMP.match(line)
    if not match:
        return line
    instant = datetime.fromisoformat(match[1]).replace(tzinfo=timezone.utc)
    timestamp = instant.astimezone(BEIJING_TIME).strftime("%Y-%m-%d %H:%M:%S")
    return timestamp + (match[2] or "") + line[match.end():]


class TSBotFormatter(logging.Formatter):
    """TSBot 统一日志格式化器"""
    
    def __init__(self):
        # 统一日志格式: [时间] [级别] [组件] 消息
        fmt = "[%(asctime)s] [%(levelname)s] [backend] %(message)s"
        super().__init__(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, BEIJING_TIME).strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def setup_logger(
    name: str = "tsbot-backend",
    level: str = "INFO",
    log_file: Optional[str] = None
) -> logging.Logger:
    """
    设置统一的日志配置
    
    Args:
        name: logger 名称
        level: 日志级别 (DEBUG, INFO, WARN, ERROR)
        log_file: 日志文件路径，None 则只输出到控制台
    
    Returns:
        配置好的 logger
    """
    logger = logging.getLogger(name)
    
    # 避免重复配置
    if logger.handlers:
        return logger

    lvl = (level or "INFO").strip().upper()
    if lvl == "WARN":
        lvl = "WARNING"
    if not hasattr(logging, lvl):
        lvl = "INFO"
    logger.setLevel(getattr(logging, lvl))
    
    formatter = TSBotFormatter()
    
    # 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件输出
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def reconfigure_logger(level: str, log_file: str) -> None:
    lvl = (level or "INFO").strip().upper().replace("WARN", "WARNING")
    logger.setLevel(getattr(logging, lvl, logging.INFO))
    formatter = TSBotFormatter()
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)


# 全局 logger 实例
logger = setup_logger(
    level=getattr(settings, 'log_level', 'INFO'),
    log_file="logs/backend.log"
)
