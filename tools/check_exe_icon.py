"""Verifica che un .exe contenga un'icona incorporata (solo Windows). Uso: python tools/check_exe_icon.py FILE.exe

Esce con 0 se l'icona c'e' (o se non siamo su Windows), con 1 se manca. Lo usano i .bat al termine della build.
"""
from __future__ import annotations

import sys


def icon_count(path: str) -> int:
    """Numero di icone incorporate nell'exe (risorse RT_GROUP_ICON) secondo la shell di Windows."""
    import ctypes
    from ctypes import wintypes
    fn = ctypes.windll.shell32.ExtractIconExW
    fn.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    fn.restype = wintypes.UINT
    n = fn(path, -1, None, None, 0)
    return 0 if n == 0xFFFFFFFF else int(n)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    if sys.platform != "win32":
        print("verifica icona: disponibile solo su Windows (saltata)")
        return 0
    n = icon_count(argv[1])
    if n > 0:
        print(f"Icona incorporata: OK ({n} icona/e) - {argv[1]}")
        return 0
    print(f"ATTENZIONE: nessuna icona incorporata in {argv[1]}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
