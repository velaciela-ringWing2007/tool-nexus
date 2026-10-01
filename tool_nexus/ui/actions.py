"""起動・停止・再起動・まとめて起動・グループの起動／停止（画面から呼ぶ操作）."""

from __future__ import annotations

import enum
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from tool_nexus.core.constants import HEALTH_PROCESS, KIND_LINK
from tool_nexus.core.models import Tool
from tool_nexus.core.ports import is_port_free
from tool_nexus.core.repositories import ToolRepository
from tool_nexus.process.control import (
    LaunchError,
    PidStatus,
    ProcessInfo,
    ProcessNotIdentifiedError,
    ProcessQueryError,
    StopError,
    append_log_marker,
    find_relay_processes,
    find_relayed,
    launch,
    prepare_launch,
    resolve_log_path,
    run_stop_command,
    stop,
)
from tool_nexus.process.health import ToolHealth, derive_status, expects_running, probe_all
from tool_nexus.process.log_relay import stop_marker
from tool_nexus.ui.grouping import StopSummary, should_start, should_stop
from tool_nexus.ui.state import flash, health_timeout, request_check

# 停止後にポートの解放を確認する時間
STOP_RELEASE_TIMEOUT = 5.0
# 再起動でポートの解放を待つ時間（解放されないまま起動すると「ポートが使用中」になるため長めに待つ）
RESTART_RELEASE_TIMEOUT = 10.0


def log_path_for(tool: Tool, settings: dict[str, str]) -> Path:
    return resolve_log_path(
        directory=Path(tool.directory),
        log_path=tool.log_path,
        default_log_dir=settings.get("default_log_dir", ""),
        tool_id=tool.id,
    )


# ----------------------------------------------------------------------
# TOOL NEXUS が起動したプロセスの探し直し（SPEC 6.3・6.4）
# ----------------------------------------------------------------------
RelayFinder = Callable[[], list[ProcessInfo]]


def expected_launch(tool: Tool, settings: dict[str, str]) -> tuple[Path, list[str]] | None:
    """登録内容から、起動するときと同じ手順で (ログファイル, argv) を組み立てる。組み立てられなければ None。"""
    if tool.kind == KIND_LINK or tool.id is None:
        return None
    try:
        argv, _ = prepare_launch(
            command=tool.command, kind=tool.kind, port=tool.port, directory=tool.directory
        )
    except (LaunchError, ValueError):
        return None
    return log_path_for(tool, settings), argv


def find_launched(
    tools: Iterable[Tool], settings: dict[str, str], *, finder: RelayFinder = find_relay_processes
) -> dict[int, ProcessInfo]:
    """TOOL NEXUS が起動したプロセス（中継プロセス）を探し、{ツールID: プロセス} を返す。

    プロセスの照会はまとめて1回。照会に失敗したときは何も見つからなかった扱いにする。
    """
    expected = {int(tool.id): e for tool in tools if (e := expected_launch(tool, settings)) is not None}
    if not expected:
        return {}
    try:
        relays = finder()
    except ProcessQueryError:
        return {}
    found: dict[int, ProcessInfo] = {}
    for tool_id, (log_path, argv) in expected.items():
        process = find_relayed(relays, log_path=log_path, argv=argv)
        if process is not None:
            found[tool_id] = process
    return found


def adopt(repository: ToolRepository, tool: Tool, process: ProcessInfo) -> Tool:
    """探し直したプロセスを、そのツールの PID として保存し直す。"""
    repository.record_pid(int(tool.id), pid=process.pid, created_at=str(process.created_at))
    return replace(tool, last_pid=process.pid, last_pid_created_at=process.created_at)


def stop_with_rediscovery(
    tool: Tool, *, stopper=stop, rediscover: Callable[[], ProcessInfo | None] = lambda: None
) -> None:
    """記録の PID で停止する。特定できなければ TOOL NEXUS が起動したプロセスを探し直して停止する。

    探し直しても見つからなければ、最初の ProcessNotIdentifiedError をそのまま送出する。
    """
    try:
        stopper(tool.last_pid, tool.last_pid_created_at)
        return
    except ProcessNotIdentifiedError:
        found = rediscover()
        if found is None:
            raise
    stopper(found.pid, found.created_at)


def rediscoverer(
    tool: Tool, settings: dict[str, str], *, finder: RelayFinder = find_relay_processes
) -> Callable[[], ProcessInfo | None]:
    """stop_with_rediscovery に渡す、1つのツールを探し直す関数。"""
    return lambda: find_launched([tool], settings, finder=finder).get(int(tool.id))


