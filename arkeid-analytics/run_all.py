"""Запуск всех тестов: .venv/bin/python tests/run_all.py"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
failed = []
for name in ("test_metrics.py", "test_sync.py", "test_advisor.py", "test_auth.py", "test_web.py", "test_levels.py", "test_queue.py", "test_guards.py", "test_period.py", "test_trends.py", "test_content.py", "test_kpi.py", "test_api_ready.py", "test_ux.py", "test_youtube.py", "test_threads.py", "test_producer.py", "test_settings.py"):
    print(f"\n=== {name} ===")
    if subprocess.run([sys.executable, str(ROOT / name)]).returncode:
        failed.append(name)
print(f"\n{'ВСЁ ЗЕЛЁНОЕ' if not failed else 'упало: ' + ', '.join(failed)}")
sys.exit(1 if failed else 0)
