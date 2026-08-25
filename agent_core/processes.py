from __future__ import annotations

import os


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _ERROR_INVALID_PARAMETER = 87

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _open_process = _kernel32.OpenProcess
    _open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _open_process.restype = wintypes.HANDLE
    _get_exit_code_process = _kernel32.GetExitCodeProcess
    _get_exit_code_process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _get_exit_code_process.restype = wintypes.BOOL
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL

    def process_is_alive(process_id: int | None) -> bool:
        if not process_id or process_id <= 0:
            return False
        handle = _open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
        if not handle:
            error = ctypes.get_last_error()
            if error == _ERROR_INVALID_PARAMETER:
                return False
            if error == _ERROR_ACCESS_DENIED:
                return True
            raise ctypes.WinError(error)
        try:
            exit_code = wintypes.DWORD()
            if not _get_exit_code_process(handle, ctypes.byref(exit_code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return exit_code.value == _STILL_ACTIVE
        finally:
            _close_handle(handle)

else:

    def process_is_alive(process_id: int | None) -> bool:
        if not process_id or process_id <= 0:
            return False
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