def rediscover_alive(
    repository: ToolRepository,
    settings: dict[str, str],
    tools: list[Tool],
    alive_by_id: dict[int, bool | None],
    *,
    searched: set[tuple[int, str]] | None = None,
    finder: RelayFinder = find_relay_processes,
) -> dict[int, Tool]:
    """process モードで、起動したはずなのに生存が確認できないツールを探し直す（SPEC 6.4）。

    見つかったものは alive_by_id を True にし、PID を保存し直す。{ツールID: 保存し直したツール} を返す。
    searched に入っている起動（ツールID, last_started_at）は探さない。探したものは searched に足す。
    """
    candidates = [
        tool
        for tool in tools
        if tool.health_mode == HEALTH_PROCESS
        and alive_by_id.get(int(tool.id)) is False
        and expects_running(tool)
        and (searched is None or (int(tool.id), str(tool.last_started_at)) not in searched)
    ]
    if not candidates:
        return {}
    if searched is not None:
        searched.update((int(tool.id), str(tool.last_started_at)) for tool in candidates)
    by_id = {int(tool.id): tool for tool in candidates}
    adopted: dict[int, Tool] = {}
    for tool_id, process in find_launched(candidates, settings, finder=finder).items():
        adopted[tool_id] = adopt(repository, by_id[tool_id], process)
        alive_by_id[tool_id] = True
    return adopted


# ----------------------------------------------------------------------
# 停止コマンド（SPEC 6.3）
# ----------------------------------------------------------------------
def run_tool_stop_command(tool: Tool, settings: dict[str, str]) -> None:
    """ツールの停止コマンドを実行する。終了コード0以外・実行できない・時間切れは StopError。"""
    code = run_stop_command(
        command=tool.stop_command, port=tool.port, directory=tool.directory, log_path=log_path_for(tool, settings)
    )
    if code != 0:
        raise StopError(f"停止コマンドが終了コード {code} で終わりました。ログを確認してください。")


def stop_by_command(
    tool: Tool,
    settings: dict[str, str],
    *,
    command_runner: Callable[[Tool, dict[str, str]], None] = run_tool_stop_command,
    stopper=stop,
    rediscover: Callable[[], ProcessInfo | None] | None = None,
) -> None:
    """停止コマンドを実行し、TOOL NEXUS が起動したプロセスが残っていれば照合してから止める。失敗は StopError。"""
    command_runner(tool, settings)
    try:
        stop_with_rediscovery(tool, stopper=stopper, rediscover=rediscover or rediscoverer(tool, settings))
    except ProcessNotIdentifiedError:
        pass  # 残っていない（停止コマンドで止まった、またはすぐ終わるスクリプト）


def mark_stopped(repository: ToolRepository, settings: dict[str, str], tool: Tool, reason: str = "停止") -> None:
    """停止を記録し、ログに停止の区切り行を書く（中継プロセスごと止まるため TOOL NEXUS が書く。SPEC 6.8）。"""
    repository.record_stop(int(tool.id))
    append_log_marker(log_path_for(tool, settings), stop_marker(reason))


def start_tool(repository: ToolRepository, settings: dict[str, str], tool: Tool, *, verb: str = "起動") -> None:
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

    pid, created_at = result.pid, result.created_at
    if created_at is None:
        # 起動時刻を取れなかった。TOOL NEXUS が起動したプロセスとして探し直す（SPEC 6.2・6.3）
        found = find_launched([tool], settings).get(int(tool.id))
        if found is not None:
            pid, created_at = found.pid, found.created_at
    repository.record_start(int(tool.id), pid=pid, created_at=created_at)
    if created_at is None:
        flash(
            f"「{tool.name}」を起動しましたが、プロセスの起動時刻を取得できませんでした。"
            "状態の確認や停止のときに、改めてプロセスを探します。",
            "warning",
        )
    else:
        flash(f"「{tool.name}」を{verb}しました。")


