use std::{
    fs::{self, OpenOptions},
    io,
    path::Path,
};
use tracing::{Event, Subscriber};
use tracing_subscriber::{
    fmt::{format::Writer, FmtContext, FormatEvent, FormatFields},
    fmt::writer::MakeWriterExt,
    registry::LookupSpan,
    EnvFilter,
};

/// TSBot 统一日志格式化器
pub struct TSBotFormatter;

impl<S, N> FormatEvent<S, N> for TSBotFormatter
where
    S: Subscriber + for<'a> LookupSpan<'a>,
    N: for<'a> FormatFields<'a> + 'static,
{
    fn format_event(
        &self,
        ctx: &FmtContext<'_, S, N>,
        mut writer: Writer<'_>,
        event: &Event<'_>,
    ) -> std::fmt::Result {
        // 获取时间戳
        let now = chrono::Local::now();
        let timestamp = now.format("%Y-%m-%d %H:%M:%S");
        
        // 获取日志级别
        let level = event.metadata().level();
        
        // 写入统一格式: [时间] [级别] [组件] 消息
        write!(writer, "[{}] [{}] [voice] ", timestamp, level)?;
        
        // 写入消息内容
        ctx.field_format().format_fields(writer.by_ref(), event)?;
        
        writeln!(writer)
    }
}

fn open_log_file() -> Option<std::fs::File> {
    let configured = std::env::var("TSBOT_VOICE_LOG_FILE")
        .unwrap_or_else(|_| "./logs/voice.log".to_string());
    let path = Path::new(configured.trim());
    if path.as_os_str().is_empty() {
        return None;
    }
    if let Some(parent) = path.parent() {
        if let Err(error) = fs::create_dir_all(parent) {
            eprintln!("[voice] cannot create log directory {}: {error}", parent.display());
            return None;
        }
    }
    match OpenOptions::new().create(true).append(true).open(path) {
        Ok(file) => Some(file),
        Err(error) => {
            eprintln!("[voice] cannot open log file {}: {error}", path.display());
            None
        }
    }
}

/// 初始化统一日志配置
pub fn init_logger() {
    use tracing_subscriber::fmt;
    
    let log_level = std::env::var("TSBOT_LOG_LEVEL").unwrap_or_else(|_| "info".to_string());
    let log_level = log_level.trim().to_lowercase();
    let log_level = if log_level.is_empty() { "info".to_string() } else { log_level };
    
    let builder = fmt()
        .event_format(TSBotFormatter)
        .with_env_filter(EnvFilter::new(format!("voice_service={}", log_level)));

    if let Some(file) = open_log_file() {
        builder.with_writer(io::stdout.and(file)).init();
    } else {
        builder.with_writer(io::stdout).init();
    }
}
