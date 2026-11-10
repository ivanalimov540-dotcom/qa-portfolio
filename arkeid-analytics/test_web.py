"""Разбор пользовательского ввода и поведение экранов."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web.server import parse_username


def main() -> int:
    cases = [
        ("https://www.instagram.com/demo_detailing?igsi=MWdh%3D%3D", "demo_detailing"),
        ("https://www.instagram.com/demo_detailing/?igsh=xx", "demo_detailing"),
        ("https://www.instagram.com/demo_detailing/", "demo_detailing"),
        ("instagram.com/gteam_detail.kz", "gteam_detail.kz"),
        ("@zona_detailing_161", "zona_detailing_161"),
        ("  diko_detailing  ", "diko_detailing"),
        ("https://instagram.com/nick#foo", "nick"),
        ("", ""),
    ]
    failures = []
    for raw, expect in cases:
        got = parse_username(raw)
        mark = "ok  " if got == expect else "FAIL"
        if got != expect:
            failures.append((raw, got, expect))
        print(f"  {mark} {raw[:52]:52} → {got!r}")
    print(f"\n{'все тесты прошли' if not failures else str(len(failures)) + ' упало'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
