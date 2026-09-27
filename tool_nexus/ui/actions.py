"""起動・停止・まとめて起動（画面から呼ぶ操作）."""

from __future__ import annotations

import time
from pathlib import Path

from tool_nexus.core.models import Tool
from tool_nexus.core.ports import is_port_free
from tool_nexus.core.repositories import ToolRepository
from tool_nexus.process.control import (
    LaunchError,
    PidStatus,
    ProcessNotIdentifiedError,
    StopError,
    launch,
    resolve_log_path,
    stop,
)
from tool_nexus.process.health import Status, derive_status, probe_all
from tool_nexus.ui.state import flash, health_timeout, request_check

# 停止後にポートの解放を確認する時間
STOP_RELEASE_TIMEOUT = 5.0


def log_path_for(tool: Tool, settings: dict[str, str]) -> Path:
    return resolve_log_path(
        directory=Path(tool.directory),
        log_path=tool.log_path,
        default_log_dir=settings.get("default_log_dir", ""),
        tool_id=tool.id,
    )


def start_tool(repository: ToolRepository, settings: dict[str, str], tool: Tool) -> None:
    if tool.port and not is_port_free(tool.port):
        flash(
            f"「{tool.name}」を起動できません。ポート {tool.port} は他のプロセスが使用中です。",
            "error",
        )
        return
    try:
        result = launch(
            command=tool.command,
            kind=tool.kind,
            port=tool.port,
            directory=tool.directory,
            log_path=log_path_for(tool, settings),
        )
    except LaunchError as exc:
        flash(f"「{tool.name}」を起動できませんでした。{exc}", "error")
        return

    repository.record_start(int(tool.id), pid=result.pid, created_at=result.created_at)
    if result.created_at is None:
        flash(
            f"「{tool.name}」を起動しましたが、プロセスの起動時刻を取得できませんでした。"
            "安全のため、この起動は「停止」ボタンでは止められません。",
            "warning",
        )
    else:
        flash(f"「{tool.name}」を起動しました。")


def wait_port_released(port: int, timeout: float = STOP_RELEASE_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_free(port):
            return True
        time.sleep(0.25)
    return is_port_free(port)


def stop_tool(repository: ToolRepository, tool: Tool) -> bool:
    """記録済みのPIDで停止する。

    照合が取れず、ポートから引き直せる場合は False を返す（呼び出し側で確認ダイアログを開く）。
    """
    try:
        stop(tool.last_pid, tool.last_pid_created_at)
    except ProcessNotIdentifiedError as exc:
        if exc.status in (PidStatus.NOT_FOUND, PidStatus.MISMATCH):
            # 記録しているプロセスはもう存在しない。古い記録は消しておく。
            repository.clear_pid(int(tool.id))
        if tool.port:
            return False
        flash(
            f"「{tool.name}」: 対象プロセスを特定できませんでした。停止していません。"
            "TOOL NEXUSの外で起動されたか、既に終了している可能性があります。"
            "動いている場合はタスクマネージャ（Linuxは kill）等で停止してください。",
            "error",
        )
        return True
    except StopError as exc:
        flash(f"「{tool.name}」を停止できませんでした。{exc}", "error")
        return True

    repository.clear_pid(int(tool.id))
    report_stopped(tool)
    return True


def report_stopped(tool: Tool) -> None:
    if tool.port and not wait_port_released(tool.port):
        flash(
            f"「{tool.name}」を停止しましたが、ポート {tool.port} がまだ解放されていません。",
            "warning",
        )
    else:
        flash(f"「{tool.name}」を停止しました。")


def start_autostart_tools(repository: ToolRepository, settings: dict[str, str]) -> None:
    """「まとめて起動」の対象を順に起動する。起動中・起動中…のものは飛ばす（二重起動しない）。"""
    tools = [tool for tool in repository.list_all() if tool.autostart]
    if not tools:
        flash("まとめて起動の対象がありません。編集画面で「まとめて起動の対象にする」を有効にしてください。", "warning")
        return
    alive_by_id = probe_all(tools, timeout=health_timeout(settings))
    started = skipped = 0
    for tool in tools:
        alive = alive_by_id.get(int(tool.id))
        status = derive_status(tool, alive)
        # 監視しない設定（不明）でも起動記録が残っていれば動いている可能性があるので飛ばす
        if status in (Status.RUNNING, Status.STARTING) or (status is Status.UNKNOWN and tool.last_pid):
            skipped += 1
            continue
        start_tool(repository, settings, tool)
        started += 1
    flash(f"まとめて起動: {started} 件を起動しました（起動済みのため {skipped} 件を飛ばしました）。")
    request_check()