def wait_port_released(port: int, timeout: float = STOP_RELEASE_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_free(port):
            return True
        time.sleep(0.25)
    return is_port_free(port)


def stop_tool(repository: ToolRepository, settings: dict[str, str], tool: Tool) -> bool:
    """記録済みのPIDで停止する。停止コマンドがあればそれを使う（SPEC 6.3）。

    照合が取れず、ポートから引き直せる場合は False を返す（呼び出し側で確認ダイアログを開く）。
    """
    if tool.stop_command:
        try:
            stop_by_command(tool, settings)
        except StopError as exc:
            flash(f"「{tool.name}」を停止できませんでした。{exc}", "error")
            return True
        mark_stopped(repository, settings, tool)
        report_stopped(tool)
        return True
    try:
        stop_with_rediscovery(tool, rediscover=rediscoverer(tool, settings))
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

    mark_stopped(repository, settings, tool)
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


class RestartOutcome(enum.Enum):
    """再起動の停止段階の結果（SPEC 6.3 再起動）."""

    STOPPED = "stopped"                # 停止できた → 起動してよい
    NOT_RUNNING = "not_running"        # 記録のプロセスは既に無く、ポートも空いている → そのまま起動してよい
    NOT_IDENTIFIED = "not_identified"  # 動いているかもしれないが特定できない → 停止も起動もしない
    STOP_FAILED = "stop_failed"        # 停止に失敗した → 起動しない
    PORT_BUSY = "port_busy"            # 停止したがポートが解放されない → 起動しない


def stop_for_restart(
    tool: Tool,
    *,
    stopper=stop,
    rediscover: Callable[[], ProcessInfo | None] = lambda: None,
    command_stopper: Callable[[], None] | None = None,
    port_free=is_port_free,
    wait_released=wait_port_released,
) -> tuple[RestartOutcome, str]:
    """再起動のために停止し、起動してよいかを返す（画面に依存しない。テスト用に差し替えられる）。

    記録の PID で特定できなければ、TOOL NEXUS が起動したプロセスを探し直して停止する（SPEC 6.3）。
    停止コマンドがあるツールは command_stopper（停止コマンド → 残ったプロセスの停止）で止める。
    """
    try:
        if command_stopper is not None:
            command_stopper()
        else:
            stop_with_rediscovery(tool, stopper=stopper, rediscover=rediscover)
    except ProcessNotIdentifiedError as exc:
        gone = exc.status in (PidStatus.NOT_FOUND, PidStatus.MISMATCH)
        if gone and (not tool.port or port_free(tool.port)):
            return RestartOutcome.NOT_RUNNING, ""
        return RestartOutcome.NOT_IDENTIFIED, ""
    except StopError as exc:
        return RestartOutcome.STOP_FAILED, str(exc)
    if tool.port and not wait_released(tool.port, RESTART_RELEASE_TIMEOUT):
        return RestartOutcome.PORT_BUSY, ""
    return RestartOutcome.STOPPED, ""


def restart_tool(repository: ToolRepository, settings: dict[str, str], tool: Tool) -> None:
    """停止して起動し直す。特定できないものは止めない（SPEC 6.3 再起動）。"""
    command_stopper = (lambda: stop_by_command(tool, settings)) if tool.stop_command else None
    outcome, detail = stop_for_restart(
        tool, rediscover=rediscoverer(tool, settings), command_stopper=command_stopper
    )
    if outcome is RestartOutcome.STOPPED:
        mark_stopped(repository, settings, tool, "停止（再起動）")
        start_tool(repository, settings, tool, verb="再起動")
    elif outcome is RestartOutcome.NOT_RUNNING:
        repository.clear_pid(int(tool.id))  # 記録していたプロセスはもう無い
        start_tool(repository, settings, tool)
    elif outcome is RestartOutcome.NOT_IDENTIFIED:
        flash(
            f"「{tool.name}」: 対象のプロセスを特定できないため、再起動しませんでした（停止もしていません）。"
            "TOOL NEXUS の外で起動された可能性があります。「停止」からポートで探して止めてから起動してください。",
            "error",
        )
    elif outcome is RestartOutcome.STOP_FAILED:
        flash(f"「{tool.name}」を停止できなかったため、再起動しませんでした。{detail}", "error")
    else:
        mark_stopped(repository, settings, tool, "停止（再起動）")
        flash(
            f"「{tool.name}」を停止しましたが、ポート {tool.port} が解放されないため起動しませんでした。"
            "少し待ってから「起動」を押してください。",
            "warning",
        )


def current_health(repository: ToolRepository, settings: dict[str, str], tools: list[Tool]) -> list[ToolHealth]:
    """一覧の表示（フィルター・キャッシュ）に関係なく、今の状態を確認する。"""
    alive_by_id = probe_all(tools, timeout=health_timeout(settings))
    adopted = rediscover_alive(repository, settings, tools, alive_by_id)
    tools = [adopted.get(int(t.id), t) for t in tools]
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


def stop_tools(
    repository: ToolRepository,
    settings: dict[str, str],
    healths: list[ToolHealth],
    *,
    stopper=stop,
    finder: RelayFinder = find_relay_processes,
    command_runner: Callable[[Tool, dict[str, str]], None] = run_tool_stop_command,
) -> StopSummary:
    """起動中のものを停止する。特定できないものは止めずに summary.not_identified に入れる。

    1件ずつ確認ダイアログは出さない（グループの停止で煩わしくなるため。SPEC 6.11）。
    """
    summary = StopSummary()
    for health in healths:
        tool = health.tool
        if not should_stop(health):
            continue
        rediscover = rediscoverer(tool, settings, finder=finder)
        try:
            if tool.stop_command:
                stop_by_command(tool, settings, command_runner=command_runner, stopper=stopper, rediscover=rediscover)
            else:
                stop_with_rediscovery(tool, stopper=stopper, rediscover=rediscover)
        except ProcessNotIdentifiedError as exc:
            if exc.status in (PidStatus.NOT_FOUND, PidStatus.MISMATCH):
                repository.clear_pid(int(tool.id))
            summary.not_identified.append(tool.name)
            continue
        except StopError as exc:
            summary.failed.append(f"{tool.name}: {exc}")
            continue
        mark_stopped(repository, settings, tool)
        summary.stopped.append(tool.name)
    return summary


def stop_group(repository: ToolRepository, settings: dict[str, str], group: str) -> None:
    """グループ内の起動中のツールを停止する（SPEC 6.11）。"""
    tools = group_members(repository, group)
    summary = stop_tools(repository, settings, current_health(repository, settings, tools))
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
