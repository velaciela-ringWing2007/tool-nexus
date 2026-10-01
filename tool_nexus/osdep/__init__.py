"""OS依存機能の切り替え（SPEC 3.2）.

実行中のOSに応じて windows / linux のどちらかを選び、同じ名前で公開する。
フレームワークにはせず、呼び出し側はこのモジュールの関数を使うだけにする。
OSに依存しない処理（PID照合・親子の判定・検出など）は process.control にある。
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from tool_nexus.osdep import windows as _impl
else:  # Linux（macOS は /proc が無いため未対応）
    from tool_nexus.osdep import linux as _impl

NAME: str = _impl.NAME
IS_WINDOWS: bool = NAME == "windows"

LAUNCH_KWARGS: dict = _impl.LAUNCH_KWARGS
POSIX_SPLIT: bool = _impl.POSIX_SPLIT
DEFAULT_PYTHONS: tuple[str, ...] = _impl.DEFAULT_PYTHONS
EXECUTABLE_LABEL: str = _impl.EXECUTABLE_LABEL
POWERSHELL: str = _impl.POWERSHELL

is_executable_file = _impl.is_executable_file
track_child = _impl.track_child
get_process_creation_date = _impl.get_process_creation_date
get_creation_dates = _impl.get_creation_dates
kill_tree = _impl.kill_tree
take_snapshot = _impl.take_snapshot
find_relay_processes = _impl.find_relay_processes
pick_file = _impl.pick_file
pick_folder = _impl.pick_folder
