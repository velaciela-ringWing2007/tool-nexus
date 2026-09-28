"""起動・停止・まとめて起動・グループの起動／停止（画面から呼ぶ操作）."""

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
from tool_nexus.process.health import ToolHealth, derive_status, probe_all
from tool_nexus.ui.grouping import StopSummary, should_start, should_stop
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

    repository.record_stop(int(tool.id))
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


def current_health(repository: ToolRepository, settings: dict[str, str], tools: list[Tool]) -> list[ToolHealth]:
    """一覧の表示（フィルター・キャッシュ）に関係なく、今の状態を確認する。"""
    alive_by_id = probe_all(tools, timeout=health_timeout(settings))
    return [ToolHealth(t, alive_by_id.get(int(t.id)), derive_status(t, alive_by_id.get(int(t.id)))) for t in tools]


def start_tools(repository: ToolRepository, settings: dict[str, str], tools: list[Tool]) -> tuple[int, int]:
    """起動していないものを順に起動する。起動中・起動中…・リンクは飛ばす。(起動した件数, 飛ばした件数)。"""
    started = skipped = 0
    for health in current_health(repository, settings, tools):
        if not should_start(health):
            skipped += 1
            continue
        start_tool(repository, settings, health.tool)
        started += 1
    return started, skipped


def start_autostart_tools(repository: ToolRepository, settings: dict[str, str]) -> None:
    """「まとめて起動」の対象を順に起動する。起動中・起動中…のものは飛ばす（二重起動しない）。"""
    tools = [tool for tool in repository.list_all() if tool.autostart]
    if not tools:
        flash("まとめて起動の対象がありません。編集画面で「まとめて起動の対象にする」を有効にしてください。", "warning")
        return
    started, skipped = start_tools(repository, settings, tools)
    flash(f"まとめて起動: {started} 件を起動しました（起動済みのため {skipped} 件を飛ばしました）。")
    request_check()


def group_members(repository: ToolRepository, group: str) -> list[Tool]:
    """グループのツール（表示順）。一覧のフィルターとは関係なく、グループ全体を対象にする。"""
    return [tool for tool in repository.list_all() if tool.group_name == group]


def start_group(repository: ToolRepository, settings: dict[str, str], group: str) -> None:
    """グループ内の起動していないツールを表示順に続けて起動する（SPEC 6.11）。"""
    started, skipped = start_tools(repository, settings, group_members(repository, group))
    flash(f"「{group}」: {started} 件を起動しました（起動済み・リンクの {skipped} 件を飛ばしました）。")
    request_check()


def stop_tools(repository: ToolRepository, healths: list[ToolHealth], *, stopper=stop) -> StopSummary:
    """起動中のものを停止する。特定できないものは止めずに summary.not_identified に入れる。

    1件ずつ確認ダイアログは出さない（グループの停止で煩わしくなるため。SPEC 6.11）。
    """
    summary = StopSummary()
    for health in healths:
        tool = health.tool
        if not should_stop(health):
            continue
        try:
            stopper(tool.last_pid, tool.last_pid_created_at)
        except ProcessNotIdentifiedError as exc:
            if exc.status in (PidStatus.NOT_FOUND, PidStatus.MISMATCH):
                repository.clear_pid(int(tool.id))
            summary.not_identified.append(tool.name)
            continue
        except StopError as exc:
            summary.failed.append(f"{tool.name}: {exc}")
            continue
        repository.record_stop(int(tool.id))
        summary.stopped.append(tool.name)
    return summary


def stop_group(repository: ToolRepository, settings: dict[str, str], group: str) -> None:
    """グループ内の起動中のツールを停止する（SPEC 6.11）。"""
    tools = group_members(repository, group)
    summary = stop_tools(repository, current_health(repository, settings, tools))
    if summary.stopped:
        flash(f"「{group}」: {len(summary.stopped)} 件を停止しました（{'、'.join(summary.stopped)}）。")
    elif not summary.not_identified and not summary.failed:
        flash(f"「{group}」: 起動中のツールはありません。", "warning")
    if summary.not_identified:
        flash(
            f"「{group}」: 次のツールは対象のプロセスを特定できなかったため、停止していません: "
            f"{'、'.join(summary.not_identified)}。TOOL NEXUS の外で起動された可能性があります。"
            "必要なら各行の「停止」から、ポートで探して停止してください。",
            "warning",
        )
    for failure in summary.failed:
        flash(f"停止できませんでした: {failure}", "error")
    request_check()
