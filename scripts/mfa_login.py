"""非互動環境的 Garmin MFA 登入：把驗證碼寫進指定檔案，這支程式會等到檔案出現再完成登入並快取 token。
用法：python scripts/mfa_login.py /tmp/mfa_code.txt   # 另一邊：echo 123456 > /tmp/mfa_code.txt
"""
import logging, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
CODE_FILE = Path(sys.argv[1])

def wait_for_code() -> str:
    print(f"[mfa] 等待驗證碼檔案 {CODE_FILE} ...", flush=True)
    for _ in range(600):           # 最多等 10 分鐘
        if CODE_FILE.exists():
            code = CODE_FILE.read_text().strip()
            CODE_FILE.unlink()
            if code:
                print(f"[mfa] 讀到驗證碼 {code[:2]}****", flush=True)
                return code
        time.sleep(1)
    raise TimeoutError("等不到 MFA 驗證碼")

from garmin_coach.client import GarminClient
c = GarminClient(prompt_mfa=wait_for_code)
api = c.login()
print(f"LOGIN OK: {api.full_name} ({api.display_name}) unit={api.unit_system}", flush=True)
