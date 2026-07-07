"""性能指标收集与聚合。

在会话结束时输出统计摘要到 .mewcode/metrics/{session_id}.json
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

METRICS_DIR = ".mewcode/metrics"


@dataclass
class SessionMetrics:
    """会话级性能指标。"""

    session_id: str = ""
    total_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tool_calls: int = 0
    total_agent_calls: int = 0

    # 工具延迟（毫秒）
    tool_latencies: list[float] = field(default_factory=list)

    # 缓存
    prompt_cache_hits: int = 0
    prompt_cache_misses: int = 0
    total_requests: int = 0

    # Compact
    compact_count: int = 0

    # 时间
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def avg_tool_latency_ms(self) -> float:
        if not self.tool_latencies:
            return 0.0
        return sum(self.tool_latencies) / len(self.tool_latencies)

    @property
    def p50_tool_latency_ms(self) -> float:
        return self._percentile(50)

    @property
    def p95_tool_latency_ms(self) -> float:
        return self._percentile(95)

    @property
    def cache_hit_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.prompt_cache_hits / self.total_requests

    @property
    def duration_seconds(self) -> float:
        if self.ended_at <= self.started_at:
            return 0.0
        return self.ended_at - self.started_at

    @property
    def tokens_per_second(self) -> float:
        dur = self.duration_seconds
        if dur <= 0:
            return 0.0
        return self.total_tokens / dur

    def _percentile(self, p: int) -> float:
        if not self.tool_latencies:
            return 0.0
        sorted_lat = sorted(self.tool_latencies)
        idx = int(len(sorted_lat) * p / 100)
        idx = min(idx, len(sorted_lat) - 1)
        return sorted_lat[idx]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "total_tokens": self.total_tokens,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tool_calls": self.total_tool_calls,
            "total_agent_calls": self.total_agent_calls,
            "avg_tool_latency_ms": round(self.avg_tool_latency_ms, 2),
            "p50_tool_latency_ms": round(self.p50_tool_latency_ms, 2),
            "p95_tool_latency_ms": round(self.p95_tool_latency_ms, 2),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "compact_count": self.compact_count,
            "duration_seconds": round(self.duration_seconds, 2),
            "tokens_per_second": round(self.tokens_per_second, 2),
        }


class MetricsCollector:
    """会话级性能指标收集器。"""

    def __init__(self, work_dir: str, session_id: str = "") -> None:
        self._work_dir = work_dir
        self._metrics = SessionMetrics(session_id=session_id)
        self._metrics.started_at = time.monotonic()

    def record_tool_call(self, latency_ms: float) -> None:
        """记录一次工具调用延迟。"""
        self._metrics.tool_latencies.append(latency_ms)
        self._metrics.total_tool_calls += 1

    def record_agent_call(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_hit: bool = False,
    ) -> None:
        """记录一次 Agent 调用。"""
        self._metrics.total_input_tokens += input_tokens
        self._metrics.total_output_tokens += output_tokens
        self._metrics.total_tokens += input_tokens + output_tokens
        self._metrics.total_agent_calls += 1
        self._metrics.total_requests += 1
        if cache_hit:
            self._metrics.prompt_cache_hits += 1
        else:
            self._metrics.prompt_cache_misses += 1

    def record_compact(self) -> None:
        """记录一次压缩。"""
        self._metrics.compact_count += 1

    def finalize(self) -> SessionMetrics:
        """完成收集并保存。"""
        self._metrics.ended_at = time.monotonic()
        self._save()
        return self._metrics

    def get_metrics(self) -> SessionMetrics:
        """获取当前指标（不保存）。"""
        return self._metrics

    def _save(self) -> None:
        """保存指标到磁盘。"""
        metrics_dir = Path(self._work_dir) / METRICS_DIR
        metrics_dir.mkdir(parents=True, exist_ok=True)

        file_path = metrics_dir / f"{self._metrics.session_id}.json"
        try:
            file_path.write_text(
                json.dumps(self._metrics.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            log.info("[metrics] saved to %s", file_path)
        except OSError as e:
            log.error("[metrics] failed to save: %s", e)

    @classmethod
    def load(cls, work_dir: str, session_id: str) -> SessionMetrics | None:
        """加载历史指标。"""
        file_path = Path(work_dir) / METRICS_DIR / f"{session_id}.json"
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            m = SessionMetrics(session_id=data.get("session_id", ""))
            m.total_tokens = data.get("total_tokens", 0)
            m.total_input_tokens = data.get("total_input_tokens", 0)
            m.total_output_tokens = data.get("total_output_tokens", 0)
            m.total_tool_calls = data.get("total_tool_calls", 0)
            m.total_agent_calls = data.get("total_agent_calls", 0)
            m.compact_count = data.get("compact_count", 0)
            return m
        except (json.JSONDecodeError, KeyError, OSError):
            return None
