"""Execution adapters for DeepSeek Harness (dsh) and optional fallbacks."""

from .base import ResumeTaskRequest, RunTaskRequest, TaskResult
from .ds_api import DeepSeekApiFallbackAdapter
from .dsh_cli import DshCliAdapter, build_patch_yaml, parse_dsh_json_stream
from .dsh_http import DshHttpAdapter

__all__ = [
    "DeepSeekApiFallbackAdapter",
    "DshCliAdapter",
    "DshHttpAdapter",
    "ResumeTaskRequest",
    "RunTaskRequest",
    "TaskResult",
    "build_patch_yaml",
    "parse_dsh_json_stream",
]
